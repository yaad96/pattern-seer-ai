"""FastAPI application: JSON API, file downloads and the single-page UI."""

from __future__ import annotations

import asyncio
import base64
import time
import html
import json
import logging
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field, StrictInt, ValidationError, field_validator
from starlette.background import BackgroundTask

from app import catalog, customers, sheets, sync, usage
from app.clients import ClientBrand, brand_for_path, brand_page, load_clients, login_branding, theme
from app.config import BASE_DIR, SHEET_KEY_FILE, Settings, get_settings
from app.imaging.io import ImageInputError, load_upload
from app.payments import PayBook, PayError, reason_text
from app.pipeline import JobError, JobManager, derives_back, uses_back_picture
from app.providers.registry import ProviderRegistry
from app.schemas import (
    DesignOptions,
    Job,
    JobCreated,
    JobKind,
    JobStatus,
    ParentRef,
    RecreateMode,
    RecreateOptions,
    VectorAsset,
    VectorizeRequest,
)
from app.security import (
    PARTNER_PAGE,
    SESSION_COOKIE,
    SESSION_DAYS,
    RateLimiter,
    SecurityMiddleware,
    client_key,
    client_of,
    credentials_role,
    make_session,
)
from app.site_switch import CLOSED_STATUS, SiteSwitch
from app.storage import SESSION_ID_RE, JobStore, is_valid_job_id, lock_outputs, utcnow_iso
from app.tokens import NotEnoughTokens, TokenBook, item_of, load_prices, panel_items

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

# One press of Generate / Send carries an id (the page's own); sent again after its answer was lost, the server answers
# with the job that press already made instead of making, and charging, a second one (2026-11-23 review
# A1-LOST-RESPONSE). Kept in memory for PRESS_KEEP_S; the page reuses an id for at most 10 minutes.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
PRESS_KEEP_S = 15 * 60
PRESS_MAX = 5000  # remembered at most (the oldest go first); the hourly limit keeps it far below


class NoCacheStaticFiles(StaticFiles):
    """Static UI files are revalidated on every load so an updated app is never served stale."""

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class SiteSwitchRequest(BaseModel):
    closed: bool


class TopUpRequest(BaseModel):
    tokens: StrictInt  # + adds to the purse, - takes back (a correction); true/false are not numbers
    taka: Annotated[StrictInt, Field(ge=0, le=10_000_000)] | None = None  # what the client paid, in whole taka
    note: str = Field(max_length=300)  # why, e.g. how they paid; every change has one
    # the form's own id: the same form sent again after its answer was lost adds nothing (2026-11-23 review CHAOS-4)
    request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Write a note: what the tokens are for.")
        return value


class BuyRequest(BaseModel):
    """A client's bKash payment for a package of tokens (app/payments.py): which package, and bKash's TrxID."""
    tokens: StrictInt  # the package, by its number of tokens
    trx: str = Field(max_length=40)
    sender: str = Field(max_length=30)  # the bKash number it was paid from
    request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None  # the form's own id (sent again)


class PayDecision(BaseModel):
    """Why a payment is rejected: a standard reason, or "other" with words. The client sees it."""
    reason: Literal["not_found", "amount", "used", "other"]
    text: str = Field("", max_length=200)


class PayPackage(BaseModel):
    tokens: StrictInt
    taka: StrictInt


class PaySettingsRequest(BaseModel):
    number: str = Field(max_length=30)  # the bKash number clients pay to ("" = buying off)
    packages: list[PayPackage] = Field(max_length=6)


class CustomerEdit(BaseModel):
    """What to change of a client on the dashboards (app/customers.py): only the fields sent."""
    name: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=40)
    password: str | None = Field(None, max_length=200)


class DownloadRequest(BaseModel):
    """What the downloads box asks to be made ready (JobManager.start_download)."""
    kind: Literal["png", "export", "eps-zip", "png-zip"]
    variant: int = Field(0, ge=0, le=64)  # one image: its number (png, export)
    preset: Literal["", "flat", "standard", "detailed", "raster"] = ""  # export, eps-zip
    size: Literal["", "print", "original"] = ""  # png-zip


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
    client: str | None = None  # the client page whose own login made it
    demo: bool = False  # made with the demo login
    client_name: str | None = None  # that client's name (not sent to the client itself)
    tokens: int | None = None  # our tokens it cost its client (None: no client login made it)


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
        client=job.client,
        demo=job.demo,
    )


LOGIN_PAGE = """<!doctype html>
<html lang="bn"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
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
{style}</style></head><body>
<form method="post" action="/login">
{heading}<p>চালিয়ে যেতে সাইন ইন করুন।</p>
<label for="u">ইউজারনেম</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="pw">পাসওয়ার্ড</label><input id="pw" name="password" type="password" autocomplete="current-password" required>
<input type="hidden" id="next" name="next" value="{next}">{error}
<button type="submit">সাইন ইন</button>
</form>
<script>var n=document.getElementById("next");if(location.hash&&n.value.indexOf("#")<0)n.value+=location.hash;</script>
</body></html>"""


# What the partner login is not shown of the jobs it sees: the clients' and, since 2026-11-26, the demo login's (its
# History tab, 2026-11-24; the owner: "without the real api cost, prompt and 'api call' expander"): the API costs and
# call counts, and every prompt.
PARTNER_HIDDEN: dict = {
    **dict.fromkeys(("prompt", "brief_prompt", "cost_usd", "uncosted_calls", "cost_partial", "api_calls",
                     "calls_in_flight"), True),
    "variants": {"__all__": {"prompt", "cost_usd", "uncosted_calls", "api_calls"}},
}

# What the demo login is not shown of its own jobs: costs, models, prompts and the reference check's details.
DEMO_HIDDEN: dict = {
    **dict.fromkeys((
        "provider", "model", "prompt", "brief_prompt", "brief_writer", "reference_analysis",
        "cost_usd", "uncosted_calls", "cost_partial", "api_calls", "calls_in_flight",
    ), True),
    "variants": {"__all__": {"prompt", "model_text", "cost_usd", "uncosted_calls", "api_calls"}},
}


# The dashboards' Jobs tab: the filter value for "only the demo login's jobs" (no client's link name can be this:
# those are letters and digits only), and what such a job's Client column says.
DEMO_JOBS = "_demo"
DEMO_NAME = "Demo login"


def is_demo(request: Request) -> bool:
    """The request comes from the demo login (SecurityMiddleware sets the role)."""
    return request.scope.get("jhs_role") == "demo"


def client_login(request: Request) -> str | None:
    """The client page whose own login (app/clients.py) made the request, else None."""
    return client_of(request.scope.get("jhs_role"))


def is_partner(request: Request) -> bool:
    """The partner login: the Partner dashboard only (tokens per client; since 2026-11-24 it also checks bKash
    payments and adds tokens)."""
    return request.scope.get("jhs_role") == "partner"


def partner_sees(job: Job) -> bool:
    """The jobs the Partner dashboard lists and may open: those made with a client login and, since 2026-11-26, those
    made with the demo login (the owner: "does partner dashboard now show demo login jobs in jobs or history? IT
    should."). Not the head admin's own."""
    return job.client is not None or job.demo


def is_limited(request: Request) -> bool:
    """Any login but the studio's: the demo login and the client logins get the demo page's limits (production
    panels and designs only, the default model at 2K, only the jobs they made, no costs, models or prompts); the
    partner login sees only the clients' and the demo login's jobs."""
    return is_demo(request) or is_partner(request) or client_login(request) is not None


def made_by(request: Request, job: Job | None) -> bool:
    """Whether the login making the request may use `job`: the studio login every job, the demo login its own jobs,
    a client login its own jobs, the partner login the clients' and the demo login's jobs."""
    if job is None:
        return False
    if is_demo(request):
        return job.demo
    if is_partner(request):
        return partner_sees(job)
    client = client_login(request)
    return client is None or job.client == client


def limited_refusal(request: Request) -> HTTPException:
    if is_partner(request):
        return HTTPException(403, "This login can only use the partner dashboard.")
    return HTTPException(403, "The demo login can only use the demo page." if is_demo(request)
                         else "This login can only use its own page.")


