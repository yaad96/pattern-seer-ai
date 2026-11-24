"""Colour palettes: dominant-colour swatches for the UI and the shared quantizer used by the vectorizer."""

from __future__ import annotations

import numpy as np
from PIL import Image

from app.imaging.io import scaled_size
from app.schemas import PaletteEntry

SAMPLE_LONG_EDGE = 256
MERGE_DISTANCE = 24.0  # RGB distance below which two quantized colours are shown as one swatch
MIN_SHARE = 0.005


def quantize_pixels(pixels: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Palette of up to `k` colours for an (n, 3) uint8 pixel array, without dithering.

    Max-coverage split refined by k-means (it keeps small accent colours that median cut merges
    away). Returns (colours (m, 3) uint8, pixel counts (m,)), most common first.
    """
    data = np.ascontiguousarray(pixels, dtype=np.uint8)
    strip = Image.frombytes("RGB", (len(data), 1), data.tobytes())
    quantized = strip.quantize(colors=k, method=Image.Quantize.MAXCOVERAGE, kmeans=1, dither=Image.Dither.NONE)
    palette = np.array(quantized.getpalette() or [], dtype=np.uint8).reshape(-1, 3)
    counts = np.bincount(np.asarray(quantized).ravel(), minlength=len(palette))[: len(palette)]
    order = np.argsort(-counts, kind="stable")
    order = order[counts[order] > 0]
    return palette[order], counts[order]


def symmetric_indices(n: int, m: int) -> np.ndarray:
    """About `m` of the indices 0 … n-1, evenly spread and mirror-symmetric (i is in it exactly when n-1-i is). An image
    and its mirror image are then sampled at matching pixels, so a back panel mirrored from the front gets exactly the
    front's colours (2026-11-23: the owner saw them differ by a few units)."""
    if m >= n:
        return np.arange(n)
    half = np.floor(np.linspace(0, n - 1, m)[: (m + 1) // 2]).astype(np.int64)
    return np.unique(np.concatenate([half, n - 1 - half]))


def dominant_colors(img: Image.Image, k: int = 8) -> list[PaletteEntry]:
    """Up to `k` dominant colours, largest share first; fully transparent pixels are ignored. Read from an evenly
    spread grid of exact pixels (SAMPLE_LONG_EDGE per side), the same for an image and its mirror image."""
    rgba = np.asarray(img.convert("RGBA"))
    height, width = rgba.shape[:2]
    if max(width, height) > SAMPLE_LONG_EDGE:
        w, h = scaled_size((width, height), SAMPLE_LONG_EDGE)
        rgba = rgba[np.ix_(symmetric_indices(height, h), symmetric_indices(width, w))]
    rgba = rgba.reshape(-1, 4)
    pixels = rgba[rgba[:, 3] > 0, :3]
    if not len(pixels):
        return []
    colors, counts = _merge_similar(*quantize_pixels(pixels, k))
    total = len(pixels)
    return [
        PaletteEntry(hex="#{:02X}{:02X}{:02X}".format(*color), pct=round(100 * count / total, 1))
        for color, count in zip(colors, counts)
        if count / total >= MIN_SHARE
    ]


def _merge_similar(colors: np.ndarray, counts: np.ndarray) -> tuple[list[tuple[int, int, int]], list[int]]:
    """Fold each colour into an earlier (more common) one closer than MERGE_DISTANCE; result sorted by count."""
    kept: list[np.ndarray] = []
    totals: list[int] = []
    for color, count in zip(colors.astype(np.int32), counts):
        for i, other in enumerate(kept):
            if float(np.sqrt(((other - color) ** 2).sum())) < MERGE_DISTANCE:
                totals[i] += int(count)
                break
        else:
            kept.append(color)
            totals.append(int(count))
    order = sorted(range(len(kept)), key=lambda i: -totals[i])
    return [tuple(int(v) for v in kept[i]) for i in order], [totals[i] for i in order]
