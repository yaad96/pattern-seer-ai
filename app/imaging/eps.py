"""EPS export for print: vector fills from traced layers or an SVG, or the raster image embedded losslessly.

All writers emit a minimal EPSF-3.0 file that Adobe Illustrator and CorelDRAW import:
DSC header comments, 7-bit ASCII only, lines of at most 255 characters, procedures in a
private dictionary, no page-device operators and no numbers in exponent notation. Vector
coordinates stay in traced/SVG pixels (two decimals); one translate/scale maps them onto the
physical size in points with a y-flip. The layer helpers here are shared with the PDF writer
(vector_out.py): PDF content streams use the same m/l/c/h/rg operator names. `combine_eps` puts
several of these files on one page (the combined front, back and sleeve in the panel ZIP).
"""

from __future__ import annotations

import io
import math
import numbers
import re
import time
import unicodedata
import zlib
from collections.abc import Iterable

import numpy as np
from PIL import Image
from svgelements import SVG, Arc, Close, CubicBezier, Line, Move, Point, QuadraticBezier, Shape

from app.imaging.io import flatten_onto_white
from app.imaging.layers import ColorLayer, Subpath, VectorArt

A85_LINE_WIDTH = 76
MAX_TITLE = 100

_PROLOG = (
    "%%BeginProlog",
    "/JHSDict 16 dict def",
    "JHSDict begin",
    "/m { moveto } bind def",
    "/l { lineto } bind def",
    "/c { curveto } bind def",
    "/h { closepath } bind def",
    "/f { fill } bind def",
    "/ef { eofill } bind def",
    "/rg { setrgbcolor } bind def",
    "end",
    "%%EndProlog",
)
_TITLE_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")

# Fast path for our own vtracer output (see vectorize.py): only <path d="absolute M/L/C/Z" fill="#RRGGBB"
# transform="translate(x,y)"/> elements. svgelements needs ~8 s for 50k such paths; this needs about one.
_TRACED_DOC = re.compile(r"\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--[^>]*-->\s*)*<svg\b([^>]*)>(.*)</svg>\s*", re.S)
_TRACED_PATH = re.compile(
    r'\s*<path d="([MLCZ0-9 .,-]*)" fill="#([0-9A-Fa-f]{6})" transform="translate\((-?[0-9.]+),(-?[0-9.]+)\)"\s*/>'
)
_PATH_TOKEN = re.compile(r"[MLCZ]|-?[0-9.]+")
_ARG_COUNT = {"M": 2, "L": 2, "C": 6, "Z": 0}
_PS_OP = {"M": "m", "L": "l", "C": "c"}
_ROOT_ATTR = re.compile(r'\b(width|height|viewBox)="([^"]*)"')

RGB = tuple[int, int, int]
Fill = tuple[RGB, bool, list[str]]  # colour, even-odd rule, path operators


