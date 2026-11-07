"""Prompt builders: pure functions that turn options (+ design brief) into system/user prompts.

The wording lives in app/prompts/templates/*.j2 so the factory can tune it
without touching code. Identical inputs always produce identical prompts.
Client/staff text is untrusted: it is cleaned and wrapped in tags that the
system prompts declare to be design description only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.prompts import descriptors
from app.prompts.types import PromptPair
from app.schemas import (
    DesignBrief,
    DesignOptions,
    DesignView,
    RecreateMode,
    RecreateOptions,
    RenderStyle,
    Visibility,
    normalize_hex,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# The production prompt set is kept in a private repository, checked out at <project>/prompts-private. Its templates
# take precedence over the structural placeholders in TEMPLATES_DIR (the public repository ships only those).
PRIVATE_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "prompts-private" / "templates"
MAX_MESSAGE_CHARS = 4000
MAX_NOTES_CHARS = 2000
MAX_FIELD_CHARS = 600

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏  ‪-‮⁦-⁩]")
_OUR_TAGS = re.compile(r"<\s*/?\s*(client_request|staff_notes|previous_brief)\b[^>]*>", re.IGNORECASE)

_env = Environment(
    loader=FileSystemLoader([str(d) for d in (PRIVATE_TEMPLATES_DIR, TEMPLATES_DIR) if d.is_dir()]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    keep_trailing_newline=False,
)

REMOVALS = (
    ("remove_logos", "logos, crests, badges and kit-maker marks"),
    ("remove_sponsors", "sponsor names and brand marks"),
    ("remove_names", "player names and team names"),
    ("remove_numbers", "player numbers (front, back and sleeves)"),
    ("remove_other_text", "any other text, lettering or slogans"),
)

VIEW_PHRASES = {
    DesignView.FRONT: "front view only",
    DesignView.BACK: "back view only",
    DesignView.FRONT_BACK: "front and back views side by side",
}

STYLE_LABELS = {
    RenderStyle.FLAT: "flat 2D production artwork (technical flat)",
    RenderStyle.MOCKUP: "photorealistic studio product photo on an invisible mannequin",
    RenderStyle.PATTERN: "flat full-bleed print pattern only",
}

LETTERING_LABELS = {
    "team_name": "Team name",
    "player_name": "Player name",
    "number": "Number",
    "sponsor": "Sponsor text",
    "slogan": "Slogan",
    "other": "Text",
}

DEFAULT_PLACEMENTS = {
    "team_name": "across the centre of the front chest",
    "player_name": "across the upper back, above the number",
    "number": "large in the centre of the back",
}
DEFAULT_LETTERING_STYLE = "bold athletic block lettering in a high-contrast palette colour"


def clean_text(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """Untrusted text made safe to embed: no control characters, no fake delimiter tags, tidy whitespace."""
    text = _CONTROL_CHARS.sub(" ", text or "")
    text = _OUR_TAGS.sub(lambda m: m.group(0).replace("<", "(").replace(">", ")"), text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit].rstrip()


def _quote_safe(text: str) -> str:
    return text.replace('"', "'")


def _clean_tree(value):
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [_clean_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean_tree(v) for k, v in value.items()}
    return value


def _brief_view(brief: DesignBrief | None) -> dict | None:
    if brief is None:
        return None
    data = _clean_tree(brief.model_dump(mode="json"))
    for colour in data["palette"]:
        colour["hex"] = normalize_hex(colour["hex"]) or ""
    return data


def _render(template: str, **context) -> str:
    text = _env.get_template(template).render(**context)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _intended_use(opts: DesignOptions, sport: str) -> str:
    if opts.render_style == RenderStyle.MOCKUP:
        return f"photorealistic product photograph of a custom sublimated {sport} jersey for the client to approve."
    if opts.render_style == RenderStyle.PATTERN:
        return f"print-ready sublimation pattern for the body of a custom {sport} jersey."
    return (
        f"flat production artwork (technical flat) of a custom sublimated {sport} jersey: "
        "the design proof the client approves and the factory prints from."
    )


def _lettering(opts: DesignOptions, brief: dict | None) -> list[str]:
    """Lettering lines in fixed order: explicit form fields first, then the brief's own items."""
    if opts.text_policy == "none" or opts.render_style == RenderStyle.PATTERN:
        return []
    brief_items = brief["text_elements"] if brief else []
    explicit = {
        "team_name": _quote_safe(clean_text(opts.team_name, 60)),
        "player_name": _quote_safe(clean_text(opts.player_name, 40)),
        "number": _quote_safe(clean_text(opts.player_number, 4)),
    }
    items: list[str] = []
    for kind, value in explicit.items():
        if not value:
            continue
        match = next((t for t in brief_items if t["kind"] == kind), None)
        placement = (match and match["placement"]) or DEFAULT_PLACEMENTS[kind]
        style = (match and match["style"]) or DEFAULT_LETTERING_STYLE
        items.append(f'{LETTERING_LABELS[kind]} "{value}": {placement}; {style}')
    for t in brief_items:
        content = _quote_safe(t["content"])
        if not content or explicit.get(t["kind"]):
            continue
        items.append(f'{LETTERING_LABELS[t["kind"]]} "{content}": {t["placement"]}; {t["style"]}')
    return items


