"""Image input/output: validating uploads, decoding model output, PNG/WebP encoding and aspect ratios."""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Sequence

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

log = logging.getLogger("jersey.imaging")

ACCEPTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF", "MPO"})
ACCEPTED_LABEL = "PNG, JPEG, WEBP, GIF, BMP or TIFF"
MIN_EDGE = 16
TINY_LONG_EDGE = 512  # model inputs with a smaller long edge ...
TINY_TARGET = 1024  # ... are upscaled to this so the model sees enough detail
# A model input bigger than this as PNG goes as a JPEG (quality 92, no chroma subsampling) instead: a 3-5 MB upload is
# often cut off on a flaky connection, and the price does not change (input tokens follow the pixel size).
MODEL_INPUT_MAX_PNG_BYTES = 1_500_000
MODEL_INPUT_JPEG_QUALITY = 92
PREVIEW_QUALITY = 88

_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})
_CMS_MODES = frozenset({"RGB", "RGBA", "CMYK", "L"})
_HEIF_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"})
_DECODE_ERRORS = (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError, Image.DecompressionBombError)


class ImageInputError(ValueError):
    """An uploaded file cannot be used. The message is safe to show to users."""


def load_upload(data: bytes, *, max_bytes: int, max_pixels: int = 60_000_000) -> Image.Image:
    """Validate and decode an uploaded image (first frame, EXIF rotation applied).

    Returns mode RGB, or RGBA when the image has real transparency.
    """
    if not data:
        raise ImageInputError("The file is empty.")
    if len(data) > max_bytes:
        raise ImageInputError(f"The file is too large (maximum {max_bytes / 1_048_576:.0f} MB).")
    try:
        img = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageInputError(_too_many_pixels(max_pixels)) from exc
    except _DECODE_ERRORS as exc:
        if _is_heif(data):
            raise ImageInputError("HEIC/HEIF photos are not supported - please export as JPEG or PNG.") from exc
        raise ImageInputError(f"This file is not a readable image. Please upload a {ACCEPTED_LABEL} file.") from exc
    if img.format not in ACCEPTED_FORMATS:
        raise ImageInputError(f"{img.format} files are not supported. Please upload a {ACCEPTED_LABEL} file.")
    width, height = img.size
    if width * height > max_pixels:
        raise ImageInputError(_too_many_pixels(max_pixels))
    if min(width, height) < MIN_EDGE:
        raise ImageInputError(f"The image is too small ({width}x{height} px); it must be at least {MIN_EDGE} px on each side.")
    try:
        img.load()  # first frame of GIF/TIFF/MPO
        ImageOps.exif_transpose(img, in_place=True)
        return _normalize(img)
    except _DECODE_ERRORS as exc:
        raise ImageInputError("The image file is damaged or incomplete.") from exc


def to_png_bytes(img: Image.Image, *, dpi: int | None = None, compress_level: int = 6) -> bytes:
    """Lossless PNG; with `dpi` the pHYs chunk records the print resolution. compress_level 3 is about 2.5x
    faster than the default 6 for ~8% more bytes (worth it for 76 MP print files)."""
    buf = io.BytesIO()
    params = {"dpi": (dpi, dpi)} if dpi else {}
    img.save(buf, format="PNG", compress_level=compress_level, **params)
    return buf.getvalue()


def prepare_model_input(img: Image.Image, *, max_long_edge: int) -> bytes:
    """RGB image for an image model: alpha flattened onto white, long edge capped, tiny images upscaled. PNG, or a
    JPEG when the PNG would be over MODEL_INPUT_MAX_PNG_BYTES (image_mime() tells which)."""
    rgb = flatten_onto_white(img)
    long_edge = max(rgb.size)
    if long_edge > max_long_edge:
        rgb = rgb.resize(scaled_size(rgb.size, max_long_edge), Image.Resampling.LANCZOS)
    elif long_edge < TINY_LONG_EDGE:
        rgb = rgb.resize(scaled_size(rgb.size, min(TINY_TARGET, max_long_edge)), Image.Resampling.LANCZOS)
    png = to_png_bytes(rgb)
    if len(png) <= MODEL_INPUT_MAX_PNG_BYTES:
        return png
    buf = io.BytesIO()
    rgb.convert("RGB").save(buf, "JPEG", quality=MODEL_INPUT_JPEG_QUALITY, subsampling=0, optimize=True)
    return buf.getvalue() if buf.tell() < len(png) else png


