"""Vector artwork model shared by the tracer and the SVG / EPS / PDF writers.

Coordinates are traced-image pixels: origin top-left, x to the right, y downwards.
`VectorArt.layers` are in paint order (first = bottom). Every layer is ONE colour and is
written as ONE compound path, so in Illustrator or CorelDRAW a whole colour is selected,
and recoloured, with a single click. Layers are stacked like Illustrator's "Overlapping"
image-trace method: a layer may extend underneath the layers painted after it, which
leaves no hairline gaps between colours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

Point = tuple[float, float]
Line = tuple[Literal["L"], Point]  # straight segment to the point
Cubic = tuple[Literal["C"], Point, Point, Point]  # control 1, control 2, end point
Segment = Union[Line, Cubic]


@dataclass
class Subpath:
    """One closed contour. Writers always close it back to `start`."""

    start: Point
    segments: list[Segment] = field(default_factory=list)

    @property
    def anchor_count(self) -> int:
        """Anchor points as Illustrator counts them (the closing point is not repeated)."""
        if not self.segments:
            return 1
        last = self.segments[-1][-1]
        closes_on_start = abs(last[0] - self.start[0]) < 1e-6 and abs(last[1] - self.start[1]) < 1e-6
        return len(self.segments) if closes_on_start else len(self.segments) + 1


@dataclass
class ColorLayer:
    rgb: tuple[int, int, int]
    subpaths: list[Subpath] = field(default_factory=list)
    area_px: int = 0  # pixels of this colour visible in the traced image (statistics only)

    @property
    def hex(self) -> str:
        return "#{:02X}{:02X}{:02X}".format(*self.rgb)

    @property
    def anchor_count(self) -> int:
        return sum(sp.anchor_count for sp in self.subpaths)


@dataclass
class VectorArt:
    width: int  # traced canvas size in pixels (SVG viewBox)
    height: int
    layers: list[ColorLayer] = field(default_factory=list)
    # Fill rule applied inside each layer's compound path. "evenodd" means nested contours are
    # holes regardless of their direction; "nonzero" requires holes to wind opposite to outlines.
    fill_rule: Literal["evenodd", "nonzero"] = "evenodd"

    @property
    def anchor_count(self) -> int:
        return sum(layer.anchor_count for layer in self.layers)

    @property
    def subpath_count(self) -> int:
        return sum(len(layer.subpaths) for layer in self.layers)