def physical_size_pt(width_px: int, height_px: int, dpi: int) -> tuple[float, float]:
    """Size in PostScript points (1/72 inch) of an image printed at `dpi`."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    return width_px * 72 / dpi, height_px * 72 / dpi


def svg_to_eps(svg: str, *, width_pt: float, height_pt: float, title: str = "jersey-design") -> bytes:
    """Vector EPS (LanguageLevel 2): every SVG shape with a visible fill becomes a filled PostScript path."""
    _check_page(width_pt, height_pt)
    parsed = _parse_traced_svg(svg) or _parse_svg(svg)
    (svg_width, svg_height), fills = parsed
    sx, sy, ty = _flip_scale(width_pt, height_pt, svg_width, svg_height)
    body = [f"0 {ty} translate {sx} {sy} scale"]
    current: RGB | None = None
    for rgb, evenodd, ops in fills:
        if rgb != current:
            body.append(_rgb_op(rgb))
            current = rgb
        body.extend(ops)
        body.append("ef" if evenodd else "f")
    return _document(body, width_pt=width_pt, height_pt=height_pt, title=title, language_level=2)


def vector_to_eps(art: VectorArt, *, width_pt: float, height_pt: float, title: str = "jersey-design") -> bytes:
    """Vector EPS (LanguageLevel 2) of traced colour layers: one compound path per layer, in paint order."""
    _check_page(width_pt, height_pt)
    _check_art(art)
    sx, sy, ty = _flip_scale(width_pt, height_pt, art.width, art.height)
    fill_op = "ef" if art.fill_rule == "evenodd" else "f"
    body = [f"0 {ty} translate {sx} {sy} scale"]
    for layer in art.layers:
        if layer.subpaths:
            body.extend(_layer_ops(layer, fill_op))
    return _document(body, width_pt=width_pt, height_pt=height_pt, title=title, language_level=2)


def is_current_eps(head: bytes) -> bool:
    """True when `head`, the first few kilobytes of an EPS file, has the header line, prolog and page opening this
    module writes today. combine_eps refuses anything else, so an export made before a change to them is traced
    again instead of being reused."""
    text = head.decode("ascii", "replace")
    return text.startswith(_EPSF_LINE + "\n") and "\n" + "\n".join((*_PROLOG, *_PAGE_START)) + "\n" in text


def eps_size(eps: bytes) -> tuple[float, float]:
    """Width and height in points (the HiResBoundingBox) of an EPS file written by this module."""
    width_pt, height_pt, _, _ = _own_eps(eps)
    return width_pt, height_pt


def combine_eps(
    parts: list[tuple[bytes, float, float]], *, width_pt: float, height_pt: float, title: str = "jersey-design"
) -> bytes:
    """One EPS page with several EPS files written by this module on it (vector_to_eps, svg_to_eps, raster_eps):
    (file, x, y), where (x, y) in points is where the bottom-left corner of that file's bounding box goes. Each body is
    copied unchanged inside gsave/grestore after one translate and a clip to the file's own box (traced curves can
    bulge up to about half a millimetre past their canvas, which a separate file's bounding box crops), so the paths,
    colours and layer order, or the image and its pixels, are those of the separate files. Every body line is checked
    first: one stray operator (showpage, end, ...) would break the whole page, not just its own panel."""
    _check_page(width_pt, height_pt)
    body: list[str] = []
    level = 2
    for eps, x, y in parts:
        part_width, part_height, part_level, part_body = _own_eps(eps)
        if not (all(map(_VECTOR_LINE.fullmatch, part_body)) or _is_raster_body(part_body)):
            raise ValueError("This is not a vector or raster EPS file written by Jersey HD Studio.")
        fits = all(math.isfinite(v) for v in (x, y)) and min(x, y) > -0.01
        if not fits or x + part_width > width_pt + 0.01 or y + part_height > height_pt + 0.01:
            raise ValueError("A placed EPS file does not fit on the page.")
        level = max(level, part_level)
        body += [
            "gsave",
            f"{_num(x, 4)} {_num(y, 4)} translate",
            f"0 0 {_num(part_width, 4)} {_num(part_height, 4)} rectclip",
            *part_body,
            "grestore",
        ]
    return _document(body, width_pt=width_pt, height_pt=height_pt, title=title, language_level=level)


def raster_eps(img: Image.Image, *, width_pt: float, height_pt: float, title: str = "jersey-design") -> bytes:
    """Raster EPS (LanguageLevel 3): the exact RGB pixels (alpha flattened onto white), Flate + ASCII85."""
    _check_page(width_pt, height_pt)
    rgb = flatten_onto_white(img)
    w, h = rgb.size
    data = _ascii85_lines(zlib.compress(rgb.tobytes(), 6))
    data[-1] += "~>"
    body = [
        f"{_num(width_pt, 4)} {_num(height_pt, 4)} scale",
        "/DeviceRGB setcolorspace",
        "/JHSData currentfile /ASCII85Decode filter def",
        # image and flushfile run as one procedure so the interpreter always resumes after "~>",
        # even if the Flate decoder stops before the end of the ASCII85 data.
        f"{{ << /ImageType 1 /Width {w} /Height {h} /BitsPerComponent 8 /Decode [0 1 0 1 0 1]",
        f"/ImageMatrix [{w} 0 0 -{h} 0 {h}] /DataSource JHSData /FlateDecode filter >> image",
        "JHSData flushfile } exec",
        *data,
    ]
    return _document(body, width_pt=width_pt, height_pt=height_pt, title=title, language_level=3)


# ---------------------------------------------------------------------------
# SVG parsing
# ---------------------------------------------------------------------------


def _parse_svg(svg: str) -> tuple[tuple[float, float], list[Fill]]:
    """Any SVG via svgelements (shapes, arcs, quadratic curves, transforms, CSS fill rules)."""
    doc = SVG.parse(io.StringIO(svg), reify=True)
    size = (float(doc.width or 0), float(doc.height or 0))
    if not all(math.isfinite(v) and v > 0 for v in size):
        raise ValueError("The SVG has no usable width/height.")
    fills: list[Fill] = []
    for element in doc.elements():
        if isinstance(element, Shape) and _has_visible_fill(element):
            ops = _segment_ops(element.segments(transformed=True))
            if ops:
                color = element.fill
                evenodd = str(element.values.get("fill-rule", "")).strip() == "evenodd"
                fills.append(((color.red, color.green, color.blue), evenodd, ops))
    return size, fills


def _has_visible_fill(shape: Shape) -> bool:
    fill = shape.fill
    if fill is None or fill.value is None or fill.alpha == 0:
        return False
    try:
        return float(shape.values.get("opacity", 1)) > 0
    except (TypeError, ValueError):
        return True


def _segment_ops(segments: Iterable) -> list[str]:
    ops: list[str] = []
    for seg in segments:
        if isinstance(seg, Move):
            ops.append(f"{_xy(seg.end)} m")
        elif isinstance(seg, Close):
            ops.append("h")
        elif isinstance(seg, Line):
            ops.append(f"{_xy(seg.end)} l")
        elif isinstance(seg, CubicBezier):
            ops.append(_curve(seg.control1, seg.control2, seg.end))
        elif isinstance(seg, QuadraticBezier):
            ops.append(
                _curve(
                    seg.start + (seg.control - seg.start) * (2 / 3),
                    seg.end + (seg.control - seg.end) * (2 / 3),
                    seg.end,
                )
            )
        elif isinstance(seg, Arc):
            ops.extend(_curve(c.control1, c.control2, c.end) for c in seg.as_cubic_curves())
    return ops


def _parse_traced_svg(svg: str) -> tuple[tuple[float, float], list[Fill]] | None:
    """vtracer's output parsed directly (same result as svgelements); None if `svg` is anything else."""
    doc = _TRACED_DOC.fullmatch(svg)
    if doc is None:
        return None
    attrs = dict(_ROOT_ATTR.findall(doc.group(1)))
    try:
        size = (float(attrs["width"]), float(attrs["height"]))
        view_box = [float(v) for v in attrs.get("viewBox", f"0 0 {size[0]} {size[1]}").replace(",", " ").split()]
    except (KeyError, ValueError):
        return None
    if view_box != [0, 0, *size] or not all(math.isfinite(v) and v > 0 for v in size):
        return None
    body, pos, fills = doc.group(2), 0, []
    while (match := _TRACED_PATH.match(body, pos)) is not None:
        d, color, tx, ty = match.groups()
        ops = _traced_ops(d, float(tx), float(ty))
        if ops is None:
            return None
        if ops:
            fills.append(((int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)), False, ops))
        pos = match.end()
    return (size, fills) if not body[pos:].strip() else None


