"""Per-colour stacked vector layers of a posterized image, traced with vtracer's binary mode.

trace_layers(posterized, params) -> VectorArt: one compound path per colour, stacked "overlapping",
evenodd (holes are also wound opposite to their outline, so nonzero renders the same).

1. Pixels are labelled by palette colour. A palette colour within `merge_tol` (every channel) of a larger
   colour shares its layer (it renders within that tolerance). Regions smaller than `speckle_area` px take
   the most similar neighbouring colour. Opaque pixels never become transparent, transparency is never
   filled. An image left with more than MAX_REGIONS regions is refused with a ValueError.
2. Paint order = visible area, largest first. Layer k fills *under* the later layers: its mask is every
   4-connected region of colours ranked >= k that contains colour k. That leaves no hairline gaps, and
   regions that never touch colour k are not traced again. An opaque image's first layer is the canvas.
3. vtracer traces each mask in binary mode as an exact pixel staircase ("none"): foreground is red < 128,
   each 4-connected cluster is one <path>, and each hole is an extra subpath wound the other way. Masks are
   cut at empty rows / columns first, so sparse masks are traced on small canvases. vtracer numbers its
   clusters with 16 bits and panics past 65,535, so a piece that could need more (a fine mesh) is halved
   until it fits; vertices on such a cut line never move, so both halves still meet exactly.
4. Staircases become their unbiased midline: every step (an edge of <= step_max px whose two ends turn
   opposite ways) is replaced by its midpoint, so a shallow slanted edge becomes one straight line instead
   of a one-pixel staircase. Real corners (both ends turning the same way) are kept, and so are the ends of
   a long plateau between two opposite steps (a one-pixel bump or dent), though never as corners.
5. Corners: a midline vertex turning >= vertex_corner_deg, or a turn >= corner_deg between chords
   corner_window px long that is concentrated (it does not double over twice the chord). A rounded corner
   with straight sides is sharpened to the intersection of its two edge lines.
6. Fitting: every run between corners starts as one cubic per midline edge; rounds of pairwise merging
   refit two neighbouring cubics as one least-squares cubic (tangent-constrained, Schneider) and keep it
   while the midline stays within fit_tol px. Merges never cross a corner, smooth anchors keep one tangent
   (G1), and straight results become lines, so collinear runs collapse to a single L.
7. Next to transparency the fit tolerance and corner moves are capped at ALPHA_TOL / ALPHA_REACH and the
   midline is not decimated, so no outline strays past the one-pixel alpha edge.
"""

from __future__ import annotations

import io
import re
from typing import NamedTuple

import numpy as np
import vtracer
from PIL import Image

from app.imaging.layers import ColorLayer, Subpath, VectorArt


class LayerParams(NamedTuple):
    """Clean-up, corner and curve-fit settings of one preset."""

    speckle_area: int  # px: smaller regions take the most similar neighbouring colour
    merge_tol: int  # 0-255 per channel: palette colours this close to a larger one share its layer
    fit_tol: float  # px: max distance between a fitted cubic and the staircase midline
    corner_window: float  # px: chord length of the windowed corner test (0 = single-vertex corners only)
    corner_deg: float  # windowed turn that makes a corner
    corner_dmax: float  # px: a sharpened corner moves at most this far
    vertex_corner_deg: float = 60.0  # a single midline vertex turning this much is a corner
    step_max: float = 2.0  # px: staircase steps up to this long are replaced by their midpoints
    edge_keep: float = 16.0  # px: a plateau this long between two opposite steps keeps its exact position
    decimate_passes: int = 0  # passes dropping near-collinear midline vertices before fitting (speed)


SPECKLE_ROUNDS = 3  # enough on every test image: the third round finds nothing left
DENSIFY = 2.0  # px between fitting data points along long midline edges
LINE_EPS = 0.02  # px: a cubic whose handles are this close to its chord is written as a line
STRAIGHT_EPS = 0.6  # px: a corner side counts as straight when its middle is this close to its chord
HARD_CORNER_DEG = 75.0  # an unsharpenable windowed corner is still kept when it turns this much
CORNER_SPREAD = 1.5  # a windowed corner's turn over doubled chords must stay below this multiple
MAX_ROUNDS = 200  # safety bound on merge rounds (the test images need fewer than 30)
DECIMATE_DELTA = 0.1  # px: midline vertices this close to their neighbours' chord are dropped before fitting
RETRY_FACTOR = 2.0  # pairs whose first fit misses tol by up to this factor get a Newton refit
ALPHA_TOL = 0.6  # px: fit tolerance next to transparency, so no outline strays past the one-pixel alpha edge
ALPHA_REACH = 0.75  # px: how far a sharpened corner next to transparency may move
VTRACER_MAX_CLUSTERS = 60_000  # per vtracer call; vtracer's 16-bit cluster labels panic at 65,535
MAX_REGIONS = 300_000  # colour regions after clean-up; beyond this the vectors would be millions of anchors
TOO_FINE = "This image has too many tiny separate shapes to trace as vectors. Try the Flat preset or Raster EPS."

Cut = tuple[int, str, int, int, int]  # (layer, "x" | "y", coordinate, from, to): a line where a mask was split


def trace_layers(posterized: Image.Image, params: LayerParams) -> VectorArt:
    """Stacked per-colour layers (paint order = visible area, largest first) of a posterized image."""
    width, height = posterized.size
    lab, palette, transparent = _labels(posterized)
    lab, palette = _merge_near_duplicates(lab, palette, transparent, params.merge_tol)
    lab = _merge_speckles(lab, palette, transparent, params.speckle_area)
    comp, n_comp, colour_of = _components(lab)
    if n_comp > MAX_REGIONS:  # e.g. a one-pixel checkerboard: refuse instead of using gigabytes
        raise ValueError(TOO_FINE)
    areas = np.bincount(lab.ravel(), minlength=len(palette))
    order = sorted((c for c in range(len(palette)) if c != transparent and areas[c] > 0),
                   key=lambda c: (-int(areas[c]), c))
    rank = np.full(len(palette), -1, np.int64)
    rank[order] = np.arange(len(order))
    node_rank = rank[colour_of]
    edges = _adjacency(comp, n_comp)
    traced: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    cuts: list[Cut] = []
    for k in range(len(order)):
        if k == 0 and transparent is None:
            traced.append(_empty_polygons())  # the canvas rectangle is added below
        else:
            traced.append(_trace_mask(_fill_under_mask(comp, node_rank, edges, k), k, cuts))
    near_clear = _dilate(lab == transparent) if transparent is not None else None
    pieces, owner = _fit_layers(traced, params, near_clear, width, height, cuts)
    layers = [ColorLayer(rgb=tuple(int(v) for v in palette[c]), subpaths=[], area_px=int(areas[c])) for c in order]
    if transparent is None and layers:
        layers[0].subpaths.append(_rectangle(width, height))
    for (start, segments), k in zip(pieces, owner):
        layers[k].subpaths.append(Subpath(start=start, segments=segments))
    return VectorArt(width=width, height=height, layers=layers, fill_rule="evenodd")


