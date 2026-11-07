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


def dominant_colors(img: Image.Image, k: int = 8) -> list[PaletteEntry]:
    """Up to `k` dominant colours, largest share first; fully transparent pixels are ignored."""
    small = img
    if max(img.size) > SAMPLE_LONG_EDGE:
        small = img.resize(scaled_size(img.size, SAMPLE_LONG_EDGE), Image.Resampling.NEAREST)  # keeps exact colours
    rgba = np.asarray(small.convert("RGBA")).reshape(-1, 4)
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