def _traced_ops(d: str, tx: float, ty: float) -> list[str] | None:
    tokens = _PATH_TOKEN.findall(d)
    ops: list[str] = []
    i = 0
    try:
        while i < len(tokens):
            command = tokens[i]
            count = _ARG_COUNT[command]  # KeyError: implicit repeated command, not vtracer's style
            args = [float(v) for v in tokens[i + 1 : i + 1 + count]]
            if len(args) != count:
                return None
            i += 1 + count
            if command == "Z":
                ops.append("h")
            else:
                coords = [v + (ty if k % 2 else tx) for k, v in enumerate(args)]
                ops.append(" ".join(_num(v, 2) for v in coords) + " " + _PS_OP[command])
    except (KeyError, ValueError):
        return None
    return ops


# ---------------------------------------------------------------------------
# Traced layers (shared with the PDF writer)
# ---------------------------------------------------------------------------


def _check_art(art: VectorArt) -> None:
    """Reject art that would otherwise be written as a broken or wrongly filled file."""
    if not all(math.isfinite(v) and v > 0 for v in (art.width, art.height)):
        raise ValueError("Vector art width and height must be positive.")
    if art.fill_rule not in ("evenodd", "nonzero"):
        raise ValueError(f"Unknown fill rule {art.fill_rule!r}; expected 'evenodd' or 'nonzero'.")
    for layer in art.layers:
        if len(layer.rgb) != 3 or not all(isinstance(v, numbers.Integral) and 0 <= v <= 255 for v in layer.rgb):
            raise ValueError(f"Layer colour {layer.rgb!r} is not three integers from 0 to 255.")