# ------------------------------------------------------------------------------------------- pixel stage


def _labels(img: Image.Image) -> tuple[np.ndarray, np.ndarray, int | None]:
    """(palette index per pixel, palette RGB (n, 3) int, index of 'transparent' or None)."""
    rgba = np.asarray(img.convert("RGBA"))
    key = (rgba[..., 0].astype(np.int32) << 16) | (rgba[..., 1].astype(np.int32) << 8) | rgba[..., 2]
    opaque = rgba[..., 3] >= 128
    has_clear = not opaque.all()
    if has_clear:
        key = np.where(opaque, key, -1)
    keys, lab = np.unique(key, return_inverse=True)
    palette = np.stack([(keys >> 16) & 255, (keys >> 8) & 255, keys & 255], axis=1).astype(np.int64)
    return lab.reshape(key.shape).astype(np.int32), palette, (0 if has_clear else None)


def _merge_near_duplicates(lab: np.ndarray, palette: np.ndarray, transparent: int | None,
                           tol: int) -> tuple[np.ndarray, np.ndarray]:
    """Give each palette colour within `tol` (every channel) of a larger colour that colour's label."""
    if tol <= 0:
        return lab, palette
    areas = np.bincount(lab.ravel(), minlength=len(palette))
    target = np.arange(len(palette))
    reps: list[int] = []
    for c in sorted(range(len(palette)), key=lambda c: (-int(areas[c]), c)):
        if c == transparent:
            continue
        close = [r for r in reps if np.abs(palette[c] - palette[r]).max() <= tol]
        if close:
            target[c] = min(close, key=lambda r: (int(((palette[c] - palette[r]) ** 2).sum()), r))
        else:
            reps.append(c)
    if (target == np.arange(len(palette))).all():
        return lab, palette
    return target[lab].astype(lab.dtype), palette


def _components(lab: np.ndarray) -> tuple[np.ndarray, int, np.ndarray]:
    """4-connected regions of equal label: (region id per pixel, region count, label of each region)."""
    h, w = lab.shape
    start = np.ones((h, w), bool)
    start[:, 1:] = lab[:, 1:] != lab[:, :-1]
    run_id = np.cumsum(start.ravel(), dtype=np.int64).reshape(h, w) - 1
    link = (start[1:] | start[:-1]) & (lab[1:] == lab[:-1])  # one link per overlapping equal-label run pair
    parent = _union_find(int(run_id[-1, -1]) + 1, run_id[:-1][link], run_id[1:][link])
    roots, comp_of_run = np.unique(parent, return_inverse=True)
    colour_of = np.empty(len(roots), lab.dtype)
    colour_of[comp_of_run] = lab.ravel()[np.flatnonzero(start.ravel())]
    return comp_of_run.astype(np.int32)[run_id], len(roots), colour_of


