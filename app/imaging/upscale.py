"""Print-size upscaling: Lanczos resize plus a mild unsharp mask on the colour channels."""

from __future__ import annotations

from PIL import Image, ImageFilter

from app.imaging.io import scaled_size

SHARPEN = ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=2)
# Opaque (RGB) images are enlarged and sharpened in horizontal strips of this many output rows, each with
# STRIP_MARGIN rows of context above and below for the sharpen filter. The result looks the same as doing the
# whole image at once (Pillow rounds a region resize slightly differently: about 5% of the pixels differ, almost all
# by 1 of 255), but only the finished image is held in full: a 76 MP print PNG took about 0.9 GB the other way, more
# than a 1 GB server has (2026-11-05; measured 922 -> 444 MB peak for the whole process).
STRIP_ROWS = 512
STRIP_MARGIN = 16
ASPECT_TOLERANCE = 0.01  # larger ratio differences are centre-cropped, not stretched
EDGE_GUARD_RADIUS = 3  # px around transparency left unsharpened (transparent pixels have no meaningful colour)


def upscale_to_long_edge(img: Image.Image, long_edge: int) -> Image.Image:
    """`img` enlarged so its long edge is `long_edge`; returns `img` itself if it is already that large."""
    if long_edge <= max(img.size):
        return img
    return upscale_to_size(img, scaled_size(img.size, long_edge))


def upscale_to_size(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """`img` resized to exactly `size`. If its aspect ratio differs by more than 1% (a model without custom sizes
    returned its nearest standard size), the centre is cropped to the target ratio first instead of stretching."""
    if img.size == size:
        return img
    ratio = size[0] / size[1]
    if abs((img.width / img.height) / ratio - 1) > ASPECT_TOLERANCE:
        if img.width / img.height > ratio:  # too wide: trim the sides
            width = round(img.height * ratio)
            left = (img.width - width) // 2
            img = img.crop((left, 0, left + width, img.height))
        else:  # too tall: trim the top and bottom
            height = round(img.width / ratio)
            top = (img.height - height) // 2
            img = img.crop((0, top, img.width, top + height))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    if img.mode == "RGB":
        return _resize_sharpen_strips(img, size)
    big = img.resize(size, Image.Resampling.LANCZOS)  # RGBA is resized premultiplied
    alpha = big.getchannel("A")
    rgb = big.convert("RGB")
    # Only sharpen where the whole neighbourhood is opaque, otherwise edges against transparency get a halo.
    opaque = alpha.filter(ImageFilter.BoxBlur(EDGE_GUARD_RADIUS)).point(lambda v: 255 if v == 255 else 0)
    result = Image.composite(rgb.filter(SHARPEN), rgb, opaque)
    result.putalpha(alpha)
    return result


def _resize_sharpen_strips(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Lanczos to `size`, then SHARPEN, one strip at a time. Each strip is resized from the matching source region
    (`box`, which may read past it for the filter's support), so the strips line up exactly."""
    width, height = size
    scale = img.height / height
    out = Image.new("RGB", size)
    for top in range(0, height, STRIP_ROWS):
        bottom = min(height, top + STRIP_ROWS)
        y0, y1 = max(0, top - STRIP_MARGIN), min(height, bottom + STRIP_MARGIN)
        strip = img.resize((width, y1 - y0), Image.Resampling.LANCZOS, box=(0, y0 * scale, img.width, y1 * scale))
        out.paste(strip.filter(SHARPEN).crop((0, top - y0, width, bottom - y0)), (0, top))
    return out