def _layer_ops(layer: ColorLayer, fill_op: str) -> list[str]:
    """One colour layer as ONE compound path: `r g b rg`, m/l/c/h for every subpath, then a single fill."""
    ops = [_rgb_op(layer.rgb)]
    for subpath in layer.subpaths:
        ops.extend(f"{coords} {_PS_OP[kind]}" for kind, coords in _subpath_items(subpath))
        ops.append("h")
    ops.append(fill_op)
    return ops


def _subpath_items(subpath: Subpath) -> list[tuple[str, str]]:
    """("M" | "L" | "C", formatted coordinates) for one contour.

    A final straight segment back to the start is left out: closing the path draws that line,
    and editors would otherwise show a duplicate anchor point.
    """
    start = _point(subpath.start)
    items = [("M", start)]
    for segment in subpath.segments:
        kind = segment[0]
        if kind == "L":
            items.append(("L", _point(segment[1])))
        elif kind == "C":
            items.append(("C", f"{_point(segment[1])} {_point(segment[2])} {_point(segment[3])}"))
        else:
            raise ValueError(f"Unknown path segment type {kind!r}.")
    if len(items) > 1 and items[-1] == ("L", start):
        items.pop()
    return items


def _point(point: tuple[float, float]) -> str:
    x, y = point
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("Vector art has a coordinate that is not a finite number.")
    return f"{_num(x, 2)} {_num(y, 2)}"


# ---------------------------------------------------------------------------
# PostScript output
# ---------------------------------------------------------------------------


