"""Layered SVG and vector PDF from traced colour layers (`VectorArt`); the EPS writer is `eps.vector_to_eps`.

Every colour layer is ONE compound path: in the SVG a `<g>` that Inkscape shows as a layer (other
editors import it as a group named by its id), in the PDF an optional content group that viewers
list as a layer. Coordinates stay in traced pixels with at most two decimals and never use exponent
notation; the PDF maps them onto the physical page with one y-flipping `cm`. The SVG is pure ASCII
(non-ASCII text is written as character references).
"""

from __future__ import annotations

import hashlib
import time
import unicodedata
import zlib
from collections.abc import Iterable
from xml.sax.saxutils import escape

from app.imaging.eps import MAX_TITLE, _check_art, _check_page, _flip_scale, _layer_ops, _num, _subpath_items
from app.imaging.layers import ColorLayer, VectorArt

CREATOR = "Jersey HD Studio"
PRODUCER = "Jersey HD Studio vector PDF writer"
LINE_WIDTH = 200  # path data and long PDF arrays are wrapped into lines of about this many characters

_SVG_NS = "http://www.w3.org/2000/svg"
_INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
# PDF object numbers: catalog, page tree, page, content stream, document info, then one OCG per layer.
_CATALOG, _PAGES, _PAGE, _CONTENTS, _INFO, _FIRST_OCG = 1, 2, 3, 4, 5, 6


def vector_to_svg(art: VectorArt, *, title: str = "jersey-design") -> str:
    """SVG in traced pixels: one `<g>` layer per colour in paint order, each holding one compound `<path>`."""
    _check_art(art)
    width, height = _num(art.width, 2), _num(art.height, 2)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="{_SVG_NS}" xmlns:inkscape="{_INKSCAPE_NS}" version="1.1" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f"<title>{_xml_text(_clean_title(title))}</title>",
    ]
    for index, layer in enumerate(art.layers, start=1):
        lines += [
            f'<g id="L{index:02d}_{layer.hex[1:]}" inkscape:groupmode="layer" '
            f'inkscape:label="{_xml_text(_layer_name(index, layer))}">',
            f'<path fill="{layer.hex}" fill-rule="{art.fill_rule}" d="{_svg_path_data(layer)}"/>',
            "</g>",
        ]
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def vector_to_pdf(art: VectorArt, *, width_pt: float, height_pt: float, title: str = "jersey-design") -> bytes:
    """Vector PDF 1.5, one page: each colour layer is one compound path inside its own optional content group."""
    _check_page(width_pt, height_pt)
    _check_art(art)
    sx, sy, ty = _flip_scale(width_pt, height_pt, art.width, art.height)
    fill_op = "f*" if art.fill_rule == "evenodd" else "f"
    content = ["q", f"{sx} 0 0 {sy} 0 {ty} cm"]
    for index, layer in enumerate(art.layers, start=1):
        content.append(f"/OC /oc{index} BDC")
        if layer.subpaths:
            content.extend(_layer_ops(layer, fill_op))
        content.append("EMC")
    content.append("Q")
    stream = zlib.compress(("\n".join(content) + "\n").encode("ascii"), 6)

    ocgs = [f"{_FIRST_OCG + i} 0 R" for i in range(len(art.layers))]
    catalog = f"<< /Type /Catalog /Pages {_PAGES} 0 R"
    resources = "<< >>"
    if ocgs:  # /PageMode /UseOC opens the layers panel in Acrobat and pdf.js
        refs = _wrap(ocgs)
        catalog += f" /PageMode /UseOC\n/OCProperties << /OCGs [{refs}]\n/D << /Order [{refs}]\n/ON [{refs}] >> >>"
        properties = _wrap(f"/oc{i} {ref}" for i, ref in enumerate(ocgs, start=1))
        resources = f"<< /Properties << {properties} >> >>"
    created = time.strftime("D:%Y%m%d%H%M%SZ", time.gmtime())
    clean_title = _clean_title(title)
    objects = [
        catalog + " >>",
        f"<< /Type /Pages /Kids [{_PAGE} 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent {_PAGES} 0 R /MediaBox [0 0 {_num(width_pt, 4)} {_num(height_pt, 4)}]\n"
        f"/Resources {resources}\n/Contents {_CONTENTS} 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(stream), stream),
        f"<< /Title {_pdf_text(clean_title)}\n/Creator {_pdf_text(CREATOR)}\n/Producer {_pdf_text(PRODUCER)}\n"
        f"/CreationDate ({created}) >>",
        *(f"<< /Type /OCG /Name {_pdf_text(_layer_name(i, layer))} >>" for i, layer in enumerate(art.layers, start=1)),
    ]
    file_id = hashlib.md5(stream + f"{created}{clean_title}".encode(), usedforsecurity=False).hexdigest().upper()
    return _pdf_file([o if isinstance(o, bytes) else o.encode("ascii") for o in objects], file_id)


def _svg_path_data(layer: ColorLayer) -> str:
    """Absolute M/L/C/Z commands for every subpath of the layer, wrapped into short lines."""
    tokens: list[str] = []
    for subpath in layer.subpaths:
        tokens.extend(f"{kind}{coords}" for kind, coords in _subpath_items(subpath))
        tokens.append("Z")
    return _wrap(tokens)


def _pdf_file(objects: list[bytes], file_id: str) -> bytes:
    """Objects numbered from 1, a classic cross-reference table at exact byte offsets, and the trailer."""
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")  # the binary comment marks the file as binary
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f\r\n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n\r\n" % offset for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {_CATALOG} 0 R /Info {_INFO} 0 R "
        f"/ID [<{file_id}> <{file_id}>] >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _layer_name(index: int, layer: ColorLayer) -> str:
    return f"Colour {index} · {layer.hex}"


def _clean_title(title: str) -> str:
    """Control, format and other non-printing characters removed, whitespace collapsed, length capped."""
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in title)
    return " ".join(text.split())[:MAX_TITLE].strip() or "jersey-design"


def _xml_text(text: str) -> str:
    """Escaped for XML text and double-quoted attributes; non-ASCII as character references."""
    return escape(text, {'"': "&quot;"}).encode("ascii", "xmlcharrefreplace").decode("ascii")


def _pdf_text(text: str) -> str:
    """PDF text string: a literal for printable ASCII, else UTF-16BE hex with a byte-order mark."""
    if all(" " <= ch <= "~" for ch in text):
        return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    digits = text.encode("utf-16-be").hex().upper()
    return "<" + _wrap(["FEFF", *(digits[i : i + 4] for i in range(0, len(digits), 4))]) + ">"  # whitespace is ignored


def _wrap(tokens: Iterable[str], width: int = LINE_WIDTH) -> str:
    """Space-separated tokens, starting a new line before a line would exceed `width` characters."""
    lines: list[str] = []
    line: list[str] = []
    size = 0
    for token in tokens:
        if line and size + len(token) > width:
            lines.append(" ".join(line))
            line, size = [], 0
        line.append(token)
        size += len(token) + 1
    if line:
        lines.append(" ".join(line))
    return "\n".join(lines)
