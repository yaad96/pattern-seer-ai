"""Shared domain models: request options, the AI design brief, and job records.

Every other module imports its option/brief/job types from here, so field names
in this file are the contract between the API, the prompt builder, the
providers, the pipeline and the frontend.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared literals
# ---------------------------------------------------------------------------

# "auto" is resolved by the pipeline into one of the concrete ratios before a
# provider is called (from the input image for recreate jobs, from the view for
# design jobs).
AspectRatio = Literal["auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
CONCRETE_ASPECT_RATIOS: tuple[str, ...] = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")

Resolution = Literal["1K", "2K", "4K"]
ProviderName = Literal["openai", "mock"]
BriefWriterName = Literal["auto", "claude", "openai"]
Background = Literal["white", "transparent"]

HEX_RE = r"^#[0-9A-Fa-f]{6}$"
# production panels, in the order a panel job makes and lists them
PanelName = Literal["front", "back", "sleeve"]
PANEL_ORDER: tuple[str, ...] = ("front", "back", "sleeve")
MIN_CROP = 0.05  # the smallest front crop, as a fraction of the picture's width and height


def normalize_hex(value: str) -> str | None:
    """Return '#RRGGBB' (upper-case) for '#rgb', 'rrggbb', '#rrggbb' inputs, else None."""
    v = value.strip().lstrip("#")
    if len(v) == 3 and all(c in "0123456789abcdefABCDEF" for c in v):
        v = "".join(c * 2 for c in v)
    if len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v):
        return "#" + v.upper()
    return None


# ---------------------------------------------------------------------------
# Feature A: recreate a clean HD design from a low-res reference image
# ---------------------------------------------------------------------------


class RecreateMode(str, Enum):
    PATTERN = "pattern"  # flat, full-bleed print pattern/texture only (no garment, no mockup)
    FLAT_JERSEY = "flat_jersey"  # flat orthographic jersey artwork, garment silhouette kept
    REPLICA = "replica"  # same composition as the reference, rebuilt clean and sharp in HD
    PANELS = "panels"  # three production panels: front, back, one sleeve for both arms (unseen parts inferred)


class RecreateOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RecreateMode = RecreateMode.PATTERN
    remove_logos: bool = True  # crests, badges, kit-maker marks
    remove_sponsors: bool = True  # sponsor names / brand marks
    remove_names: bool = True  # player / team names
    remove_numbers: bool = True  # player numbers
    remove_other_text: bool = True  # any remaining lettering
    fidelity: Literal["exact", "clean"] = "exact"  # exact = same-to-same; clean = also tidy/straighten
    seamless: bool = False  # pattern mode: make the texture tile seamlessly
    correct_colors: bool = True  # undo lighting cast/shadows to recover true print colours
    background: Background = "white"  # flat_jersey / replica background
    notes: str = Field("", max_length=2000)  # extra staff instructions
    # panels: the back is the front mirrored / copied (no image call), or drawn from the uploaded back picture
    back: Literal["mirror", "copy", "image"] = "mirror"
    # panels: which panels to make (any of the three; /customerep lets the client choose). Without the front, the
    # reference itself is the picture of the panel(s) asked for: a back is drawn from it (back is then "image").
    panels: list[PanelName] = Field(default_factory=lambda: list(PANEL_ORDER), min_length=1)
    # crops as (left, top, right, bottom) fractions of a picture; None = the whole picture. front_crop applies to the
    # reference (any output type; for panels without the front, it is the box of the panel asked for), back_crop to
    # the separate back picture (panels with the front, the back and back == "image")
    front_crop: tuple[float, float, float, float] | None = None
    back_crop: tuple[float, float, float, float] | None = None
    # the box on the client's separate sleeve picture (/customerep: a sleeve made together with other panels)
    sleeve_crop: tuple[float, float, float, float] | None = None
    # panels: the AI reference check (labels, and the sleeve's wording). /customerep turns it off: there every panel
    # comes from a box the client drew, and the labels are hidden (saves one text call, about 3 cents, per job)
    reference_check: bool = True
    aspect_ratio: AspectRatio = "auto"
    resolution: Resolution = "2K"
    print_long_edge: int = Field(0, ge=0, le=12000)  # 0 = keep model output size; else Lanczos upscale target
    variants: int = Field(1, ge=1, le=4)
    provider: ProviderName | None = None  # None = server default
    model: str | None = None  # None = provider default
    seed: int | None = Field(None, ge=0, le=2**31 - 1)

    @field_validator("front_crop", "back_crop", "sleeve_crop")
    @classmethod
    def _check_crop(cls, box: tuple[float, float, float, float] | None) -> tuple[float, float, float, float] | None:
        if box is None:
            return None
        left, top, right, bottom = box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("a crop must be (left, top, right, bottom) fractions with left < right and top < bottom")
        if right - left < MIN_CROP - 1e-6 or bottom - top < MIN_CROP - 1e-6:  # tolerance: 0.151 - 0.101 < 0.05
            raise ValueError("the crop is too small")
        return None if box == (0, 0, 1, 1) else box

    @field_validator("panels")
    @classmethod
    def _order_panels(cls, panels: list[str]) -> list[str]:
        return [p for p in PANEL_ORDER if p in panels]  # no repeats, always front, back, sleeve

    @model_validator(mode="after")
    def _back_without_front(self) -> RecreateOptions:
        if self.mode == RecreateMode.PANELS and "back" in self.panels and "front" not in self.panels:
            self.back = "image"  # no front to mirror or copy: the back is drawn from the reference picture
        return self


# ---------------------------------------------------------------------------
# Feature B: design a jersey from the client's description (chat-style)
# ---------------------------------------------------------------------------


class DesignView(str, Enum):
    FRONT = "front"
    BACK = "back"
    FRONT_BACK = "front_back"  # front and back side by side on one canvas


class RenderStyle(str, Enum):
    FLAT = "flat"  # flat 2D production artwork / technical flat
    MOCKUP = "mockup"  # photo-real studio product shot (ghost mannequin)
    PATTERN = "pattern"  # the print pattern only, full-bleed


class DesignOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sport: str = Field("football (soccer)", max_length=60)
    view: DesignView = DesignView.FRONT_BACK
    render_style: RenderStyle = RenderStyle.FLAT
    collar: str = Field("", max_length=80)  # "" = designer's choice
    sleeves: str = Field("", max_length=80)  # "" = designer's choice
    text_policy: Literal["as_described", "none"] = "as_described"
    team_name: str = Field("", max_length=60)
    player_name: str = Field("", max_length=40)
    player_number: str = Field("", max_length=4)
    colors: list[str] = Field(default_factory=list, max_length=8)  # optional fixed palette, '#RRGGBB'
    background: Background = "white"
    use_brief_writer: bool = True  # expand the client's words into a structured brief first
    brief_writer: BriefWriterName = "auto"
    aspect_ratio: AspectRatio = "auto"
    resolution: Resolution = "2K"
    print_long_edge: int = Field(0, ge=0, le=12000)
    variants: int = Field(1, ge=1, le=4)
    provider: ProviderName | None = None
    model: str | None = None
    seed: int | None = Field(None, ge=0, le=2**31 - 1)

    @field_validator("colors")
    @classmethod
    def _normalize_colors(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for c in v:
            h = normalize_hex(c)
            if h is None:
                raise ValueError(f"invalid hex colour: {c!r}")
            out.append(h)
        return out


# ---------------------------------------------------------------------------
# The AI design brief (structured output of the brief-writer LLM)
#
# Every field is required and there are no numeric/string constraints so the
# same schema works with Claude structured outputs and OpenAI strict JSON
# schema. Empty strings / empty lists mean "not used".
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaletteColor(_Strict):
    name: str = Field(description="Human colour name, e.g. 'Bengal Maroon'.")
    hex: str = Field(description="Exact sRGB hex code '#RRGGBB'.")
    role: str = Field(description="Role in the design, e.g. 'base', 'primary accent', 'secondary accent', 'trim', 'numbers/text'.")


class ZoneSpec(_Strict):
    zone: str = Field(description="Garment zone, e.g. 'front body', 'back body', 'sleeves', 'collar', 'cuffs', 'side panels', 'shoulder yoke'.")
    design: str = Field(description="Precise description of what appears in this zone: colours (by hex), graphic, scale, angle, placement.")


class TextElement(_Strict):
    kind: Literal["team_name", "player_name", "number", "sponsor", "slogan", "other"]
    content: str = Field(description="The exact characters to render, spelled exactly.")
    placement: str = Field(description="Where it goes, e.g. 'centre chest', 'upper back above number', 'back centre'.")
    style: str = Field(description="Lettering style, size relative to the panel, fill colour hex, outline.")


class LogoElement(_Strict):
    description: str = Field(description="What the crest/logo/badge looks like (a simple placeholder shape if unknown).")
    placement: str = Field(description="Where it goes, e.g. 'left chest', 'right chest', 'left sleeve'.")


class DesignBrief(_Strict):
    title: str = Field(description="Short evocative design name.")
    sport: str
    garment: str = Field(description="Garment type in one phrase, e.g. 'short-sleeve crew-neck football jersey'.")
    fit: str = Field(description="Fit/cut, e.g. 'athletic slim fit'.")
    collar: str = Field(description="Collar construction and colour.")
    sleeves: str = Field(description="Sleeve construction (set-in/raglan/sleeveless...), length and colour treatment.")
    cuffs_and_hem: str = Field(description="Cuff and hem finish and colours.")
    base_color: str = Field(description="Main body base colour with hex.")
    palette: list[PaletteColor] = Field(description="2-6 colours, every colour used in the design.")
    pattern: str = Field(description="The primary graphic device: type, geometry, scale relative to the panel, angle, density, placement, colour mapping by hex.")
    zones: list[ZoneSpec]
    text_elements: list[TextElement] = Field(description="Only lettering the client asked for; empty if none.")
    logos: list[LogoElement] = Field(description="Only crests/logos the client asked for; empty if none.")
    style_keywords: list[str]
    mood: str
    avoid: list[str] = Field(description="Things the client explicitly does not want.")
    assumptions: list[str] = Field(description="Decisions you made where the client was vague, so staff can confirm them.")


class BriefResponse(_Strict):
    brief: DesignBrief
    changes: list[str] = Field(description="For a refinement: each change made versus the previous brief. Empty for a new design.")
    reply: str = Field(description="Short, friendly chat reply to the client describing the design or the changes (2-4 sentences).")
    suggestions: list[str] = Field(description="2-3 short follow-up ideas the client could ask for next.")


# ---------------------------------------------------------------------------
# The AI reference check (structured output of the text model for panel jobs):
# how much of each garment part the client's reference shows. Same strict rules
# as the brief, so it works with Claude and OpenAI structured outputs.
# ---------------------------------------------------------------------------

Visibility = Literal["full", "partial", "none"]
# a panel copied from the reference, partly guessed, guessed, or (the back) made from the front panel
PanelSource = Literal["reference", "partial", "inferred", "mirror", "copy"]


class PartVisibility(_Strict):
    part: Literal["front", "back", "sleeve"] = Field(
        description="Garment part; sleeve stands for both sleeves, which are printed from one design."
    )
    visibility: Visibility = Field(
        description="full: all of it visible clearly enough to copy exactly; partial: only some of it; none: not visible."
    )
    note: str = Field(description="One short sentence for staff: where the part is seen or what is missing.")


class ReferenceAnalysis(_Strict):
    parts: list[PartVisibility] = Field(description="One entry per part: front, back, sleeve.")
    views: str = Field(description="The views and insets the reference shows.")
    summary: str = Field(description="One sentence: which parts the panels will have to infer.")


class SavedPartVisibility(BaseModel):
    part: str  # jobs from 2026-11-01 also have left_sleeve, right_sleeve and collar
    visibility: Visibility
    note: str = ""


class SavedReferenceAnalysis(BaseModel):
    """The reference check as stored on a job. Lenient, so jobs made with an earlier part list still load."""

    parts: list[SavedPartVisibility] = Field(default_factory=list)
    views: str = ""
    summary: str = ""


# ---------------------------------------------------------------------------
# Jobs (persisted as outputs/jobs/<job_id>/job.json)
# ---------------------------------------------------------------------------


class JobKind(str, Enum):
    RECREATE = "recreate"
    DESIGN = "design"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # every variant succeeded
    PARTIAL = "partial"  # at least one variant succeeded, at least one failed
    FAILED = "failed"


class PaletteEntry(BaseModel):
    hex: str
    pct: float  # share of pixels, 0-100


class VectorAsset(BaseModel):
    preset: str  # "flat" | "standard" | "detailed" | "raster"
    eps: str  # filename inside the job directory
    svg: str | None = None  # None for raster EPS
    pdf: str | None = None  # vector PDF; None for raster EPS and for exports made before colour layers
    colors: int | None = None  # colour layers (older exports: distinct fill colours)
    paths: int | None = None  # subpaths over all layers (older exports: vtracer shapes)
    layers: int | None = None  # colour layers, one compound path each; None for raster and older exports
    anchors: int | None = None  # anchor points over all layers; None for raster and older exports
    eps_bytes: int = 0
    pdf_bytes: int = 0


class PromptRecord(BaseModel):
    system: str
    user: str


class VariantInfo(BaseModel):
    index: int  # 1-based
    # production-panel jobs: "front" | "back" | "sleeve" (2026-11-01 jobs: left_sleeve, right_sleeve, collar;
    # older jobs: sleeve, collar)
    panel: str | None = None
    label: str | None = None  # display name, e.g. "Front panel" (None = "Variant N")
    aspect_ratio: str | None = None  # set when it differs per variant (panels)
    prompt: PromptRecord | None = None  # set when each variant has its own prompt (panels)
    source: PanelSource | None = None  # panels: what the AI reference check found (None = not checked)
    source_note: str | None = None  # the reference check's note for this part
    status: Literal["pending", "succeeded", "failed"] = "pending"
    error: str | None = None
    png: str | None = None  # final PNG filename (upscaled if print_long_edge > 0)
    raw: str | None = None  # untouched model output filename
    preview: str | None = None  # ~1024 px WebP for fast display in the UI
    width: int | None = None
    height: int | None = None
    raw_width: int | None = None
    raw_height: int | None = None
    palette: list[PaletteEntry] = Field(default_factory=list)
    model_text: str = ""  # any text the image model returned alongside the image
    latency_s: float | None = None
    vectors: dict[str, VectorAsset] = Field(default_factory=dict)
    # the paid API calls made for this image (every attempt, including retries) and what they cost in USD, worked
    # out from the providers' token counts (app/pricing.py); None = not recorded (jobs made before 2026-11-03)
    api_calls: int = 0
    cost_usd: float | None = None
    uncosted_calls: int = 0  # of those calls, how many have an unknown cost (no reply, or no price)


class ParentRef(BaseModel):
    job_id: str
    variant: int = Field(ge=1)


class Job(BaseModel):
    id: str
    kind: JobKind
    demo: bool = False  # made with the demo login (/customerep); that login can only see these jobs
    status: JobStatus = JobStatus.QUEUED
    stage: str = "queued"  # human-readable progress, e.g. "Writing design brief"
    created_at: str  # ISO-8601 UTC
    updated_at: str
    options: dict  # RecreateOptions / DesignOptions as submitted (model_dump(mode="json"))
    provider: str | None = None
    model: str | None = None
    aspect_ratio: str | None = None  # concrete ratio actually used
    resolution: str | None = None  # resolution actually requested from the provider
    prompt: PromptRecord | None = None  # image-model prompt actually sent
    reference_analysis: SavedReferenceAnalysis | None = None  # panel jobs: the AI reference check, if it succeeded
    # the text model call: the design brief (design jobs) or the reference check (panel jobs)
    brief_writer: str | None = None  # e.g. "claude:claude-opus-5-5"
    brief_prompt: PromptRecord | None = None
    # design jobs only
    session_id: str | None = None
    parent: ParentRef | None = None
    message: str | None = None  # the client's words for this turn
    brief: DesignBrief | None = None
    reply: str | None = None
    changes: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # files
    inputs: list[str] = Field(default_factory=list)  # input image filenames (normalized PNG)
    variants: list[VariantInfo] = Field(default_factory=list)
    error: str | None = None
    # every paid API call (images, reference check, design brief) is logged in <job>/api_calls.jsonl; these are
    # its totals. cost_usd None = not recorded (jobs made before 2026-11-03).
    api_calls: int = 0
    cost_usd: float | None = None
    uncosted_calls: int = 0  # calls whose cost could not be worked out (no token counts, or no price for the model)
    cost_partial: bool = False  # a job made before costs were recorded got a logged call later (e.g. a retry)
    # API calls under way, saved before each request goes out: a server killed mid-call leaves them here, and the
    # next start-up logs each as interrupted (it may have been billed)
    calls_in_flight: list[dict] = Field(default_factory=list)


class JobCreated(BaseModel):
    job_id: str
    session_id: str | None = None


class VectorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: Literal["flat", "standard", "detailed", "raster"] = "standard"
    colors: int | None = Field(None, ge=2, le=64)  # override the preset's colour count
