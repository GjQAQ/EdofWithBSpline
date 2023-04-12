import abc

import torch
import numpy as np

import optics
import utils
import utils.old_complex as old_complex
import utils.fft as fft
import optics.kernel as kn


class ClassicCamera(optics.Camera, metaclass=abc.ABCMeta):
    def __init__(self, effective_psf_factor, double_precision: bool = True, **kwargs):
        r"""
        Construct camera model with a DOE(Diffractive Optical Element) on its aperture.
        The height of DOE :math:`h(u,v)` is given by method heightmap,
        where :math:`(u,v)` is coordinate on the aperture plane.
        Its PSF is computed by DFT(Discrete Fourier Transform), which is 'classic' method.
        :param kwargs: Arguments used to construct super class
        """
        super().__init__(**kwargs)

        self.__double = double_precision
        self.__psf_factor = effective_psf_factor

        const = self.camera_pitch / self.sensor_distance
        self.__scale_factor = int(torch.ceil(
            const * self.aperture_diameter / torch.min(self.buf_wavelengths)
        ).item() + 1e-5)
        self.__u_axis = self.__uv_grid(1)
        self.__v_axis = self.__uv_grid(0)
        self.register_buffer('buf_r_sqr', self.u_axis ** 2 + self.v_axis ** 2)

        self.__heightmap_history = None

    @abc.abstractmethod
    def compute_heightmap(self):
        pass

    @abc.abstractmethod
    def lattice_focal_init(self):
        pass

    def prepare_lattice_focal_init(self):
        slope_range = kn.get_slope_range(*self.depth_range)
        n = (self.aperture_diameter * slope_range
             / (2 * kn.get_delta(self.camera_pitch, self.focal_length, self.focal_depth))) ** (1 / 3)
        n = max(3, round(n))
        if n < 2:
            raise ValueError(f'Wrong subsquare number: {n}')
        wl = self.buf_wavelengths[self.n_wavelengths // 2]
        return slope_range, n, wl

    def psf(self, scene_distances, modulate_phase):
        r_sqr = self.buf_r_sqr.unsqueeze(1)  # n_wl x D x N_u x N_v
        scene_distances = scene_distances.reshape(1, -1, 1, 1)
        wl = self.buf_wavelengths.reshape(-1, 1, 1, 1)
        if self.__double:
            r_sqr, scene_distances, wl = r_sqr.double(), scene_distances.double(), wl.double()

        item = r_sqr + scene_distances ** 2
        phase1 = torch.sqrt(item) - scene_distances
        phase2 = torch.sqrt(r_sqr + self.focal_depth ** 2) - self.focal_depth
        phase = (phase1 - phase2) * (2 * np.pi / wl)
        if modulate_phase:
            phase += optics.heightmap2phase(self.heightmap().unsqueeze(1), wl, optics.refractive_index(wl))

        amplitude = scene_distances / (wl * item)
        amplitude = self.apply_stop(r_sqr, amplitude)
        amplitude = amplitude / amplitude.max()

        psf = old_complex.abs2(fft.old_fft_exp(amplitude, phase))
        del amplitude, phase
        sf = self.__scale_factor
        psf = psf[..., sf // 2::sf, sf // 2::sf]
        psf *= torch.prod(self.interval, 1).reshape(-1, 1, 1, 1) ** 2
        psf /= (wl * self.sensor_distance) ** 2
        if self.__double:
            psf = psf.float()
        psf = fft.fftshift(psf, (-1, -2))
        return utils.pad_or_crop(psf, self._image_size)

    def heightmap(self, use_cache=False) -> torch.Tensor:
        if not use_cache or self.__heightmap_history is None:
            self.__heightmap_history = self.compute_heightmap()
        return self.__heightmap_history

    def specific_log(self, *args, **kwargs):
        log = super().specific_log(*args, **kwargs)
        h = self.heightmap(use_cache=True)
        h = self.apply_stop(self.buf_r_sqr, h)
        log['optics/heightmap_max'] = h.max()
        log['optics/heightmap_min'] = h.min()
        return log

    @property
    def u_axis(self):
        return self.__u_axis

    @property
    def v_axis(self):
        return self.__v_axis

    @property
    def interval(self):
        sample_range = self.buf_wavelengths[:, None] * self.sensor_distance / self.camera_pitch
        return sample_range * self.__psf_factor / torch.tensor([self._image_size], device=self.device)

    def __uv_grid(self, dim):
        n = self._image_size[dim] * self.__scale_factor // self.__psf_factor
        x = torch.linspace(-n / 2, n / 2, n).reshape((1, -1)) * self.interval[:, [dim]]  # n_wl x N
        return x[:, None, :] if dim == 1 else x[:, :, None]
