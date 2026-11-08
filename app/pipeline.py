"""Job orchestration: design brief -> prompt -> image generation -> post-processing -> storage.

Jobs run as asyncio tasks inside the server process; the browser polls
GET /api/jobs/{id}. All job mutations happen on the event-loop thread (CPU work
runs in worker threads and only returns values), so a job object is never
modified concurrently.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import io
import json
import logging
import math
import os
import tempfile
import time
import zipfile
from collections import OrderedDict
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from pydantic import ValidationError

from app import pricing, usage
from app.config import Settings
from app.imaging import io as imgio
from app.imaging.eps import combine_eps, eps_size, is_current_eps, physical_size_pt, raster_eps, vector_to_eps
from app.imaging.palette import dominant_colors
from app.imaging.upscale import upscale_to_long_edge, upscale_to_size
from app.imaging.vector_out import vector_to_pdf, vector_to_svg
from app.imaging.vectorize import trace_to_vector
from app.prompts import (
    PromptPair,
    build_brief_prompt,
    build_design_prompt,
    build_panel_prompt,
    build_recreate_prompt,
    build_refine_prompt,
)
from app.prompts.builder import build_reference_check_prompt
from app.prompts.descriptors import PANEL_PRINT_IN
from app.providers.base import ImageGenRequest, ImageGenResult, ImageProvider, InputImage, ModelInfo, ProviderError
from app.providers.registry import ProviderRegistry
from app.providers.text.base import BriefRequest, BriefWriter
from app.schemas import (
    BriefResponse,
    DesignBrief,
    DesignOptions,
    DesignView,
    Job,
    JobKind,
    JobStatus,
    PanelSource,
    ParentRef,
    PartVisibility,
    PromptRecord,
    RecreateMode,
    RecreateOptions,
    ReferenceAnalysis,
    RenderStyle,
    SavedReferenceAnalysis,
    VariantInfo,
    VectorAsset,
    VectorizeRequest,
    Visibility,
)
from app.storage import JobStore, new_job_id, new_session_id, utcnow_iso

log = logging.getLogger("jersey.pipeline")

RESOLUTIONS = ("1K", "2K", "4K")
# retries after a retryable provider error (the SDK itself no longer re-sends). A flaky connection often drops the
# upload at once (2026-11-03: 10 quick failures in a row on one sleeve), so there are several, with short pauses.
RETRY_DELAYS_S = (1.0, 3.0, 6.0, 10.0)
JOB_CACHE_SIZE = 300
INTERRUPTED_CALL = "The server stopped while this call was under way; it may have been billed."
CALL_LOG = "api_calls.jsonl"  # every paid API call of a job, one JSON object per line (GET /api/jobs/{id}/calls)
# "retry" inside a manual retry of a failed image (JobManager._retry), else "job"; task-local, like the retry task
_CALL_TRIGGER: contextvars.ContextVar[str] = contextvars.ContextVar("api_call_trigger", default="job")
REFERENCE_CHECK_TIMEOUT_S = 90.0  # the optional reference check; the back, sleeves and collar wait for it


class JobError(Exception):
    """A request cannot be turned into a job; the message is shown to the user (HTTP 400/404/409)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class _Resolved:
    provider: ImageProvider
    model: ModelInfo
    aspect_ratio: str
    resolution: str
    background: str  # "opaque" | "transparent"
    warnings: list[str]


def _ratio_value(ratio: str) -> float:
    w, h = ratio.split(":")
    return float(w) / float(h)


def _nearest_ratio(target: float, allowed: tuple[str, ...]) -> str:
    return min(allowed, key=lambda r: abs(math.log(_ratio_value(r)) - math.log(target)))


def _clamp_resolution(requested: str, supported: tuple[str, ...]) -> str:
    if requested in supported:
        return requested
    rank = RESOLUTIONS.index(requested)
    lower = [r for r in supported if RESOLUTIONS.index(r) <= rank]
    if lower:
        return max(lower, key=RESOLUTIONS.index)
    return min(supported, key=RESOLUTIONS.index)


# Production panels: (panel, label). Each is a full-bleed rectangle at its 3XL print size
# (descriptors.PANEL_PRINT_IN), written at print_dpi; the factory cuts the pieces and makes the
# collar. One sleeve design serves both arms. The back is the front mirrored or copied unless the
# client gives a back picture. Jobs from 2026-11-01 also have left_sleeve, right_sleeve and collar
# panels and older ones sleeve and collar; those drew shaped pieces on white.
PANEL_SPECS = (
    ("front", "Front panel"),
    ("back", "Back panel"),
    ("sleeve", "Sleeve panel"),
)
# The AI reference check's visibility of a part -> its panel's source label.
PANEL_SOURCES: dict[Visibility, PanelSource] = {"full": "reference", "partial": "partial", "none": "inferred"}
# Mirroring would print these reversed, so a job that keeps any of them copies the front instead.
TEXT_REMOVALS = ("remove_logos", "remove_sponsors", "remove_names", "remove_numbers", "remove_other_text")
# A panel's raster EPS embeds the model output resampled to this resolution on its print-size page: at the full
# print dpi it would be 94-250 MB, and the extra pixels would be interpolation anyway.
RASTER_EPS_PANEL_DPI = 150


FRONT_FAILED_SLEEVE = "The front panel failed, so the sleeve was drawn from the reference only."
FRONT_FAILED_SLEEVE_AND_BACK = (
    "The front panel failed, so the sleeve was drawn from the reference only and the back could not be made."
)
BACK_FAILED_SLEEVE = "The back panel failed, so the sleeve was drawn from the reference only."
# prepare_downloads: the vector preset made ahead of time (the EPS ZIP menu's default)
PREPARED_PRESET = "detailed"


def uses_back_picture(opts: RecreateOptions) -> bool:
    """A panel job whose back is drawn from the client's separate back picture (the back_image upload). Only a job
    that also makes the front has one: without the front, the back is drawn from the reference itself."""
    return (
        opts.mode == RecreateMode.PANELS and opts.back == "image" and "front" in opts.panels and "back" in opts.panels
    )


def derives_back(opts: RecreateOptions) -> bool:
    """The back panel is the front panel mirrored or copied (no image call)."""
    return "front" in opts.panels and opts.back != "image"


def _back_method(opts: RecreateOptions, job: Job) -> PanelSource:
    """How a back without its own picture is made: the front mirrored (default) or copied. Mirroring would print
    kept text and logos reversed, so a job that keeps any of them copies instead (with a warning)."""
    if opts.back == "copy":
        return "copy"
    if not all(getattr(opts, flag) for flag in TEXT_REMOVALS):
        warning = (
            "Some text or logos are kept, so the back is an exact copy of the front, including those logos and text "
            "(mirroring would print them reversed)."
        )
        if warning not in job.warnings:  # a back made again must not repeat it
            job.warnings.append(warning)
        return "copy"
    return "mirror"


# The panel ZIP's combined EPS: the front and back side by side and the sleeve centred under the front, like the
# factory's own "full view" sample without its collar and second sleeve. The gaps were measured on that sample.
COMBINED_PANELS = ("front", "back", "sleeve")
COMBINED_GAPS_CM = (8.95, 4.73)  # front to back; the front/back row to the sleeve


def combined_layout(sizes: list[tuple[float, float]]) -> tuple[float, float, list[tuple[float, float]]]:
    """Page width and height and each panel's bottom-left corner, all in points, for the front, back and sleeve
    sizes in points (COMBINED_PANELS order)."""
    (front_w, front_h), (back_w, back_h), (sleeve_w, sleeve_h) = sizes
    gap_x, gap_y = (cm / 2.54 * 72 for cm in COMBINED_GAPS_CM)
    sleeve_x = (front_w - sleeve_w) / 2
    shift = max(0.0, -sleeve_x)  # a sleeve wider than the front (older jobs) moves the front and back right instead
    width = max(shift + front_w + gap_x + back_w, shift + sleeve_x + sleeve_w)
    height = max(front_h, back_h) + gap_y + sleeve_h
    return width, height, [(shift, height - front_h), (shift + front_w + gap_x, height - back_h), (shift + sleeve_x, 0.0)]


def panel_ratio(panel: str) -> str:
    """The image model's aspect ratio for a panel: its print size reduced, e.g. 25 x 34 in -> "25:34"."""
    width, height = PANEL_PRINT_IN[panel]
    step = math.gcd(width, height)
    return f"{width // step}:{height // step}"


def file_stem(variant: VariantInfo) -> str:
    """File name stem: the panel name for panel jobs ("front.png"), else "v<index>"."""
    return variant.panel or f"v{variant.index}"


async def _stop(*tasks: asyncio.Task | None) -> None:
    """Cancel helper tasks that may still run (the reference check, a back drawn alongside the front) and wait
    until they have stopped, so the calls they had out are logged before their job is saved."""
    running = [t for t in tasks if t is not None and not t.done()]
    for task in running:
        task.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)


def _describe_images(images: list[InputImage]) -> list[dict]:
    """The call log's record of the images sent: what each was and which job file it was made from, plus a
    fingerprint (sha256) of the exact bytes sent. Runs in a worker thread."""
    return [{"label": img.label, "source": img.source, **_describe_bytes(img.data, img.mime_type)} for img in images]


def _describe_bytes(data: bytes, mime_type: str) -> dict:
    out: dict = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "mime_type": mime_type}
    try:
        with Image.open(io.BytesIO(data)) as im:  # reads the header only
            out["width"], out["height"] = im.size
    except Exception:  # noqa: BLE001 - the fingerprint is enough
        pass
    return out


