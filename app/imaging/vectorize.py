"""Raster -> layered colour vectors, tuned for flat sublimation jersey artwork.

Steps: threshold alpha, fit the long edge into the preset's window (upscaling small AI
outputs before quantizing gives smoother curves), optional median denoise, palette
quantization without dithering, then `layer_trace` turns the posterized image into one
stacked compound path per colour (`VectorArt`), which `vector_out` and `eps` write as
SVG, PDF and EPS.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

from app.imaging.io import scaled_size
from app.imaging.layer_trace import LayerParams, trace_layers
from app.imaging.layers import VectorArt
from app.imaging.palette import quantize_pixels
from app.imaging.vector_out import vector_to_svg

log = logging.getLogger("jersey.imaging.vectorize")

PALETTE_SAMPLES = 200_000
EDGE_TOLERANCE = 24  # a sampled pixel differing more than this from a neighbour is an edge blend, not a palette colour
MIN_FLAT_SAMPLES = 2_000


@dataclass(frozen=True)
class TracePreset:
    colors: int  # palette size (shown in the UI)
    min_long_edge: int  # smaller images are upscaled first
    max_long_edge: int  # larger images are downscaled first (bounds time and anchor count)
    denoise: int  # median filter size before quantizing (0 = off)
    layers: LayerParams  # speckle clean-up, corner and curve-fit settings of the layer tracer


# Tuned with the vector lab benchmark: on every test image, fidelity and edge fidelity match or beat the
# vtracer colour tracing used before, with 24-43% of its anchor points (totals per preset).
PRESETS: dict[str, TracePreset] = {
    "flat": TracePreset(
        colors=8, min_long_edge=2048, max_long_edge=3072, denoise=3,
        layers=LayerParams(speckle_area=64, merge_tol=8, fit_tol=1.25, corner_window=3.0, corner_deg=35.0,
                           corner_dmax=3.0),
    ),
    "standard": TracePreset(
        colors=16, min_long_edge=2048, max_long_edge=3072, denoise=3,
        layers=LayerParams(speckle_area=36, merge_tol=8, fit_tol=1.0, corner_window=3.0, corner_deg=35.0,
                           corner_dmax=3.0),
    ),
    # Denoising matters most here: with 32 colours, unfiltered JPEG/AI noise splits flat areas into
    # tens of thousands of speckles.
    "detailed": TracePreset(
        colors=32, min_long_edge=2048, max_long_edge=3072, denoise=3,
        layers=LayerParams(speckle_area=9, merge_tol=8, fit_tol=0.75, corner_window=3.0, corner_deg=35.0,
                           corner_dmax=3.0, decimate_passes=2),
    ),
}


@dataclass
class TraceResult:
    svg: str
    width: int
    height: int
    colors: int  # colour layers (one compound path each)
    paths: int  # subpaths over all layers


def trace_to_vector(img: Image.Image, preset: str = "standard", colors: int | None = None) -> VectorArt:
    """Trace `img` into stacked colour layers (one compound path per colour) in traced pixels."""
    settings = PRESETS.get(preset)
    if settings is None:
        raise ValueError(f"Unknown vector preset '{preset}'; expected one of {', '.join(PRESETS)}.")
    traced = prepare_trace_image(img, settings, colors or settings.colors)
    started = time.perf_counter()
    art = trace_layers(traced, settings.layers)
    log.info("traced %dx%d preset=%s in %.2fs: %d colour layers, %d subpaths, %d anchors", art.width, art.height,
             preset, time.perf_counter() - started, len(art.layers), art.subpath_count, art.anchor_count)
    return art


def trace_to_svg(img: Image.Image, preset: str = "standard", colors: int | None = None) -> TraceResult:
    """`trace_to_vector` written as a layered SVG (one `<g>` with one compound `<path>` per colour)."""
    art = trace_to_vector(img, preset, colors)
    return TraceResult(svg=vector_to_svg(art), width=art.width, height=art.height, colors=len(art.layers),
                       paths=art.subpath_count)


def prepare_trace_image(img: Image.Image, preset: TracePreset, colors: int) -> Image.Image:
    """The posterized image that is traced: RGB, or RGBA whose alpha is only 0 or 255."""
    if not 2 <= colors <= 256:
        raise ValueError("colors must be between 2 and 256")
    has_alpha = "A" in img.getbands() or "transparency" in img.info
    work = _fit_long_edge(img.convert("RGBA" if has_alpha else "RGB"), preset.min_long_edge, preset.max_long_edge)
    opaque = None
    if has_alpha:
        mask = np.asarray(work.getchannel("A")) >= 128
        if not mask.any():
            raise ValueError("The image is fully transparent; there is nothing to vectorize.")
        opaque = None if mask.all() else mask
        work = work.convert("RGB")
    if preset.denoise:
        work = work.filter(ImageFilter.MedianFilter(preset.denoise))
    palette = _palette(np.asarray(work), opaque, colors)
    palette_image = Image.new("P", (1, 1))
    palette_image.putpalette(palette.ravel().tolist())
    posterized = work.quantize(palette=palette_image, dither=Image.Dither.NONE).convert("RGB")
    if opaque is not None:
        posterized.putalpha(Image.fromarray(opaque.astype(np.uint8) * 255))
    return posterized


def _fit_long_edge(img: Image.Image, low: int, high: int) -> Image.Image:
    long_edge = max(img.size)
    if long_edge < low:  # bilinear never overshoots, so quantized contours stay smooth (no ringing slivers)
        return img.resize(scaled_size(img.size, low), Image.Resampling.BILINEAR)
    if long_edge > high:
        return img.resize(scaled_size(img.size, high), Image.Resampling.BOX)
    return img


def _palette(rgb: np.ndarray, opaque: np.ndarray | None, colors: int) -> np.ndarray:
    """Palette from a deterministic random sample, preferring pixels inside flat areas over anti-aliased blends."""
    height, width, _ = rgb.shape
    rng = np.random.default_rng(0)
    ys = rng.integers(0, height, PALETTE_SAMPLES)
    xs = rng.integers(0, width, PALETTE_SAMPLES)
    if opaque is not None:
        inside = opaque[ys, xs]
        if not inside.any():  # only a sliver is opaque: use those pixels directly
            return quantize_pixels(rgb[opaque][:PALETTE_SAMPLES], colors)[0]
        ys, xs = ys[inside], xs[inside]
    pixels = rgb[ys, xs].astype(np.int16)
    right = rgb[ys, np.minimum(xs + 1, width - 1)]
    below = rgb[np.minimum(ys + 1, height - 1), xs]
    flat = (np.abs(pixels - right).max(axis=1) <= EDGE_TOLERANCE) & (np.abs(pixels - below).max(axis=1) <= EDGE_TOLERANCE)
    if flat.sum() >= MIN_FLAT_SAMPLES:
        pixels = pixels[flat]
    return quantize_pixels(pixels.astype(np.uint8), colors)[0]
