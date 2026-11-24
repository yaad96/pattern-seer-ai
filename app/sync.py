"""The Mac's Dashboard data on the online server (2026-11-23; the owner asked to see "the local machine's dashboard
data" from the online Dashboard).

The Mac (JERSEY_SYNC_URL and JERSEY_SYNC_TOKEN set) sends its whole Dashboard report every few minutes and when its
Dashboard's button is pressed. The server (the same JERSEY_SYNC_TOKEN) keeps the latest one per machine in
<output_dir>/remote/<machine>.json, and its Dashboard shows those jobs beside its own, with a machine filter. Only the
report travels: numbers, no prompts or pictures (the report has none, and the thumbnails stay on the Mac)."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app.storage import is_valid_job_id, utcnow_iso
from app.usage import TEXT_FIELDS

log = logging.getLogger(__name__)

REMOTE_DIR = "remote"
MACHINE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # starts with a letter or digit (never "__proto__" and the like)
MAX_BODY = 8 * 1024 * 1024
MAX_JOBS = 5000  # far above a year of the Mac's jobs; a report is the whole history
MAX_CALLS = 500  # per job
PATH = "/api/sync/report"
TIMEOUT_S = 20.0  # per socket operation; a stalled line shouldn't hold the button or a shutdown for long

# What a report's jobs and calls may hold, checked before anything is stored: the server's Dashboard trusts them.
_JOB_TEXT = ("created_at", "status", "kind", "mode", "client", "model", "provider", "resolution", "back", "style", "chat",
             "deleted_at", "machine")
_JOB_FLAGS = ("demo", "deleted", "refine", "cost_recorded")
_JOB_NUMBERS = ("tokens", "cost_usd", "uncosted_calls", "variants_ok", "variants_failed")
_CALL_TEXT = ("started_at", "finished_at", "status", "purpose", "provider", "model", "size", "trigger", "label", "panel",
              "error", "requested_model")
_CALL_NUMBERS = ("cost_usd", "attempt", "latency_s", "variant")


def _number(value) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _check_job(job: object) -> None:
    """ValueError naming what is wrong with one job of a report."""
    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not is_valid_job_id(job["id"]):
        raise ValueError("jobs: each job needs a job id")
    name = job["id"]
    for key in _JOB_TEXT:
        if not isinstance(job.get(key), (str, type(None))):
            raise ValueError(f"job {name}: {key} must be text")
    for key in _JOB_FLAGS:
        if not isinstance(job.get(key), (bool, type(None))):
            raise ValueError(f"job {name}: {key} must be true or false")
    for key in _JOB_NUMBERS:
        if not _number(job.get(key)):
            raise ValueError(f"job {name}: {key} must be a number")
    panels = job.get("panels")
    if panels is not None and not (isinstance(panels, list) and all(isinstance(x, str) for x in panels)):
        raise ValueError(f"job {name}: panels must be a list of names")
    items = job.get("token_items")
    if items is not None and not (isinstance(items, list) and all(
            isinstance(i, dict) and isinstance(i.get("item"), (str, type(None))) and _number(i.get("tokens")) for i in items)):
        raise ValueError(f"job {name}: token_items must be a list of items")
    calls = job.get("calls", [])
    if not isinstance(calls, list) or len(calls) > MAX_CALLS:
        raise ValueError(f"job {name}: calls must be a list of at most {MAX_CALLS}")
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError(f"job {name}: each call must be an object")
        for key in _CALL_TEXT:
            if not isinstance(call.get(key), (str, type(None))):
                raise ValueError(f"job {name}: a call's {key} must be text")
        for key in _CALL_NUMBERS:
            if not _number(call.get(key)):
                raise ValueError(f"job {name}: a call's {key} must be a number")
        tokens = call.get("tokens")
        if tokens is not None and not (isinstance(tokens, dict) and all(_number(v) for v in tokens.values())):
            raise ValueError(f"job {name}: a call's tokens must be numbers")


def token_ok(expected: str, header: str | None) -> bool:
    """Whether an Authorization header carries the shared sync token (compared in constant time)."""
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].encode(), expected.encode())


def clean_report(payload: object) -> tuple[str, dict]:
    """The machine name and the report from a pushed body, checked in full (the Dashboard trusts what is stored);
    ValueError naming the first problem otherwise."""
    if not isinstance(payload, dict):
        raise ValueError("the body must be an object")
    machine = payload.get("machine")
    if not isinstance(machine, str) or not MACHINE_RE.fullmatch(machine):
        raise ValueError("machine: lower-case letters, digits, - or _ (1 to 32), starting with a letter or digit")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("jobs: a list of jobs")
    if len(jobs) > MAX_JOBS:
        raise ValueError(f"jobs: at most {MAX_JOBS}")
    for job in jobs:
        _check_job(job)
    for job in jobs:
        job["thumb"] = None  # the pictures stay on the sending machine
        for key in TEXT_FIELDS:  # and so do a Design message's words
            job.pop(key, None)
    generated = payload.get("generated_at")
    return machine, {"machine": machine, "generated_at": generated if isinstance(generated, str) else None,
                     "received_at": utcnow_iso(), "jobs": jobs}


def save_remote(output_dir: Path, machine: str, report: dict) -> None:
    """Keep a machine's latest report, replaced whole (a temp file of its own per write, so two reports arriving at
    once can't trip over each other)."""
    folder = Path(output_dir) / REMOTE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{machine}.json"
    tmp = folder / f".{machine}.{uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_remote(output_dir: Path) -> list[dict]:
    """The latest report of each other machine (unreadable files are skipped)."""
    out = []
    for path in sorted((Path(output_dir) / REMOTE_DIR).glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("could not read %s", path)
            continue
        if isinstance(report, dict) and isinstance(report.get("jobs"), list):
            out.append(report)
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: urllib would re-send the token to wherever the server points, as a GET without the
    report, and that would read as sent."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read() or b"{}").get("detail")
    except Exception:
        return ""
    return detail if isinstance(detail, str) else ""


def push(url: str, token: str, machine: str, report: dict, timeout: float = TIMEOUT_S) -> dict:
    """Send this machine's report to the server (blocking; run in a worker thread). Never raises: the result says how
    it went. https only (plain http only to this computer), no redirects, and sent only when the server says so."""
    at = utcnow_iso()

    def failed(error: str) -> dict:
        return {"ok": False, "at": at, "jobs": None, "error": error}

    try:
        parts = urlsplit(url or "")
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")):
            return failed("JERSEY_SYNC_URL must be the server's https:// address")
        jobs = [{k: v for k, v in job.items() if k not in TEXT_FIELDS} for job in report.get("jobs", [])]  # numbers only
        body = json.dumps({"machine": machine, "generated_at": report.get("generated_at"),
                           "jobs": jobs}, ensure_ascii=False, default=str).encode("utf-8")
        if len(body) > MAX_BODY:
            return failed(f"the report is too large ({len(body) / 1e6:.1f} MB; the server takes {MAX_BODY / 1e6:.0f} MB)")
        request = urllib.request.Request(
            url.rstrip("/") + PATH, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "User-Agent": "jersey-hd-studio-sync"})
        with _OPENER.open(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            return failed("the server didn't confirm the report")
        return {"ok": True, "at": utcnow_iso(), "jobs": answer.get("jobs"), "error": None}
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return failed(f"the server redirected the report ({exc.code}): set JERSEY_SYNC_URL to its final https address")
        detail = _detail(exc)
        hint = " (check JERSEY_SYNC_URL and JERSEY_SYNC_TOKEN)" if exc.code == 404 else ""
        return failed(f"the server answered {exc.code}" + (f": {detail}" if detail else "") + hint)
    except Exception as exc:  # unreachable, a bad address, a broken reply: the status says so, nothing escapes
        return failed(f"could not reach the server ({type(exc).__name__}: {exc})")