def _error_record(exc: BaseException | None) -> dict:
    if isinstance(exc, ProviderError):
        return {
            "kind": exc.kind, "message": exc.message, "retryable": exc.retryable, "status_code": exc.status_code,
            "billing_unknown": exc.billing_unknown, "not_sent": exc.not_sent, "detail": exc.detail,
        }
    if isinstance(exc, asyncio.CancelledError):
        return {"kind": "cancelled", "message": "Cancelled: the job stopped or the server shut down."}
    if isinstance(exc, TimeoutError):
        return {"kind": "timeout", "message": "No reply in time."}
    return {"kind": "unknown", "message": f"{type(exc).__name__}: {exc}"}


def recreate_auto_ratio(opts: RecreateOptions, input_ratio: float) -> float:
    """Flat artwork may need room for front + back side by side (seller posters usually show both)."""
    if opts.mode == RecreateMode.PANELS:
        width, height = PANEL_PRINT_IN["front"]  # the job-level ratio is the front panel's; each panel has its own
        return width / height
    return 1.5 if opts.mode == RecreateMode.FLAT_JERSEY else input_ratio


def design_auto_aspect(opts: DesignOptions) -> str:
    if opts.render_style == RenderStyle.PATTERN:
        return "1:1"
    if opts.view == DesignView.FRONT_BACK:
        return "16:9"
    return "4:5" if opts.render_style == RenderStyle.MOCKUP else "3:4"


