import collections

import torch
import torch.nn as nn

from .unet import UNet
from .dnet import DNet
import utils

CH_DEPTH = 1
CH_RGB = 3
ReconstructionOutput = collections.namedtuple('ReconstructionOutput', ['est_img', 'est_depthmap'])


class Reconstructor(nn.Module):
    """
    A reconstructor for image received by sensor directly.
    Composed of three module: an input layer, Res-UNet and an output layer.
    Input:
        1. Captured image (B x C x H x W)
        2. Pre-inversed image volume (B x C x D x H x W)
    Output:
        1. Reconstructed image (B x 3 x H x W)
        2. Estimated depthmap (B x 1 x H x W)
    """
    def __init__(
        self,
        n_depth: int = 16,
        norm_layer=None
    ):
        super().__init__()
        ch_pin = CH_RGB * (n_depth + 1)

        input_layer = nn.Sequential(
            nn.Conv2d(ch_pin, ch_pin, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(ch_pin),
            nn.ReLU(),
            nn.Conv2d(ch_pin, 32, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(32),
            nn.ReLU()
        )

        output_blocks = [nn.Conv2d(32, CH_RGB + CH_DEPTH, kernel_size=1, bias=True)]
        output_layer = nn.Sequential(*output_blocks)
        self.__decoder = nn.Sequential(
            input_layer,
            UNet([32, 32, 64, 64, 128], norm_layer),
            # DNet((64, 128, 256, 512)),
            output_layer,
        )

        utils.init_module(self)

    def forward(self, capt_img, pin_volume) -> ReconstructionOutput:
        b, _, _, h, w = pin_volume.shape
        inputs = torch.cat([capt_img.unsqueeze(2), pin_volume], 2)
        est = torch.sigmoid(self.__decoder(inputs.reshape(b, -1, h, w)))
        return ReconstructionOutput(est[:, :-1], est[:, [-1]])