def _design_context(
    opts: DesignOptions, *, message: str, brief: DesignBrief | None, aspect_ratio: str, inspiration_count: int
) -> dict:
    sport = clean_text(opts.sport, 60) or "football (soccer)"
    view = _brief_view(brief)
    lettering = _lettering(opts, view)
    return {
        "intended_use": _intended_use(opts, sport),
        "sport": sport,
        "collar": clean_text(opts.collar, 120),
        "sleeves": clean_text(opts.sleeves, 120),
        "view": opts.view.value,
        "style": opts.render_style.value,
        "background": opts.background,
        "text_policy": opts.text_policy,
        "lettering": lettering,
        "lettering_allowed": opts.text_policy != "none" and opts.render_style != RenderStyle.PATTERN,
        "colors": list(opts.colors),
        "brief": view,
        "message": clean_text(message, MAX_MESSAGE_CHARS),
        "inspiration_count": inspiration_count,
        "descriptor": descriptors.design_line(opts, view, aspect_ratio, has_lettering=bool(lettering)),
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def build_recreate_prompt(opts: RecreateOptions, *, aspect_ratio: str) -> PromptPair:
    if opts.mode == RecreateMode.PANELS:  # a panel job sends one prompt per panel; the front one represents it
        return build_panel_prompt(opts, panel="front", aspect_ratio=aspect_ratio, has_front_reference=False)
    removals = [label for flag, label in REMOVALS if getattr(opts, flag)]
    keeps = [label for flag, label in REMOVALS if not getattr(opts, flag)]
    user = _render(
        "recreate_user.j2",
        mode=opts.mode.value,
        removals=removals,
        keeps=keeps,
        fidelity=opts.fidelity,
        seamless=opts.seamless and opts.mode == RecreateMode.PATTERN,
        correct_colors=opts.correct_colors,
        background=opts.background,
        notes=clean_text(opts.notes, MAX_NOTES_CHARS),
        descriptor=descriptors.recreate_line(opts, aspect_ratio),
    )
    return PromptPair(system=_render("recreate_system.j2"), user=user)


def build_panel_prompt(
    opts: RecreateOptions,
    *,
    panel: str,
    aspect_ratio: str,
    has_front_reference: bool,
    visibility: Visibility | None = None,
    back_picture: bool = False,
    sleeve_back_picture: bool = False,
    anchor: str = "front",
    sleeve_picture: bool = False,
) -> PromptPair:
    """Prompt for one production panel (a key of descriptors.PANEL_NAMES), a full-bleed rectangle.

    `visibility` is what the AI reference check found for this part; None keeps the generic wording.
    `back_picture`: the back panel is drawn from the client's own back picture, sent as Image 1.
    `sleeve_back_picture`: the sleeve also gets that back picture, as the image after the front panel.
    `sleeve_picture`: Image 1 is the client's own picture of the sleeve (its box), not of the whole jersey.
    `has_front_reference`: Image 2 is a finished body panel to match: the front, or (`anchor` "back", a job without a
    front) the back.
    """
    user = _render(
        "panel_user.j2",
        panel=panel,
        panel_name=descriptors.PANEL_NAMES[panel],
        print_size=descriptors.PANEL_PRINT_IN[panel],
        anchor_name=f"{anchor} panel",
        anchor_size=descriptors.PANEL_PRINT_IN[anchor],
        visibility=visibility,
        back_picture=back_picture,
        sleeve_back_picture=sleeve_back_picture,
        sleeve_picture=sleeve_picture,
        has_front_reference=has_front_reference,
        removals=[label for flag, label in REMOVALS if getattr(opts, flag)],
        keeps=[label for flag, label in REMOVALS if not getattr(opts, flag)],
        fidelity=opts.fidelity,
        correct_colors=opts.correct_colors,
        notes=clean_text(opts.notes, MAX_NOTES_CHARS),
        descriptor=descriptors.panel_line(
            opts, panel, aspect_ratio, visibility, has_front_reference=has_front_reference, back_picture=back_picture,
            anchor=anchor,
        ),
    )
    return PromptPair(system=_render("recreate_system.j2"), user=user)


def build_reference_check_prompt(opts: RecreateOptions) -> PromptPair:
    """Prompt for the AI reference check of a panel job: how much of each garment part the reference shows."""
    user = _render("reference_check_user.j2", notes=clean_text(opts.notes, MAX_NOTES_CHARS))
    return PromptPair(system=_render("reference_check_system.j2"), user=user)


def build_design_prompt(
    opts: DesignOptions, *, message: str, brief: DesignBrief | None, aspect_ratio: str, inspiration_count: int
) -> PromptPair:
    context = _design_context(
        opts, message=message, brief=brief, aspect_ratio=aspect_ratio, inspiration_count=inspiration_count
    )
    return PromptPair(system=_render("design_system.j2"), user=_render("design_user.j2", **context))


def build_refine_prompt(
    opts: DesignOptions,
    *,
    message: str,
    brief: DesignBrief | None,
    changes: list[str],
    aspect_ratio: str,
    inspiration_count: int,
) -> PromptPair:
    context = _design_context(
        opts, message=message, brief=brief, aspect_ratio=aspect_ratio, inspiration_count=inspiration_count
    )
    context["changes"] = [c for c in (clean_text(c) for c in changes) if c]
    return PromptPair(system=_render("design_system.j2"), user=_render("refine_user.j2", **context))


def build_brief_prompt(
    opts: DesignOptions,
    *,
    message: str,
    previous_brief: DesignBrief | None,
    has_current_design: bool,
    inspiration_count: int,
) -> PromptPair:
    previous = ""
    if previous_brief is not None:
        previous = json.dumps(_brief_view(previous_brief), indent=2, ensure_ascii=False)
    user = _render(
        "brief_user.j2",
        sport=clean_text(opts.sport, 60) or "football (soccer)",
        collar=clean_text(opts.collar, 120),
        sleeves=clean_text(opts.sleeves, 120),
        view_phrase=VIEW_PHRASES[opts.view],
        style_label=STYLE_LABELS[opts.render_style],
        text_policy=opts.text_policy,
        team_name=_quote_safe(clean_text(opts.team_name, 60)),
        player_name=_quote_safe(clean_text(opts.player_name, 40)),
        player_number=_quote_safe(clean_text(opts.player_number, 4)),
        colors=list(opts.colors),
        has_current_design=has_current_design,
        inspiration_count=inspiration_count,
        previous_brief=previous,
        message=clean_text(message, MAX_MESSAGE_CHARS),
    )
    return PromptPair(system=_render("brief_system.j2"), user=user)
