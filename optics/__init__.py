import abc

import math
import typing

import torch
import torch.nn as nn
import torchvision.utils
import numpy as np

import utils
import utils.fft as fft
import utils.old_complex as old_complex
import algorithm.image


def _depthmap2layers(depthmap, n_depths, binary=False):
    depthmap = depthmap[:, None, ...]  # add color dim

    depthmap = depthmap.clamp(1e-8, 1.0)
    d = torch.arange(
        0, n_depths, dtype=depthmap.dtype, device=depthmap.device
    ).reshape(1, 1, -1, 1, 1) + 1
    depthmap = depthmap * n_depths
    diff = d - depthmap
    layered_mask = torch.zeros_like(diff)
    if binary:
        layered_mask[torch.logical_and(diff >= 0., diff < 1.)] = 1.
    else:
        mask = torch.logical_and(diff > -1., diff <= 0.)
        layered_mask[mask] = diff[mask] + 1.
        layered_mask[torch.logical_and(diff > 0., diff <= 1.)] = 1.

    return layered_mask


class Camera(nn.Module, metaclass=abc.ABCMeta):
    def __init__(
        self,
        focal_depth: float,
        min_depth: float,
        max_depth: float,
        n_depths: int,
        image_size: typing.Union[int, typing.List[int]],
        aperture_size: int,
        focal_length: float,
        aperture_diameter: float,
        camera_pitch: float,
        wavelengths=(632e-9, 550e-9, 450e-9),
        diffraction_efficiency=0.7
    ):
        super().__init__()
        if min_depth < 1e-6:
            raise ValueError(f'Provided min depth({min_depth}) is too small')

        scene_distances = utils.ips_to_metric(
            torch.linspace(0, 1, steps=n_depths), min_depth, max_depth
        )

        self._diffraction_efficiency = diffraction_efficiency
        self._n_depths = n_depths
        self._min_depth = min_depth
        self._max_depth = max_depth
        self.__focal_depth = focal_depth
        self._aperture_diameter = aperture_diameter
        self._camera_pitch = camera_pitch
        self.__focal_length = focal_length
        self._image_size = self.normalize_image_size(image_size)
        self._aperture_size = aperture_size

        self._diffraction_scaler = None

        self.__register_wavlength(wavelengths)
        self.register_buffer('buf_scene_distances', scene_distances)

        self.__psf_cache = None

    @abc.abstractmethod
    def psf(self, scene_distances, modulate_phase):
        pass

    @abc.abstractmethod
    def psf_out_energy(self, psf_size: int):
        pass

    @abc.abstractmethod
    def heightmap(self):
        pass

    @abc.abstractmethod
    def aberration(self, u, v, wavelength=None):
        pass

    def extra_repr(self):
        return f'''
Camera module...
Refcative index for center wavelength: {self.refractive_index(self.buf_wavelengths[self.n_wavelengths // 2])}
Aperture pitch: {self.aperture_pitch * 1e6}[um]
f number: {self.f_number:.3f}
Depths: {self.buf_scene_distances}
Input image size: {self._image_size}
              '''

    def forward(self, img, depthmap, occlusion, is_training=False):
        psf = self.psf_at_camera(img.shape[-2:], is_training=is_training).unsqueeze(0)
        psf = self.normalize_psf(psf)
        captimg, volume = self.get_capt_img(img, depthmap, psf, occlusion)
        return captimg, volume, psf

    def get_capt_img(self, img, depthmap, psf, occlusion):
        layered_mask = _depthmap2layers(depthmap, self._n_depths, binary=True)
        volume = layered_mask * img[:, :, None, ...]
        return algorithm.image.image_formation(volume, layered_mask, psf, occlusion)

    def psf_at_camera(
        self,
        size: typing.Tuple[int, int] = None,
        is_training: bool = False,
        use_psf_cache: bool = False
    ):
        if use_psf_cache and self.__psf_cache is not None:
            return utils.pad_or_crop(self.__psf_cache, size)

        device = self.device
        init_sd = torch.linspace(0, 1, steps=self._n_depths, device=device)
        if is_training:
            init_sd += (torch.rand(self._n_depths, device=device) - 0.5) / self._n_depths
        scene_distances = utils.ips_to_metric(init_sd, self._min_depth, self._max_depth)
        if is_training:
            scene_distances[-1] += torch.rand(1, device=device)[0] * (100.0 - self._max_depth)

        diffracted_psf = self.psf(scene_distances, True)
        undiffracted_psf = self.psf(scene_distances, False)

        # Keep the normalization factor for penalty computation
        self._diffraction_scaler = diffracted_psf.sum(dim=(-1, -2), keepdim=True)
        undiffraction_scaler = undiffracted_psf.sum(dim=(-1, -2), keepdim=True)

        diffracted_psf = diffracted_psf / self._diffraction_scaler
        undiffracted_psf = undiffracted_psf / undiffraction_scaler

        self.__psf_cache = \
            self._diffraction_efficiency * diffracted_psf + \
            (1 - self._diffraction_efficiency) * undiffracted_psf

        # In training, randomly pixel-shifts the PSF around green channel.
        if is_training:
            max_shift = 2
            r_shift = tuple(np.random.randint(low=-max_shift, high=max_shift, size=2))
            b_shift = tuple(np.random.randint(low=-max_shift, high=max_shift, size=2))
            psf_r = torch.roll(self.__psf_cache[0], shifts=r_shift, dims=(-1, -2))
            psf_g = self.__psf_cache[1]
            psf_b = torch.roll(self.__psf_cache[2], shifts=b_shift, dims=(-1, -2))
            self.__psf_cache = torch.stack([psf_r, psf_g, psf_b], dim=0)

        self.__psf_cache = utils.pad_or_crop(self.__psf_cache, size)
        return self.__psf_cache

    def apply_stop(self, r2, x):
        return torch.where(r2 < self.aperture_diameter ** 2 / 4, x, torch.zeros_like(x))

    def mtf_loss(self, normalize=True):
        mtf = self.mtf
        if normalize:
            s = self.slope_range
            fy = torch.linspace(-0.5, 0.5, mtf.shape[-2]).reshape(-1, 1) / self.camera_pitch
            fx = torch.linspace(-0.5, 0.5, mtf.shape[-1]).reshape(1, -1) / self.camera_pitch
            factor = self.aperture_diameter ** 3 / (s * torch.sqrt(fx ** 2 + fy ** 2))
            while len(factor.shape) != len(mtf.shape):
                factor = factor.unsqueeze(0)
        else:
            factor = 1

        loss = factor.to(self.device) / mtf
        return torch.sum(loss)

    # logging method shuould be executed on cpu

    @torch.no_grad()
    def heightmap_log(self, size):
        heightmap = utils.img_resize(self.heightmap().cpu()[None, None, ...], size).squeeze(0)
        heightmap -= heightmap.min()
        heightmap /= heightmap.max()
        return heightmap

    @torch.no_grad()
    def psf_log(self, log_size, depth_step):
        # PSF is not visualized at computed size.
        psf = self.psf_at_camera(log_size, is_training=False, use_psf_cache=True).cpu()
        psf = self.normalize_psf(psf)
        psf /= psf.max()
        streched_psf = psf / psf \
            .max(dim=-1, keepdim=True)[0] \
            .max(dim=-2, keepdim=True)[0] \
            .max(dim=0, keepdim=True)[0]
        return self.__make_grid(psf, depth_step), self.__make_grid(streched_psf, depth_step)

    @torch.no_grad()
    def mtf_log(self, depth_step):
        mtf = self.mtf
        mtf /= mtf.max()
        return self.__make_grid(mtf, depth_step)

    @torch.no_grad()
    def specific_log(self, *args, **kwargs):
        psf_loss = self.psf_out_energy(kwargs['psf_size'])
        return {
            'optics/psf_out_of_fov_energy': psf_loss[0],
            'optics/psf_out_of_fov_max': psf_loss[1]
        }

    @property
    def f_number(self):
        return self.__focal_length / self._aperture_diameter

    @property
    def sensor_distance(self):
        return 1. / (1. / self.__focal_length - 1. / self.__focal_depth)

    @property
    def focal_depth(self):
        return self.__focal_depth

    @property
    def focal_length(self):
        return self.__focal_length

    @property
    def slope_range(self):
        return 2 * (self._max_depth - self._min_depth) / (self._max_depth + self._min_depth)

    @property
    def aperture_pitch(self):
        return self._aperture_diameter / self._aperture_size

    @property
    def camera_pitch(self):
        return self._camera_pitch

    @property
    def n_wavelengths(self):
        return len(self.buf_wavelengths)

    @property
    def aperture_diameter(self):
        return self._aperture_diameter

    @property
    def otf(self):
        psf = self.psf_at_camera(use_psf_cache=True)
        return fft.fftshift(torch.rfft(psf, 2, onesided=False), (-2, -3))

    @property
    def mtf(self):
        return old_complex.abs(self.otf)

    def __register_wavlength(self, wavelengths):
        if isinstance(wavelengths, tuple):
            wavelengths = torch.tensor(wavelengths)
        elif isinstance(wavelengths, float):
            wavelengths = torch.tensor([wavelengths])
        else:
            raise ValueError(f'Wrong wavelength type({wavelengths})')

        if len(wavelengths) % 3 != 0:
            raise ValueError('the number of wavelengths has to be a multiple of 3.')

        if not hasattr(self, 'buf_wavelengths'):
            self.register_buffer('buf_wavelengths', wavelengths)
        else:
            self.buf_wavelengths = wavelengths.to(self.buf_wavelengths.device)

    @staticmethod
    def __make_grid(image, depth_step):
        # expect image with shape CxDxHxW
        return torchvision.utils.make_grid(
            image[:, ::depth_step].transpose(0, 1),
            nrow=4, pad_value=1, normalize=False
        )

    @staticmethod
    def normalize_psf(psf):
        return psf / psf.sum(dim=(-2, -1), keepdims=True)

    @staticmethod
    def normalize_image_size(img_sz):
        if isinstance(img_sz, int):
            img_sz = [img_sz, img_sz]
        elif isinstance(img_sz, list):
            if img_sz[0] % 2 == 1 or img_sz[1] % 2 == 1:
                raise ValueError('Image size has to be even.')
        else:
            raise ValueError('image_size has to be int or list of int.')
        return img_sz

    @staticmethod
    def heightmap2phase(height, wavelength, refractive_index):
        return height * (2 * math.pi / wavelength) * (refractive_index - 1)

    @staticmethod
    def refractive_index(wavelength, a=1.5375, b=0.00829045, c=-0.000211046):
        """Cauchy's equation - dispersion formula
        Default coefficients are for NOA61.
        https://refractiveindex.info/?shelf=other&book=Optical_adhesives&page=Norland_NOA61
        """
        return a + b / (wavelength * 1e6) ** 2 + c / (wavelength * 1e6) ** 4
