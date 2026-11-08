"""The admin dashboard's data (GET /api/admin/usage): every job and every paid API call, with tokens and costs.

Two sources:
- each job's own call log (pipeline.CALL_LOG), for the jobs that still exist;
- the usage ledger, `<output_dir>/usage_ledger.jsonl` (since 2026-11-08): one compact line per call (no prompts,
  pictures or replies), appended by JobManager._log_call and filled once from the existing call logs at start-up,
  plus one line per deleted job. It keeps a deleted job's calls and costs on the dashboard, since deleting a job in
  History removes its folder and its log.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

LEDGER = "usage_ledger.jsonl"


def job_meta(job) -> dict:
    """What a job was, as the dashboard names it (kept in each ledger line, so a deleted job can still be shown)."""
    o = job.options or {}
    kind = getattr(job.kind, "value", job.kind)
    meta = {"kind": kind, "created_at": job.created_at, "demo": bool(job.demo)}
    if kind == "recreate":
        meta["mode"] = o.get("mode")
        if o.get("mode") == "panels":
            meta["panels"] = o.get("panels") or ["front", "back", "sleeve"]
            meta["back"] = o.get("back")
    else:
        meta["style"] = o.get("render_style")
        meta["refine"] = job.parent is not None
    return meta


def tokens(usage: dict | None) -> dict:
    """The token counts of one call (OpenAI images or Claude), only those the provider reported."""
    u = usage or {}
    details = u.get("input_tokens_details") or {}
    found = {
        "input": u.get("input_tokens"),
        "input_text": details.get("text_tokens"),
        "input_image": details.get("image_tokens"),
        "cache_read": u.get("cache_read_input_tokens"),
        "cache_write": u.get("cache_creation_input_tokens"),
        "output": u.get("output_tokens"),
    }
    return {k: v for k, v in found.items() if isinstance(v, int) and not isinstance(v, bool)}


def compact_call(record: dict) -> dict:
    """One call-log record without its prompt, pictures and reply: what the dashboard and the ledger keep."""
    error = record.get("error") or {}
    return {
        "n": record.get("n"),
        "purpose": record.get("purpose"),
        "panel": record.get("panel"),
        "label": record.get("label"),
        "trigger": record.get("trigger"),
        "attempt": record.get("attempt"),
        "provider": record.get("provider"),
        "model": record.get("model"),
        "size": (record.get("params") or {}).get("size"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "latency_s": record.get("latency_s"),
        "tokens": tokens(record.get("usage")),
        "cost_usd": record.get("cost_usd"),
        "error": error.get("message") if error else None,
    }


def ledger_line(record: dict, job) -> dict:
    return {"type": "call", "job_id": record.get("job_id") or job.id, "job": job_meta(job), **compact_call(record)}


def append(path: Path, entry: dict) -> None:
    """Append one line to the ledger. Never raises: the ledger must not break a job."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        log.exception("could not write the usage ledger %s", path)


