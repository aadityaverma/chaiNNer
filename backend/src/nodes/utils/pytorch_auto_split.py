from __future__ import annotations

import gc
import os
from functools import reduce
from operator import mul
from typing import Tuple, Union

import torch
from sanic.log import logger
from torch import Tensor


def torch_center_crop(tensor, crop_x, crop_y):
    x, y = tensor.size()[-2:]
    start_x = x // 2 - (crop_x // 2)
    start_y = y // 2 - (crop_y // 2)
    return tensor[..., start_x : start_x + crop_x, start_y : start_y + crop_y]


def torch_center_replace(tensor, crop_x, crop_y, replacement):
    x, y = tensor.size()[-2:]
    start_x = x // 2 - (crop_x // 2)
    start_y = y // 2 - (crop_y // 2)
    tensor[..., start_x : start_x + crop_x, start_y : start_y + crop_y] = replacement
    return tensor


@torch.inference_mode()
def auto_split_process(
    lr_img: Tensor,
    model: torch.nn.Module,
    scale: int = 4,
    overlap: int = 16,
    max_depth: Union[int, None] = None,
    current_depth: int = 1,
) -> Tuple[Tensor, int]:
    """
    Run PyTorch upscaling with automatic recursive tile splitting based on ability to process with current size
    """
    # Original code: https://github.com/JoeyBallentine/ESRGAN/blob/master/utils/dataops.py

    # if os.environ["killed"] == "True":
    #     torch.cuda.empty_cache()
    #     gc.collect()
    #     raise RuntimeError("Upscaling killed mid-processing")

    logger.debug(
        f"auto_split_process: scale={scale}, overlap={overlap}, max_depth={max_depth}, current_depth={current_depth}"
    )

    # Prevent splitting from causing an infinite out-of-vram loop
    if current_depth > 15:
        torch.cuda.empty_cache()
        gc.collect()
        raise RuntimeError("Splitting stopped to prevent infinite loop")

    # Attempt to upscale if unknown depth or if reached known max depth
    if max_depth is None or max_depth == current_depth:
        d_img = None
        try:
            device = torch.device(os.environ["device"])
            d_img = lr_img.to(device)
            model = model.to(device)
            if os.environ["isFp16"] == "True":
                model = model.half()
                d_img = d_img.half()
            else:
                model = model.float()
                d_img = d_img.float()
            result = model(d_img)
            result = result.detach().cpu()
            del d_img
            return result, current_depth
        except RuntimeError as e:
            # Check to see if its actually the CUDA out of memory error
            if "allocate" in str(e) or "CUDA" in str(e):
                # Collect garbage (clear VRAM)
                gc.collect()
                if d_img is not None:
                    d_img.detach().cpu()
                    del d_img
                torch.cuda.empty_cache()
            # Re-raise the exception if not an OOM error
            else:
                raise

    b, c, h, w = lr_img.shape

    # Split image into 4ths
    top_left = lr_img[..., : h // 2 + overlap, : w // 2 + overlap]
    top_right = lr_img[..., : h // 2 + overlap, w // 2 - overlap :]
    bottom_left = lr_img[..., h // 2 - overlap :, : w // 2 + overlap]
    bottom_right = lr_img[..., h // 2 - overlap :, w // 2 - overlap :]

    # Recursively upscale the quadrants
    # After we go through the top left quadrant, we know the maximum depth and no longer need to test for out-of-memory
    top_left_rlt, depth = auto_split_process(
        top_left,
        model,
        scale=scale,
        overlap=overlap,
        max_depth=max_depth,
        current_depth=current_depth + 1,
    )
    top_right_rlt, _ = auto_split_process(
        top_right,
        model,
        scale=scale,
        overlap=overlap,
        max_depth=depth,
        current_depth=current_depth + 1,
    )
    bottom_left_rlt, _ = auto_split_process(
        bottom_left,
        model,
        scale=scale,
        overlap=overlap,
        max_depth=depth,
        current_depth=current_depth + 1,
    )
    bottom_right_rlt, _ = auto_split_process(
        bottom_right,
        model,
        scale=scale,
        overlap=overlap,
        max_depth=depth,
        current_depth=current_depth + 1,
    )

    # Define output shape
    out_h = h * scale
    out_w = w * scale

    # Create blank output image
    output_img = torch.empty(
        (b, c, out_h, out_w), dtype=lr_img.dtype, device=lr_img.device
    )

    # Fill output image with tiles, cropping out the overlaps
    output_img[..., : out_h // 2, : out_w // 2] = top_left_rlt[
        ..., : out_h // 2, : out_w // 2
    ]
    output_img[..., : out_h // 2, -out_w // 2 :] = top_right_rlt[
        ..., : out_h // 2, -out_w // 2 :
    ]
    output_img[..., -out_h // 2 :, : out_w // 2] = bottom_left_rlt[
        ..., -out_h // 2 :, : out_w // 2
    ]
    output_img[..., -out_h // 2 :, -out_w // 2 :] = bottom_right_rlt[
        ..., -out_h // 2 :, -out_w // 2 :
    ]

    return output_img, depth