def image_mime(data: bytes) -> str:
    """The media type of encoded image bytes (PNG, JPEG or WebP)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def decode_model_output(data: bytes) -> Image.Image:
    """Decode image bytes returned by a model: RGB, or RGBA when the image has transparency."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return _normalize(img)
    except _DECODE_ERRORS as exc:
        raise ValueError("The model returned data that is not a readable image.") from exc


def make_preview(img: Image.Image, *, long_edge: int = 1024) -> bytes:
    """WebP preview for fast display; keeps transparency and never upscales."""
    preview = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA" if _has_alpha(img) else "RGB")
    if max(preview.size) > long_edge:
        preview = preview.resize(scaled_size(preview.size, long_edge), Image.Resampling.LANCZOS, reducing_gap=3.0)
    buf = io.BytesIO()
    preview.save(buf, format="WEBP", quality=PREVIEW_QUALITY, method=4)
    return buf.getvalue()


def nearest_aspect_ratio(width: int, height: int, allowed: Sequence[str]) -> str:
    """The 'W:H' ratio in `allowed` closest to width/height on a log scale (earlier entries win ties)."""
    if width <= 0 or height <= 0 or not allowed:
        raise ValueError("a positive size and at least one allowed ratio are required")
    target = math.log(width / height)
    return min(allowed, key=lambda ratio: abs(_log_ratio(ratio) - target))


def flatten_onto_white(img: Image.Image) -> Image.Image:
    """RGB version of `img` with any transparency composited onto white."""
    if img.mode == "RGB":
        return img
    if not _has_alpha(img):
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def crop_fraction(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """`img` cut to (left, top, right, bottom) given as fractions of its width and height."""
    width, height = img.size
    left, top, right, bottom = box
    x0, y0 = round(left * width), round(top * height)
    return img.crop((x0, y0, max(x0 + 1, round(right * width)), max(y0 + 1, round(bottom * height))))


def scaled_size(size: tuple[int, int], long_edge: int) -> tuple[int, int]:
    """`size` scaled (aspect kept, each side >= 1 px) so that its long edge equals `long_edge`."""
    width, height = size
    scale = long_edge / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _normalize(img: Image.Image) -> Image.Image:
    """RGB, or RGBA if any pixel is not fully opaque; ICC-tagged colours are converted to sRGB."""
    has_alpha = _has_alpha(img)
    if img.mode not in _CMS_MODES or (has_alpha and img.mode != "RGBA"):
        img = img.convert("RGBA" if has_alpha else "RGB")
    icc = img.info.get("icc_profile")
    if icc:
        img = _to_srgb(img, icc)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    if img.mode == "RGBA" and img.getchannel("A").getextrema()[0] == 255:
        img = img.convert("RGB")
    img.info.pop("icc_profile", None)  # pixels are sRGB now; a stale profile would be re-embedded in PNGs
    return img


def _to_srgb(img: Image.Image, icc: bytes) -> Image.Image:
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.profileToProfile(
            img, source, ImageCms.createProfile("sRGB"), outputMode="RGBA" if img.mode == "RGBA" else "RGB"
        )
    except (ImageCms.PyCMSError, OSError, ValueError) as exc:
        log.info("ignoring unusable ICC profile: %s", exc)
        return img


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in _ALPHA_MODES or "transparency" in img.info


def _is_heif(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS


def _too_many_pixels(max_pixels: int) -> str:
    return f"The image has too many pixels (maximum {max_pixels / 1_000_000:g} megapixels)."


def _log_ratio(ratio: str) -> float:
    w, h = ratio.split(":")
    return math.log(float(w) / float(h))
