"""Descriptor lines: the dense one-line "IMAGE GENERATION PROMPT" that closes every image prompt.

This public version is a structural placeholder with the same interface. The production descriptor vocabulary lives
in the private prompt set (prompts-private/descriptors.py) and replaces everything below when it is present.
"""

from __future__ import annotations

from pathlib import Path

from app.schemas import DesignOptions, RecreateOptions, Visibility

REMOVAL_TAGS = (
    ("remove_logos", "no logos"),
    ("remove_sponsors", "no sponsor marks"),
    ("remove_names", "no names"),
    ("remove_numbers", "no numbers"),
    ("remove_other_text", "no other text"),
)
PANEL_NAMES = {"front": "front body panel", "back": "back body panel", "sleeve": "sleeve panel"}
# Each panel's 3XL print size in inches (width, height); the image model's canvas uses the same ratio.
PANEL_PRINT_IN = {"front": (25, 34), "back": (25, 34), "sleeve": (24, 14)}
VISIBILITY_WORDS = {"full": "as shown in the reference", "partial": "visible part kept, rest completed", "none": "inferred"}


def _join(parts: list[str]) -> str:
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _removed(opts: RecreateOptions) -> list[str]:
    return [tag for flag, tag in REMOVAL_TAGS if getattr(opts, flag)]


def recreate_line(opts: RecreateOptions, aspect_ratio: str) -> str:
    return _join([f"{opts.mode.value.replace('_', ' ')} artwork recreated from the reference jersey", *_removed(opts), aspect_ratio])


def panel_line(
    opts: RecreateOptions,
    panel: str,
    aspect_ratio: str,
    visibility: Visibility | None = None,
    *,
    has_front_reference: bool = False,
    back_picture: bool = False,
    anchor: str = "front",
) -> str:
    width, height = PANEL_PRINT_IN[panel]
    return _join([
        f"full-bleed {PANEL_NAMES[panel]} print block", f"{width} x {height} in",
        VISIBILITY_WORDS[visibility] if visibility else "", f"matching the {anchor} panel" if has_front_reference else "",
        *_removed(opts), aspect_ratio,
    ])


def design_line(opts: DesignOptions, brief: dict | None, aspect_ratio: str, *, has_lettering: bool) -> str:
    return _join([f"{opts.render_style.value} design of a {opts.sport or 'sports'} jersey", aspect_ratio])


_PRIVATE = Path(__file__).resolve().parents[2] / "prompts-private" / "descriptors.py"
if _PRIVATE.is_file():  # the production vocabulary replaces the placeholders above
    exec(compile(_PRIVATE.read_text(encoding="utf-8"), str(_PRIVATE), "exec"), globals())