def read_ledger(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    out = []
    for line in text.split("\n"):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:  # a line cut short, or one being written: skip it
                continue
    return out


def backfill(path: Path, store, read_calls: Callable[[str], list[dict]]) -> int:
    """Create the ledger from every existing job's call log, once (when the file doesn't exist yet). Returns the
    number of calls written."""
    if path.exists():
        return 0
    lines = []
    for job_id in reversed(store.list_ids()):  # oldest first, like the appends that follow
        job = store.load(job_id)
        if job is None:
            continue
        lines.extend(json.dumps(ledger_line(rec, job), ensure_ascii=False, default=str) for rec in read_calls(job_id))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    os.replace(tmp, path)
    return len(lines)


def _first_preview(job) -> str | None:
    for v in job.variants:
        if v.status == "succeeded" and v.preview:
            return v.preview
    return None


def _job_entry(job, calls: list[dict]) -> dict:
    meta = job_meta(job)
    costs = [c.get("cost_usd") for c in calls]
    return {
        "id": job.id,
        **meta,
        "status": getattr(job.status, "value", job.status),
        "model": job.model,
        "provider": job.provider,
        "resolution": job.resolution,
        "deleted": False,
        "deleted_at": None,
        "thumb": _first_preview(job),
        "variants_ok": sum(v.status == "succeeded" for v in job.variants),
        "variants_failed": sum(v.status == "failed" for v in job.variants),
        # jobs made before 2026-11-03 have no call log: their images have no recorded cost
        "cost_recorded": bool(calls) or not any(v.status == "succeeded" for v in job.variants),
        "cost_usd": round(sum(c for c in costs if c is not None), 6) if calls else None,
        "uncosted_calls": sum(c is None for c in costs),
        "calls": calls,
    }


def parse_floor(value: str) -> str:
    """Settings.dashboard_since as a UTC "YYYY-MM-DDTHH:MM:SS" ('' when empty or unreadable). A date alone means
    midnight on this machine's clock; a time without a zone is this machine's time too."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        log.warning("dashboard_since %r is not an ISO date or time; showing everything", value)
        return ""
    if moment.tzinfo is None:
        moment = moment.astimezone()  # this machine's local time
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def build_report(
    store, ledger_path: Path, read_calls: Callable[[str], list[dict]], days: int = 30, now: datetime | None = None,
    floor: str = "",
) -> dict:
    """Every job created, or with a call, in the last `days` days (0 = all), newest first, with its calls; deleted
    jobs come from the ledger. The browser groups them by day (UTC or local) and adds them up. `floor` (UTC, from
    parse_floor): nothing earlier is shown or counted, neither jobs nor calls."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S") if days else ""
    since = max(since, floor)

    def after_floor(calls: list[dict]) -> list[dict]:
        if not floor:
            return calls
        return [c for c in calls if (c.get("finished_at") or c.get("started_at") or "") >= floor]

    def recent(created: str | None, calls: Iterable[dict]) -> bool:
        if not since:
            return True
        return (created or "") >= since or any((c.get("finished_at") or c.get("started_at") or "") >= since for c in calls)

    jobs, present = [], set()
    for job_id in store.list_ids():
        present.add(job_id)
        job = store.load(job_id)
        if job is None:
            continue
        calls = after_floor([compact_call(r) for r in read_calls(job_id)])
        if recent(job.created_at, calls):
            jobs.append(_job_entry(job, calls))

    deleted: dict[str, dict] = {}
    for entry in read_ledger(ledger_path):
        job_id = entry.get("job_id")
        if not job_id or job_id in present:
            continue
        item = deleted.setdefault(job_id, {"id": job_id, "calls": [], "deleted_at": None, "meta": {}, "summary": {}})
        if entry.get("type") == "deleted":
            item["deleted_at"] = entry.get("at")
            item["meta"] = entry.get("job") or item["meta"]
            item["summary"] = entry
        elif entry.get("type") == "call":
            item["meta"] = item["meta"] or entry.get("job") or {}
            item["calls"].append({k: v for k, v in entry.items() if k not in ("type", "job_id", "job")})
    for job_id, item in deleted.items():
        meta, calls, summary = item["meta"], after_floor(item["calls"]), item["summary"]
        if not recent(meta.get("created_at"), calls):
            continue
        costs = [c.get("cost_usd") for c in calls]
        jobs.append({
            "id": job_id, **meta,
            "status": summary.get("status"), "model": summary.get("model"), "provider": summary.get("provider"),
            "resolution": summary.get("resolution"),
            "deleted": True, "deleted_at": item["deleted_at"], "thumb": None,
            "variants_ok": summary.get("variants_ok"), "variants_failed": summary.get("variants_failed"),
            "cost_recorded": bool(calls), "cost_usd": round(sum(c for c in costs if c is not None), 6) if calls else None,
            "uncosted_calls": sum(c is None for c in costs), "calls": calls,
        })
    jobs.sort(key=lambda j: j["id"], reverse=True)  # job ids sort by creation time
    return {
        "generated_at": now.isoformat(timespec="seconds"), "days": days, "since": since or None,
        "floor": f"{floor}Z" if floor else None,  # the browser drops rows before this day too
        "jobs": jobs,
    }


def deleted_line(job) -> dict:
    """The ledger's note that a job was deleted in History, with what the dashboard shows for it."""
    from app.storage import utcnow_iso  # local import: storage imports schemas, which this module does not need

    return {
        "type": "deleted", "job_id": job.id, "at": utcnow_iso(), "job": job_meta(job),
        "status": getattr(job.status, "value", job.status), "model": job.model, "provider": job.provider,
        "resolution": job.resolution,
        "variants_ok": sum(v.status == "succeeded" for v in job.variants),
        "variants_failed": sum(v.status == "failed" for v in job.variants),
    }