def _document(body: list[str], *, width_pt: float, height_pt: float, title: str, language_level: int) -> bytes:
    lines = [
        _EPSF_LINE,
        f"%%BoundingBox: 0 0 {math.ceil(round(width_pt, 6))} {math.ceil(round(height_pt, 6))}",
        f"%%HiResBoundingBox: 0 0 {_num(width_pt, 4)} {_num(height_pt, 4)}",
        "%%Creator: Jersey HD Studio",
        f"%%Title: {_ascii_title(title)}",
        f"%%CreationDate: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC",
        "%%DocumentData: Clean7Bit",
        f"%%LanguageLevel: {language_level}",
        "%%Pages: 1",
        "%%EndComments",
        *_PROLOG,
        "%%Page: 1 1",
        "JHSDict begin",
        "gsave",
        *body,
        "grestore",
        "end",
        "showpage",
        "%%Trailer",
        "%%EOF",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


_EPSF_LINE = "%!PS-Adobe-3.0 EPSF-3.0"
_PAGE_START = ("%%Page: 1 1", "JHSDict begin", "gsave")
_PAGE_END = ("grestore", "end", "showpage", "%%Trailer", "%%EOF")
_HIRES_BBOX = re.compile(r"%%HiResBoundingBox: 0 0 ([0-9.]+) ([0-9.]+)")
_LEVEL = re.compile(r"%%LanguageLevel: ([23])")
# Every line a vector body has: the canvas-to-page transform, then colours and path operators (see _layer_ops).
_VECTOR_LINE = re.compile(r"(?:-?[0-9.]+ )+(?:m|l|c|rg)|[hf]|ef|0 -?[0-9.]+ translate -?[0-9.]+ -?[0-9.]+ scale")
# A raster body (raster_eps): these six lines, then ASCII85 data lines, the last one ending in "~>".
_RASTER_HEAD = tuple(map(re.compile, (
    r"[0-9.]+ [0-9.]+ scale",
    r"/DeviceRGB setcolorspace",
    r"/JHSData currentfile /ASCII85Decode filter def",
    r"\{ << /ImageType 1 /Width [0-9]+ /Height [0-9]+ /BitsPerComponent 8 /Decode \[0 1 0 1 0 1\]",
    r"/ImageMatrix \[[0-9]+ 0 0 -[0-9]+ 0 [0-9]+\] /DataSource JHSData /FlateDecode filter >> image",
    r"JHSData flushfile \} exec",
)))
# the ASCII85 alphabet ("~", the end of data, and "z" are never inside a line), with the space _ascii85_lines puts
# before a line that would start with "%"
_ASCII85_LINE = re.compile(r" ?[!-u]*")


def _is_raster_body(lines: list[str]) -> bool:
    """True for exactly the body raster_eps writes: one image read from the lines after it, up to its "~>"."""
    head, data = lines[: len(_RASTER_HEAD)], lines[len(_RASTER_HEAD) :]
    return (
        len(head) == len(_RASTER_HEAD) and all(p.fullmatch(line) for p, line in zip(_RASTER_HEAD, head))
        and bool(data) and data[-1].endswith("~>") and _ASCII85_LINE.fullmatch(data[-1][:-2]) is not None
        and all(map(_ASCII85_LINE.fullmatch, data[:-1]))
    )


def _own_eps(eps: bytes) -> tuple[float, float, int, list[str]]:
    """(width pt, height pt, LanguageLevel, body lines) of an EPS file with the header, prolog and page envelope
    that _document() writes; ValueError otherwise. The body is everything between the page's opening gsave and its
    closing grestore; it is not inspected here (combine_eps does that)."""
    try:
        lines = eps.decode("ascii").split("\n")
    except UnicodeDecodeError:
        lines = []
    if lines and lines[-1] == "":
        lines.pop()
    start = lines.index(_PROLOG[0]) if _PROLOG[0] in lines else -1
    page = start + len(_PROLOG)
    header = lines[:start] if start > 0 else []
    size = next((m.groups() for m in map(_HIRES_BBOX.fullmatch, header) if m), None)
    level = next((int(m.group(1)) for m in map(_LEVEL.fullmatch, header) if m), None)
    if (
        not header or header[0] != _EPSF_LINE or size is None or level is None
        or tuple(lines[start:page]) != _PROLOG or tuple(lines[page : page + 3]) != _PAGE_START
        or len(lines) < page + 3 + len(_PAGE_END) or tuple(lines[-len(_PAGE_END) :]) != _PAGE_END
    ):
        raise ValueError("This is not an EPS file written by Jersey HD Studio.")
    width_pt, height_pt = float(size[0]), float(size[1])
    _check_page(width_pt, height_pt)
    return width_pt, height_pt, level, lines[page + 3 : -len(_PAGE_END)]


def _ascii85_lines(data: bytes) -> list[str]:
    """Adobe ASCII85 (no '<~' prefix, no 'z' shortcut) in short lines; the caller appends '~>'.

    No line starts with '%', so DSC readers never mistake data for a comment.
    """
    pad = -len(data) % 4
    words = np.frombuffer(data + b"\0" * pad, dtype=">u4").astype(np.int64)
    digits = np.empty((words.size, 5), dtype=np.uint8)
    for i in range(4, -1, -1):
        words, digits[:, i] = np.divmod(words, 85)
    text = (digits + 33).tobytes()[: digits.size - pad].decode("ascii")
    lines = [text[i : i + A85_LINE_WIDTH] for i in range(0, len(text), A85_LINE_WIDTH)]
    return [" " + line if line.startswith("%") else line for line in lines]


def _check_page(width_pt: float, height_pt: float) -> None:
    if not all(math.isfinite(v) and v > 0 for v in (width_pt, height_pt)):
        raise ValueError("Page width and height must be positive numbers of points.")


def _flip_scale(width_pt: float, height_pt: float, width: float, height: float) -> tuple[str, str, str]:
    """x scale, y scale and y offset mapping a top-left-origin canvas onto the page in points (y-flip)."""
    return _num(width_pt / width, 8), _num(-height_pt / height, 8), _num(height_pt, 4)


def _rgb_op(rgb: RGB) -> str:
    return " ".join(_num(v / 255, 4) for v in rgb) + " rg"


def _ascii_title(title: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return _TITLE_UNSAFE.sub("-", ascii_text).strip(" -.")[:MAX_TITLE] or "jersey-design"


def _num(value: float, places: int) -> str:
    """Fixed-point number for PostScript: no exponent, no trailing zeros, no '-0' (places >= 1)."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _xy(point: Point) -> str:
    return f"{_num(point.x, 2)} {_num(point.y, 2)}"


def _curve(c1: Point, c2: Point, end: Point) -> str:
    return f"{_xy(c1)} {_xy(c2)} {_xy(end)} c"
