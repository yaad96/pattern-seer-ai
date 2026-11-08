"""FastAPI application: JSON API, file downloads and the single-page UI."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, ValidationError
from starlette.background import BackgroundTask

from app import catalog
from app.config import BASE_DIR, Settings, get_settings
from app.imaging.io import ImageInputError, load_upload
from app.pipeline import JobError, JobManager, uses_back_picture
from app.providers.registry import ProviderRegistry
from app.schemas import (
    DesignOptions,
    Job,
    JobCreated,
    JobKind,
    ParentRef,
    RecreateMode,
    RecreateOptions,
    VectorAsset,
    VectorizeRequest,
)
from app.security import (
    SESSION_COOKIE,
    SESSION_DAYS,
    RateLimiter,
    SecurityMiddleware,
    client_key,
    credentials_role,
    make_session,
)
from app.storage import SESSION_ID_RE, JobStore, is_valid_job_id

__version__ = "1.0.0"
STATIC_DIR = BASE_DIR / "static"

MEDIA_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".eps": "application/postscript",
    ".pdf": "application/pdf",
}

log = logging.getLogger("jersey.api")


class NoCacheStaticFiles(StaticFiles):
    """Static UI files are revalidated on every load so an updated app is never served stale."""

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class PromptPreviewRequest(BaseModel):
    kind: Literal["recreate", "design", "refine", "brief"]
    options: dict = {}
    message: str = ""
    input_ratio: float | None = None  # width / height of the uploaded reference (recreate)


class JobSummary(BaseModel):
    id: str
    kind: JobKind
    status: str
    stage: str
    created_at: str
    provider: str | None
    model: str | None
    message: str | None
    session_id: str | None
    preview: str | None  # filename of the first finished variant's preview
    variants: int
    succeeded: int
    cost_usd: float | None  # what the job's API calls cost; None = not recorded (jobs before 2026-11-03)
    uncosted_calls: int
    cost_partial: bool  # an older job with some calls recorded since (e.g. a retry)


def _summary(job: Job) -> JobSummary:
    done = [v for v in job.variants if v.status == "succeeded"]
    return JobSummary(
        id=job.id,
        kind=job.kind,
        status=job.status.value,
        stage=job.stage,
        created_at=job.created_at,
        provider=job.provider,
        model=job.model,
        message=(job.message[:160] if job.message else None),
        session_id=job.session_id,
        preview=done[0].preview if done else None,
        variants=len(job.variants),
        succeeded=len(done),
        cost_usd=job.cost_usd,
        uncosted_calls=job.uncosted_calls,
        cost_partial=job.cost_partial,
    )


LOGIN_PAGE = """<!doctype html>
<html lang="bn"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>সাইন ইন - জার্সি এইচডি স্টুডিও</title>
<style>
:root{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--text:#14161a;--muted:#5b6170;--line:#d9dce3;--accent:#2b59e8;--err:#b00020}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e25;--text:#eceef2;--muted:#9aa1b0;--line:#2c313b;--accent:#5b82ff;--err:#ff6b7f}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
form{width:100%;max-width:360px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 20px;color:var(--muted)}label{display:block;font-weight:600;margin:14px 0 6px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:22px;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--err);margin:12px 0 0;font-weight:600}
</style></head><body>
<form method="post" action="/login">
<h1>জার্সি এইচডি স্টুডিও</h1><p>চালিয়ে যেতে সাইন ইন করুন।</p>
<label for="u">ইউজারনেম</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="pw">পাসওয়ার্ড</label><input id="pw" name="password" type="password" autocomplete="current-password" required>
<input type="hidden" id="next" name="next" value="{next}">{error}
<button type="submit">সাইন ইন</button>
</form>
<script>var n=document.getElementById("next");if(location.hash&&n.value.indexOf("#")<0)n.value+=location.hash;</script>
</body></html>"""


# What the demo login is not shown of its own jobs: costs, models, prompts and the reference check's details.
DEMO_HIDDEN: dict = {
    **dict.fromkeys((
        "provider", "model", "prompt", "brief_prompt", "brief_writer", "reference_analysis",
        "cost_usd", "uncosted_calls", "cost_partial", "api_calls", "calls_in_flight",
    ), True),
    "variants": {"__all__": {"prompt", "model_text", "cost_usd", "uncosted_calls", "api_calls"}},
}


def is_demo(request: Request) -> bool:
    """The request comes from the demo login (SecurityMiddleware sets the role)."""
    return request.scope.get("jhs_role") == "demo"


def _safe_next(target: str) -> str:
    """Only allow redirects back into this app (blocks //evil.example and absolute URLs)."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


def render_login(next_path: str, error: str | None = None) -> str:
    message = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return LOGIN_PAGE.replace("{next}", html.escape(_safe_next(next_path), quote=True)).replace("{error}", message)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = JobStore(settings.output_dir)
    registry = ProviderRegistry(settings)
    manager = JobManager(settings, store, registry)
    limiter = RateLimiter(settings.rate_limit_per_hour)
    max_bytes = settings.max_upload_mb * 1024 * 1024

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for provider, status in settings.key_status.items():
            log.info("%s API key: %s", provider, status)
        log.info(
            "image providers: %s | brief writers: %s",
            ", ".join(registry.image_providers) or "none",
            ", ".join(registry.brief_writers) or "none",
        )
        manager.ensure_usage_ledger()  # first, so the calls recover_interrupted logs land after the backfill
        fixed = manager.recover_interrupted()
        if fixed:
            log.info("marked %d interrupted job(s) as failed", fixed)
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Jersey HD Studio",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.manager = manager
    app.state.registry = registry
    app.add_middleware(SecurityMiddleware, settings=settings)

    # -- helpers ---------------------------------------------------------------

    def parse_options(model_cls: type[BaseModel], raw: str) -> BaseModel:
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "options must be a JSON object") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "options must be a JSON object")
        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc

    def rate_limit(request: Request) -> None:
        headers = {k.lower(): v for k, v in request.headers.items()}
        client = (request.client.host, request.client.port) if request.client else None
        wait = limiter.check(client_key(client, headers, settings.trust_proxy_headers))
        if wait is not None:
            minutes = int(wait // 60) + 1
            raise HTTPException(
                429,
                f"Hourly generation limit reached. Try again in about {minutes} minute(s).",
                headers={"Retry-After": str(int(wait))},
            )

    async def read_image(upload: UploadFile) -> Image.Image:
        data = await upload.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(413, f"'{upload.filename}' is larger than {settings.max_upload_mb} MB.")
        try:
            return await asyncio.to_thread(load_upload, data, max_bytes=max_bytes)
        except ImageInputError as exc:
            raise HTTPException(400, f"'{upload.filename}': {exc}") from exc

    def job_error(exc: JobError) -> HTTPException:
        return HTTPException(exc.status_code, exc.message)

    def demo_scope(request: Request, job_id: str) -> None:
        """The demo login may only use jobs made with it: any other job answers 404, as if it did not exist."""
        if is_demo(request):
            job = manager.get(job_id) if is_valid_job_id(job_id) else None
            if job is None or not job.demo:
                raise HTTPException(404, "Job not found.")

    # -- API -------------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    async def config() -> dict:
        default_writer = registry.get_brief_writer("auto")
        return {
            "app": {"name": "Jersey HD Studio", "version": __version__},
            "providers": [
                {
                    "name": p.name,
                    "label": p.label,
                    "default_model": p.default_model(),
                    "models": [asdict(m) for m in p.models()],
                }
                for p in registry.image_providers.values()
            ],
            "default_provider": registry.default_image_provider(),
            "brief_writers": [
                {"name": w.name, "label": w.label, "model": w.model} for w in registry.brief_writers.values()
            ],
            "default_brief_writer": default_writer.name if default_writer else None,
            "brief_writer_enabled": settings.brief_writer != "off" and bool(registry.brief_writers),
            "limits": {
                "max_upload_mb": settings.max_upload_mb,
                "max_attachments": settings.max_attachments,
                "max_message_chars": settings.max_message_chars,
                "max_variants": 4,
            },
            "print_dpi": settings.print_dpi,
            "login_enabled": bool(settings.app_password),
            "panels_page": settings.panels_page,  # the studio page links to /panels only when it is served
            "catalog": catalog.as_dict(),
            "defaults": {
                "recreate": RecreateOptions().model_dump(mode="json"),
                "design": DesignOptions().model_dump(mode="json"),
            },
        }

    @app.get("/api/admin/usage")
    async def admin_usage(request: Request, days: int = Query(30, ge=0, le=3650)) -> dict:
        """The admin dashboard (studio login only): every job created or with a paid API call in the last `days`
        days (0 = all), with each call's tokens and cost, including jobs deleted since (app/usage.py)."""
        if is_demo(request):  # the middleware already refuses it; this keeps it so if DEMO_ROUTES ever widens
            raise HTTPException(403, "The demo login can only use the demo page.")
        return await asyncio.to_thread(manager.usage_report, days)

    @app.post("/api/recreate", status_code=202, response_model=JobCreated)
    async def recreate(
        request: Request,
        image: UploadFile = File(...),
        options: str = Form("{}"),
        back_image: UploadFile | None = File(None),  # panels: the client's back picture (uses_back_picture)
        sleeve_image: UploadFile | None = File(None),  # panels: the client's sleeve picture (with other panels)
    ) -> JobCreated:
        opts = parse_options(RecreateOptions, options)
        demo = is_demo(request)
        if demo:  # the demo page: production panels only, always with the default model at 2K
            if opts.mode != RecreateMode.PANELS:  # type: ignore[attr-defined]
                raise HTTPException(403, "The demo login can only make production panels.")
            opts = opts.model_copy(update={
                "provider": None, "model": None, "resolution": "2K", "variants": 1, "print_long_edge": 0, "seed": None,
                "reference_check": False,
            })
        rate_limit(request)
        img = await read_image(image)
        wants_back = back_image is not None and uses_back_picture(opts)  # type: ignore[arg-type]
        back = await read_image(back_image) if wants_back else None
        wants_sleeve = (sleeve_image is not None and opts.mode == RecreateMode.PANELS  # type: ignore[attr-defined]
                        and "sleeve" in opts.panels and len(opts.panels) > 1)  # type: ignore[attr-defined]
        sleeve = await read_image(sleeve_image) if wants_sleeve else None
        try:
            job = await manager.submit_recreate(opts, img, back, demo=demo, sleeve_image=sleeve)  # type: ignore[arg-type]
        except JobError as exc:
            raise job_error(exc) from exc
        return JobCreated(job_id=job.id)

    @app.post("/api/design", status_code=202, response_model=JobCreated)
    async def design(
        request: Request,
        message: str = Form(...),
        options: str = Form("{}"),
        session_id: str | None = Form(None),
        parent_job_id: str | None = Form(None),
        parent_variant: int | None = Form(None),
        attachments: list[UploadFile] | None = File(None),
    ) -> JobCreated:
        opts = parse_options(DesignOptions, options)
        if session_id and not SESSION_ID_RE.match(session_id):
            raise HTTPException(400, "Invalid session id.")
        parent = None
        if parent_job_id:
            if not is_valid_job_id(parent_job_id):
                raise HTTPException(400, "Invalid parent job id.")
            parent = ParentRef(job_id=parent_job_id, variant=parent_variant or 1)
        demo = is_demo(request)
        if demo:  # the demo page: the default model at 2K, the default AI designer; only its own designs to refine
            opts = opts.model_copy(update={
                "provider": None, "model": None, "resolution": "2K", "aspect_ratio": "auto", "variants": 1,
                "print_long_edge": 0, "seed": None, "brief_writer": "auto",
            })
            if parent is not None and not getattr(manager.get(parent.job_id), "demo", False):
                raise HTTPException(404, "The design you are refining no longer exists.")
        uploads = [a for a in (attachments or []) if a.filename]
        if len(uploads) > settings.max_attachments:
            raise HTTPException(400, f"At most {settings.max_attachments} reference images per message.")
        rate_limit(request)
        images = [await read_image(a) for a in uploads]
        try:
            job = await manager.submit_design(
                opts,  # type: ignore[arg-type]
                message,
                session_id=session_id,
                parent=parent,
                attachments=images,
                demo=demo,
            )
        except JobError as exc:
            raise job_error(exc) from exc
        return JobCreated(job_id=job.id, session_id=job.session_id)

    @app.post("/api/prompt-preview")
    async def prompt_preview(body: PromptPreviewRequest) -> dict:
        model_cls = RecreateOptions if body.kind == "recreate" else DesignOptions
        try:
            opts = model_cls.model_validate(body.options)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc
        try:
            pair, resolved = manager.preview_prompt(body.kind, opts, message=body.message, input_ratio=body.input_ratio)
        except JobError as exc:
            raise job_error(exc) from exc
        return {
            "system": pair.system,
            "user": pair.user,
            "provider": resolved.provider.name,
            "model": resolved.model.id,
            "aspect_ratio": resolved.aspect_ratio,
            "resolution": resolved.resolution,
            "warnings": resolved.warnings,
        }

    def add_job_routes(api: str, files: str, mgr: JobManager, job_store: JobStore) -> None:
        """History endpoints: the jobs and their files."""

        @app.get(api, response_model=list[JobSummary])
        async def list_jobs(
            limit: int = Query(60, ge=1, le=500),
            kind: JobKind | None = None,
            session_id: str | None = None,
        ) -> list[JobSummary]:
            return [_summary(j) for j in mgr.list(limit=limit, kind=kind, session_id=session_id)]

        @app.get(api + "/{job_id}", response_model=Job)
        async def get_job(request: Request, job_id: str) -> Job | JSONResponse:
            demo_scope(request, job_id)
            job = mgr.get(job_id) if is_valid_job_id(job_id) else None
            if job is None:
                raise HTTPException(404, "Job not found.")
            if is_demo(request):
                return JSONResponse(job.model_dump(mode="json", exclude=DEMO_HIDDEN), headers={"Cache-Control": "no-store"})
            return job

        @app.delete(api + "/{job_id}", status_code=204)
        async def delete_job(job_id: str) -> Response:
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            try:
                deleted = mgr.delete(job_id)
            except JobError as exc:
                raise job_error(exc) from exc
            if not deleted:
                raise HTTPException(404, "Job not found.")
            return Response(status_code=204)

        @app.post(api + "/{job_id}/variants/{index}/vectorize", response_model=VectorAsset)
        async def vectorize(request: Request, job_id: str, index: int, body: VectorizeRequest) -> VectorAsset:
            demo_scope(request, job_id)
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            try:
                return await mgr.vectorize(job_id, index, body)
            except JobError as exc:
                raise job_error(exc) from exc

        @app.get(api + "/{job_id}/calls")
        async def job_calls(job_id: str) -> list[dict]:
            """Every paid API call of the job, oldest first: prompts, images sent, settings, reply, cost."""
            if not is_valid_job_id(job_id) or mgr.get(job_id) is None:
                raise HTTPException(404, "Job not found.")
            return mgr.call_log(job_id)

        @app.post(api + "/{job_id}/variants/{index}/retry", status_code=202, response_model=JobCreated)
        async def retry_variant(request: Request, job_id: str, index: int) -> JobCreated:
            """Generate one failed image of a Recreate job again (a new image call)."""
            demo_scope(request, job_id)
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            rate_limit(request)
            try:
                job = await mgr.retry_variant(job_id, index)
            except JobError as exc:
                raise job_error(exc) from exc
            return JobCreated(job_id=job.id)

        @app.get(api + "/{job_id}/panels-eps.zip")
        async def panels_eps_zip(
            request: Request, job_id: str, preset: Literal["flat", "standard", "detailed", "raster"] = "detailed"
        ) -> Response:
            demo_scope(request, job_id)
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            try:
                name, data = await mgr.panel_eps_zip(job_id, preset)
            except JobError as exc:
                raise job_error(exc) from exc
            return Response(
                data,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
            )

        @app.get(api + "/{job_id}/panels-png.zip")
        async def panels_png_zip(
            request: Request, job_id: str, size: Literal["print", "original"] = "print"
        ) -> FileResponse:
            demo_scope(request, job_id)
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            try:
                name, path = await mgr.panel_png_zip(job_id, size)
            except JobError as exc:
                raise job_error(exc) from exc
            # a print-size ZIP can be a few hundred MB, so it is streamed from a temporary file, deleted once sent
            return FileResponse(
                path, media_type="application/zip", filename=name, headers={"Cache-Control": "no-store"},
                background=BackgroundTask(path.unlink, missing_ok=True),
            )

        @app.get(files + "/{job_id}/{filename}")
        async def get_file(request: Request, job_id: str, filename: str, download: bool = False) -> FileResponse:
            demo_scope(request, job_id)
            path = job_store.file_path(job_id, filename)
            if path is None and await mgr.ensure_print_png(job_id, filename):  # a panel's print PNG, made on demand
                path = job_store.file_path(job_id, filename)
            media_type = MEDIA_TYPES.get(path.suffix.lower()) if path else None
            if path is None or media_type is None:
                raise HTTPException(404, "File not found.")
            headers = {"Cache-Control": "private, max-age=31536000, immutable"}
            if path.suffix.lower() == ".svg":
                headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
            return FileResponse(
                path,
                media_type=media_type,
                headers=headers,
                filename=f"jersey-{job_id}-{filename}" if download else None,
                content_disposition_type="attachment" if download else "inline",
            )

    add_job_routes("/api/jobs", "/files", manager, store)

    # -- login -----------------------------------------------------------------

    @app.get("/login", include_in_schema=False)
    async def login_page(next: str = "/") -> Response:
        if not settings.app_password:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(render_login(next), headers={"Cache-Control": "no-store"})

    @app.post("/login", include_in_schema=False)
    async def login(username: str = Form(""), password: str = Form(""), next: str = Form("/")) -> Response:
        role = credentials_role(settings, username, password) if settings.app_password else None
        if role:
            # the demo login goes to the demo page, whatever page it came from
            response = RedirectResponse("/customerep" if role == "demo" else _safe_next(next), status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                make_session(settings, role),
                max_age=SESSION_DAYS * 86400,
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
                path="/",
            )
            return response
        await asyncio.sleep(1)  # slows down password guessing
        return HTMLResponse(render_login(next, "ইউজারনেম বা পাসওয়ার্ড ভুল।"), status_code=401)

    @app.get("/logout", include_in_schema=False)
    async def logout() -> Response:
        response = RedirectResponse("/login" if settings.app_password else "/", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- UI --------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    def marked_page(mode: str) -> HTMLResponse:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace('<html lang="bn">', f'<html lang="bn" data-app="{mode}">', 1),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/customerep", include_in_schema=False)
    async def customer_page() -> HTMLResponse:
        """The page for showing clients a demo: the same app, marked data-app="customer" before any script runs,
        so it shows only production panels (and Design) and no output settings, models, costs, prompts or History."""
        return marked_page("customer")

    @app.get("/experiment", include_in_schema=False)
    async def experiment_page() -> HTMLResponse:
        """A copy of /customerep for trying other image models (studio login only; its own saved state)."""
        return marked_page("experiment")

    @app.get("/panels", include_in_schema=False)
    async def panels_page() -> HTMLResponse:
        """The studio's production panels (studio login only; the demo login is sent to /customerep): the /customerep
        flow with the studio's details (output settings, View prompt, costs, image details), the back mirrored or
        copied, and the full Design tab; its own saved state. Not served when panels_page is off
        (JERSEY_PANELS_PAGE=false)."""
        if not settings.panels_page:
            raise HTTPException(status_code=404, detail="Not Found")
        return marked_page("panels")

    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