class JobManager:
    def __init__(self, settings: Settings, store: JobStore, registry: ProviderRegistry) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self._gen_sem = asyncio.Semaphore(max(1, settings.max_concurrent_generations))
        self._cpu_sem = asyncio.Semaphore(max(1, settings.cpu_jobs))  # each print PNG takes about 1 GB
        self._cache: OrderedDict[str, Job] = OrderedDict()
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._prep_tasks: set[asyncio.Task] = set()  # prepare_downloads; not in _running, so retries are not blocked
        self._vector_locks: dict[tuple[str, int, str], asyncio.Lock] = {}
        # every paid call in one append-only file, which outlives a deleted job (the admin dashboard; app/usage.py)
        self.usage_ledger = store.root.parent / usage.LEDGER

    # ------------------------------------------------------------------
    # admin dashboard (app/usage.py)
    # ------------------------------------------------------------------

    def ensure_usage_ledger(self) -> None:
        """At start-up, before anything is logged: create the usage ledger from the existing call logs, once."""
        try:
            written = usage.backfill(self.usage_ledger, self.store, self.call_log)
        except OSError:
            log.exception("could not create the usage ledger %s", self.usage_ledger)
            return
        if written:
            log.info("usage ledger created from %d logged API call(s)", written)

    def usage_report(self, days: int = 30) -> dict:
        """Jobs and their paid API calls for the dashboard (run in a worker thread: it reads every job's files)."""
        floor = usage.parse_floor(self.settings.dashboard_since)
        return usage.build_report(self.store, self.usage_ledger, self.call_log, days, floor=floor)

    # ------------------------------------------------------------------
    # job cache / persistence
    # ------------------------------------------------------------------

    def _remember(self, job: Job) -> None:
        self._cache[job.id] = job
        self._cache.move_to_end(job.id)
        while len(self._cache) > JOB_CACHE_SIZE:
            oldest = next(iter(self._cache))
            if oldest in self._running:
                self._cache.move_to_end(oldest)
                break
            self._cache.pop(oldest)

    def get(self, job_id: str) -> Job | None:
        job = self._cache.get(job_id)
        if job is None:
            job = self.store.load(job_id)
            if job is not None:
                self._remember(job)
        return job

    def _save(self, job: Job, stage: str | None = None) -> None:
        if stage is not None:
            job.stage = stage
        job.updated_at = utcnow_iso()
        self.store.save(job)

    def list(self, *, limit: int = 60, kind: JobKind | None = None, session_id: str | None = None) -> list[Job]:
        out: list[Job] = []
        for job_id in self.store.list_ids():
            job = self._cache.get(job_id) or self.store.load(job_id)
            if job is None:
                continue
            if kind is not None and job.kind != kind:
                continue
            if session_id is not None and job.session_id != session_id:
                continue
            out.append(job)
            if len(out) >= limit:
                break
        return out

    def delete(self, job_id: str) -> bool:
        if job_id in self._running:
            raise JobError("This job is still running.", 409)
        job = self._cache.pop(job_id, None)
        try:
            if job is None:
                job = self.store.load(job_id)
            deleted = self.store.delete(job_id)
        except ValueError:
            return False
        if deleted and job is not None:  # its calls stay in the usage ledger; note the deletion there too
            usage.append(self.usage_ledger, usage.deleted_line(job))
        return deleted

    def recover_interrupted(self, scan: int = 500) -> int:
        """Mark jobs left queued/running by a previous process as failed (partial when some images were done). The API
        calls that were under way (calls_in_flight) are logged as interrupted: they may have been sent and billed."""
        fixed = 0
        for job_id in self.store.list_ids()[:scan]:
            job = self.store.load(job_id)
            if job and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                self._recount_calls(job)  # job.json may lag the log when the server was killed
                job.status = JobStatus.PARTIAL if any(v.status == "succeeded" for v in job.variants) else JobStatus.FAILED
                job.error = "Interrupted because the server restarted. Please run it again."
                for v in job.variants:
                    if v.status == "pending":
                        v.status, v.error = "failed", job.error
                for call in list(job.calls_in_flight):
                    variant = next((v for v in job.variants if v.index == call.get("variant")), None)
                    self._log_call(job, {
                        "purpose": call.get("purpose") or "image", "trigger": call.get("trigger") or "job",
                        "variant": call.get("variant"), "panel": call.get("panel"), "label": call.get("label"),
                        "attempt": call.get("attempt"), "provider": call.get("provider") or job.provider or "",
                        "model": call.get("model") or job.model, "endpoint": None, "started_at": call.get("started_at"),
                        "finished_at": utcnow_iso(), "latency_s": None, "status": "error",
                        "error": {"kind": "interrupted", "message": INTERRUPTED_CALL},
                    }, variant, key=call.get("key"))
                job.calls_in_flight = []
                self._save(job, "Interrupted")
                fixed += 1
        return fixed

    def _spawn(self, job: Job, coro) -> None:
        self._running.add(job.id)
        task = asyncio.create_task(coro, name=f"job-{job.id}")
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            self._running.discard(job.id)
            if not t.cancelled() and t.exception() is not None:  # defensive: _run_* catch everything
                log.error("job %s crashed: %r", job.id, t.exception())

        task.add_done_callback(_done)

    async def shutdown(self) -> None:
        tasks = list(self._tasks) + list(self._prep_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # resolution of provider / model / aspect / resolution
    # ------------------------------------------------------------------

    def _resolve(
        self,
        provider_name: str | None,
        model_id: str | None,
        aspect_ratio: str,
        auto_ratio: float,
        resolution: str,
        background: str,
        *,
        image_count: int,
    ) -> _Resolved:
        try:
            provider = self.registry.get_image_provider(provider_name)
        except LookupError as exc:
            raise JobError(str(exc)) from exc
        models = provider.models()
        wanted = model_id or provider.default_model()
        model = next((m for m in models if m.id == wanted), None)
        if model is None:
            raise JobError(f"Unknown model '{wanted}' for {provider.label}.")
        if image_count > model.max_input_images:
            raise JobError(f"{model.label} accepts at most {model.max_input_images} input image(s).")
        warnings: list[str] = []
        if aspect_ratio == "auto":
            ratio = _nearest_ratio(auto_ratio, model.aspect_ratios)
        elif aspect_ratio in model.aspect_ratios:
            ratio = aspect_ratio
        else:
            ratio = _nearest_ratio(_ratio_value(aspect_ratio), model.aspect_ratios)
            warnings.append(f"{model.label} does not support {aspect_ratio}; used {ratio}.")
        res = _clamp_resolution(resolution, model.resolutions)
        if res != resolution:
            warnings.append(f"{model.label} does not support {resolution} output; used {res}.")
        bg = "opaque"
        if background == "transparent":
            if model.supports_transparent:
                bg = "transparent"
            else:
                warnings.append(f"{model.label} cannot make transparent backgrounds; used white.")
        return _Resolved(provider, model, ratio, res, bg, warnings)

    def preview_prompt(
        self,
        kind: str,
        options: RecreateOptions | DesignOptions,
        *,
        message: str = "",
        input_ratio: float | None = None,
    ) -> tuple[PromptPair, _Resolved]:
        """The exact image-model prompt a job would send (no brief writer, no API call)."""
        if isinstance(options, RecreateOptions):
            panels = options.mode == RecreateMode.PANELS
            resolved = self._resolve(
                options.provider, options.model, "auto" if panels else options.aspect_ratio,
                recreate_auto_ratio(options, input_ratio or 1.0), options.resolution, options.background, image_count=1,
            )
            if panels:  # as the job sends it: the first panel asked for (usually the front), at its exact ratio
                first = options.panels[0]
                resolved.aspect_ratio = panel_ratio(first)
                pair = build_panel_prompt(
                    options, panel=first, aspect_ratio=resolved.aspect_ratio, has_front_reference=False,
                    back_picture=first == "back",
                )
                return pair, resolved
            return build_recreate_prompt(options, aspect_ratio=resolved.aspect_ratio), resolved
        resolved = self._resolve(
            options.provider, options.model, options.aspect_ratio, _ratio_value(design_auto_aspect(options)),
            options.resolution, options.background, image_count=0,
        )
        if kind == "refine":
            pair = build_refine_prompt(
                options, message=message, brief=None, changes=[], aspect_ratio=resolved.aspect_ratio,
                inspiration_count=0,
            )
        elif kind == "brief":
            pair = build_brief_prompt(
                options, message=message, previous_brief=None, has_current_design=False, inspiration_count=0
            )
        else:
            pair = build_design_prompt(
                options, message=message, brief=None, aspect_ratio=resolved.aspect_ratio, inspiration_count=0
            )
        return pair, resolved

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------

    def _new_job(self, kind: JobKind, options: dict, resolved: _Resolved, variants: int) -> Job:
        now = utcnow_iso()
        return Job(
            id=new_job_id(),
            kind=kind,
            status=JobStatus.QUEUED,
            stage="Queued",
            created_at=now,
            updated_at=now,
            options=options,
            provider=resolved.provider.name,
            model=resolved.model.id,
            aspect_ratio=resolved.aspect_ratio,
            resolution=resolved.resolution,
            warnings=list(resolved.warnings),
            variants=[VariantInfo(index=i) for i in range(1, variants + 1)],
        )

    async def submit_recreate(
        self, opts: RecreateOptions, image: Image.Image, back_image: Image.Image | None = None, *, demo: bool = False,
        sleeve_image: Image.Image | None = None,
    ) -> Job:
        """`image` is the reference (the front, for panels; for panels without the front, the picture of the panels
        asked for); `back_image` is the client's separate back picture (only used when uses_back_picture)."""
        panels = opts.mode == RecreateMode.PANELS
        chosen = [spec for spec in PANEL_SPECS if spec[0] in opts.panels] if panels else []
        back_from_picture = uses_back_picture(opts)
        if back_from_picture and back_image is None:
            raise JobError("Add the back picture, or answer that the back is the same as the front.", 400)
        full = image  # panels: the sleeve and the reference check see the whole picture, not just the front crop
        if opts.front_crop:
            image = imgio.crop_fraction(image, opts.front_crop)
        resolved = self._resolve(
            opts.provider,
            opts.model,
            "auto" if panels else opts.aspect_ratio,
            recreate_auto_ratio(opts, image.width / image.height),
            opts.resolution,
            "white" if opts.mode in (RecreateMode.PATTERN, RecreateMode.PANELS) else opts.background,  # full-bleed
            # the sleeve receives the picture and the front panel (and the back picture, when there is one)
            image_count=(3 if back_from_picture else 2) if panels else 1,
        )
        job = self._new_job(
            JobKind.RECREATE, opts.model_dump(mode="json"), resolved, len(chosen) if panels else opts.variants
        )
        job.demo = demo
        model_input = await asyncio.to_thread(
            imgio.prepare_model_input, image, max_long_edge=self.settings.model_input_long_edge
        )
        original = await asyncio.to_thread(imgio.to_png_bytes, image)
        self.store.write_file(job.id, "input_1.png", original)
        job.inputs = ["input_1.png"]
        reference = InputImage(data=model_input, mime_type=imgio.image_mime(model_input), label="reference", source="input_1.png")
        if opts.front_crop:  # keep the upload the crop was taken from (not a job input: input_1.png is what was used)
            self.store.write_file(job.id, "input_full.png", await asyncio.to_thread(imgio.to_png_bytes, full))
        if panels:
            whole_reference = reference
            if opts.front_crop and opts.panels != ["sleeve"]:  # a sleeve asked for alone is drawn from its own box
                whole_input = await asyncio.to_thread(
                    imgio.prepare_model_input, full, max_long_edge=self.settings.model_input_long_edge
                )
                whole_reference = InputImage(data=whole_input, mime_type=imgio.image_mime(whole_input), label="reference", source="input_full.png")
            back_reference = None
            if back_from_picture:
                if opts.back_crop:  # the back box; keep the full back upload as well (not a job input)
                    self.store.write_file(job.id, "input_full_2.png", await asyncio.to_thread(imgio.to_png_bytes, back_image))
                    back_image = imgio.crop_fraction(back_image, opts.back_crop)
                self.store.write_file(job.id, "input_2.png", await asyncio.to_thread(imgio.to_png_bytes, back_image))
                job.inputs.append("input_2.png")
                back_input = await asyncio.to_thread(
                    imgio.prepare_model_input, back_image, max_long_edge=self.settings.model_input_long_edge
                )
                back_reference = InputImage(data=back_input, mime_type=imgio.image_mime(back_input), label="back_reference", source="input_2.png")
            sleeve_reference = None  # the client's own sleeve picture (its box), for a sleeve made with other panels
            if sleeve_image is not None and "sleeve" in opts.panels and len(opts.panels) > 1:
                if opts.sleeve_crop:
                    self.store.write_file(job.id, "input_full_3.png", await asyncio.to_thread(imgio.to_png_bytes, sleeve_image))
                    sleeve_image = imgio.crop_fraction(sleeve_image, opts.sleeve_crop)
                self.store.write_file(job.id, "input_3.png", await asyncio.to_thread(imgio.to_png_bytes, sleeve_image))
                job.inputs.append("input_3.png")
                sleeve_input = await asyncio.to_thread(
                    imgio.prepare_model_input, sleeve_image, max_long_edge=self.settings.model_input_long_edge
                )
                sleeve_reference = InputImage(data=sleeve_input, mime_type=imgio.image_mime(sleeve_input), label="sleeve_reference", source="input_3.png")
            for variant, (panel, label) in zip(job.variants, chosen):
                variant.panel, variant.label = panel, label
                variant.aspect_ratio = panel_ratio(panel)  # exact: 25:34 bodies, 12:7 sleeve
            first = job.variants[0]  # the front, or the first panel asked for; its prompt represents the job
            job.aspect_ratio = first.aspect_ratio
            first_prompt = self._panel_prompt(opts, first, has_front_reference=False, back_picture=first.panel == "back")
            job.prompt = PromptRecord(system=first_prompt.system, user=first_prompt.user)
            self._remember(job)
            self._save(job, "Queued")
            self._spawn(job, self._run_panels(job, opts, resolved, reference, whole_reference, back_reference, sleeve_reference))
            return job
        prompt = build_recreate_prompt(opts, aspect_ratio=resolved.aspect_ratio)
        job.prompt = PromptRecord(system=prompt.system, user=prompt.user)
        self._remember(job)
        self._save(job, "Queued")
        self._spawn(job, self._run_generation(job, resolved, prompt, [reference], opts.seed, opts.print_long_edge))
        return job

    async def submit_design(
        self,
        opts: DesignOptions,
        message: str,
        *,
        session_id: str | None,
        parent: ParentRef | None,
        attachments: list[Image.Image],
        demo: bool = False,
    ) -> Job:
        message = message.strip()
        if not message:
            raise JobError("Please describe the jersey (or the change you want).")
        if len(message) > self.settings.max_message_chars:
            raise JobError(f"Message is too long (max {self.settings.max_message_chars} characters).")
        if len(attachments) > self.settings.max_attachments:
            raise JobError(f"At most {self.settings.max_attachments} reference images per message.")

        parent_job: Job | None = None
        parent_raw: bytes | None = None
        if parent is not None:
            parent_job = self.get(parent.job_id)
            if parent_job is None:
                raise JobError("The design you are refining no longer exists.", 404)
            if parent.variant > len(parent_job.variants):
                raise JobError("That variant does not exist.", 404)
            pv = parent_job.variants[parent.variant - 1]
            if pv.status != "succeeded" or not (pv.raw or pv.png):
                raise JobError("That variant has no image to refine.", 409)
            parent_raw = self.store.read_file(parent_job.id, pv.raw or pv.png)
            if parent_raw is None:
                raise JobError("The image for that variant is missing on disk.", 404)

        image_count = len(attachments) + (1 if parent_raw else 0)
        if parent_job is not None and opts.aspect_ratio == "auto" and parent_job.aspect_ratio:
            auto_ratio = _ratio_value(parent_job.aspect_ratio)  # keep the canvas stable while refining
        else:
            auto_ratio = _ratio_value(design_auto_aspect(opts))
        resolved = self._resolve(
            opts.provider,
            opts.model,
            opts.aspect_ratio,
            auto_ratio,
            opts.resolution,
            opts.background if opts.render_style != RenderStyle.PATTERN else "white",
            image_count=image_count,
        )
        job = self._new_job(JobKind.DESIGN, opts.model_dump(mode="json"), resolved, opts.variants)
        job.demo = demo
        if session_id:
            job.session_id = session_id
        elif parent_job is not None and parent_job.session_id:
            job.session_id = parent_job.session_id
        else:
            job.session_id = new_session_id()
        job.parent = parent
        job.message = message

        images: list[InputImage] = []
        if parent_raw is not None:
            parent_img = await asyncio.to_thread(imgio.decode_model_output, parent_raw)
            data = await asyncio.to_thread(
                imgio.prepare_model_input, parent_img, max_long_edge=self.settings.model_input_long_edge
            )
            images.append(InputImage(
                data=data, mime_type=imgio.image_mime(data), label="current_design", source=f"{parent_job.id}/{pv.raw or pv.png}"
            ))
        for i, att in enumerate(attachments, start=1):
            self.store.write_file(job.id, f"attach_{i}.png", await asyncio.to_thread(imgio.to_png_bytes, att))
            job.inputs.append(f"attach_{i}.png")
            data = await asyncio.to_thread(
                imgio.prepare_model_input, att, max_long_edge=self.settings.model_input_long_edge
            )
            images.append(InputImage(data=data, mime_type=imgio.image_mime(data), label="inspiration", source=f"attach_{i}.png"))

        self._remember(job)
        self._save(job, "Queued")
        parent_brief = parent_job.brief if parent_job is not None else None
        self._spawn(job, self._run_design(job, opts, resolved, images, parent_brief, refining=parent_job is not None))
        return job

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------

    async def _run_design(
        self,
        job: Job,
        opts: DesignOptions,
        resolved: _Resolved,
        images: list[InputImage],
        parent_brief: DesignBrief | None,
        *,
        refining: bool,
    ) -> None:
        try:
            job.status = JobStatus.RUNNING
            inspiration_count = sum(1 for im in images if im.label == "inspiration")
            brief = parent_brief
            writer = self.registry.get_brief_writer(opts.brief_writer) if opts.use_brief_writer else None
            if opts.use_brief_writer and writer is None:
                job.warnings.append("No AI designer (text model) is configured; used the client's words directly.")
            if writer is not None:
                self._save(job, "AI designer is writing the design brief")
                bp = build_brief_prompt(
                    opts,
                    message=job.message or "",
                    previous_brief=parent_brief,
                    has_current_design=refining,
                    inspiration_count=inspiration_count,
                )
                job.brief_prompt = PromptRecord(system=bp.system, user=bp.user)
                try:
                    resp = await self._call_writer(
                        job, "brief", writer, BriefRequest(system_prompt=bp.system, user_prompt=bp.user, images=images),
                        self.settings.provider_timeout_s,
                    )
                except (ProviderError, asyncio.TimeoutError) as exc:
                    reason = exc.message if isinstance(exc, ProviderError) else "timed out"
                    job.warnings.append(f"AI designer failed ({reason}); used the client's words directly.")
                    log.warning("brief writer failed for job %s: %r", job.id, exc)
                else:
                    brief = resp.brief
                    job.brief = resp.brief
                    job.reply = resp.reply
                    job.changes = list(resp.changes)
                    job.suggestions = list(resp.suggestions)
                    job.brief_writer = f"{writer.name}:{writer.model}"
            if job.brief is None and brief is not None:
                job.brief = brief  # carry the previous brief forward so later turns keep context
            if refining:
                prompt = build_refine_prompt(
                    opts,
                    message=job.message or "",
                    brief=brief,
                    changes=job.changes,
                    aspect_ratio=resolved.aspect_ratio,
                    inspiration_count=inspiration_count,
                )
            else:
                prompt = build_design_prompt(
                    opts,
                    message=job.message or "",
                    brief=brief,
                    aspect_ratio=resolved.aspect_ratio,
                    inspiration_count=inspiration_count,
                )
            job.prompt = PromptRecord(system=prompt.system, user=prompt.user)
            self._save(job)
        except asyncio.CancelledError:
            self._fail(job, "Cancelled because the server is shutting down.")
            raise
        except Exception as exc:  # noqa: BLE001 - a job must always end in a terminal state
            log.exception("design job %s failed before generation", job.id)
            self._fail(job, f"Could not prepare the design: {exc}")
            return
        await self._run_generation(job, resolved, prompt, images, opts.seed, opts.print_long_edge)

    async def _run_generation(
        self,
        job: Job,
        resolved: _Resolved,
        prompt: PromptPair,
        images: list[InputImage],
        seed: int | None,
        print_long_edge: int,
    ) -> None:
        try:
            job.status = JobStatus.RUNNING
            n = len(job.variants)
            self._save(job, f"Generating {n} variant{'s' if n > 1 else ''} with {resolved.model.label}")
            await asyncio.gather(
                *(
                    self._run_variant(job, v, resolved, prompt, images, seed, print_long_edge)
                    for v in job.variants
                )
            )
            self._finish(job)
        except asyncio.CancelledError:
            self._fail(job, "Cancelled because the server is shutting down.")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            self._fail(job, f"Unexpected error: {exc}")

    async def _run_panels(
        self,
        job: Job,
        opts: RecreateOptions,
        resolved: _Resolved,
        reference: InputImage,
        whole_reference: InputImage,
        back_reference: InputImage | None,
        sleeve_reference: InputImage | None = None,
    ) -> None:
        """The panels asked for (opts.panels). The front first (from the front crop, `reference`), with the AI
        reference check alongside. A back drawn from a picture (the client's back picture or, without the front, the
        reference itself) is drawn from that picture alone, at the same time as the front: given the front panel too,
        the model copied the front instead (2026-11-02). Then the sleeve, which sees the whole picture, the finished
        front panel (without the front: the back panel) and any back picture; a back without a picture is the front
        mirrored or copied. A sleeve asked for alone is drawn from the client's picture only. With the client's own
        sleeve picture (`sleeve_reference`, /customerep), the sleeve is drawn from that picture and the finished front
        (or back) panel, and the reference check judges that picture and labels only the sleeve."""
        check: asyncio.Task[ReferenceAnalysis | None] | None = None
        back_task: asyncio.Task[None] | None = None
        try:
            job.status = JobStatus.RUNNING
            made = {v.panel: v for v in job.variants}
            front, back, sleeve = made.get("front"), made.get("back"), made.get("sleeve")
            model = resolved.model.label
            if opts.reference_check and (front is not None or sleeve is not None):  # a back alone: nothing to label
                check = self._start_reference_check(job, opts, resolved, sleeve_reference or whole_reference)
            back_picture = back_reference if back_reference is not None else (reference if front is None else None)
            if back is not None and back_picture is not None:
                back_prompt = self._panel_prompt(opts, back, has_front_reference=False, back_picture=True)
                back_task = asyncio.create_task(
                    self._run_variant(job, back, resolved, back_prompt, [back_picture], opts.seed, opts.print_long_edge),
                    name=f"back-panel-{job.id}",
                )
            anchor = None  # the finished body panel the sleeve is matched to
            if front is not None:
                self._save(job, f"Drawing the front and back panels with {model}" if back_task else f"Drawing the front panel with {model}")
                prompt = self._panel_prompt(opts, front, has_front_reference=False)
                await self._run_variant(job, front, resolved, prompt, [reference], opts.seed, opts.print_long_edge)
                anchor = front
            elif back_task is not None:
                self._save(job, f"Drawing the back panel with {model}")
                if sleeve is not None:
                    await back_task
                    anchor = back
            anchor_panel = None
            if sleeve is not None and anchor is not None:
                raw = self.store.read_file(job.id, anchor.raw) if anchor.status == "succeeded" and anchor.raw else None
                if raw is not None:
                    anchor_img = await asyncio.to_thread(imgio.decode_model_output, raw)
                    data = await asyncio.to_thread(
                        imgio.prepare_model_input, anchor_img, max_long_edge=self.settings.model_input_long_edge
                    )
                    anchor_panel = InputImage(data=data, mime_type=imgio.image_mime(data), label="current_design", source=anchor.raw)
                elif anchor is back:
                    job.warnings.append(BACK_FAILED_SLEEVE)
                else:
                    derived = back is not None and back_task is None
                    job.warnings.append(FRONT_FAILED_SLEEVE_AND_BACK if derived else FRONT_FAILED_SLEEVE)
            visibility: dict[str, Visibility] = {}
            if check is not None:
                if not check.done():
                    self._save(job, "Checking which parts the reference shows")
                analysis = await check  # bounded: the check has its own provider timeout
                if analysis is not None:
                    only = ("sleeve",) if sleeve_reference is not None else None  # it saw the sleeve's picture only
                    visibility = self._apply_reference_analysis(job, analysis, only=only)

            work: list[Awaitable[None]] = []
            if sleeve is not None:
                own = sleeve_reference is not None  # its own picture replaces the whole picture and the back picture
                images = [img for img in ((sleeve_reference if own else whole_reference), anchor_panel,
                                          None if own else back_reference) if img is not None]
                prompt = self._panel_prompt(
                    opts, sleeve, has_front_reference=anchor_panel is not None, visibility=visibility.get("sleeve"),
                    sleeve_back_picture=back_reference is not None and not own,
                    anchor=anchor.panel if anchor_panel else "front", sleeve_picture=own,
                )
                work.append(self._run_variant(job, sleeve, resolved, prompt, images, opts.seed, opts.print_long_edge))
            if back_task is not None:
                work.append(back_task)
            elif back is not None:
                work.append(self._derive_back(job, front, back, opts))
            if sleeve is not None:
                if back_task is not None and not back_task.done():
                    self._save(job, "Drawing the back and sleeve panels")
                elif back is not None and back_task is None:
                    self._save(job, "Drawing the sleeve panel; the back is made from the front")
                else:
                    self._save(job, f"Drawing the sleeve panel with {model}")
            elif back_task is not None and not back_task.done():
                self._save(job, f"Drawing the back panel with {model}")
            elif back is not None and back_task is None:
                self._save(job, "Making the back from the front")
            await asyncio.gather(*work)
            if back_task is not None and back.status == "succeeded":
                back.source = "reference"  # drawn from the client's own picture
            self._finish(job)
            self._prepare_downloads(job)
        except asyncio.CancelledError:
            await _stop(check, back_task)  # their calls are logged before the job is saved as failed
            self._fail(job, "Cancelled because the server is shutting down.")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("panel job %s failed", job.id)
            await _stop(check, back_task)
            self._fail(job, f"Unexpected error: {exc}")
        finally:
            if check is not None:
                check.cancel()  # no-op once finished; stops a check left running by an error
            if back_task is not None:
                back_task.cancel()  # likewise for a back drawn alongside the front

    def _start_reference_check(
        self, job: Job, opts: RecreateOptions, resolved: _Resolved, reference: InputImage
    ) -> asyncio.Task[ReferenceAnalysis | None] | None:
        """Start the AI reference check (which parts the reference shows); without a text model, only warn.
        Demo jobs (offline placeholder images) skip it, so they never pay for a text model call."""
        if resolved.provider.name == "mock":
            return None
        writer = self.registry.get_brief_writer()  # None when brief_writer is "off" or no key is set
        if writer is None:
            job.warnings.append("Guessed-part labels need the AI designer (Claude or OpenAI text model).")
            return None
        try:
            prompt = build_reference_check_prompt(opts)
        except Exception as exc:  # noqa: BLE001 - e.g. an edited template; the check is optional
            log.exception("reference check prompt for job %s failed", job.id)
            job.warnings.append(f"AI reference check failed ({exc}); the panels have no guessed-part labels.")
            return None
        request = BriefRequest(system_prompt=prompt.system, user_prompt=prompt.user, images=[reference])
        job.brief_writer = f"{writer.name}:{writer.model}"
        job.brief_prompt = PromptRecord(system=prompt.system, user=prompt.user)
        log.info("job %s: reference check with %s", job.id, job.brief_writer)
        return asyncio.create_task(self._check_reference(job, writer, request), name=f"reference-check-{job.id}")

    async def _check_reference(self, job: Job, writer: BriefWriter, request: BriefRequest) -> ReferenceAnalysis | None:
        """The reference check, bounded by REFERENCE_CHECK_TIMEOUT_S (never more than the provider timeout), so
        a stuck text API holds the other panels back for a minute at most. A failure adds a warning and
        returns None."""
        timeout = min(self.settings.provider_timeout_s, REFERENCE_CHECK_TIMEOUT_S)
        try:
            return await self._call_writer(job, "reference_check", writer, request, timeout)
        except ProviderError as exc:
            reason = exc.message
        except TimeoutError:
            reason = "timed out"
        except Exception:  # noqa: BLE001 - the check is optional; the panels are drawn either way
            log.exception("reference check for job %s crashed", job.id)
            reason = "unexpected error"
        log.warning("reference check for job %s failed: %s", job.id, reason)
        job.warnings.append(f"AI reference check failed ({reason}); the panels have no guessed-part labels.")
        return None

    @staticmethod
    def _apply_reference_analysis(
        job: Job, analysis: ReferenceAnalysis, only: tuple[str, ...] | None = None
    ) -> dict[str, Visibility]:
        """Store the check, label each panel with its source and return part -> visibility for the prompts.
        The back is labelled by how it is made (from the front, or from the client's back picture) instead.
        `only`: label just these panels (the check saw a picture of those only)."""
        job.reference_analysis = SavedReferenceAnalysis.model_validate(analysis.model_dump())
        found: dict[str, PartVisibility] = {}
        for item in analysis.parts:
            found.setdefault(item.part, item)  # the first entry wins if a part is repeated
        for v in job.variants:
            item = found.get(v.panel or "")
            if item is not None and v.panel != "back" and (only is None or v.panel in only):
                v.source = PANEL_SOURCES[item.visibility]
                v.source_note = " ".join(item.note.split()) or None
        missing = [v.panel for v in job.variants if v.panel not in found]
        if missing:
            log.warning("reference check for job %s did not cover %s", job.id, missing)
        return {part: item.visibility for part, item in found.items()}

    @staticmethod
    def _panel_prompt(
        opts: RecreateOptions,
        variant: VariantInfo,
        *,
        has_front_reference: bool,
        visibility: Visibility | None = None,
        back_picture: bool = False,
        sleeve_back_picture: bool = False,
        anchor: str = "front",
        sleeve_picture: bool = False,
    ) -> PromptPair:
        prompt = build_panel_prompt(
            opts,
            panel=variant.panel or "front",
            aspect_ratio=variant.aspect_ratio or panel_ratio("front"),
            has_front_reference=has_front_reference,
            visibility=visibility,
            back_picture=back_picture,
            sleeve_back_picture=sleeve_back_picture,
            anchor=anchor,
            sleeve_picture=sleeve_picture,
        )
        variant.prompt = PromptRecord(system=prompt.system, user=prompt.user)
        return prompt

    async def _derive_back(self, job: Job, front: VariantInfo, back: VariantInfo, opts: RecreateOptions) -> None:
        """The back panel made from the finished front panel, mirrored or copied: no image call."""
        raw = self.store.read_file(job.id, front.raw) if front.status == "succeeded" and front.raw else None
        if raw is None:
            back.status, back.error = "failed", "The front panel failed, so there was nothing to make the back from."
        else:
            how = _back_method(opts, job)  # only now: its warning would be wrong for a back that was never made
            try:
                async with self._cpu_sem:
                    info = await asyncio.to_thread(self._derive_back_sync, job.id, back, raw, how)
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s: making the back from the front failed", job.id)
                back.status, back.error = "failed", f"Could not make the back from the front: {exc}"
            else:
                for key, value in info.items():
                    setattr(back, key, value)
                back.status, back.source, back.source_note, back.latency_s = "succeeded", how, None, 0.0
        done = sum(1 for v in job.variants if v.status != "pending")
        self._save(job, f"{done}/{len(job.variants)} variant(s) finished")

    def _derive_back_sync(self, job_id: str, back: VariantInfo, front_raw: bytes, how: PanelSource) -> dict:
        img = imgio.decode_model_output(front_raw)
        if how == "mirror":
            img = ImageOps.mirror(img)
        return self._postprocess_image(job_id, file_stem(back), img, 0, self._print_size(back))

    def _print_size(self, variant: VariantInfo) -> tuple[int, int] | None:
        """Pixel size of a panel at its print size and print_dpi (25 x 34 in at 300 dpi = 7500 x 10200 px)."""
        size = PANEL_PRINT_IN.get(variant.panel or "")
        if size is None:
            return None
        return size[0] * self.settings.print_dpi, size[1] * self.settings.print_dpi

    def _finish(self, job: Job) -> None:
        ok = [v for v in job.variants if v.status == "succeeded"]
        if len(ok) == len(job.variants):
            job.status = JobStatus.SUCCEEDED
            stage = "Done"
        elif ok:
            job.status = JobStatus.PARTIAL
            stage = "Done (some images failed)"
        else:
            job.status = JobStatus.FAILED
            job.error = next((v.error for v in job.variants if v.error), "Generation failed.")
            stage = "Failed"
        self._save(job, stage)

    def _fail(self, job: Job, message: str) -> None:
        job.status = JobStatus.FAILED
        job.error = message
        for v in job.variants:
            if v.status == "pending":
                v.status = "failed"
                v.error = message
        self._save(job, "Failed")

    async def _run_variant(
        self,
        job: Job,
        variant: VariantInfo,
        resolved: _Resolved,
        prompt: PromptPair,
        images: list[InputImage],
        seed: int | None,
        print_long_edge: int,
    ) -> None:
        request = ImageGenRequest(
            system_prompt=prompt.system,
            user_prompt=prompt.user,
            images=images,
            aspect_ratio=variant.aspect_ratio or resolved.aspect_ratio,
            resolution=resolved.resolution,
            background=resolved.background,  # type: ignore[arg-type]
            seed=None if seed is None else (seed + variant.index - 1) % (2**31),
            model=resolved.model.id,
        )
        inputs = await asyncio.to_thread(_describe_images, images)
        try:
            result = await self._generate_with_retry(job, variant, resolved.provider, request, inputs)
            async with self._cpu_sem:
                info = await asyncio.to_thread(
                    self._postprocess, job.id, file_stem(variant), result, print_long_edge, self._print_size(variant)
                )
        except ProviderError as exc:
            variant.status = "failed"
            variant.error = exc.message
            log.warning("job %s variant %s failed: %s", job.id, variant.index, exc)
        except Exception as exc:  # noqa: BLE001 - e.g. undecodable image bytes
            variant.status = "failed"
            variant.error = f"Could not process the generated image: {exc}"
            log.exception("job %s variant %s post-processing failed", job.id, variant.index)
        else:
            for key, value in info.items():
                setattr(variant, key, value)
            variant.model_text = result.text
            variant.latency_s = round(result.latency_s, 2)
            variant.status = "succeeded"
        done = sum(1 for v in job.variants if v.status != "pending")
        self._save(job, f"{done}/{len(job.variants)} variant(s) finished")

    async def _generate_with_retry(
        self, job: Job, variant: VariantInfo, provider: ImageProvider, request: ImageGenRequest, inputs: list[dict]
    ) -> ImageGenResult:
        """The image call, retried after a retryable error; every attempt goes into the job's call log."""
        attempt = 0
        while True:
            attempt += 1
            async with self._gen_sem:
                request.meta.clear()  # the provider fills it just before the request goes out
                started, clock = utcnow_iso(), time.perf_counter()
                key = self._call_started(job, "image", provider.name, request.model or provider.default_model(),
                                         started, variant=variant, attempt=attempt)
                try:
                    result = await asyncio.wait_for(provider.generate(request), timeout=self.settings.provider_timeout_s)
                except asyncio.TimeoutError as exc:
                    error = ProviderError(
                        "The image model took too long to respond.", kind="timeout", provider=provider.name
                    )
                    self._log_image_call(job, variant, provider, request, inputs, attempt, started, clock, key, error=error)
                    raise error from exc
                except ProviderError as exc:
                    self._log_image_call(job, variant, provider, request, inputs, attempt, started, clock, key, error=exc)
                    error = exc
                except BaseException as exc:  # cancelled, or a bug: logged too, since the request may have gone out
                    self._log_image_call(job, variant, provider, request, inputs, attempt, started, clock, key, error=exc)
                    raise
                else:
                    self._log_image_call(job, variant, provider, request, inputs, attempt, started, clock, key, result=result)
                    return result
            if not error.retryable or attempt > len(RETRY_DELAYS_S):
                raise error
            await asyncio.sleep(RETRY_DELAYS_S[attempt - 1])

    # ------------------------------------------------------------------
    # the API call log: every paid call, with what it cost
    # ------------------------------------------------------------------

    def _log_image_call(
        self,
        job: Job,
        variant: VariantInfo,
        provider: ImageProvider,
        request: ImageGenRequest,
        inputs: list[dict],
        attempt: int,
        started: str,
        clock: float,
        key: str,
        *,
        result: ImageGenResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        model = (result.model if result is not None else None) or request.model or provider.default_model()
        # what was sent: from the reply, the error, or what the provider published before sending (a timeout)
        meta = result.request_meta if result is not None else getattr(error, "request_meta", None) or request.meta
        record = {
            "purpose": "image",
            "trigger": _CALL_TRIGGER.get(),
            "variant": variant.index,
            "panel": variant.panel,
            "label": variant.label,
            "attempt": attempt,
            "provider": provider.name,
            "model": model,
            "endpoint": (meta or {}).get("endpoint"),
            "started_at": started,
            "finished_at": utcnow_iso(),
            "latency_s": round(time.perf_counter() - clock, 2),
            "params": dict(meta or {}),
            "prompt": {"system": request.system_prompt, "user": request.user_prompt},
            "inputs": inputs,
        }
        if result is not None:
            record.update(
                status="ok", request_id=result.request_id, usage=result.usage or None,
                output={"saved_as": f"{file_stem(variant)}_raw.png", **_describe_bytes(result.data, result.mime_type)},
            )
        else:
            record.update(
                status="error", request_id=getattr(error, "request_id", None),
                usage=getattr(error, "usage", None) or None, error=_error_record(error),
            )
        self._log_call(job, record, variant, sent=bool(meta) and not getattr(error, "not_sent", False), key=key)

    async def _call_writer(
        self, job: Job, purpose: str, writer: BriefWriter, request: BriefRequest, timeout: float
    ) -> BriefResponse | ReferenceAnalysis:
        """One AI-designer call (purpose "brief" or "reference_check"), logged like the image calls."""
        method = writer.analyze_reference if purpose == "reference_check" else writer.write
        inputs = await asyncio.to_thread(_describe_images, request.images)
        started, clock = utcnow_iso(), time.perf_counter()
        key = self._call_started(job, purpose, writer.name, writer.model, started)
        try:
            result = await asyncio.wait_for(method(request), timeout=timeout)
        except BaseException as exc:  # logged, then handled by the caller as before
            self._log_text_call(job, purpose, writer, request, inputs, started, clock, key, error=exc)
            raise
        self._log_text_call(job, purpose, writer, request, inputs, started, clock, key, result=result)
        return result

    def _log_text_call(
        self,
        job: Job,
        purpose: str,
        writer: BriefWriter,
        request: BriefRequest,
        inputs: list[dict],
        started: str,
        clock: float,
        key: str,
        *,
        result: BriefResponse | ReferenceAnalysis | None = None,
        error: BaseException | None = None,
    ) -> None:
        meta = request.meta  # filled by the writer: endpoint, params, then model, usage, request_id, stop_reason
        record = {
            "purpose": purpose,
            "trigger": _CALL_TRIGGER.get(),
            "attempt": 1,
            "provider": writer.name,
            "model": meta.get("model") or writer.model,  # the model that answered (Claude may fall back)
            "requested_model": writer.model,
            "endpoint": meta.get("endpoint"),
            "started_at": started,
            "finished_at": utcnow_iso(),
            "latency_s": round(time.perf_counter() - clock, 2),
            "params": dict(meta.get("params") or {}),
            "prompt": {"system": request.system_prompt, "user": request.user_prompt},
            "inputs": inputs,
            "stop_reason": meta.get("stop_reason"),
            "stop_category": meta.get("stop_category"),
            "usage": meta.get("usage") or None,
            "request_id": meta.get("request_id") or getattr(error, "request_id", None),
        }
        if error is None:
            record.update(status="ok", output=result.model_dump(mode="json") if result is not None else None)
        else:
            record.update(status="error", error=_error_record(error))
        self._log_call(job, record, sent="endpoint" in meta and not getattr(error, "not_sent", False), key=key)

    def _call_started(
        self, job: Job, purpose: str, provider: str, model: str | None, started: str, *,
        variant: VariantInfo | None = None, attempt: int = 1,
    ) -> str:
        """Note a call that is about to go out, saved at once: if the server is killed before the call is logged,
        the next start-up logs it as interrupted (recover_interrupted). Returns its key for _log_call."""
        key = f"{started}-{purpose}-{variant.index if variant else 0}-{attempt}-{len(job.calls_in_flight)}"
        job.calls_in_flight.append({
            "key": key, "purpose": purpose, "trigger": _CALL_TRIGGER.get(), "attempt": attempt,
            "variant": variant.index if variant else None, "panel": variant.panel if variant else None,
            "label": variant.label if variant else None, "provider": provider, "model": model, "started_at": started,
        })
        self._save(job)
        return key

    def _log_call(
        self, job: Job, record: dict, variant: VariantInfo | None = None, *, sent: bool = True, key: str | None = None
    ) -> None:
        """Price one call, add it to the job's (and image's) totals, append it to the job's call log and save the
        job, so the totals never lag the log. Runs on the event-loop thread, like every job mutation."""
        error = record.get("error") or {}
        if record["status"] == "ok":
            outcome = "ok"
        elif error.get("status_code") is not None and not error.get("billing_unknown"):
            outcome = "error_reply"
        else:
            outcome = "no_reply" if sent else "not_sent"
        cost, price = pricing.call_cost(
            record["provider"], record["model"], record.get("usage"), outcome=outcome,
            stop_reason=record.get("stop_reason"), stop_category=record.get("stop_category"),
            requested_model=record.get("requested_model"), api_host=(record.get("params") or {}).get("api_host"),
        )
        record.update(cost_usd=cost, price=price)
        if job.cost_usd is None and any(v.status == "succeeded" for v in job.variants):
            job.cost_partial = True  # an older job: the images it made before the log have no recorded cost
        job.api_calls += 1
        job.cost_usd = round((job.cost_usd or 0.0) + (cost or 0.0), 6)
        if cost is None:
            job.uncosted_calls += 1
        if variant is not None:
            variant.api_calls += 1
            variant.cost_usd = round((variant.cost_usd or 0.0) + (cost or 0.0), 6)
            if cost is None:
                variant.uncosted_calls += 1
        job.calls_in_flight = [c for c in job.calls_in_flight if c.get("key") != key]
        full = {"n": job.api_calls, "job_id": job.id, **record}
        line = json.dumps(full, ensure_ascii=False, default=str)
        try:
            self.store.append_line(job.id, CALL_LOG, line)
        except (OSError, ValueError):
            log.exception("job %s: could not write the API call log", job.id)
        usage.append(self.usage_ledger, usage.ledger_line(full, job))  # the same call, without prompts or pictures
        self._save(job)

    def _recount_calls(self, job: Job) -> None:
        """The job's call totals rebuilt from its log, the source of truth (after a server was killed, job.json may
        lag the log). Jobs without a log keep theirs."""
        calls = self.call_log(job.id)
        if not calls:
            return
        job.api_calls = max(len(calls), max((c.get("n") or 0) for c in calls))
        job.cost_usd = round(sum(c.get("cost_usd") or 0.0 for c in calls), 6)
        job.uncosted_calls = sum(1 for c in calls if c.get("cost_usd") is None)
        for v in job.variants:
            mine = [c for c in calls if c.get("purpose") == "image" and c.get("variant") == v.index]
            if mine:
                v.api_calls = len(mine)
                v.cost_usd = round(sum(c.get("cost_usd") or 0.0 for c in mine), 6)
                v.uncosted_calls = sum(1 for c in mine if c.get("cost_usd") is None)

    def _postprocess(
        self, job_id: str, stem: str, result: ImageGenResult, print_long_edge: int, print_size: tuple[int, int] | None
    ) -> dict:
        """CPU work for one finished variant. Runs in a worker thread; must not touch the Job."""
        return self._postprocess_image(job_id, stem, imgio.decode_model_output(result.data), print_long_edge, print_size)

    def _postprocess_image(
        self, job_id: str, stem: str, img: Image.Image, print_long_edge: int, print_size: tuple[int, int] | None
    ) -> dict:
        """Write <stem>_raw.png (the model output), <stem>.png (the final print file) and the preview. Other jobs
        are upscaled to `print_long_edge`, if set. A panel's <stem>.png is its exact print size (`print_size`): it is
        only recorded here and made on its first download (ensure_print_png), since each is 40-100 MB."""
        dpi = self.settings.print_dpi
        raw_name, png_name, preview_name = f"{stem}_raw.png", f"{stem}.png", f"{stem}_preview.webp"
        raw_png = imgio.to_png_bytes(img, dpi=dpi)
        self.store.write_file(job_id, raw_name, raw_png)
        if print_size:
            width, height = print_size
        else:
            final = upscale_to_long_edge(img, print_long_edge) if print_long_edge else img
            self.store.write_file(job_id, png_name, raw_png if final is img else imgio.to_png_bytes(final, dpi=dpi))
            width, height = final.size
        self.store.write_file(job_id, preview_name, imgio.make_preview(img))
        return {
            "raw": raw_name,
            "png": png_name,
            "preview": preview_name,
            "width": width,
            "height": height,
            "raw_width": img.width,
            "raw_height": img.height,
            "palette": dominant_colors(img),
        }

    # ------------------------------------------------------------------
    # vector export
    # ------------------------------------------------------------------

    async def vectorize(self, job_id: str, index: int, req: VectorizeRequest) -> VectorAsset:
        job = self.get(job_id)
        if job is None:
            raise JobError("Job not found.", 404)
        if index < 1 or index > len(job.variants):
            raise JobError("Variant not found.", 404)
        variant = job.variants[index - 1]
        if variant.status != "succeeded":
            raise JobError("This variant has no image yet.", 409)
        key = req.preset if req.colors is None or req.preset == "raster" else f"{req.preset}-{req.colors}"
        existing = variant.vectors.get(key)
        if self._cached(job_id, existing):
            return existing
        lock = self._vector_locks.setdefault((job_id, index, key), asyncio.Lock())
        async with lock:
            existing = variant.vectors.get(key)
            if self._cached(job_id, existing):
                return existing
            async with self._cpu_sem:
                asset = await asyncio.to_thread(self._vectorize_sync, job_id, variant, req, key)
        job = self.get(job_id)  # the same cached object unless evicted meanwhile
        if job is None:  # deleted while the export ran: do not write it back
            raise JobError("Job not found.", 404)
        job.variants[index - 1].vectors[key] = asset
        self._remember(job)
        self._save(job)
        return asset

    # ------------------------------------------------------------------
    # one failed image, generated again
    # ------------------------------------------------------------------

    async def retry_variant(self, job_id: str, index: int) -> Job:
        """Generate one failed image of a finished Recreate job again (e.g. after a network error), with the job's
        own pictures, model and settings. A front made again also lets a back made from it follow (no image call)."""
        job = self.get(job_id)
        if job is None:
            raise JobError("Job not found.", 404)
        if index < 1 or index > len(job.variants):
            raise JobError("Variant not found.", 404)
        if job.kind != JobKind.RECREATE:
            raise JobError("Only images made from a reference can be generated again here.", 409)
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING) or job.id in self._running:
            raise JobError("This job is still running.", 409)
        variant = job.variants[index - 1]
        if variant.status != "failed":
            raise JobError("Only a failed image can be generated again.", 409)
        old = "This job was made by an older version of the app, so it cannot be generated again. Make it again instead."
        if variant.panel and (variant.panel not in PANEL_PRINT_IN or variant.aspect_ratio != panel_ratio(variant.panel)):
            raise JobError(old, 409)  # shaped panels (3:4, 1:1) or a 4-/5-panel set: today's prompts would not match
        try:
            opts = RecreateOptions.model_validate(job.options)
        except ValidationError as exc:
            raise JobError(old, 409) from exc
        derived_back = variant.panel == "back" and derives_back(opts)
        front = next((v for v in job.variants if v.panel == "front"), None)
        if derived_back and (front is None or front.status != "succeeded"):
            raise JobError("Generate the front panel again first: the back is made from it.", 409)
        resolved = self._resolve(
            job.provider, job.model, job.aspect_ratio or "auto", 1.0, job.resolution or opts.resolution,
            "white" if opts.mode in (RecreateMode.PATTERN, RecreateMode.PANELS) else opts.background,
            image_count=3 if variant.panel else 1,
        )
        work = None if derived_back else await self._retry_inputs(job, variant, opts)  # checks the stored pictures
        variant.status, variant.error = "pending", None
        job.status, job.error = JobStatus.RUNNING, None
        self._save(job, f"Generating the {variant.label.lower()} again" if variant.label else f"Generating variant {index} again")
        self._spawn(job, self._retry(job, variant, opts, resolved, work))
        return job

    async def _retry_inputs(
        self, job: Job, variant: VariantInfo, opts: RecreateOptions
    ) -> tuple[list[InputImage], PromptPair]:
        """The pictures and prompt for generating `variant` again, built as the job first built them."""

        async def stored(name: str | None, label: str) -> InputImage | None:
            data = self.store.read_file(job.id, name) if name else None
            if data is None:
                return None
            img = await asyncio.to_thread(imgio.decode_model_output, data)
            prepared = await asyncio.to_thread(
                imgio.prepare_model_input, img, max_long_edge=self.settings.model_input_long_edge
            )
            return InputImage(data=prepared, mime_type=imgio.image_mime(prepared), label=label, source=name)

        reference = await stored("input_1.png", "reference")
        if reference is None:
            raise JobError("The reference image of this job is missing on disk.", 404)
        if not variant.panel:  # pattern / flat jersey / replica: the job's own prompt again
            record = variant.prompt or job.prompt
            if record is None:
                raise JobError("The prompt of this image is missing.", 404)
            return [reference], PromptPair(system=record.system, user=record.user)
        if variant.panel == "front":
            return [reference], self._panel_prompt(opts, variant, has_front_reference=False)
        back_ref = await stored("input_2.png", "back_reference") if uses_back_picture(opts) else None
        if variant.panel == "back":  # drawn from a picture (a derived back never gets here)
            picture = back_ref if uses_back_picture(opts) else reference  # without the front: the reference itself
            if picture is None:
                raise JobError("The back picture of this job is missing on disk.", 404)
            return [picture], self._panel_prompt(opts, variant, has_front_reference=False, back_picture=True)
        # the sleeve sees the whole upload (a sleeve asked for alone: the box the client drew), and the finished
        # front panel, or, without the front, the back panel
        own = "input_3.png" in job.inputs  # the client's own sleeve picture (its box)
        if own:
            whole = await stored("input_3.png", "sleeve_reference")
            if whole is None:
                raise JobError("The sleeve picture of this job is missing on disk.", 404)
            back_ref = None
        else:
            whole = reference if opts.panels == ["sleeve"] else await stored("input_full.png", "reference") or reference
        anchor = next((v for v in job.variants if v.panel == ("front" if "front" in opts.panels else "back")), None)
        anchor_panel = None
        if anchor is not None and anchor is not variant and anchor.status == "succeeded":
            anchor_panel = await stored(anchor.raw, "current_design")
        parts = job.reference_analysis.parts if job.reference_analysis else []
        visibility = next((p.visibility for p in parts if p.part == "sleeve"), None)
        images = [img for img in (whole, anchor_panel, back_ref) if img is not None]
        prompt = self._panel_prompt(
            opts, variant, has_front_reference=anchor_panel is not None, visibility=visibility,
            sleeve_back_picture=back_ref is not None, anchor=anchor.panel if anchor_panel else "front",
            sleeve_picture=own,
        )
        return images, prompt

    async def _retry(
        self,
        job: Job,
        variant: VariantInfo,
        opts: RecreateOptions,
        resolved: _Resolved,
        work: tuple[list[InputImage], PromptPair] | None,
    ) -> None:
        _CALL_TRIGGER.set("retry")  # this task's API calls are logged as a manual retry
        try:
            back = next((v for v in job.variants if v.panel == "back"), None)
            if work is None:  # a back made from the front again
                front = next(v for v in job.variants if v.panel == "front")
                await self._derive_back(job, front, variant, opts)
            else:
                images, prompt = work
                await self._run_variant(job, variant, resolved, prompt, images, opts.seed, opts.print_long_edge)
                if variant.panel == "back" and variant.status == "succeeded":
                    variant.source = "reference"  # drawn from the client's own back picture, as in _run_panels
                if (
                    variant.panel == "front" and variant.status == "succeeded"
                    and back is not None and back.status == "failed" and derives_back(opts)
                ):  # that back failed only for want of a front
                    back.status, back.error = "pending", None
                    self._save(job, "Making the back from the front")
                    await self._derive_back(job, variant, back, opts)
                    if back.status == "succeeded":  # the back exists now; the sleeve was still drawn without a front
                        job.warnings = [
                            FRONT_FAILED_SLEEVE if w == FRONT_FAILED_SLEEVE_AND_BACK else w for w in job.warnings
                        ]
            self._finish(job)
            self._prepare_downloads(job)
        except asyncio.CancelledError:
            for v in job.variants:  # the job's other images stay; this one can be generated again later
                if v.status == "pending":
                    v.status, v.error = "failed", "Cancelled because the server is shutting down."
            self._finish(job)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s: generating variant %s again failed", job.id, variant.index)
            variant.status, variant.error = "failed", f"Unexpected error: {exc}"
            self._finish(job)

    def _prepare_downloads(self, job: Job) -> None:
        """prepare_downloads: start making a finished panel job's print PNGs, then its detailed EPS, in the
        background. They go through the same calls and locks as the download buttons, so a click on something being
        made waits for it instead of making it twice, and one heavy step runs at a time (the CPU semaphore): a
        client's own request waits at most for the step under way. Only today's panels (a print size) qualify."""
        if not self.settings.prepare_downloads or job.status not in (JobStatus.SUCCEEDED, JobStatus.PARTIAL):
            return
        indexes = [v.index for v in job.variants if v.status == "succeeded" and self._print_size(v)]
        if not indexes:
            return
        task = asyncio.create_task(self._prepare(job.id, indexes), name=f"prepare-{job.id}")
        self._prep_tasks.add(task)
        task.add_done_callback(self._prep_tasks.discard)

    async def _prepare(self, job_id: str, indexes: list[int]) -> None:
        try:
            for step in ("png", "eps"):  # every print PNG first: they are quick and the PNG ZIP needs them all
                for index in indexes:
                    job = self.get(job_id)
                    if job is None:  # deleted meanwhile
                        return
                    v = job.variants[index - 1]
                    if v.status != "succeeded":  # e.g. being generated again
                        continue
                    if step == "png":
                        await self.ensure_print_png(job_id, v.png or "")
                    else:
                        await self.vectorize(job_id, index, VectorizeRequest(preset=PREPARED_PRESET))
            log.info("job %s: downloads prepared", job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - only a head start; the buttons still make them on demand
            log.exception("job %s: preparing the downloads failed", job_id)

    def call_log(self, job_id: str) -> list[dict]:
        """The job's paid API calls, oldest first (its api_calls.jsonl); [] for jobs made before the log existed."""
        data = self.store.read_file(job_id, CALL_LOG)
        calls = []
        for number, line in enumerate((data or b"").decode("utf-8", "replace").split("\n"), start=1):
            if not line.strip():
                continue
            try:
                calls.append(json.loads(line))
            except ValueError:  # a line cut short (e.g. a full disk): keep the rest readable
                log.warning("job %s: unreadable line %d in %s", job_id, number, CALL_LOG)
        return calls

    async def ensure_print_png(self, job_id: str, filename: str) -> bool:
        """Make a panel's print-size PNG (e.g. front.png, 7500 x 10200 px) from its <stem>_raw.png on first request.
        True when `filename` is such a file and it now exists; False for any other file."""
        job = self.get(job_id)
        variant = next((v for v in job.variants if v.png == filename and v.raw), None) if job else None
        size = self._print_size(variant) if variant and variant.status == "succeeded" else None
        if size is None:
            return False
        lock = self._vector_locks.setdefault((job_id, variant.index, "print-png"), asyncio.Lock())
        async with lock:
            if self.store.file_path(job_id, filename):
                return True
            async with self._cpu_sem:
                return await asyncio.to_thread(self._write_print_png, job_id, variant, size)

    def _write_print_png(self, job_id: str, variant: VariantInfo, size: tuple[int, int]) -> bool:
        data = self.store.read_file(job_id, variant.raw or "")
        if data is None:  # the model output is gone, so there is nothing to make it from (the route answers 404)
            return False
        img = upscale_to_size(imgio.decode_model_output(data), size)
        dpi = self.settings.print_dpi  # written straight to disk: the encoded PNG is not held in memory as well
        try:
            self.store.write_image(job_id, variant.png or "", img, format="PNG", dpi=(dpi, dpi), compress_level=3)
        except FileNotFoundError:  # the job was deleted while its PNG was being made
            return False
        return True

    async def panel_eps_zip(self, job_id: str, preset: str) -> tuple[str, bytes]:
        """(download name, ZIP bytes): the EPS of every panel of a panel job at one preset (a vector preset, or
        raster), plus one EPS with the front, back and sleeve together (combined_layout). Panels without that export
        get it first, exactly as the per-panel export button would make it."""
        job = self.get(job_id)
        if job is None:
            raise JobError("Job not found.", 404)
        panels = [v for v in job.variants if v.panel]
        if not panels:
            raise JobError("This job has no production panels.", 409)
        if any(v.status != "succeeded" for v in panels):
            raise JobError("Not all panels are ready yet.", 409)
        req = VectorizeRequest(preset=preset)
        assets = await asyncio.gather(*(self.vectorize(job_id, v.index, req) for v in panels))
        names = {v.panel: asset.eps for v, asset in zip(panels, assets)}
        # jobs from 2026-11-01 (left and right sleeve, collar) have no single sleeve panel, so no combined EPS
        combined = [names[p] for p in COMBINED_PANELS] if all(p in names for p in COMBINED_PANELS) else None
        data = await asyncio.to_thread(self._zip_files, job_id, list(names.values()), combined, preset)
        return f"jersey-{job_id}-panels-eps-{preset}.zip", data

    def _zip_files(self, job_id: str, names: list[str], combined: list[str] | None, preset: str) -> bytes:
        """The panel EPS files under their download names and, when `combined` lists the front, back and sleeve
        files, those three on one page."""
        files: dict[str, bytes] = {}
        for name in names:
            data = self.store.read_file(job_id, name)
            if data is None:
                raise JobError("A panel's EPS file is missing on disk.", 404)
            files[name] = data
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(f"jersey-{job_id}-{name}", data)  # same names as the single EPS downloads
            if combined:
                parts = [files[name] for name in combined]
                try:
                    width, height, corners = combined_layout([eps_size(eps) for eps in parts])
                    sheet = combine_eps(
                        [(eps, x, y) for eps, (x, y) in zip(parts, corners)], width_pt=width, height_pt=height,
                        title=f"jersey-{job_id}-combined-panels",
                    )
                except ValueError as exc:
                    log.exception("job %s: combining %s into one EPS failed", job_id, combined)
                    raise JobError(f"Could not put the panels together in one EPS: {exc}", 500) from exc
                zf.writestr(f"jersey-{job_id}-combined_panels_{preset}.eps", sheet)
        return buf.getvalue()

    async def panel_png_zip(self, job_id: str, size: str) -> tuple[str, Path]:
        """(download name, a temporary ZIP file the caller deletes once sent): the PNG of every panel of a panel job,
        at its print size ("print": the PNG download, made first where missing) or exactly as the model returned it
        ("original"). The PNGs are stored as they are, not compressed again."""
        job = self.get(job_id)
        if job is None:
            raise JobError("Job not found.", 404)
        panels = [v for v in job.variants if v.panel]
        if not panels:
            raise JobError("This job has no production panels.", 409)
        if any(v.status != "succeeded" for v in panels):
            raise JobError("Not all panels are ready yet.", 409)
        if size == "print":
            for v in panels:  # one at a time: a print PNG takes about 1 GB of memory while it is made
                if not self.store.file_path(job_id, v.png or ""):
                    await self.ensure_print_png(job_id, v.png or "")
            names = [v.png or "" for v in panels]
        else:
            names = [v.raw if v.raw and self.store.file_path(job_id, v.raw) else v.png or "" for v in panels]
        paths = [self.store.file_path(job_id, name) for name in names]
        if not all(paths):
            raise JobError("A panel's PNG file is missing on disk.", 404)
        zipped = await asyncio.to_thread(self._zip_to_temp, [(path, f"jersey-{job_id}-{path.name}") for path in paths])
        return f"jersey-{job_id}-panels-png-{size}.zip", zipped

    @staticmethod
    def _zip_to_temp(files: list[tuple[Path, str]]) -> Path:
        fd, tmp = tempfile.mkstemp(prefix="jhs-panels-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as zf:
                for path, arcname in files:  # same names as the single PNG downloads
                    zf.write(path, arcname)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return Path(tmp)

    def _cached(self, job_id: str, asset: VectorAsset | None) -> bool:
        """An export that can be served again: its EPS exists with today's EPS prolog and, for vectors, it has colour
        layers. Exports made before the layered tracer (no layers or PDF), or by an EPS writer whose prolog has
        changed since (the panel ZIP's combined EPS needs today's), are made again under the same names."""
        path = self.store.file_path(job_id, asset.eps) if asset is not None else None
        if path is None:
            return False
        if asset.preset != "raster" and asset.layers is None:
            return False
        with path.open("rb") as fh:
            return is_current_eps(fh.read(4096))

    def _vectorize_sync(self, job_id: str, variant: VariantInfo, req: VectorizeRequest, key: str) -> VectorAsset:
        if not self.store.job_dir(job_id).is_dir():  # deleted meanwhile (e.g. while being prepared): no orphan files
            raise JobError("Job not found.", 404)
        dpi = self.settings.print_dpi
        width_pt, height_pt = physical_size_pt(variant.width or 1, variant.height or 1, dpi)
        stem = file_stem(variant)
        title = f"jersey-{job_id}-{stem}"
        if req.preset == "raster":
            # today's panels: the model output at 150 dpi on the print page, not the 76 MP print PNG (~250 MB EPS);
            # older shaped panels (other ratios) keep their own final PNG, like any other image
            current = variant.panel in PANEL_PRINT_IN and variant.aspect_ratio == panel_ratio(variant.panel)
            panel_in = PANEL_PRINT_IN[variant.panel] if current else None
            if panel_in:
                data = self.store.read_file(job_id, variant.raw or "")
            else:
                data = self.store.read_file(job_id, variant.png or "") or self.store.read_file(job_id, variant.raw or "")
            if data is None:
                raise JobError("The PNG for this variant is missing on disk.", 404)
            img = imgio.decode_model_output(data)
            if panel_in:
                img = upscale_to_size(img, (panel_in[0] * RASTER_EPS_PANEL_DPI, panel_in[1] * RASTER_EPS_PANEL_DPI))
            eps = raster_eps(img, width_pt=width_pt, height_pt=height_pt, title=title)
            eps_name = f"{stem}_raster.eps"
            self.store.write_file(job_id, eps_name, eps)
            return VectorAsset(preset="raster", eps=eps_name, eps_bytes=len(eps))
        data = self.store.read_file(job_id, variant.raw or "") or self.store.read_file(job_id, variant.png or "")
        if data is None:
            raise JobError("The image for this variant is missing on disk.", 404)
        try:
            art = trace_to_vector(imgio.decode_model_output(data), preset=req.preset, colors=req.colors)
        except ValueError as exc:  # e.g. a fully transparent image, or a pattern too fine to trace
            raise JobError(str(exc), 422) from exc
        files = {  # one compound path per colour layer in every format; EPS and PDF at the print size
            "svg": vector_to_svg(art, title=title).encode("utf-8"),
            "eps": vector_to_eps(art, width_pt=width_pt, height_pt=height_pt, title=title),
            "pdf": vector_to_pdf(art, width_pt=width_pt, height_pt=height_pt, title=title),
        }
        names = {ext: f"{stem}_{key}.{ext}" for ext in files}
        for ext, body in files.items():
            self.store.write_file(job_id, names[ext], body)
        return VectorAsset(
            preset=req.preset,
            eps=names["eps"],
            svg=names["svg"],
            pdf=names["pdf"],
            colors=len(art.layers),
            paths=art.subpath_count,
            layers=len(art.layers),
            anchors=art.anchor_count,
            eps_bytes=len(files["eps"]),
            pdf_bytes=len(files["pdf"]),
        )
