import typing

import torch
from torch import Tensor
import numpy as np
import scipy.interpolate as intp

import optics
import utils.fft as fft
import utils
import algorithm


def clamped_knot_vector(n, p):
    kv = torch.linspace(0, 1, n - p - 1)
    kv = torch.cat([torch.zeros(p + 1), kv, torch.ones(p + 1)])
    return kv


def design_matrix(x, k, p) -> np.ndarray:
    return intp.BSpline.design_matrix(x, k, p).toarray().astype('float32')


class BSplineApertureCamera(optics.ClassicCamera):
    def __init__(
        self,
        grid_size=(50, 50),
        knot_vectors=None,
        degrees=(3, 3),
        requires_grad: bool = False,
        init_type='default',
        **kwargs
    ):
        r"""
        Construct a camera model whose DOE surface is characterized as a B-spline surface.
        The parameters to be trained are control points :math:`c_{ij}, 0\leq i<N, 0\leq j<M`
        Control points are located evenly on aperture plane, i.e. the area

        :math:`[-D/2, D/2] \times [-D/2, D/2]`

        where D is the diameter of the aperture.
        When compute the height of point :math:`(u,v)` on aperture, the coordinates will be normalized:

        :math:`u'=(u+D/2)/D`

        :param grid_size: Size of control points grid :math:`(N,M)`
        :param knot_vectors: Knot vectors, default to which used in clamped B-spline
        :param degrees:
        :param requires_grad:
        :param kwargs:
        """
        super().__init__(**kwargs)

        if knot_vectors is None:
            self.degrees = degrees
            knot_vectors = (
                clamped_knot_vector(grid_size[0], degrees[0]), clamped_knot_vector(grid_size[1], degrees[1]))
        else:
            self.degrees = (len(knot_vectors[0]) - grid_size[0] - 1, len(knot_vectors[1]) - grid_size[1] - 1,)
        self.grid_size = grid_size
        self.knot_vectors = knot_vectors
        self.u_matrix: torch.Tensor = ...
        self.v_matrix: torch.Tensor = ...

        if init_type == 'lattice_focal':
            init = self.lattice_focal_init()
        else:
            init = torch.zeros(grid_size)
        self.control_points = torch.nn.Parameter(init, requires_grad=requires_grad)

        # buffered tensors used to compute heightmap in psf
        self.register_buffer('u_matrix', self.design_matrix(1), persistent=False)
        self.register_buffer('v_matrix', self.design_matrix(0), persistent=False)

    def heightmap(self):
        return self.__heightmap(
            self.u_matrix,
            self.v_matrix,
            self.control_points.unsqueeze(0)
        )  # n_wl x N_u x N_v

    @torch.no_grad()
    def lattice_focal_init(self):
        slope_range, n, wl = self.prepare_lattice_focal_init()
        r = self.aperture_diameter / 2
        u = torch.linspace(-r, r, self.grid_size[0])[None, ...]
        v = torch.linspace(-r, r, self.grid_size[1])[..., None]
        return algorithm.slope2height(
            u, v,
            *algorithm.slopemap(u, v, n, slope_range, self.aperture_diameter, fill='inscribe'),
            12, self.focal_length, self.focal_depth, wl
        )

    @torch.no_grad()
    def aberration(self, u, v, wavelength: float = None):
        if wavelength is None:
            wavelength = self.wavelengths[len(self.wavelengths) / 2]
        c = self.control_points.cpu()[None, None, ...]

        r2 = u ** 2 + v ** 2
        scaled_u = self.scale_coordinate(u).squeeze(-2)  # 1 x omega_x x t1
        scaled_v = self.scale_coordinate(v).squeeze(-1)  # omega_y x 1 x t2
        u_mat = self.design_matrices(scaled_u, c.shape[-2], self.knot_vectors[0], self.degrees[0])
        v_mat = self.design_matrices(scaled_v, c.shape[-1], self.knot_vectors[1], self.degrees[1])
        h = self.__heightmap(u_mat, v_mat, c)

        phase = utils.heightmap2phase(h, wavelength, utils.refractive_index(wavelength))
        phase = torch.transpose(phase, 0, 1)
        return self.apply_circular_stop(
            fft.exp2xy(1, phase),
            r2=torch.stack([r2, r2], -1),
            x=torch.stack([u, u], -1),
            y=torch.stack([v, v], -1)
        )

    @torch.no_grad()
    def heightmap_log(self, size, normalize=True):
        m = []
        axis = []
        for sz, kv, p in zip(size, self.knot_vectors, self.degrees):
            axis.append(torch.linspace(0, 1, sz))
            m.append(torch.from_numpy(design_matrix(axis[-1], kv, p)))

        u, v = torch.meshgrid(*axis)
        h = self.__heightmap(*m, self.control_points.cpu())
        u = u - 0.5
        v = v - 0.5
        h = self.apply_stop(h, 0.5, x=u, y=v, r2=u ** 2 + v ** 2).unsqueeze(0)
        if normalize:
            h -= h.min()
            h /= h.max()
        return h

    @classmethod
    def extract_parameters(cls, kwargs) -> typing.Dict:
        it = kwargs['initialization_type']
        if it not in ('default', 'lattice_focal'):
            raise ValueError(f'Unsupported initialization type: {it}')

        base = super().extract_parameters(kwargs)
        base.update({
            "degrees": [kwargs['bspline_degree']] * 2,
            "grid_size": [kwargs['bspline_grid_size']] * 2
        })
        return base

    @classmethod
    def add_specific_args(cls, parser):
        base = super().add_specific_args(parser)
        base.add_argument(
            '--bspline_grid_size', type=int, default=50,
            help='Number of control points in each direction for B-spline DOE'
        )
        base.add_argument(
            '--bspline_degree', type=int, default=5,
            help='Degree of B-spline surface in each directin'
        )
        return base

    @torch.no_grad()
    def design_matrix(self, dim):
        n, kv, p = self.image_size[dim], self.knot_vectors[dim], self.degrees[dim]
        x = torch.flatten(self.u_grid if dim == 1 else self.v_grid, -2, -1)

        x = self.scale_coordinate(x)
        m = torch.stack([torch.from_numpy(design_matrix(x[i].numpy(), kv, p)) for i in range(x.shape[0])])
        return m  # n_wl x N x n_ctrl

    def __heightmap(self, u, v, c) -> Tensor:
        h = torch.matmul(torch.matmul(u, c), torch.transpose(v, -1, -2))
        return utils.fold_profile(h, self.design_wavelength)

    @staticmethod
    def design_matrices(x, c_n, kv, p):
        mat = torch.zeros(*x.shape, c_n)

        shape = x.shape
        mat = mat.reshape(-1, x.shape[-1], c_n)
        x = x.reshape(-1, x.shape[-1])
        for i in range(mat.shape[0]):
            mat[i] = torch.from_numpy(design_matrix(x[i].numpy(), kv, p))
        return mat.reshape(*shape, c_n)
