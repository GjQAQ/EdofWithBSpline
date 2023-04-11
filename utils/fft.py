import warnings

import torch

__fft = torch.fft
import torch.fft as fft

warnings.filterwarnings('ignore')


def fftshift(x, dims=(-1, -2)):
    shifts = [(x.size(dim)) // 2 for dim in dims]
    x = torch.roll(x, shifts=shifts, dims=dims)
    return x


def ifftshift(x, dims):
    shifts = [(x.size(dim) + 1) // 2 for dim in dims]
    x = torch.roll(x, shifts=shifts, dims=dims)
    return x


def rfft2(x: torch.Tensor):
    return fft.rfftn(x, x.shape[-2:])


def irfft2(x, size):
    return fft.irfftn(x, size)


def fft2(x):
    return fft.fftn(x, x.shape[-2:])


def conv2(x, y):
    return irfft2(rfft2(x) * rfft2(y), x.shape[-2:])


def autocorrelation1d(x: torch.Tensor) -> torch.Tensor:
    res = fft.irfft(fft.rfft(x).abs() ** 2, x.shape[-1])
    return res / res.max()


# the followings are implemented by torch.* rather than torch.fft.*

old_fft = __fft


def exp2xy(amplitude, phase):
    real = amplitude * torch.cos(phase)
    imag = amplitude * torch.sin(phase)
    return torch.stack([real, imag], -1)


def old_fft_exp(amplitude, phase):
    return __fft(exp2xy(amplitude, phase), 2)


def old_ifft_exp(amplitude, phase):
    return torch.ifft(exp2xy(amplitude, phase), 2)
