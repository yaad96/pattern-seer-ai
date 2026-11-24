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

from app.storage import append_line

log = logging.getLogger(__name__)

LEDGER = "usage_ledger.jsonl"


def job_meta(job) -> dict:
    """What a job was, as the dashboard names it (kept in each ledger line, so a deleted job can still be shown)."""
    o = job.options or {}
    kind = getattr(job.kind, "value", job.kind)
    meta = {"kind": kind, "created_at": job.created_at, "demo": bool(job.demo)}
    if job.client:  # made with a client page's own login (app/clients.py)
        meta["client"] = job.client
    if kind == "recreate":
        meta["mode"] = o.get("mode")
        if o.get("mode") == "panels":
            meta["panels"] = o.get("panels") or ["front", "back", "sleeve"]
            meta["back"] = o.get("back")
    else:
        meta["style"] = o.get("render_style")
        meta["refine"] = job.parent is not None
        meta["chat"] = job.session_id  # the Design chat it belongs to: the dashboards nest a chat's messages
    return meta


# A Design message's words (the client's message, the AI designer's reply and the design's name), shown on this
# machine's dashboards only: kept out of the usage ledger (compact) and of the reports sent to another (app/sync.py).
TEXT_FIELDS = ("message", "reply", "title")
TEXT_LIMITS = {"message": 600, "reply": 400, "title": 120}


def design_texts(job) -> dict:
    """The words of a Design job for the dashboards, shortened (History has them in full); {} for other jobs."""
    if getattr(job.kind, "value", job.kind) != "design":
        return {}
    title = job.brief.title if job.brief is not None else None
    texts = {"message": job.message, "reply": job.reply, "title": title}
    return {key: (value or "")[:TEXT_LIMITS[key]] or None for key, value in texts.items()}


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
        append_line(path, json.dumps(entry, ensure_ascii=False, default=str))
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
        **design_texts(job),
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


def token_charges(entries: Iterable[dict]) -> dict[str, dict]:
    """Our tokens charged per job (app/tokens.py ledger): {job_id: {"tokens": total, "items": [{item, tokens}]}}."""
    out: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "charge" or not entry.get("job_id"):
            continue
        item = out.setdefault(entry["job_id"], {"tokens": 0, "items": []})
        item["tokens"] += -entry["tokens"]
        item["items"].append({"item": entry.get("item"), "tokens": -entry["tokens"], "variant": entry.get("variant")})
    return out


def build_report(
    store, ledger_path: Path, read_calls: Callable[[str], list[dict]], days: int = 30, now: datetime | None = None,
    floor: str = "", tokens: Iterable[dict] | None = None, remote: Iterable[dict] = (), machine: str = "server",
) -> dict:
    """Every job created, or with a call, in the last `days` days (0 = all), newest first, with its calls; deleted
    jobs come from the ledger. The browser groups them by day (UTC or local) and adds them up. `floor` (UTC, from
    parse_floor): nothing earlier is shown or counted, neither jobs nor calls. `tokens`: the token ledger's entries
    (each job gets the tokens charged for it); `remote`: the latest reports other machines sent (app/sync.py), whose
    jobs are added with their machine's name; this machine's jobs are named `machine`."""
    charges = token_charges(tokens or [])
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
    for job in jobs:  # this machine's jobs: its name and our tokens
        job["machine"] = machine
        charged = charges.get(job["id"])
        job["tokens"] = charged["tokens"] if charged else None
        job["token_items"] = charged["items"] if charged else []
    own = {j["id"] for j in jobs}
    for report in remote:  # other machines' jobs, as their own Dashboard shows them
        name = report.get("machine") or "?"
        for job in report.get("jobs") or []:
            # sync.clean_report checks every report it stores; this keeps the Dashboard up even with an older file
            if (not isinstance(job, dict) or job.get("id") in own or not isinstance(job.get("id"), str)
                    or not isinstance(job.get("created_at"), (str, type(None))) or not isinstance(job.get("calls", []), list)):
                log.warning("sync: skipped a malformed job in %s's report", name)
                continue
            calls = after_floor([c for c in job.get("calls") or [] if isinstance(c, dict)
                                 and isinstance(c.get("started_at"), (str, type(None)))
                                 and isinstance(c.get("finished_at"), (str, type(None)))])
            if recent(job.get("created_at"), calls):
                jobs.append({**job, "calls": calls, "machine": name, "thumb": None})
    jobs.sort(key=lambda j: j["id"], reverse=True)  # job ids sort by creation time
    return {
        "generated_at": now.isoformat(timespec="seconds"), "days": days, "since": since or None,
        "floor": f"{floor}Z" if floor else None,  # the browser drops rows before this day too
        "machine": machine,
        "remote": [{"machine": r.get("machine"), "generated_at": r.get("generated_at"),
                    "received_at": r.get("received_at"), "jobs": len(r.get("jobs") or [])} for r in remote],
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