def _safe_next(target: str) -> str:
    """Only allow redirects back into this app (blocks //evil.example and absolute URLs)."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


LOGIN_PLACEHOLDER = re.compile(r"\{(next|error|title|style|heading)\}")


def render_login(next_path: str, error: str | None = None, brand: ClientBrand | None = None) -> str:
    """The sign-in page; in a client's name, logo and colours when it leads to that client's page (app/clients.py)."""
    title, style, heading = (login_branding(brand) if brand is not None
                             else ("সাইন ইন - জার্সি এইচডি স্টুডিও", "", "<h1>জার্সি এইচডি স্টুডিও</h1>"))
    values = {
        "next": html.escape(_safe_next(next_path), quote=True),
        "error": f'<p class="err" role="alert">{html.escape(error)}</p>' if error else "",
        "title": title, "style": style, "heading": heading,
    }
    return LOGIN_PLACEHOLDER.sub(lambda m: values[m.group(1)], LOGIN_PAGE)  # one pass: inserted text isn't re-read


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = JobStore(settings.output_dir)
    registry = ProviderRegistry(settings)
    manager = JobManager(settings, store, registry)
    limiter = RateLimiter(settings.rate_limit_per_hour)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    # our tokens: the client purses and the token ledger (app/tokens.py); prices from client_assets/tokens.json
    book = TokenBook(settings.output_dir, load_prices(settings.client_assets_dir))
    manager.tokens = book
    # every interaction, also in the owner's Google Sheet (app/sheets.py; the link in the key file .google_sheet)
    sheet = sheets.SheetLog(settings.output_dir, settings.sheet_url, settings.sheet_secret, settings.machine_name)
    manager.sheet = sheet
    # bKash payments checked by hand (app/payments.py): the requests, and the head admin's number and packages
    pays = PayBook(settings.output_dir)
    # the Mac's Dashboard on the online server (app/sync.py): this machine sends its report when sync_url is set
    sync_status: dict = {"enabled": bool(settings.sync_url and settings.sync_token), "last_ok_at": None,
                         "last_attempt_at": None, "last_error": None, "jobs": None}

    sync_lock = asyncio.Lock()  # one push at a time: the timer's and the Dashboard button's

    async def push_report() -> dict:
        async with sync_lock:
            report = await asyncio.to_thread(manager.usage_report, 0, False)  # this machine's own jobs only
            result = await asyncio.to_thread(sync.push, settings.sync_url, settings.sync_token, settings.machine_name,
                                             report)
        sync_status["last_attempt_at"] = result["at"]
        if result["ok"]:
            sync_status.update(last_ok_at=result["at"], last_error=None, jobs=result["jobs"])
        else:
            sync_status["last_error"] = result["error"]
            log.warning("sync: %s", result["error"])
        return result

    async def sync_loop() -> None:
        await asyncio.sleep(30)  # let the app settle first
        while True:
            try:
                await push_report()
            except Exception:
                log.exception("sync: the report could not be sent")
            await asyncio.sleep(max(1, settings.sync_minutes) * 60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # one server per output folder: a second one (e.g. ./run.sh while one runs) stops here, before it marks the
        # first one's running jobs as interrupted or charges their tokens again (storage.lock_outputs)
        outputs_lock = lock_outputs(settings.output_dir)
        # read again now that this process holds the folder: a server still stopping when this app was made may have
        # written to the ledger and the sheet outbox since (2026-11-23 review CHAOS-1)
        book.reload()
        sheet.reload()
        site.reload()
        pays.reload()
        repaired = pays.repair(book.by_request)  # approved just as the last server stopped: the tokens are in the ledger
        if repaired:
            log.warning("payments: %d payment(s) whose tokens were already added are now marked approved", repaired)
        for provider, status in settings.key_status.items():
            log.info("%s API key: %s", provider, status)
        log.info(
            "image providers: %s | brief writers: %s",
            ", ".join(registry.image_providers) or "none",
            ", ".join(registry.brief_writers) or "none",
        )
        log.info("tokens: %s", f"on, prices {book.prices.as_dict()}" if book.enabled else "off (no tokens.json)")
        log.info("payments: buying with bKash is %s (%d package(s), %d payment(s) waiting)",
                 "on" if pays.enabled and book.enabled else "off", len(pays.settings["packages"]), pays.waiting())
        manager.ensure_usage_ledger()  # first, so the calls recover_interrupted logs land after the backfill
        if sheet.fresh:  # the sheet log's first start: every earlier job and top-up goes in first, oldest first
            rows = await asyncio.to_thread(sheets.backfill_rows, store, book.entries(), manager.client_name,
                                           manager.call_log)
            for row in rows:
                sheet.enqueue(row)
            sheet.outbox.touch()  # the outbox exists from now on, even with nothing earlier: the backfill runs once
            log.info("sheet: %d earlier job(s) and top-up(s) queued", len(rows))
        fixed = manager.recover_interrupted()
        if fixed:
            log.info("marked %d interrupted job(s) as failed", fixed)
        syncer = asyncio.create_task(sync_loop(), name="sync") if sync_status["enabled"] else None
        sheet_sender = asyncio.create_task(sheet.run(), name="sheet")
        log.info("sheet: %s (%d row(s) waiting)", "sending to the Google Sheet" if sheet.enabled
                 else f"not set up ({SHEET_KEY_FILE}): rows are kept in {sheets.OUTBOX}", sheet.status["pending"])
        if syncer is not None:
            log.info("sync: sending this machine's Dashboard to %s every %d min", settings.sync_url,
                     settings.sync_minutes)
        # the ready-made downloads (print-size PNGs, EPS/SVG/PDF) of older jobs are deleted; each is made again on
        # its next download (JobManager.clean_downloads)
        cleaner = None
        if settings.cleanup_days > 0 or settings.cleanup_min_free_gb > 0:
            cleaner = asyncio.create_task(manager.run_cleanup(), name="cleanup")
            rules = ([f"those made more than {settings.cleanup_days} day(s) ago"] if settings.cleanup_days > 0 else [])
            if settings.cleanup_min_free_gb > 0:
                rules.append(f"the oldest whenever less than {settings.cleanup_min_free_gb:g} GB is free")
            log.info("clean-up: on, ready-made downloads are deleted: %s", "; ".join(rules))
        yield
        if syncer is not None:
            syncer.cancel()
        if cleaner is not None:
            cleaner.cancel()
        sheet_sender.cancel()
        await manager.shutdown()
        if outputs_lock is not None:
            outputs_lock.close()  # frees the lock

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
    site = SiteSwitch(settings.output_dir)  # the dashboard's open/closed switch, kept in outputs/site_closed.json
    app.state.site = site
    # The client pages /client_<name> and their own logins (app/clients.py): those written by hand in the git-ignored
    # client_assets/, then those made on the dashboards (app/customers.py, in <output_dir>/clients/). `clients` and
    # `logins` are the app's own, shared with the middleware: the builder adds to them while the app runs.
    try:
        reserved = (settings.app_username, settings.demo_username, settings.partner_username)
        clients = load_clients(settings.client_assets_dir, reserved)
        clients.update(load_clients(
            customers.folder_of(settings.output_dir), taken_slugs=tuple(clients),
            reserved_usernames=(*reserved, *(b.login.username for b in clients.values() if b.login is not None))))
        index_page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for slug, edit in customers.read_edits(customers.folder_of(settings.output_dir)).items():
            if slug in clients:  # a name, phone or password changed on a dashboard since the client was made
                try:
                    clients[slug] = customers.apply_edit(clients[slug], edit)
                except customers.CustomerError as exc:
                    log.error("the changes kept for the client %s are left out: %s", slug, exc)
        for slug, brand in list(clients.items()):  # a page that can't be branded is left out now, not served as 500
            try:
                brand_page(index_page, brand)
            except ValueError:
                log.exception("the client page %s is off: it can't be made", brand.path)
                del clients[slug]
    except Exception:  # whatever goes wrong with the client pages, the rest of the app starts
        log.exception("the client pages are off")
        clients = {}
    logins = {b.slug: b.login for b in clients.values() if b.login is not None}
    app.state.clients = clients
    manager.client_name = lambda slug: clients[slug].name if slug in clients else slug  # for the sheet log's rows
    app.add_middleware(SecurityMiddleware, settings=settings, site=site, logins=logins)  # the dict itself: it grows

    def login_brand(target: str) -> ClientBrand | None:
        """The client page a sign-in or sign-out leads to, when it has its own login and the site is open: its sign-in
        page then shows the client's name, logo and colours. Otherwise none: the plain sign-in page (so a page without
        its own login, which only the studio can open, isn't revealed)."""
        brand = brand_for_path(clients, target)
        return brand if brand is not None and brand.login is not None and not site.closed else None

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

    def login_scope(request: Request, job_id: str) -> None:
        """The demo login and the client logins may only use the jobs they made: any other job answers 404, as if it
        did not exist."""
        if is_limited(request):
            job = manager.get(job_id) if is_valid_job_id(job_id) else None
            if not made_by(request, job):
                raise HTTPException(404, "Job not found.")

    def hold_tokens(request: Request, items: list[str]) -> str | None:
        """For a client login, hold the price of `items` (app/tokens.py) before its job starts; 402 when the purse
        can't cover it. None when nothing is held: another login, tokens off, or a free job (a mirrored back)."""
        client = client_login(request)
        if client is None or not book.enabled:
            return None
        need = sum(book.prices.of(item) for item in items)
        if need <= 0:
            return None
        try:
            return book.hold(client, need)
        except NotEnoughTokens as exc:
            raise HTTPException(402, str(exc)) from exc

    presses: dict[tuple[str, str], list] = {}  # (login, request id) -> [time, job id (None while starting), session id]
    started = time.monotonic()  # for the first PRESS_KEEP_S after a start, presses are also looked up on disk

    def press_login(client: str | None, demo: bool) -> str:
        """Whose press: the same for a request and for the job it made (Job.client, Job.demo)."""
        return f"client:{client}" if client else "demo" if demo else "head"

    def press_key(request: Request, request_id: str | None) -> tuple[str, str] | None:
        """The key of one press of Generate / Send (REQUEST_ID_RE): per login, so only the same login gets its job."""
        if not request_id:
            return None
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise HTTPException(400, "Invalid request id.")
        return (press_login(client_login(request), is_demo(request)), request_id)

    def press_on_disk(key: tuple[str, str]) -> Job | None:
        """The job a press made before this server started (its answer lost, then a restart): from the records of the
        jobs made in the last PRESS_KEEP_S, newest first (job ids begin with their UTC time)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=PRESS_KEEP_S)).strftime("%Y%m%d-%H%M%S")
        for job_id in store.list_ids():
            if job_id[:15] < cutoff:
                break
            job = manager.get(job_id)
            if job is not None and job.request_id == key[1] and press_login(job.client, job.demo) == key[0]:
                return job
        return None

    def press_seen(key: tuple[str, str] | None) -> JobCreated | None:
        """The job this press already made (the page sent it again after its answer was lost), or None: then the press
        is noted as starting. Plain code with no await, so two copies of one press can't both get through."""
        if key is None:
            return None
        now = time.monotonic()
        for old in [k for k, v in presses.items() if now - v[0] > PRESS_KEEP_S]:
            del presses[old]
        known = presses.pop(key, None)
        if known is not None:
            if known[1] is None:
                presses[key] = known
                raise HTTPException(409, "This request is still being handled. Please wait a moment.")
            if manager.get(known[1]) is not None:  # not deleted meanwhile
                presses[key] = known
                return JobCreated(job_id=known[1], session_id=known[2])
        elif now - started < PRESS_KEEP_S:  # a press from before a restart: its job's record knows it
            job = press_on_disk(key)
            if job is not None:
                presses[key] = [now, job.id, job.session_id]
                trim_presses()
                return JobCreated(job_id=job.id, session_id=job.session_id)
        presses[key] = [now, None, None]  # starting; trimmed only once it has its job, so a refused press evicts none
        return None

    def trim_presses() -> None:
        """At most PRESS_MAX presses remembered: the oldest finished ones go first (one still starting stays)."""
        over = len(presses) - PRESS_MAX
        if over > 0:
            for k in [k for k, v in presses.items() if v[1] is not None][:over]:
                del presses[k]

    def press_done(key: tuple[str, str] | None, job: Job | None) -> None:
        """The press made `job` (kept for its repeats), or failed (None: forgotten, so a repeat is a new try)."""
        if key is None:
            return
        if job is None:
            presses.pop(key, None)
        else:
            presses[key] = [time.monotonic(), job.id, job.session_id]
            trim_presses()

    def bind_hold(hold: str | None, job: Job) -> None:
        """The job exists: its hold now lasts until the job ends (JobManager settles it). A job that already ended
        has been settled, so its hold just goes."""
        if hold is None:
            return
        book.bind(hold, job.id)
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            book.release(job.id)

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
            "site_closed": site.closed,  # the studio pages show a reminder banner while the site is closed
            "catalog": catalog.as_dict(),
            "defaults": {
                "recreate": RecreateOptions().model_dump(mode="json"),
                "design": DesignOptions().model_dump(mode="json"),
            },
        }

    @app.get("/api/admin/site")
    async def site_state(request: Request) -> dict:
        """The dashboard's site switch (studio login only): {closed, since}."""
        if is_limited(request):
            raise limited_refusal(request)
        return dict(site.state)

    @app.post("/api/admin/site")
    async def site_switch(request: Request, body: SiteSwitchRequest) -> dict:
        """Close the site (only the studio login can use it; everyone else gets a plain 404, as if nothing were there)
        or open it again. The server keeps running; jobs already under way finish."""
        if is_limited(request):
            raise limited_refusal(request)
        return await asyncio.to_thread(site.set, body.closed)

    @app.get("/api/admin/usage")
    async def admin_usage(request: Request, days: int = Query(30, ge=0, le=3650)) -> dict:
        """The admin dashboard (studio login only): every job created or with a paid API call in the last `days`
        days (0 = all), with each call's tokens and cost, including jobs deleted since (app/usage.py)."""
        if is_limited(request):  # the middleware already refuses it; this keeps it so if the routes ever widen
            raise limited_refusal(request)
        report = await asyncio.to_thread(manager.usage_report, days)
        taka = taka_since(since_of(days))
        report["tokens"] = {"enabled": book.enabled, "prices": book.prices.as_dict() if book.enabled else None,
                            "clients": [{"slug": slug, "name": name, "balance": book.balance(slug),
                                         "taka": taka.get(slug, 0)}  # received in the period
                                        for slug, name in client_names().items()]}
        report["sync"] = dict(sync_status)
        report["sheet"] = dict(sheet.status)  # the Google Sheet log: set up, rows waiting, the last try, its address
        # for the Supreme dashboard's "Is everything running?" and Settings (2026-11-24)
        disk = shutil.disk_usage(settings.output_dir)
        report["disk"] = {"free": disk.free, "total": disk.total}
        report["site"] = dict(site.state)
        report["cleanup"] = {"days": settings.cleanup_days, "min_free_gb": settings.cleanup_min_free_gb}
        report["payments"] = {"waiting": pays.waiting(), "enabled": pays.enabled and book.enabled}
        return report

    # -- our tokens (app/tokens.py): purses, the client dashboard's data -----------------------------------------

    def client_names() -> dict[str, str]:
        """Every client with a page or with token history, by slug: its name (the slug when it has no page now)."""
        names = {slug: brand.name for slug, brand in clients.items()}
        for slug in book.clients():  # clients with ledger history but no page now
            names.setdefault(slug, slug)
        return names

    def taka_of(entry: dict) -> int:
        """What a top-up recorded as paid, in whole taka (0 when none was recorded)."""
        taka = entry.get("taka")
        return taka if isinstance(taka, int) and not isinstance(taka, bool) else 0

    def taka_since(since: str) -> dict[str, int]:
        """Taka received per client since `since` ("" = all time), from the top-ups."""
        out: dict[str, int] = {}
        for entry in book.entries():
            if entry.get("type") != "charge" and (not since or entry.get("at", "") >= since):
                out[entry["client"]] = out.get(entry["client"], 0) + taka_of(entry)
        return out

    def since_of(days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S") if days else ""

    def client_jobs(since: str) -> dict[str, list[Job]]:
        """The jobs made with each client login since `since` (read in a worker thread), newest first."""
        out: dict[str, list[Job]] = {}
        for job_id in store.list_ids():
            job = store.load(job_id)
            if job is not None and job.client and (not since or job.created_at >= since):
                out.setdefault(job.client, []).append(job)
        return out

    def studio_or_partner(request: Request) -> None:
        if is_limited(request) and not is_partner(request):
            raise limited_refusal(request)

    def who(request: Request) -> str:
        """Who is acting, as the ledgers keep it: "partner", else "studio" (the head admin)."""
        return "partner" if is_partner(request) else "studio"

    def board_flags(request: Request) -> dict:
        """What the login may do on the two dashboards: both add tokens and check payments; only the head admin takes
        tokens back."""
        return {"can_add": True, "can_take_back": not is_limited(request), "role": who(request),
                "waiting": pays.waiting(), "buying": pays.enabled and book.enabled,
                "welcome": settings.welcome_tokens if book.enabled else 0}  # what a customer made here starts with

    def entry_point(slug: str) -> dict | None:
        """A client's way in, for the two dashboards (the head admin and the partner; the owner, 2026-11-24: "for
        partner dashboard, he should also see the password and edit"): its page, phone, login name and password.
        None for a client without a page now."""
        brand = clients.get(slug)
        if brand is None:
            return None
        return {"path": brand.path, "phone": brand.phone, "username": brand.login.username if brand.login else None,
                "password": brand.login.password if brand.login else None,
                "made_at": brand.made_at, "made_by": brand.made_by}

    def pay_view(pay: dict) -> dict:
        """A payment request for the two dashboards, with the number it was paid from in full for both (the owner,
        2026-11-24: "there should be no concealing of any phone numbers in my app anywhere"; the partner saw it
        masked for a few hours that day)."""
        return {**pay, "client_name": client_names().get(pay["client"], pay["client"]), "reason_label": reason_text(pay)}

    def pay_mine(pay: dict) -> dict:
        """A client's own payment request, for its page: not who checked it."""
        return {key: pay.get(key) for key in ("id", "tokens", "taka", "trx", "sender", "status", "at", "checked_at",
                                                 "reason", "reason_text")}

    @app.get("/api/tokens/balance")
    async def token_balance(request: Request, client: str = "") -> dict:
        """A purse: a client login's own, or (studio login) the one named. The client page shows it in its header and
        next to Generate / Send; `charged` says whether this login's jobs are charged (only the client's own)."""
        own = client_login(request)
        if own is None and (is_demo(request) or is_partner(request)):
            return {"enabled": False, "client": None}
        slug = own or client
        if slug not in client_names():
            raise HTTPException(404, "No such client.")
        return {"enabled": book.enabled, "client": slug, "balance": book.balance(slug),
                "available": book.available(slug), "prices": book.prices.as_dict() if book.enabled else None,
                "charged": own is not None}

    @app.get("/api/tokens/clients")
    async def token_clients(request: Request, days: int = Query(30, ge=0, le=3650)) -> dict:
        """The client dashboard's overview (studio and partner logins): each client's balance, and what it used, was
        given, paid (taka) and made in the last `days` days (0 = all). No API costs or AI details."""
        studio_or_partner(request)
        since = since_of(days)
        jobs = await asyncio.to_thread(client_jobs, since)
        used: dict[str, int] = {}
        added: dict[str, int] = {}
        paid: dict[str, int] = {}
        charged_jobs: dict[str, set] = {}
        for entry in book.entries():
            if since and entry.get("at", "") < since:
                continue
            who = entry["client"]
            if entry.get("type") == "charge":  # charges are negative in the ledger
                used[who] = used.get(who, 0) - entry["tokens"]
                charged_jobs.setdefault(who, set()).add(entry.get("job_id"))
            else:  # top-ups (+) and corrections (-), and the taka a top-up recorded
                added[who] = added.get(who, 0) + entry["tokens"]
                paid[who] = paid.get(who, 0) + taka_of(entry)
        rows = []
        for slug, name in client_names().items():
            mine = jobs.get(slug, [])
            rows.append({"slug": slug, "name": name, "has_page": slug in clients, "balance": book.balance(slug),
                         "phone": clients[slug].phone if slug in clients else "",
                         "used": used.get(slug, 0), "added": added.get(slug, 0), "taka": paid.get(slug, 0),
                         "jobs": len({job.id for job in mine} | charged_jobs.get(slug, set())),  # made or charged then
                         "last_job_at": mine[0].created_at if mine else None})
        rows.sort(key=lambda r: (-r["used"], r["name"].lower()))
        return {"enabled": book.enabled, **board_flags(request), "days": days,
                "login_enabled": bool(settings.app_password),  # the page's Log out button
                "prices": book.prices.as_dict() if book.enabled else None, "clients": rows}

    def job_row(job: Job, items: list[dict]) -> dict:
        """A job as the dashboards' token tables list it; `items`: the ledger's charges for it in the period."""
        paid_for = set(job.charge_variants)
        return {
            "id": job.id, "created_at": job.created_at, "kind": job.kind.value, "status": job.status.value,
            # a Design message: its chat (the dashboard nests a chat's messages) and its words
            "chat": job.session_id if job.kind == JobKind.DESIGN else None, **usage.design_texts(job),
            "refine": job.parent is not None,  # a change request in its chat
            "panels": (job.options or {}).get("panels"), "back": (job.options or {}).get("back"),
            "preview": next((v.preview for v in job.variants if v.status == "succeeded" and v.preview), None),
            "deleted": False, "tokens": sum(-e["tokens"] for e in items),
            "items": [{"item": e.get("item"), "tokens": -e["tokens"]} for e in items],
            "failed": [item_of(job, v) or "image" for v in job.variants if v.status == "failed"],
            # images that came out without costing a client: made again by the studio, with tokens off, or by the
            # demo login (whose jobs are never charged)
            "waived": [item_of(job, v) or "image" for v in job.variants
                       if v.status == "succeeded" and v.index not in paid_for],
            "running": job.status in (JobStatus.QUEUED, JobStatus.RUNNING),
        }

    async def demo_rows(since: str) -> list[dict]:
        """The demo login's jobs made since `since` ("" = all), as the dashboards' Jobs tab lists them (2026-11-26):
        no client and no tokens (the demo login is never charged)."""
        def load() -> list[Job]:
            out = []
            for job_id in store.list_ids():
                job = store.load(job_id)
                if job is not None and job.demo and not job.client and (not since or job.created_at >= since):
                    out.append(job)
            return out

        return [{**job_row(job, []), "client": None, "client_name": DEMO_NAME, "demo": True}
                for job in await asyncio.to_thread(load)]

    async def client_rows(slug: str, since: str) -> tuple[list[dict], list[dict]]:
        """A client's jobs made or charged since `since` ("" = all), newest first, each with the tokens its images cost
        in that time (a failed image costs nothing; one the studio made again, or one made with tokens off, is free;
        charged jobs deleted from History since are listed too), and the client's ledger entries (all time)."""
        entries = [e for e in book.entries() if e["client"] == slug]
        charges: dict[str, list[dict]] = {}
        for e in entries:
            if e.get("type") == "charge" and (not since or e.get("at", "") >= since):
                charges.setdefault(e.get("job_id"), []).append(e)

        def load() -> tuple[list[Job], list[str]]:
            made = client_jobs(since).get(slug, [])
            listed = {job.id for job in made}
            older, gone = [], []
            for job_id in charges:  # made before the period, charged in it (e.g. a panel made again)
                if job_id in listed:
                    continue
                job = store.load(job_id) if is_valid_job_id(job_id) else None
                if job is None:
                    gone.append(job_id)
                else:
                    older.append(job)
            return made + older, gone

        jobs, gone = await asyncio.to_thread(load)
        # the ledger again, now: a job may have been charged, or tokens added, while the jobs were read. The answer (and
        # the balance the caller reads next, with no await between) then describes one moment (2026-11-23 review A3-4)
        entries = [e for e in book.entries() if e["client"] == slug]
        charges = {}
        for e in entries:
            if e.get("type") == "charge" and (not since or e.get("at", "") >= since):
                charges.setdefault(e.get("job_id"), []).append(e)
        seen = {job.id for job in jobs} | set(gone)
        for job_id in [j for j in charges if j not in seen]:  # charged while the jobs were read: one small file each
            job = store.load(job_id) if is_valid_job_id(job_id) else None
            if job is None:
                gone.append(job_id)
            else:
                jobs.append(job)
        rows = [job_row(job, charges.get(job.id, [])) for job in jobs]
        for job_id in gone:  # charged jobs deleted from History since
            items = charges[job_id]
            rows.append({"id": job_id, "created_at": items[0].get("at"), "kind": None, "status": None, "panels": None,
                         "back": None, "preview": None, "deleted": True, "tokens": sum(-e["tokens"] for e in items),
                         "items": [{"item": e.get("item"), "tokens": -e["tokens"]} for e in items], "failed": [],
                         "waived": [], "running": False})
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows, entries

    @app.get("/api/tokens/clients/{slug}")
    async def token_client(request: Request, slug: str, days: int = Query(30, ge=0, le=3650)) -> JSONResponse:
        """One client on the client dashboard (studio and partner logins): its jobs in the last `days` days with the
        tokens each image cost (a failed image costs nothing), and the tokens added to its purse."""
        studio_or_partner(request)
        names = client_names()
        if slug not in names:
            raise HTTPException(404, "No such client.")
        since = since_of(days)
        rows, entries = await client_rows(slug, since)
        in_period = [e for e in entries if not since or e.get("at", "") >= since]
        return JSONResponse({
            "enabled": book.enabled, **board_flags(request), "days": days,
            "prices": book.prices.as_dict() if book.enabled else None,
            "entry": entry_point(slug),  # its page, phone and login
            "payments": [pay_view(pay) for pay in pays.of_client(slug)],  # its bKash payments, all time
            "client": {"slug": slug, "name": names[slug], "has_page": slug in clients, "balance": book.balance(slug),
                       "available": book.available(slug),  # less what running jobs hold: the most a take-back can take
                       "used": sum(-e["tokens"] for e in in_period if e.get("type") == "charge"),
                       "added": sum(e["tokens"] for e in in_period if e.get("type") != "charge"),
                       "taka": sum(taka_of(e) for e in in_period if e.get("type") != "charge")},
            "jobs": rows,
            "topups": [{"at": e.get("at"), "tokens": e["tokens"], "taka": e.get("taka"), "note": e.get("note"),
                        "by": e.get("by"), "balance": e.get("balance"), "trx": e.get("trx"),  # a bKash payment's TrxID
                        "request_id": e.get("request_id")}  # the page checks a top-up whose answer was lost
                       for e in reversed(entries) if e.get("type") != "charge"],
        }, headers={"Cache-Control": "no-store"})

    # -- the Customer EntryPoint Builder (app/customers.py, 2026-11-24): a client page made from a dashboard form ----

    customer_lock = asyncio.Lock()  # one customer is made at a time: the link names and the file are checked under it

    def taken_names() -> set[str]:
        """Every link name and every login's username in use, in lower case: a new customer's link name is also its
        username."""
        names = {slug for slug in clients} | {login.username.casefold() for login in logins.values()}
        return names | {n.casefold() for n in (settings.app_username, settings.demo_username, settings.partner_username) if n}

    def data_uri(data: bytes, ext: str) -> str:
        return f"data:image/{'jpeg' if ext == 'jpg' else ext};base64,{base64.b64encode(data).decode('ascii')}"

    async def uploaded_logo(logo: UploadFile, colors: str) -> customers.Logo:
        try:
            given = customers.clean_colors(json.loads(colors) if colors.strip() else None)
            data = await logo.read(customers.MAX_UPLOAD_BYTES + 1)
            return await asyncio.to_thread(customers.prepare_logo, data, given)
        except customers.CustomerError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:  # the colours weren't JSON
            raise HTTPException(422, "The colours must be three colours like #1F4FD8.") from exc

    @app.post("/api/clients/preview")
    async def customer_preview(request: Request, name: str = Form(""), slug: str = Form(""), colors: str = Form(""),
                               logo: UploadFile | None = File(None)) -> dict:
        """The builder's preview (head admin and partner), nothing kept: a link name suggested from the name; whether
        the one typed can be used; and, with a logo, the logo as the page will show it, the three colours picked from
        it (or the ones sent) and the page's colour tokens for light and dark."""
        studio_or_partner(request)
        taken = taken_names()
        out: dict = {"suggested": customers.suggest_slug(name, taken), "slug": slug,
                     "slug_problem": customers.slug_problem(slug, taken) if slug else None,
                     "welcome": settings.welcome_tokens if book.enabled else 0}
        if logo is not None and logo.filename:
            made = await uploaded_logo(logo, colors)
            light, dark = theme(made.colors)
            out.update(logo=data_uri(made.logo, made.logo_ext), mark=data_uri(made.mark, made.mark_ext),
                       colors=made.colors, light=light, dark=dark)
        return out

    @app.post("/api/clients", status_code=201)
    async def customer_create(request: Request, name: str = Form(""), phone: str = Form(""), slug: str = Form(""),
                              colors: str = Form(""), logo: UploadFile = File(...)) -> JSONResponse:
        """Make a customer's entry point (head admin and partner): the page /client_<slug> with the logo as uploaded,
        the colours and the name, its own login (the link name as the username, a generated password) and the welcome
        tokens. It works at once. The answer is the only time the partner sees the password."""
        studio_or_partner(request)
        try:
            name, phone = customers.clean_name(name), customers.clean_phone(phone)
        except customers.CustomerError as exc:
            raise HTTPException(422, str(exc)) from exc
        made = await uploaded_logo(logo, colors)
        by = who(request)
        async with customer_lock:
            problem = customers.slug_problem(slug, taken_names())
            if problem:
                raise HTTPException(409 if slug in taken_names() else 422, problem)
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            try:
                brand = await asyncio.to_thread(
                    customers.save, customers.folder_of(settings.output_dir), slug=slug, name=name, phone=phone,
                    logo=made, password=customers.new_password(), by=by, at=utcnow_iso(),
                    check=lambda b: brand_page(page, b))
            except customers.CustomerError as exc:
                raise HTTPException(409, str(exc)) from exc
            except Exception as exc:  # the files couldn't be written, or the page couldn't be made from them
                log.exception("customers: %s could not be made", slug)
                raise HTTPException(500, "The entry point could not be made. Nothing was kept; please try again.") from exc
            clients[slug] = brand
            logins[slug] = brand.login
            log.info("customers: %s made /client_%s (own login)", by, slug)
            sheet.enqueue(sheets.customer_row(brand, by))
            tokens = 0
            if book.enabled and settings.welcome_tokens > 0:
                entry = book.add(slug, settings.welcome_tokens, "Welcome tokens for a new customer", by,
                                 request_id=f"welcome-{slug}")
                sheet.enqueue(sheets.topup_row(entry, name))
                tokens = entry["tokens"]
        return JSONResponse({
            "client": {"slug": slug, "name": name, "phone": phone, "path": brand.path,
                       "username": brand.login.username, "password": brand.login.password},
            "tokens": tokens, "balance": book.balance(slug) if book.enabled else 0,
        }, status_code=201, headers={"Cache-Control": "no-store"})

    @app.post("/api/clients/{slug}")
    async def customer_edit(request: Request, slug: str, body: CustomerEdit) -> JSONResponse:
        """Change a client's name, phone or password (head admin and partner; any client with a page, also one
        written by hand). Only what is sent is changed. A new password signs the client out everywhere: it signs in
        again with the new one. Kept in the server's own data (app/customers.py), so a deploy never undoes it."""
        studio_or_partner(request)
        by = who(request)
        async with customer_lock:
            brand = clients.get(slug)
            if brand is None:
                raise HTTPException(404, "No such client.")
            try:
                changes: dict = {}
                if body.name is not None and customers.clean_name(body.name) != brand.name:
                    changes["name"] = customers.clean_name(body.name)
                if body.phone is not None and body.phone.strip() != brand.phone:
                    phone = customers.clean_phone(body.phone) if body.phone.strip() else ""
                    if phone != brand.phone:  # the same number typed with other spacing is no change
                        changes["phone"] = phone
                if body.password is not None and (brand.login is None or body.password != brand.login.password):
                    changes["password"] = customers.clean_password(body.password)
                changed = customers.apply_edit(brand, changes)
            except customers.CustomerError as exc:
                raise HTTPException(422, str(exc)) from exc
            if changes:
                page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
                at = utcnow_iso()
                try:
                    brand_page(page, changed)
                    await asyncio.to_thread(customers.save_edit, customers.folder_of(settings.output_dir), slug, changes, by, at)
                except Exception as exc:  # the file couldn't be written, or the page couldn't be made with the change
                    log.exception("customers: the change to %s could not be kept", slug)
                    raise HTTPException(500, "The change could not be kept. Nothing was changed; please try again.") from exc
                clients[slug] = changed
                if changed.login is not None:
                    logins[slug] = changed.login
                log.info("customers: %s changed %s of /client_%s", by, ", ".join(changes), slug)
                sheet.enqueue(sheets.customer_row(changed, by, changed=tuple(changes), at=at))
        return JSONResponse({"entry": entry_point(slug), "name": clients[slug].name, "changed": sorted(changes)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/tokens/mine")
    async def my_tokens(request: Request, client: str = "") -> dict:
        """A client page's "Customer dashboard" tab (2026-11-23): the client's own purse, what each of its jobs used and
        what it paid when, all time. The client login gets its own; the studio login names the client of the page it is on.
        Without the studio's notes on top-ups (they are for the studio and the partner) and without API costs."""
        own = client_login(request)
        if own is None and (is_demo(request) or is_partner(request)):
            return {"enabled": False, "client": None}
        slug = own or client
        if slug not in client_names():
            raise HTTPException(404, "No such client.")
        rows, entries = await client_rows(slug, "")
        changes = [e for e in entries if e.get("type") != "charge"]
        return {
            "enabled": book.enabled, "client": slug, "charged": own is not None,
            "prices": book.prices.as_dict() if book.enabled else None,
            "balance": book.balance(slug), "available": book.available(slug),
            "used": sum(-e["tokens"] for e in entries if e.get("type") == "charge"),
            "added": sum(e["tokens"] for e in changes), "taka": sum(taka_of(e) for e in changes),
            "jobs": rows,
            "payments": [{"at": e.get("at"), "tokens": e["tokens"], "taka": e.get("taka"), "balance": e.get("balance"),
                          "trx": e.get("trx")} for e in reversed(changes)],
            # "Buy tokens" (app/payments.py): where to pay and what can be bought, and this client's bKash payments
            "buy": {"enabled": pays.enabled and book.enabled, "number": pays.settings["number"] if pays.enabled else "",
                    "packages": pays.settings["packages"] if pays.enabled else []},
            "bkash": [pay_mine(pay) for pay in pays.of_client(slug)],
        }

    @app.post("/api/tokens/clients/{slug}/topup")
    async def token_topup(request: Request, slug: str, body: TopUpRequest) -> dict:
        """Add tokens to a client's purse (the head admin, and since 2026-11-24 the partner), or take some back with a
        negative number (the head admin only). Every change is a ledger line with the note, who and when."""
        studio_or_partner(request)
        if is_partner(request) and body.tokens < 0:
            raise HTTPException(403, "Only the head admin can take tokens back.")
        if slug not in client_names():
            raise HTTPException(404, "No such client.")
        done = book.by_request(body.request_id)
        if done is not None:  # the same form again (the answer to the first press was lost): recorded once, not twice
            if done["client"] != slug or done["tokens"] != body.tokens:
                raise HTTPException(409, "This form was already used for another change. Reload the page and try again.")
            return {"entry": done, "balance": book.balance(slug), "repeated": True}
        try:
            entry = book.add(slug, body.tokens, body.note, who(request), taka=body.taka, request_id=body.request_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        sheet.enqueue(sheets.topup_row(entry, client_names()[slug]))  # also in the Google Sheet log
        return {"entry": entry, "balance": book.balance(slug)}

    @app.get("/api/tokens/jobs")
    async def token_jobs(request: Request, days: int = Query(7, ge=0, le=3650), client: str = "") -> dict:
        """The two dashboards' Jobs tab: every client's jobs in the last `days` days (0 = all) with the tokens each
        part cost, and the demo login's jobs (no tokens; `client=_demo`: only those), newest first. Tokens only: no
        API costs, models or prompts (head admin and partner)."""
        studio_or_partner(request)
        names = client_names()
        if client and client != DEMO_JOBS and client not in names:
            raise HTTPException(404, "No such client.")
        since = since_of(days)
        rows: list[dict] = []
        for slug in ([] if client == DEMO_JOBS else [client] if client else list(names)):
            mine, _ = await client_rows(slug, since)
            rows += [{**row, "client": slug, "client_name": names[slug]} for row in mine]
        if client in ("", DEMO_JOBS):
            rows += await demo_rows(since)
        rows.sort(key=lambda r: r["id"], reverse=True)
        return {"days": days, "jobs": rows, "clients": [{"slug": slug, "name": name} for slug, name in names.items()]}

    @app.get("/api/tokens/topups")
    async def token_topups(request: Request, days: int = Query(0, ge=0, le=3650)) -> dict:
        """The two dashboards' Payments tab: the tokens added or taken back by hand for every client, newest first
        (a bKash payment's top-up is in the payments list instead). Head admin and partner."""
        studio_or_partner(request)
        names = client_names()
        since = since_of(days)
        rows = [{"at": e.get("at"), "client": e["client"], "client_name": names.get(e["client"], e["client"]),
                 "tokens": e["tokens"], "taka": e.get("taka"), "note": e.get("note"), "by": e.get("by"),
                 "balance": e.get("balance")}
                for e in reversed(book.entries())
                if e.get("type") != "charge" and not e.get("pay_id") and (not since or e.get("at", "") >= since)]
        return {"days": days, "topups": rows}

    # -- bKash payments, checked by hand (app/payments.py, 2026-11-24) -------------------------------------------

    @app.get("/api/pay/settings")
    async def pay_settings(request: Request) -> dict:
        """The bKash number clients pay to and the packages they can buy (the head admin's; the Supreme dashboard)."""
        if is_limited(request):
            raise limited_refusal(request)
        return {**pays.settings, "enabled": pays.enabled, "tokens_enabled": book.enabled}

    @app.post("/api/pay/settings")
    async def pay_settings_save(request: Request, body: PaySettingsRequest) -> dict:
        if is_limited(request):
            raise limited_refusal(request)
        try:
            saved = pays.set_settings(body.number, [package.model_dump() for package in body.packages], "studio")
        except PayError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return {**saved, "enabled": pays.enabled, "tokens_enabled": book.enabled}

    @app.post("/api/pay/requests", status_code=201)
    async def pay_submit(request: Request, body: BuyRequest) -> dict:
        """A client page's "Buy tokens": the client paid with bKash and gives the TrxID (a client's own login only).
        The payment waits until the head admin or the partner has checked it."""
        own = client_login(request)
        if own is None:
            raise HTTPException(403, "Only a client's own login can send a payment.")
        if not book.enabled:
            raise HTTPException(409, "Tokens are not in use.")
        repeated = pays.by_form(body.request_id) is not None  # the same form again: no second request, no second row
        try:
            pay = pays.submit(own, body.tokens, body.trx, body.sender, body.request_id)
        except PayError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        if not repeated:
            sheet.enqueue(sheets.payment_row(pay, sheets.PAY_SENT, client_names().get(own, own),
                                             balance=book.balance(own)))
        return {"payment": pay_mine(pay), "repeated": repeated}

    @app.get("/api/pay/requests")
    async def pay_requests(request: Request) -> dict:
        """Every bKash payment, newest first (head admin and partner)."""
        studio_or_partner(request)
        return {"payments": [pay_view(pay) for pay in pays.all()], **board_flags(request)}

    @app.post("/api/pay/requests/{pay_id}/approve")
    async def pay_approve(request: Request, pay_id: str) -> dict:
        """The money was seen in bKash: the payment's tokens go into the client's purse (head admin and partner).
        Nothing is awaited between the look and the two lines, so two people approving add the tokens once."""
        studio_or_partner(request)
        pay = pays.get(pay_id)
        if pay is None:
            raise HTTPException(404, "No such payment.")
        if pay["status"] != "waiting":
            by_whom = sheets.WHO.get(pay.get("checked_by"), pay.get("checked_by"))
            raise HTTPException(409, f"This payment was already {pay['status']}" + (f" by {by_whom}." if by_whom else "."))
        if not book.enabled:
            raise HTTPException(409, "Tokens are off on this server, so none can be added.")
        by = who(request)
        try:  # the ledger first: it takes "pay-<id>" once, whatever happens after (app/payments.py)
            entry = book.add(pay["client"], pay["tokens"], f"bKash {pay['trx']}", by, taka=pay["taka"],
                             request_id=f"pay-{pay_id}", extra={"pay_id": pay_id, "trx": pay["trx"]})
            done = pays.decide(pay_id, "approved", by, balance=entry["balance"])
        except PayError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        sheet.enqueue(sheets.payment_row(done, sheets.PAY_APPROVED, client_names().get(pay["client"], pay["client"]),
                                         balance=entry["balance"]))
        return {"payment": pay_view(done), "balance": book.balance(pay["client"])}

    @app.post("/api/pay/requests/{pay_id}/reject")
    async def pay_reject(request: Request, pay_id: str, body: PayDecision) -> dict:
        """The money wasn't found, or doesn't match: the payment is rejected with a reason the client sees."""
        studio_or_partner(request)
        try:
            done = pays.decide(pay_id, "rejected", who(request), reason=body.reason, text=body.text)
        except PayError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        sheet.enqueue(sheets.payment_row(done, sheets.PAY_REJECTED, client_names().get(done["client"], done["client"]),
                                         balance=book.balance(done["client"]), reason=reason_text(done)))
        return {"payment": pay_view(done)}

    # -- the Mac's Dashboard on the online server (app/sync.py) --------------------------------------------------

    @app.post(sync.PATH, include_in_schema=False)
    async def sync_receive(request: Request) -> JSONResponse:
        """Another machine's Dashboard report (the Mac's), sent with the shared sync token. The middleware lets only a
        POST with the token through without a session (and also while the site is closed); anything else gets the
        same answers as any other path. This check is the second line."""
        if not settings.sync_token or not sync.token_ok(settings.sync_token, request.headers.get("authorization")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            if int(request.headers.get("content-length") or 0) > sync.MAX_BODY:
                raise HTTPException(413, "The report is too large.")
        except ValueError as exc:
            raise HTTPException(400, "Bad Content-Length.") from exc
        chunks, size = [], 0
        async for chunk in request.stream():  # counted as it arrives: a chunked body has no Content-Length
            size += len(chunk)
            if size > sync.MAX_BODY:
                raise HTTPException(413, "The report is too large.")
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            machine, report = sync.clean_report(json.loads(body))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if machine == settings.machine_name:
            raise HTTPException(422, "A machine can't send its report to itself.")
        await asyncio.to_thread(sync.save_remote, settings.output_dir, machine, report)
        log.info("sync: received %d job(s) from %s", len(report["jobs"]), machine)
        return JSONResponse({"ok": True, "jobs": len(report["jobs"])})

    @app.api_route(sync.PATH, methods=["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    async def sync_other(request: Request) -> JSONResponse:
        """Only POST carries a report; every other method answers like a path that isn't there (not 405)."""
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.get("/api/admin/sync")  # not used by a page (the Dashboard reads report.sync); kept for checking by hand
    async def sync_state(request: Request) -> dict:
        """Whether this machine sends its Dashboard to the server, and how the last try went (studio login)."""
        if is_limited(request):
            raise limited_refusal(request)
        return dict(sync_status)

    @app.post("/api/admin/sync")
    async def sync_now(request: Request) -> dict:
        """Send this machine's Dashboard to the server now (the Dashboard's button; studio login)."""
        if is_limited(request):
            raise limited_refusal(request)
        if not sync_status["enabled"]:
            raise HTTPException(409, "Sending to the server isn't set up on this machine.")
        await push_report()
        return dict(sync_status)

    @app.get("/api/admin/sheet-script")
    async def sheet_script(request: Request) -> JSONResponse:
        """The Apps Script to paste into the Google Sheet, with this machine's secret in it (the Supreme dashboard's
        Settings; the head admin only). The script file on disk is a template without it (app/sheet_script.gs)."""
        if is_limited(request):
            raise limited_refusal(request)
        if not sheet.enabled:
            raise HTTPException(409, f"The Google Sheet isn't set up on this machine (the key file {SHEET_KEY_FILE}).")
        return JSONResponse({"script": sheets.script_text(settings.sheet_secret), "version": sheets.SCRIPT_VERSION},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/sheet-check")
    async def sheet_check(request: Request) -> dict:
        """Ask the sheet's script which version it is, now (nothing is written there): after a new script was pasted,
        the Supreme dashboard shows at once that it took (the head admin only)."""
        if is_limited(request):
            raise limited_refusal(request)
        if not sheet.enabled:
            raise HTTPException(409, f"The Google Sheet isn't set up on this machine (the key file {SHEET_KEY_FILE}).")
        return await sheet.check()

    @app.post("/api/recreate", status_code=202, response_model=JobCreated)
    async def recreate(
        request: Request,
        image: UploadFile = File(...),
        options: str = Form("{}"),
        back_image: UploadFile | None = File(None),  # panels: the client's back picture (uses_back_picture)
        sleeve_image: UploadFile | None = File(None),  # panels: the client's sleeve picture (with other panels)
        request_id: str | None = Form(None),  # this press of Generate (sent again after a lost answer)
    ) -> JobCreated:
        opts = parse_options(RecreateOptions, options)
        if is_limited(request):  # the demo page and the client pages: production panels only, the default model at 2K
            if opts.mode != RecreateMode.PANELS:  # type: ignore[attr-defined]
                raise HTTPException(403, "The demo login can only make production panels." if is_demo(request)
                                    else "This login can only make production panels.")
            opts = opts.model_copy(update={
                "provider": None, "model": None, "resolution": "2K", "variants": 1, "print_long_edge": 0, "seed": None,
                "reference_check": False,
            })
        panels = opts.mode == RecreateMode.PANELS  # type: ignore[attr-defined]
        press = press_key(request, request_id)
        made = press_seen(press)
        if made is not None:  # this press already made its job: the same answer again, nothing new made or charged
            return made
        try:
            hold = hold_tokens(request, panel_items(list(opts.panels), derives_back(opts)) if panels else [])  # type: ignore
        except BaseException:
            press_done(press, None)
            raise
        try:
            rate_limit(request)
            img = await read_image(image)
            wants_back = back_image is not None and uses_back_picture(opts)  # type: ignore[arg-type]
            back = await read_image(back_image) if wants_back else None
            wants_sleeve = (sleeve_image is not None and panels
                            and "sleeve" in opts.panels and len(opts.panels) > 1)  # type: ignore[attr-defined]
            sleeve = await read_image(sleeve_image) if wants_sleeve else None
            try:
                job = await manager.submit_recreate(opts, img, back, demo=is_demo(request),  # type: ignore[arg-type]
                                                    sleeve_image=sleeve, client=client_login(request),
                                                    request_id=press[1] if press else None)
            except JobError as exc:
                raise job_error(exc) from exc
        except BaseException:
            book.release(hold or "")
            press_done(press, None)
            raise
        bind_hold(hold, job)
        press_done(press, job)
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
        request_id: str | None = Form(None),  # this press of Send (sent again after a lost answer)
    ) -> JobCreated:
        opts = parse_options(DesignOptions, options)
        if session_id and not SESSION_ID_RE.match(session_id):
            raise HTTPException(400, "Invalid session id.")
        parent = None
        if parent_job_id:
            if not is_valid_job_id(parent_job_id):
                raise HTTPException(400, "Invalid parent job id.")
            parent = ParentRef(job_id=parent_job_id, variant=parent_variant or 1)
        if is_limited(request):  # the demo page and the client pages: the default model at 2K and AI designer
            opts = opts.model_copy(update={
                "provider": None, "model": None, "resolution": "2K", "aspect_ratio": "auto", "variants": 1,
                "print_long_edge": 0, "seed": None, "brief_writer": "auto",
            })
            if parent is not None and not made_by(request, manager.get(parent.job_id)):  # only its own designs
                raise HTTPException(404, "The design you are refining no longer exists.")
        uploads = [a for a in (attachments or []) if a.filename]
        if len(uploads) > settings.max_attachments:
            raise HTTPException(400, f"At most {settings.max_attachments} reference images per message.")
        press = press_key(request, request_id)
        made = press_seen(press)
        if made is not None:  # this press already made its message: the same answer again, nothing new made or charged
            return made
        try:
            hold = hold_tokens(request, ["design"] * opts.variants)  # type: ignore[attr-defined]
        except BaseException:
            press_done(press, None)
            raise
        try:
            rate_limit(request)
            images = [await read_image(a) for a in uploads]
            try:
                job = await manager.submit_design(
                    opts,  # type: ignore[arg-type]
                    message,
                    session_id=session_id,
                    parent=parent,
                    attachments=images,
                    demo=is_demo(request),
                    client=client_login(request),
                    request_id=press[1] if press else None,
                )
            except JobError as exc:
                raise job_error(exc) from exc
        except BaseException:
            book.release(hold or "")
            press_done(press, None)
            raise
        bind_hold(hold, job)
        press_done(press, job)
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

        def job_tokens(job: Job, names: dict[str, str] | None = None) -> dict:
            """What a job cost its client in our tokens, for History (2026-11-24): the total and each charged image;
            None for a job no client login made (it costs no tokens)."""
            if not job.client:
                return {"tokens": None, "token_items": [], "client_name": None}
            charges = book.job_charges(job.id)
            return {"tokens": sum(-e["tokens"] for e in charges),
                    "token_items": [{"variant": e.get("variant"), "item": e.get("item"), "tokens": -e["tokens"]} for e in charges],
                    "client_name": (names or client_names()).get(job.client, job.client)}

        @app.get(api, response_model=list[JobSummary])
        async def list_jobs(
            request: Request,
            limit: int = Query(60, ge=1, le=500),
            kind: JobKind | None = None,
            session_id: str | None = None,
            clients: bool = False,
            client: str = Query("", max_length=32),
            demo: bool = False,
        ) -> list[JobSummary]:
            """History's cards, newest first. The head admin gets every job (`clients`: only those the Partner
            dashboard lists: made with a client login or with the demo login; `demo`: only the demo login's, as the
            demo page's History lists them); a client login only the jobs it made, and since 2026-11-26 the demo
            login only the jobs made with the demo login (the owner: "treat Demo as any other customer. So all the
            features of a customer, except token-based usage should be there"); the partner the clients' and the demo
            login's jobs. None of the three gets costs, and the client and the demo login no model either. Each job
            made with a client login says the tokens it cost."""
            own = client_login(request)
            mine = is_demo(request)
            # a client login always gets its own jobs, the demo login always the demo login's; `client`: one client's
            # jobs (the head admin on that client's page); `demo`: the demo login's (the head admin on the demo page)
            match = ((lambda j: j.client == own) if own else (lambda j: j.demo) if mine or demo
                     else (lambda j: j.client == client) if client
                     else partner_sees if is_partner(request) or clients else None)
            names = client_names()
            out = []
            for job in mgr.list(limit=limit, kind=kind, session_id=session_id, match=match):
                extra = job_tokens(job, names)
                update = {"tokens": extra["tokens"], "client_name": extra["client_name"]}
                if is_limited(request):
                    update.update(cost_usd=None, uncosted_calls=0, cost_partial=False)
                if own or mine:  # the client itself, or the demo login: no model names either, as on the rest of its page
                    update.update(provider=None, model=None, client_name=None)
                out.append(_summary(job).model_copy(update=update))
            return out

        @app.get(api + "/{job_id}")
        async def get_job(request: Request, job_id: str) -> JSONResponse:
            login_scope(request, job_id)
            job = mgr.get(job_id) if is_valid_job_id(job_id) else None
            if job is None:
                raise HTTPException(404, "Job not found.")
            # the demo page and the client pages: no costs, models or prompts; the partner: no costs or prompts
            hidden = PARTNER_HIDDEN if is_partner(request) else DEMO_HIDDEN if is_limited(request) else None
            return JSONResponse({**job.model_dump(mode="json", exclude=hidden), **job_tokens(job)},
                                headers={"Cache-Control": "no-store"} if hidden else None)

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
            login_scope(request, job_id)
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
            login_scope(request, job_id)
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            known = mgr.get(job_id)
            # the client's own retry costs the price of what it makes (charged when it comes out: the image, and for a
            # front a failed back made from it, which follows); the studio's retry is free
            charge = known is not None and known.client is not None and client_login(request) == known.client
            hold = hold_tokens(request, mgr.retry_items(known, index) if charge else [])
            try:
                rate_limit(request)
                try:
                    by = "client" if client_login(request) else "demo" if is_demo(request) else "head admin"
                    job = await mgr.retry_variant(job_id, index, charge=charge, by=by)
                except JobError as exc:
                    raise job_error(exc) from exc
            except BaseException:
                book.release(hold or "")
                raise
            bind_hold(hold, job)
            return JobCreated(job_id=job.id)

        @app.post(api + "/{job_id}/downloads", status_code=202)
        async def download_start(request: Request, job_id: str, body: DownloadRequest) -> dict:
            """The downloads box: make a download's files ready in the background (a panel's print PNG, an export,
            every panel's for a ZIP). The answer and the GET below say how far it is; the file itself is then fetched
            from its usual address by the browser."""
            login_scope(request, job_id)
            if not is_valid_job_id(job_id):
                raise HTTPException(404, "Job not found.")
            try:
                return mgr.start_download(job_id, body.kind, variant=body.variant, preset=body.preset, size=body.size)
            except JobError as exc:
                raise job_error(exc) from exc

        @app.get(api + "/{job_id}/downloads")
        async def download_state(request: Request, job_id: str, key: str = Query(max_length=200)) -> dict:
            login_scope(request, job_id)
            state = mgr.download_state(job_id, key) if is_valid_job_id(job_id) else None
            if state is None:
                raise HTTPException(404, "This download is not known here. Start it again.")
            return state

        @app.get(api + "/{job_id}/panels-eps.zip")
        async def panels_eps_zip(
            request: Request, job_id: str, preset: Literal["flat", "standard", "detailed", "raster"] = "detailed"
        ) -> Response:
            login_scope(request, job_id)
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
            login_scope(request, job_id)
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
            login_scope(request, job_id)  # the partner: the files of jobs made with a client login (its History tab)
            path = job_store.file_path(job_id, filename)
            # a panel's print PNG is made on demand; so is an export the clean-up deleted after a page had listed it
            if path is None and (await mgr.ensure_print_png(job_id, filename)
                                 or await mgr.ensure_export(job_id, filename)):
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
        return HTMLResponse(render_login(next, brand=login_brand(next)), headers={"Cache-Control": "no-store"})

    @app.post("/login", include_in_schema=False)
    async def login(username: str = Form(""), password: str = Form(""), next: str = Form("/")) -> Response:
        role = credentials_role(settings, username, password, tuple(logins.values())) if settings.app_password else None
        if role and role != "full" and site.closed:  # closed on the dashboard: only the studio login may sign in
            return Response(status_code=CLOSED_STATUS, headers={"Cache-Control": "no-store"})
        if role:
            # the demo login goes to the demo page and a client login to its own page, whatever page they came from
            # (a client login keeps the #tab when it came from its page); the studio login goes back where it was
            target = _safe_next(next)
            client = client_of(role)
            if role == "demo":
                target = "/customerep"
            elif role == "partner":
                target = PARTNER_PAGE
            elif client is not None and getattr(brand_for_path(clients, target), "slug", None) != client:
                target = f"/client_{client}"
            response = RedirectResponse(target, status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                make_session(settings, role, logins.get(client) if client else None),
                max_age=SESSION_DAYS * 86400,
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
                path="/",
            )
            return response
        await asyncio.sleep(1)  # slows down password guessing
        return HTMLResponse(render_login(next, "ইউজারনেম বা পাসওয়ার্ড ভুল।", login_brand(next)), status_code=401)

    @app.get("/logout", include_in_schema=False)
    async def logout(next: str = "") -> Response:
        """Signs out; from a client page (?next=/client_<name>) back to that client's sign-in page."""
        target = "/"
        if settings.app_password:
            target = "/login"
            if login_brand(next) is not None:
                target += "?next=" + quote(_safe_next(next), safe="")
        response = RedirectResponse(target, status_code=303)
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

    @app.get("/client_{slug}", include_in_schema=False)
    async def client_page(slug: str) -> HTMLResponse:
        """A client's own demo page (2026-11-22): /customerep with the client's name, logo and colours, from
        client_assets/ (app/clients.py). For the studio login and the page's own login (the middleware keeps every
        other login out); unknown names are 404."""
        brand = clients.get(slug)
        if brand is None:
            raise HTTPException(status_code=404, detail="Not Found")
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(brand_page(page, brand), headers={"Cache-Control": "no-cache"})

    @app.get(PARTNER_PAGE, include_in_schema=False)
    async def partner_dashboard() -> FileResponse:
        """The Partner dashboard (2026-11-23; the owner's name for it since 2026-11-24): for the partner login and the
        head admin. Payments to check, clients, payments and jobs; our tokens and taka only, never the API costs."""
        return FileResponse(STATIC_DIR / "partner-dashboard.html", headers={"Cache-Control": "no-cache"})

    @app.get("/client-dashboard", include_in_schema=False)
    async def client_dashboard_old() -> RedirectResponse:
        """The Partner dashboard's address until 2026-11-24 (the browser keeps the #part of an old link)."""
        return RedirectResponse(PARTNER_PAGE, status_code=307)

    @app.get("/supreme-dashboard", include_in_schema=False)
    async def supreme_dashboard(request: Request) -> FileResponse:
        """The Supreme dashboard (2026-11-24): the head admin's own, with everything (the Partner dashboard's tabs plus
        the LLM costs and the settings). The middleware sends every other login to its own page."""
        if is_limited(request):
            raise limited_refusal(request)
        return FileResponse(STATIC_DIR / "supreme-dashboard.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