def _union_find(n: int, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Root (smallest member) of every node's set after linking each (u, v) pair."""
    parent = np.arange(n, dtype=np.int64)
    while len(u):
        pu, pv = parent[u], parent[v]
        keep = pu != pv
        if not keep.any():
            break
        u, v, pu, pv = u[keep], v[keep], pu[keep], pv[keep]
        parent[np.maximum(pu, pv)] = np.minimum(pu, pv)  # colliding writes drop links; the loop retries them
        while True:
            grand = parent[parent]
            if np.array_equal(grand, parent):
                break
            parent = grand
    return parent


def _neighbour_pairs(comp: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Both orientations of every 4-neighbour pixel pair whose regions differ."""
    pairs_a, pairs_b = [], []
    for a, b in ((comp[:, :-1], comp[:, 1:]), (comp[:-1, :], comp[1:, :])):
        differ = a != b
        pairs_a += [a[differ], b[differ]]
        pairs_b += [b[differ], a[differ]]
    return np.concatenate(pairs_a), np.concatenate(pairs_b)


def _merge_speckles(lab: np.ndarray, palette: np.ndarray, transparent: int | None, area: int) -> np.ndarray:
    """Recolour opaque regions smaller than `area` px with the most similar neighbouring colour (ties: the
    longer shared border), preferring neighbours that are not speckles themselves."""
    if area <= 1:
        return lab
    n_colours = len(palette)
    dist = ((palette[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2)  # colour distance table
    for _ in range(SPECKLE_ROUNDS):
        comp, n, colour_of = _components(lab)
        sizes = np.bincount(comp.ravel(), minlength=n)
        small = sizes < area
        if transparent is not None:
            small &= colour_of != transparent
        if not small.any():
            break
        a, b = _neighbour_pairs(comp)
        keep = small[a]
        if transparent is not None:
            keep &= colour_of[b] != transparent
        a, b = a[keep], b[keep]
        index = np.full(n, -1, np.int64)
        index[small] = np.arange(int(small.sum()))
        target = colour_of.astype(np.int64)
        decided = np.zeros(n, bool)
        for stable_only in (True, False):
            sel = (~small[b] if stable_only else np.ones(len(b), bool)) & ~decided[a]
            if not sel.any():
                continue
            counts = np.zeros((int(small.sum()), n_colours), np.int64)
            np.add.at(counts, (index[a[sel]], colour_of[b[sel]]), 1)
            has = counts.sum(axis=1) > 0
            ids = np.flatnonzero(small)[has]
            score = dist[colour_of[ids]] * (counts.max() + 1) - counts[has]
            score = np.where(counts[has] > 0, score, np.iinfo(np.int64).max)
            target[ids] = score.argmin(axis=1)
            decided[ids] = True
        new = target[comp].astype(lab.dtype)
        if np.array_equal(new, lab):
            break
        lab = new
    return lab


def _dilate(mask: np.ndarray) -> np.ndarray:
    """3x3 binary dilation."""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    wide = out.copy()
    out[:, 1:] |= wide[:, :-1]
    out[:, :-1] |= wide[:, 1:]
    return out


def _adjacency(comp: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Unique undirected region adjacency edges (u < v)."""
    keys = []
    for a, b in ((comp[:, :-1], comp[:, 1:]), (comp[:-1, :], comp[1:, :])):
        differ = a != b
        lo = np.minimum(a[differ], b[differ]).astype(np.int64)
        hi = np.maximum(a[differ], b[differ]).astype(np.int64)
        keys.append(lo * n + hi)
    key = np.unique(np.concatenate(keys))
    return key // n, key % n


def _fill_under_mask(comp: np.ndarray, node_rank: np.ndarray, edges: tuple[np.ndarray, np.ndarray],
                     k: int) -> np.ndarray:
    """Pixels of every connected region of colours ranked >= k that contains colour k."""
    u, v = edges
    active = node_rank >= k
    both = active[u] & active[v]
    roots = _union_find(len(node_rank), u[both], v[both])
    chosen = active & np.isin(roots, roots[node_rank == k])
    return chosen[comp]


# ------------------------------------------------------------------------------------------ tracing


_PATH_TAG = re.compile(r"<path\b([^>]*?)/?>", re.S)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_TRANSLATE = re.compile(r"^\s*translate\(\s*([-+.\deE]+)(?:[\s,]+([-+.\deE]+))?\s*\)\s*$")
_POLYGON_DATA = re.compile(r"^[MLZ\d.,\s+\-eE]*$")
_TO_SPACES = str.maketrans({"M": " ", "L": " ", "Z": " ", ",": " "})
_VTRACER_PATH = re.compile(  # vtracer's own layout: d, fill, transform in this order
    r'<path d="([^"]*)" fill="#[0-9A-Fa-f]{6}" transform="translate\(([-+.\deE]+),([-+.\deE]+)\)"\s*/>')


def _empty_polygons() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return np.zeros(0), np.zeros(0), np.zeros(1, np.int64)


def _trace_mask(mask: np.ndarray, layer: int, cuts: list[Cut]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Exact pixel outlines of a boolean mask via vtracer binary mode. The mask is cut at empty rows and
    columns into independent pieces (no 4-connected region crosses an empty line), so a sparse mask is
    traced on small canvases instead of its whole bounding box. Pieces too dense for one vtracer call are
    halved; those cut lines are appended to `cuts`."""
    pieces = []
    for box in _xy_cut(mask):
        for y0, y1, x0, x1 in _split_dense(mask, box, layer, cuts):
            buf = io.BytesIO()  # a 1-bit PNG: black (red < 128) is vtracer's binary foreground
            Image.fromarray(~mask[y0:y1, x0:x1]).save(buf, format="PNG", compress_level=1)
            try:
                svg = vtracer.convert_raw_image_to_svg(buf.getvalue(), img_format="png", colormode="binary",
                                                       mode="none", filter_speckle=0, path_precision=3)
            except BaseException as exc:  # a Rust panic arrives as pyo3's PanicException, not an Exception
                if type(exc).__name__ != "PanicException":
                    raise
                raise ValueError(TOO_FINE) from exc
            pieces.append(parse_vtracer_polygons(svg, x0, y0))
    return _concat_polygons(pieces)


def _split_dense(mask: np.ndarray, box: tuple[int, int, int, int], layer: int,
                 cuts: list[Cut]) -> list[tuple[int, int, int, int]]:
    """Halve `box` (y0, y1, x0, x1) across its longer side until vtracer can label every piece."""
    out, todo = [], [box]
    while todo:
        y0, y1, x0, x1 = todo.pop()
        if _cluster_bound(mask[y0:y1, x0:x1]) < VTRACER_MAX_CLUSTERS:
            out.append((y0, y1, x0, x1))
        elif y1 - y0 >= x1 - x0:
            mid = (y0 + y1) // 2
            cuts.append((layer, "y", mid, x0, x1))
            todo += [(y0, mid, x0, x1), (mid, y1, x0, x1)]
        else:
            mid = (x0 + x1) // 2
            cuts.append((layer, "x", mid, y0, y1))
            todo += [(y0, y1, x0, mid), (y0, y1, mid, x1)]
    return sorted(out)


def _cluster_bound(piece: np.ndarray) -> int:
    """Upper bound of the cluster labels vtracer creates for a mask: it labels a 4-connected run that
    starts with nothing set on its left or above, and, for the holes of one cluster, the unset pixels
    closed off on the left and above plus the pixels along its box's top and left edges."""
    h, w = piece.shape
    if h * w + h + w < VTRACER_MAX_CLUSTERS:  # every label holds at least one pixel
        return 0
    left = np.zeros_like(piece)
    left[:, 1:] = piece[:, :-1]
    up = np.zeros_like(piece)
    up[1:] = piece[:-1]
    starts = np.count_nonzero(piece & ~left & ~up)
    closed = np.count_nonzero(~piece & left & up) + h + w
    return max(starts, closed)


def _xy_cut(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes (y0, y1, x0, x1) of the pieces of a mask separated by empty rows or columns, cut
    recursively while a cut saves at least a quarter of the box area."""
    out, todo = [], [(0, mask.shape[0], 0, mask.shape[1])]
    while todo:
        y0, y1, x0, x1 = todo.pop()
        sub = mask[y0:y1, x0:x1]
        rows = np.flatnonzero(sub.any(axis=1))
        if not len(rows):
            continue
        cols = np.flatnonzero(sub.any(axis=0))
        y0, y1, x0, x1 = y0 + int(rows[0]), y0 + int(rows[-1]) + 1, x0 + int(cols[0]), x0 + int(cols[-1]) + 1
        rows, cols = rows - rows[0], cols - cols[0]
        pieces = []
        for idx, lo, horizontal in ((rows, y0, True), (cols, x0, False)):
            gaps = np.flatnonzero(np.diff(idx) > 1)  # an empty line between idx[g] and idx[g + 1]
            if len(gaps):
                bounds = np.concatenate(([idx[0]], idx[gaps + 1]))
                ends = np.concatenate((idx[gaps] + 1, [idx[-1] + 1]))
                pieces = [(lo + int(a), lo + int(b), x0, x1) if horizontal else (y0, y1, lo + int(a), lo + int(b))
                          for a, b in zip(bounds, ends)]
                break
        area = (y1 - y0) * (x1 - x0)
        if pieces and sum((b - a) * (d - c) for a, b, c, d in pieces) <= 0.75 * area:
            todo.extend(pieces)
        else:
            out.append((y0, y1, x0, x1))
    return sorted(out)


def _concat_polygons(pieces: list[tuple[np.ndarray, np.ndarray, np.ndarray]]):
    if not pieces:
        return _empty_polygons()
    counts = np.concatenate([np.diff(off) for _, _, off in pieces])
    return (np.concatenate([x for x, _, _ in pieces]), np.concatenate([y for _, y, _ in pieces]),
            np.concatenate(([0], np.cumsum(counts))).astype(np.int64))


def parse_vtracer_polygons(svg: str, dx: float = 0.0, dy: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Closed polygons of a vtracer SVG written with absolute M/L/Z and a `translate` per <path>, shifted
    by (dx, dy), concatenated: (x, y, offsets). Anything else (curves, relative commands, other transforms)
    raises instead of being misread. A repeated closing vertex is dropped."""
    paths = _VTRACER_PATH.findall(svg)
    if len(paths) != svg.count("<path"):  # not vtracer's exact layout: read every tag on its own
        paths = [_path_attributes(m.group(1)) for m in _PATH_TAG.finditer(svg)]
    if not paths:
        return _empty_polygons()
    data = " ".join(d for d, _, _ in paths)
    if not _POLYGON_DATA.match(data):
        raise ValueError("vtracer path data is not absolute M/L/Z")
    sizes, owner = [], []
    for i, (d, _, _) in enumerate(paths):
        head, *parts = d.split("M")
        if head.strip() or any(part.count("Z") > 1 for part in parts):
            raise ValueError("malformed vtracer path data")
        sizes.extend(part.count(",") for part in parts)  # every point is written "x,y"
        owner.extend([i] * len(parts))
    vals = np.array(data.translate(_TO_SPACES).split(), float)
    size = np.array(sizes, np.int64)
    if len(vals) != 2 * int(size.sum()):
        raise ValueError("vtracer path data does not match its point count")
    pts = vals.reshape(-1, 2)
    shift = np.array([(float(tx), float(ty)) for _, tx, ty in paths]) + (dx, dy)
    pts += np.repeat(shift[np.array(owner, np.int64)], size, axis=0)
    ends = np.cumsum(size)
    starts = ends - size
    closing = (size > 1) & np.all(pts[np.maximum(ends - 1, 0)] == pts[np.minimum(starts, max(len(pts) - 1, 0))], axis=1)
    keep = np.ones(len(pts), bool)
    keep[(ends - 1)[closing]] = False
    counts = size - closing
    return pts[keep, 0], pts[keep, 1], np.concatenate(([0], np.cumsum(counts))).astype(np.int64)


def _path_attributes(tag_body: str) -> tuple[str, str, str]:
    attrs = dict(_ATTR.findall(tag_body))
    tx = ty = "0"
    if "transform" in attrs:
        m = _TRANSLATE.match(attrs["transform"])
        if not m:
            raise ValueError(f"unsupported transform {attrs['transform']!r}")
        tx, ty = m.group(1), m.group(2) or "0"
    return attrs.get("d", ""), tx, ty


def _rectangle(width: int, height: int) -> Subpath:
    w, h = float(width), float(height)
    return Subpath(start=(0.0, 0.0), segments=[("L", (w, 0.0)), ("L", (w, h)), ("L", (0.0, h))])


# ------------------------------------------------------------------------------------- polygon geometry


def _cyclic(off: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(polygon id, next vertex, previous vertex) for every vertex of concatenated closed polygons."""
    counts = np.diff(off)
    pid = np.repeat(np.arange(len(counts)), counts)
    idx = np.arange(int(off[-1]))
    nxt, prv = idx + 1, idx - 1
    nxt[off[1:] - 1] = off[:-1]
    prv[off[:-1]] = off[1:] - 1
    return pid, nxt, prv


def _turns(x, y, nxt, prv):
    """Signed turn (radians) at every vertex plus the incoming and outgoing edge vectors."""
    dxi, dyi = x - x[prv], y - y[prv]
    dxo, dyo = x[nxt] - x, y[nxt] - y
    return np.arctan2(dxi * dyo - dyi * dxo, dxi * dxo + dyi * dyo), dxi, dyi, dxo, dyo


def _subset(x, y, off, keep, *extra):
    """Keep the selected vertices; offsets are recomputed, polygons may become empty."""
    pid = np.repeat(np.arange(len(off) - 1), np.diff(off))
    counts = np.bincount(pid[keep], minlength=len(off) - 1)
    noff = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
    return (x[keep], y[keep], noff) + tuple(e[keep] for e in extra)


def staircase_midline(x, y, off, step_max: float, edge_keep: float, fixed: np.ndarray):
    """Replace every staircase step (an edge <= step_max px whose two end turns have opposite signs) by its
    midpoint and drop the vertices touching it, so a digital straight line (long runs joined by steps that
    all go the same way) becomes the line through the step midpoints. Only a plateau keeps its ends: an
    edge >= edge_keep px between two steps going opposite ways (a one-pixel bump or dent in a straight edge)
    stays exactly in place. `fixed` vertices (canvas edge, cut lines) are always kept and never belong to a
    step. Returns (x, y, off, soft); soft marks the kept vertices next to a step, which are never corners on
    their own turn (that turn is the pixel jog, not the design)."""
    pid, nxt, prv = _cyclic(off)
    turn = _turns(x, y, nxt, prv)[0]
    sign = np.sign(turn)
    dx, dy = x[nxt] - x, y[nxt] - y
    length = np.hypot(dx, dy)
    step = (length <= step_max + 1e-9) & (sign * sign[nxt] < 0) & ~fixed & ~fixed[nxt]
    plateau = (length >= edge_keep) & step[prv] & step[nxt] & (dx[prv] * dx[nxt] + dy[prv] * dy[nxt] < 0)
    near_step = step | step[prv]
    keep_v = fixed | ~near_step | (step & plateau[prv]) | (step[prv] & plateau)
    emit = np.stack([keep_v, step], 1).ravel()  # vertex i, then the midpoint of edge i
    ex = np.stack([x, (x + x[nxt]) / 2], 1).ravel()[emit]
    ey = np.stack([y, (y + y[nxt]) / 2], 1).ravel()[emit]
    soft = np.stack([keep_v & near_step & ~fixed, np.zeros_like(step)], 1).ravel()[emit]
    counts = np.bincount(np.repeat(pid, 2)[emit], minlength=len(off) - 1)
    return ex, ey, np.concatenate(([0], np.cumsum(counts))).astype(np.int64), soft


def decimate(x, y, off, delta: float, protect_deg: float, passes: int, fixed: np.ndarray, soft: np.ndarray):
    """Drop midline vertices that lie within `delta` px of the chord between their neighbours (never a
    `fixed` one or one turning >= protect_deg, never two neighbours in one pass, never below four vertices).
    Each pass moves the outline by at most `delta`, so `passes * delta` bounds the change. `soft` flags
    follow their vertices."""
    for _ in range(passes):
        if not len(x):
            break
        pid, nxt, prv = _cyclic(off)
        turn = _turns(x, y, nxt, prv)[0]
        cx, cy = x[nxt] - x[prv], y[nxt] - y[prv]
        cl = np.hypot(cx, cy)
        dev = np.abs((x - x[prv]) * cy - (y - y[prv]) * cx) / np.maximum(cl, 1e-12)
        dev = np.where(cl > 1e-9, dev, np.hypot(x - x[prv], y - y[prv]))
        dev += ((np.arange(len(x), dtype=np.uint64) * np.uint64(2654435761)) % np.uint64(1000003)) * 1e-13
        cand = (dev < delta) & (np.abs(turn) < np.radians(protect_deg)) & (np.diff(off)[pid] > 4) & ~fixed
        remove = cand & ~(cand[prv] & (dev[prv] < dev)) & ~(cand[nxt] & (dev[nxt] < dev))
        if not remove.any():
            break
        x, y, off, fixed, soft = _subset(x, y, off, ~remove, fixed, soft)
    return x, y, off, soft


def _arclength(x, y, off, pid, nxt):
    seg = np.hypot(x[nxt] - x, y[nxt] - y)
    total = np.concatenate(([0.0], np.cumsum(seg)))
    s = total[:-1] - total[off[:-1]][pid]  # arc length of each vertex from its polygon start
    per = total[off[1:]] - total[off[:-1]]
    return s, per, seg, total


def _point_at(x, y, off, pid, per, seg, total, q):
    """Point at arc length q (wrapped cyclically) along each vertex's own polygon."""
    target = total[off[:-1]][pid] + np.mod(q, np.maximum(per[pid], 1e-12))
    k = np.clip(np.searchsorted(total, target, side="right") - 1, off[:-1][pid], off[1:][pid] - 1)
    t = np.clip((target - total[k]) / np.maximum(seg[k], 1e-12), 0.0, 1.0)
    nk = np.where(k + 1 < off[1:][pid], k + 1, off[:-1][pid])
    return x[k] + t * (x[nk] - x[k]), y[k] + t * (y[nk] - y[k])


def _walk(j, nxt, prv, k, forward):
    for _ in range(k):
        j = nxt[j] if forward else prv[j]
    return j


def find_corners(x, y, off, p: LayerParams, reach: np.ndarray, fixed: np.ndarray, soft: np.ndarray):
    """Corners and their one-sided tangents; rounded corners are sharpened (see module docstring).

    `reach` is how far (px) each vertex may move when sharpened; `fixed` vertices (canvas edge, cut lines)
    are corners that never move and are never dropped; `soft` vertices (next to a pixel jog) only become
    corners through the windowed test. Returns (x, y, off, is_corner,
    tangent_in (n, 2), tangent_out (n, 2)) with sharpened vertices moved and the vertices inside a sharpened
    corner's rounded zone removed."""
    n = len(x)
    pid, nxt, prv = _cyclic(off)
    turn, dxi, dyi, dxo, dyo = _turns(x, y, nxt, prv)
    exact = ((np.abs(turn) >= np.radians(p.vertex_corner_deg)) & ~soft) | fixed
    w = p.corner_window
    sharp = np.zeros(n, bool)
    drop = np.zeros(n, bool)
    keep = np.zeros(n, bool)
    ux = uy = vx = vy = ix = iy = ax = ay = cx = cy = np.zeros(n)
    phi = np.zeros(n)
    if w > 0 and n:
        s, per, seg, total = _arclength(x, y, off, pid, nxt)
        at = lambda q: _point_at(x, y, off, pid, per, seg, total, q)  # noqa: E731
        bx, by = at(s - w)
        fx, fy = at(s + w)
        b2x, b2y = at(s - 2 * w)
        f2x, f2y = at(s + 2 * w)
        bmx, bmy = at(s - 1.5 * w)
        fmx, fmy = at(s + 1.5 * w)
        ux, uy, vx, vy = x - bx, y - by, fx - x, fy - y
        phi = np.abs(np.arctan2(ux * vy - uy * vx, ux * vx + uy * vy))
        # a corner concentrates its turn: measured over chords twice as long it barely grows, while on a
        # curve it doubles (this keeps small round blobs from being cut into stars)
        u2x, u2y, v2x, v2y = x - b2x, y - b2y, f2x - x, f2y - y
        phi2 = np.abs(np.arctan2(u2x * v2y - u2y * v2x, u2x * v2x + u2y * v2y))
        cand = (per[pid] >= 6 * w) & (phi >= np.radians(p.corner_deg)) & (phi2 < CORNER_SPREAD * phi)
        idx = np.arange(n)
        keep = cand.copy()
        for k in range(1, 8):  # non-maximum suppression within +-w of arc length
            for forward in (True, False):
                j = _walk(idx, nxt, prv, k, forward)
                ds = np.abs(s[j] - s)
                ds = np.minimum(ds, per[pid] - ds)
                stronger = (phi[j] > phi) | ((phi[j] == phi) & (j < idx))
                keep &= ~((ds <= w) & stronger & cand[j])
        ax, ay, cx, cy = bx - b2x, by - b2y, f2x - fx, f2y - fy  # edge lines outside the rounded zone
        den = ax * cy - ay * cx
        with np.errstate(divide="ignore", invalid="ignore"):
            tpar = ((fx - b2x) * cy - (fy - b2y) * cx) / den
            ix, iy = b2x + tpar * ax, b2y + tpar * ay
        line_turn = np.abs(np.arctan2(den, ax * cx + ay * cy))
        dev_b = np.abs(ax * (bmy - b2y) - ay * (bmx - b2x)) / np.maximum(np.hypot(ax, ay), 1e-12)
        dev_f = np.abs(cx * (fmy - fy) - cy * (fmx - fx)) / np.maximum(np.hypot(cx, cy), 1e-12)
        with np.errstate(invalid="ignore"):
            sharp = (keep & ~fixed & np.isfinite(ix) & np.isfinite(iy) & (np.hypot(ix - x, iy - y) <= reach)
                     & (line_turn >= np.radians(p.corner_deg)) & (dev_b <= STRAIGHT_EPS) & (dev_f <= STRAIGHT_EPS))
        sh = np.flatnonzero(sharp)
        for k in range(1, 8):
            for forward in (True, False):
                j = _walk(sh, nxt, prv, k, forward)
                ds = np.abs(s[j] - s[sh])
                ds = np.minimum(ds, per[pid[sh]] - ds)
                drop[j[(ds < w) & ~sharp[j] & ~fixed[j]]] = True
        left = np.bincount(pid[~drop], minlength=len(off) - 1)
        tiny = (left < 3)[pid]  # such a polygon keeps its vertices and is not sharpened
        drop &= ~tiny
        sharp &= ~tiny
    corner = (sharp | exact | (keep & (phi >= np.radians(HARD_CORNER_DEG)))) & ~drop
    nx, ny = np.where(sharp, ix, x), np.where(sharp, iy, y)
    tin = np.stack([np.where(sharp, ax, np.where(exact, dxi, ux)), np.where(sharp, ay, np.where(exact, dyi, uy))], 1)
    tout = np.stack([np.where(sharp, cx, np.where(exact, dxo, vx)), np.where(sharp, cy, np.where(exact, dyo, vy))], 1)
    tin /= np.maximum(np.hypot(tin[:, 0], tin[:, 1]), 1e-12)[:, None]
    tout /= np.maximum(np.hypot(tout[:, 0], tout[:, 1]), 1e-12)[:, None]
    return _subset(nx, ny, off, ~drop, corner, tin, tout)


# ---------------------------------------------------------------------------------------------- fitting


def _ranges(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Flattened inclusive ranges [a_i, b_i]: (indices, range of each, start of each range)."""
    counts = b - a + 1
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    owner = np.repeat(np.arange(len(a)), counts)
    return a[owner] + (np.arange(int(counts.sum())) - starts[owner]), owner, starts


def _bezier(p0, p1, p2, p3, u):
    v = 1.0 - u
    return v * v * v * p0 + 3 * v * v * u * p1 + 3 * v * u * u * p2 + u * u * u * p3


def fit_cubics(dx, dy, dtol, a, b, t1x, t1y, t2x, t2y):
    """One cubic per data range [a_i, b_i] from point a to point b, leaving along unit t1 and arriving along
    unit t2: least-squares handle lengths (Schneider) on chord-length parameters. The error of a range is
    max(distance / dtol) over its points (<= 1 means within tolerance). Ranges whose error lands in
    (1, RETRY_FACTOR] get one Newton reparameterization and a refit, which often brings them under 1;
    clearly good or clearly bad fits skip it. Returns (c1x, c1y, c2x, c2y, error)."""
    c1x, c1y, c2x, c2y, err, u = _fit_once(dx, dy, dtol, a, b, t1x, t1y, t2x, t2y, None)
    retry = np.flatnonzero((err > 1.0) & (err <= RETRY_FACTOR))
    if len(retry):
        r = _fit_once(dx, dy, dtol, a[retry], b[retry], t1x[retry], t1y[retry], t2x[retry], t2y[retry],
                      (c1x[retry], c1y[retry], c2x[retry], c2y[retry]))
        better = r[4] < err[retry]
        sel = retry[better]
        c1x[sel], c1y[sel], c2x[sel], c2y[sel], err[sel] = (v[better] for v in r[:5])
    return c1x, c1y, c2x, c2y, err


def _fit_once(dx, dy, dtol, a, b, t1x, t1y, t2x, t2y, previous):
    """Chord-length fit, or (when `previous` control points are given) a Newton step on the parameters
    against that cubic followed by a refit."""
    idx, owner, starts = _ranges(a, b)
    px, py = dx[idx], dy[idx]
    step = np.zeros(len(idx))
    step[1:] = np.hypot(np.diff(px), np.diff(py))
    step[starts] = 0.0
    cum = np.cumsum(step)
    cum -= np.repeat(cum[starts], b - a + 1)
    u = cum / np.maximum(cum[starts + (b - a)], 1e-12)[owner]
    x0, y0, x3, y3 = dx[a], dy[a], dx[b], dy[b]
    if previous is not None:
        u = _reparameterize(px, py, u, owner, x0, y0, *previous, x3, y3)
    chord = np.hypot(x3 - x0, y3 - y0)
    l1, l2 = _handle_lengths(px, py, u, owner, starts, x0, y0, x3, y3, t1x, t1y, -t2x, -t2y, chord)
    c1x, c1y, c2x, c2y = x0 + l1 * t1x, y0 + l1 * t1y, x3 - l2 * t2x, y3 - l2 * t2y
    o = owner
    dist = np.hypot(_bezier(x0[o], c1x[o], c2x[o], x3[o], u) - px, _bezier(y0[o], c1y[o], c2y[o], y3[o], u) - py)
    return c1x, c1y, c2x, c2y, np.maximum.reduceat(dist / dtol[idx], starts), u


def _handle_lengths(px, py, u, o, starts, x0, y0, x3, y3, t1x, t1y, t2x, t2y, chord):
    v = 1.0 - u
    b1, b2 = 3 * v * v * u, 3 * v * u * u
    lo, hi = v * v * v + b1, b2 + u * u * u
    a1x, a1y, a2x, a2y = t1x[o] * b1, t1y[o] * b1, t2x[o] * b2, t2y[o] * b2
    rx = px - (x0[o] * lo + x3[o] * hi)
    ry = py - (y0[o] * lo + y3[o] * hi)
    red = np.add.reduceat
    c11, c12, c22 = red(a1x * a1x + a1y * a1y, starts), red(a1x * a2x + a1y * a2y, starts), red(a2x * a2x + a2y * a2y, starts)
    x1, x2 = red(rx * a1x + ry * a1y, starts), red(rx * a2x + ry * a2y, starts)
    det = c11 * c22 - c12 * c12
    ok = np.abs(det) > 1e-12
    safe = np.where(ok, det, 1.0)
    l1 = np.where(ok, (x1 * c22 - x2 * c12) / safe, 0.0)
    l2 = np.where(ok, (c11 * x2 - c12 * x1) / safe, 0.0)
    bad = ~ok | (l1 < 1e-6 * chord) | (l2 < 1e-6 * chord) | (l1 > 3 * chord) | (l2 > 3 * chord)
    return np.where(bad, chord / 3, l1), np.where(bad, chord / 3, l2)


def _reparameterize(px, py, u, o, x0, y0, c1x, c1y, c2x, c2y, x3, y3):  # noqa: PLR0913
    """One Newton-Raphson step of every parameter towards its point's nearest point on the cubic."""
    qx = (x0[o], c1x[o], c2x[o], x3[o])
    qy = (y0[o], c1y[o], c2y[o], y3[o])
    v = 1.0 - u
    ex, ey = _bezier(*qx, u) - px, _bezier(*qy, u) - py
    d1x = 3 * (v * v * (qx[1] - qx[0]) + 2 * v * u * (qx[2] - qx[1]) + u * u * (qx[3] - qx[2]))
    d1y = 3 * (v * v * (qy[1] - qy[0]) + 2 * v * u * (qy[2] - qy[1]) + u * u * (qy[3] - qy[2]))
    d2x = 6 * (v * (qx[2] - 2 * qx[1] + qx[0]) + u * (qx[3] - 2 * qx[2] + qx[1]))
    d2y = 6 * (v * (qy[2] - 2 * qy[1] + qy[0]) + u * (qy[3] - 2 * qy[2] + qy[1]))
    num = ex * d1x + ey * d1y
    den = d1x * d1x + d1y * d1y + ex * d2x + ey * d2y
    ok = np.abs(den) > 1e-12
    return np.clip(u - np.where(ok, num / np.where(ok, den, 1.0), 0.0), 0.0, 1.0)


def fit_closed(x, y, off, corner, tin, tout, tol: np.ndarray) -> list[tuple[tuple[float, float], list] | None]:
    """Fit every closed polygon with as few cubics as stay within `tol` px of it (one tolerance per vertex;
    see module docstring). Returns, per polygon, (start, segments) or None for an empty polygon."""
    n_poly = len(off) - 1
    counts = np.diff(off)
    if n_poly == 0 or not counts.any():
        return [None] * n_poly
    pid, nxt, prv = _cyclic(off)
    _, dxi, dyi, dxo, dyo = _turns(x, y, nxt, prv)
    # smooth vertices share one tangent: the length-weighted bisector of their two edges
    sx, sy = dxi + dxo, dyi + dyo
    ns = np.hypot(sx, sy)
    no = np.maximum(np.hypot(dxo, dyo), 1e-12)
    flat = ns < 1e-9
    sx = np.where(flat, dxo / no, sx / np.maximum(ns, 1e-12))
    sy = np.where(flat, dyo / no, sy / np.maximum(ns, 1e-12))
    tinx, tiny = np.where(corner, tin[:, 0], sx), np.where(corner, tin[:, 1], sy)
    toutx, touty = np.where(corner, tout[:, 0], sx), np.where(corner, tout[:, 1], sy)
    # each polygon as a sequence starting at its first corner (or vertex 0), closing vertex repeated
    first = np.full(n_poly, -1, np.int64)
    ci = np.flatnonzero(corner)
    if len(ci):
        lead = np.concatenate(([True], pid[ci][1:] != pid[ci][:-1]))  # first corner of each polygon
        first[pid[ci][lead]] = ci[lead]
    rot = np.where(first >= 0, first - off[:-1], 0)
    seq_counts = np.where(counts > 0, counts + 1, 0)
    seq_off = np.concatenate(([0], np.cumsum(seq_counts)))
    seq_pid = np.repeat(np.arange(n_poly), seq_counts)
    j = np.arange(int(seq_off[-1])) - seq_off[seq_pid]
    src = off[:-1][seq_pid] + (rot[seq_pid] + j) % counts[seq_pid]
    is_first, is_last = j == 0, j == counts[seq_pid]
    inner_corner = corner[src] & ~is_first & ~is_last  # duplicated: ends one run, starts the next
    rep = np.where(inner_corner, 2, 1)
    vsrc = np.repeat(src, rep)
    starts_run = np.repeat(is_first, rep)
    dup_at = np.flatnonzero(np.repeat(inner_corner, rep))
    starts_run[dup_at[1::2]] = True
    run = np.cumsum(starts_run) - 1
    run_pid = np.zeros(int(run[-1]) + 1, np.int64)
    run_pid[run] = np.repeat(seq_pid, rep)
    loop_run = first[run_pid] < 0  # the single run of a polygon without corners: it keeps >= 2 cubics
    vx, vy = x[vsrc], y[vsrc]
    # data: run vertices plus points every DENSIFY px along long edges
    has_next = np.zeros(len(vx), bool)
    has_next[:-1] = run[1:] == run[:-1]
    ex = np.where(has_next, np.roll(vx, -1) - vx, 0.0)
    ey = np.where(has_next, np.roll(vy, -1) - vy, 0.0)
    per = np.where(has_next, np.maximum(np.ceil(np.hypot(ex, ey) / DENSIFY), 1), 1).astype(np.int64)
    d_off = np.concatenate(([0], np.cumsum(per)))
    own = np.repeat(np.arange(len(vx)), per)
    f = (np.arange(int(d_off[-1])) - d_off[own]) / per[own]
    dx, dy = vx[own] + f * ex[own], vy[own] + f * ey[own]
    tv = tol[vsrc]
    tnext = np.where(has_next, np.roll(tv, -1), tv)
    dtol = np.where(f > 0, np.minimum(tv[own], tnext[own]), tv[own])  # tolerance of every data point
    vdata = d_off[:-1]
    # initial segments: one per run edge
    sva = np.flatnonzero(has_next)
    svb = sva + 1
    sa, sb, srun = vdata[sva], vdata[svb], run[sva]
    third = np.hypot(dx[sb] - dx[sa], dy[sb] - dy[sa]) / 3  # one edge: handles chord/3 along the tangents
    c1x, c1y = dx[sa] + third * toutx[vsrc[sva]], dy[sa] + third * touty[vsrc[sva]]
    c2x, c2y = dx[sb] - third * tinx[vsrc[svb]], dy[sb] - third * tiny[vsrc[svb]]
    # pairwise merge rounds; each pair's fit is cached until one of its two segments changes
    m = len(sa)
    jitter = ((np.arange(m, dtype=np.uint64) * np.uint64(2654435761)) % np.uint64(1000003)).astype(float) * 1e-15
    err = np.full(m, np.nan)
    m1x, m1y, m2x, m2y = np.zeros(m), np.zeros(m), np.zeros(m), np.zeros(m)
    for _ in range(MAX_ROUNDS):
        valid = np.zeros(len(sa), bool)
        valid[:-1] = srun[:-1] == srun[1:]
        valid &= ~(loop_run[srun] & (np.bincount(srun, minlength=len(loop_run))[srun] <= 2))
        need = np.flatnonzero(valid & np.isnan(err))
        if len(need):
            va, vb = vsrc[sva[need]], vsrc[svb[need + 1]]
            fitted = fit_cubics(dx, dy, dtol, sa[need], sb[need + 1], toutx[va], touty[va], tinx[vb], tiny[vb])
            m1x[need], m1y[need], m2x[need], m2y[need], err[need] = fitted
        # a pair merges when its error beats both overlapping pairs; equal errors (common on regular
        # staircases) are ordered by a fixed pseudo-random jitter, so long tied runs still merge every round
        e = np.where(valid & (err <= 1.0), err + jitter[: len(err)], np.inf)  # err is relative to the tolerance
        left = np.concatenate(([np.inf], e[:-1]))
        right = np.concatenate((e[1:], [np.inf]))
        acc = np.flatnonzero(np.isfinite(e) & (e < left) & (e < right))  # never two overlapping pairs
        if not len(acc):
            break
        sb[acc], svb[acc] = sb[acc + 1], svb[acc + 1]
        c1x[acc], c1y[acc], c2x[acc], c2y[acc] = m1x[acc], m1y[acc], m2x[acc], m2y[acc]
        err[acc] = np.nan
        err[acc[acc > 0] - 1] = np.nan
        keep = np.ones(len(sa), bool)
        keep[acc + 1] = False
        sa, sb, sva, svb, srun = sa[keep], sb[keep], sva[keep], svb[keep], srun[keep]
        c1x, c1y, c2x, c2y = c1x[keep], c1y[keep], c2x[keep], c2y[keep]
        err, m1x, m1y, m2x, m2y = err[keep], m1x[keep], m1y[keep], m2x[keep], m2y[keep]
    # output: straight cubics become lines
    x0, y0, x3, y3 = dx[sa], dy[sa], dx[sb], dy[sb]
    chx, chy = x3 - x0, y3 - y0
    cl = np.maximum(np.hypot(chx, chy), 1e-12)
    straight = ((np.abs(chx * (c1y - y0) - chy * (c1x - x0)) / cl <= LINE_EPS)
                & (np.abs(chx * (c2y - y0) - chy * (c2x - x0)) / cl <= LINE_EPS))
    seg_pid = run_pid[srun]
    bounds = np.searchsorted(seg_pid, np.arange(n_poly + 1))
    vals = np.stack([c1x, c1y, c2x, c2y, x3, y3], 1).tolist()
    line = straight.tolist()
    starts = np.stack([x0, y0], 1).tolist()
    out: list = [None] * n_poly
    for poly in range(n_poly):
        s0, s1 = int(bounds[poly]), int(bounds[poly + 1])
        if s1 > s0:
            segs = [("L", (q[4], q[5])) if line[s] else ("C", (q[0], q[1]), (q[2], q[3]), (q[4], q[5]))
                    for s, q in zip(range(s0, s1), vals[s0:s1])]
            out[poly] = (tuple(starts[s0]), segs)
    return out


def _fit_layers(traced: list[tuple[np.ndarray, np.ndarray, np.ndarray]], params: LayerParams,
                near_clear: np.ndarray | None, width: int, height: int, cuts: list[Cut]) -> tuple[list, list[int]]:
    """Midline, corners and curve fit for the outlines of every layer at once. Outline points next to
    transparency use ALPHA_TOL; vertices on the canvas edge or on a layer's own cut lines stay exactly
    there (they are corners). Returns the fitted subpaths as (start, segments) and the layer of each."""
    xs = [t[0] for t in traced]
    ys = [t[1] for t in traced]
    counts = [np.diff(t[2]) for t in traced]
    owner = np.concatenate([np.full(len(c), k) for k, c in enumerate(counts)] or [np.zeros(0, np.int64)])
    counts_all = np.concatenate(counts) if counts else np.zeros(0, np.int64)
    if not counts_all.sum():
        return [], []
    x, y = np.concatenate(xs), np.concatenate(ys)
    off = np.concatenate(([0], np.cumsum(counts_all))).astype(np.int64)
    x, y, off, owner = _compact(x, y, off, owner)

    def pinned(x, y, off, owner):
        fixed = (x <= 0) | (x >= width) | (y <= 0) | (y >= height)
        if cuts:
            layer = np.repeat(owner, np.diff(off))
            for k, axis, at, lo, hi in cuts:
                along, across = (x, y) if axis == "x" else (y, x)
                fixed |= (layer == k) & (along == at) & (across >= lo) & (across <= hi)
        return fixed

    x, y, off, soft = staircase_midline(x, y, off, params.step_max, params.edge_keep, pinned(x, y, off, owner))
    x, y, off, owner, soft = _compact(x, y, off, owner, soft)
    if params.decimate_passes:  # never next to transparency, where the outline has no slack left
        x, y, off, soft = decimate(x, y, off, DECIMATE_DELTA, params.vertex_corner_deg, params.decimate_passes,
                                   pinned(x, y, off, owner) | _near(x, y, near_clear), soft)
    reach = _per_vertex(x, y, near_clear, params.corner_dmax, min(params.corner_dmax, ALPHA_REACH))
    x, y, off, corner, tin, tout = find_corners(x, y, off, params, reach, pinned(x, y, off, owner), soft)
    x, y, off, owner, corner, tin, tout = _compact(x, y, off, owner, corner, tin, tout)
    tol = _per_vertex(x, y, near_clear, params.fit_tol, min(params.fit_tol, ALPHA_TOL))
    fitted = fit_closed(x, y, off, corner, tin, tout, tol)
    pieces, layer_of = [], []
    for piece, k in zip(fitted, owner.tolist()):
        if piece is not None and len(piece[1]) >= 2:
            pieces.append(piece)
            layer_of.append(k)
    return pieces, layer_of


def _per_vertex(x, y, near_clear: np.ndarray | None, value: float, near_value: float) -> np.ndarray:
    """`near_value` for outline points within a pixel of transparency, `value` elsewhere."""
    if near_clear is None or near_value >= value:
        return np.full(len(x), value)
    return np.where(_near(x, y, near_clear), near_value, value)


def _near(x, y, near_clear: np.ndarray | None) -> np.ndarray:
    """Outline points within a pixel of transparency."""
    if near_clear is None:
        return np.zeros(len(x), bool)
    h, w = near_clear.shape
    return near_clear[np.clip(np.floor(y).astype(np.int64), 0, h - 1), np.clip(np.floor(x).astype(np.int64), 0, w - 1)]


def _compact(x, y, off, owner, *extra):
    """Drop polygons with fewer than three vertices or no area (and their vertices); keep the owners."""
    counts = np.diff(off)
    pid = np.repeat(np.arange(len(counts)), counts)
    area2 = np.zeros(len(counts))
    if len(x):
        nxt = np.arange(len(x)) + 1
        nonempty = counts > 0
        nxt[off[1:][nonempty] - 1] = off[:-1][nonempty]
        area2 = np.bincount(pid, weights=x * y[nxt] - x[nxt] * y, minlength=len(counts))
    good = (counts >= 3) & (np.abs(area2) > 1e-9)
    if good.all():
        return (x, y, off, owner) + extra
    sel = good[pid]
    noff = np.concatenate(([0], np.cumsum(counts[good]))).astype(np.int64)
    return (x[sel], y[sel], noff, owner[good]) + tuple(e[sel] for e in extra)
