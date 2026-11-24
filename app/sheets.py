"""Every interaction, also in a Google Sheet (2026-11-23; the owner: "since we are talking about real people, real
money, we need to save every interaction"; "the columns should be as many as possible and also should pack as much
textual data as possible", with the real cost, from the server and the Mac; then "no need to save the prompt, because
they are templates", so the prompts stay in the job's own files only).

- One row per event: each job when it ends (made, made again, interrupted), and each top-up or take-back of tokens.
- Each row goes first to a local outbox, <output_dir>/sheet_outbox.jsonl (append-only, fsync'd), so nothing is lost
  while the sheet is unreachable or not set up yet. A background sender delivers the waiting rows in order to a
  Google Apps Script web app bound to the owner's sheet (google_sheet_script.gs). <output_dir>/sheet_cursor.json
  keeps how far into the outbox everything is delivered (a byte offset), moved on only when the sheet confirms every
  row of a batch; so only new rows are ever read. The script skips a Row ID it already has, so a row sent twice
  (a batch tried again) appears once.
- The link and its shared secret come from the key file `.google_sheet` (url=…, secret=…), never from the
  environment, like the API keys. Without it the rows still go to the outbox; they are sent once it is set.
- The first start with this module also queues every earlier job and top-up, so the sheet starts complete.
- A job never waits for the sheet."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.storage import append_line, utcnow_iso

log = logging.getLogger(__name__)

OUTBOX = "sheet_outbox.jsonl"
CURSOR = "sheet_cursor.json"
BATCH = 50  # rows per request
CELL_LIMIT = 45_000  # a Google Sheets cell holds 50,000 characters
TIMEOUT_S = 30.0
IDLE_S = 60.0  # how often the sender looks again with nothing to do
BD = timezone(timedelta(hours=6), "Asia/Dhaka")  # Bangladesh: UTC+6 all year

# The sheet's columns, in order (the script adds any new one to the header row).
COLUMNS = [
    "Row ID", "Event", "Machine", "Time (UTC)", "Time (Bangladesh)", "Login", "Customer", "Client ID",
    "Customer phone", "Job ID",
    "Job started (UTC)", "Job started (Bangladesh)", "Took (s)", "Type", "What was asked", "Back made from",
    "Status", "Images made", "Images failed", "Failed parts", "Tokens charged", "Token items", "Tokens added",
    "Tokens left after", "Taka received", "Top-up note", "Added by", "bKash TrxID", "bKash sender", "Checked by",
    "Reason", "bKash package taka", "bKash package tokens", "bKash sent (UTC)", "bKash sent (Bangladesh)",
    "Real cost (USD)", "Image cost (USD)",
    "AI designer cost (USD)", "API calls", "Calls of unknown cost", "Model", "Resolution", "Chat ID",
    "Change request", "Client's message", "Designer's reply", "Design title", "Design brief", "Suggestions",
    "Staff notes", "Removals", "Fidelity", "Warnings", "Errors", "Reference check", "Input files", "Output files",
]

ITEM_NAMES = {"front": "Front", "back_picture": "Back (own picture)", "back_same": "Back (same as front)",
              "sleeve": "Sleeve", "design": "Design message"}
MODE_NAMES = {"pattern": "Pattern only", "flat_jersey": "Flat jersey artwork", "replica": "HD replica",
              "panels": "Production panels"}
STATUS_NAMES = {"queued": "Queued", "running": "Working", "succeeded": "Done", "partial": "Partly done",
                "failed": "Failed"}
REMOVALS = (("remove_logos", "logos"), ("remove_sponsors", "sponsors"), ("remove_names", "names"),
            ("remove_numbers", "numbers"), ("remove_other_text", "other text"))


def _times(iso: str | None) -> tuple[str, str]:
    """An ISO time as 'YYYY-MM-DD HH:MM:SS' in UTC and in Bangladesh."""
    if not iso:
        return "", ""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso, ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return utc, moment.astimezone(BD).strftime("%Y-%m-%d %H:%M:%S")


def _text(value) -> str:
    """A cell's text, cut to what a cell holds."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= CELL_LIMIT else text[:CELL_LIMIT] + " …(cut)"


def _login(job) -> str:
    if getattr(job, "client", None):
        return f"Client ({job.client})"
    return "Demo" if getattr(job, "demo", False) else "Head admin"


def _item(job, variant) -> str:
    from app.tokens import item_of  # here: tokens.py imports nothing from this module

    name = item_of(job, variant)
    return ITEM_NAMES.get(name or "", variant.label or f"Image {variant.index}")


def job_row(job, event: str, *, charged=(), balance_after: int | None = None, customer: str = "",
            calls=(), at: str | None = None) -> dict:
    """A job's row: what was asked and made, what it cost (tokens and money) and every text it carries. `at`: when
    the event happened (default now; an earlier job's row uses its last change)."""
    kind = getattr(job.kind, "value", job.kind)
    options = job.options or {}
    now = at or utcnow_iso()
    now_utc, now_bd = _times(now)
    start_utc, start_bd = _times(job.created_at)
    try:  # from the job's start to this row (the job's end)
        took = round((datetime.fromisoformat(now) - datetime.fromisoformat(job.created_at)).total_seconds(), 1)
    except (TypeError, ValueError):
        took = ""
    if kind == "design":
        asked = "Design message"
        back = ""
    elif options.get("mode") == "panels":
        asked = ", ".join(_item(job, v) for v in job.variants) or ", ".join(options.get("panels") or [])
        back = {"mirror": "Front, mirrored", "copy": "Front, copied", "image": "Its own picture"}.get(
            options.get("back"), "") if "back" in (options.get("panels") or []) else ""
    else:
        asked = MODE_NAMES.get(options.get("mode"), options.get("mode") or "")
        back = ""
    image_cost = sum(c.get("cost_usd") or 0 for c in calls if c.get("purpose") == "image")
    designer_cost = sum(c.get("cost_usd") or 0 for c in calls if c.get("purpose") != "image")
    outputs = []
    for v in job.variants:
        outputs += [name for name in (v.png, v.raw, v.preview) if name]
    return {
        "Event": event, "Time (UTC)": now_utc, "Time (Bangladesh)": now_bd, "Login": _login(job),
        "Customer": customer, "Client ID": job.client or "", "Job ID": job.id,
        "Job started (UTC)": start_utc, "Job started (Bangladesh)": start_bd, "Took (s)": took,
        "Type": "Design from description" if kind == "design" else MODE_NAMES.get(options.get("mode"), "Recreate"),
        "What was asked": asked, "Back made from": back,
        "Status": STATUS_NAMES.get(getattr(job.status, "value", job.status), str(job.status)),
        "Images made": sum(v.status == "succeeded" for v in job.variants),
        "Images failed": sum(v.status == "failed" for v in job.variants),
        "Failed parts": ", ".join(_item(job, v) for v in job.variants if v.status == "failed"),
        "Tokens charged": sum(-e["tokens"] for e in charged) if charged else (0 if job.client else ""),
        "Token items": ", ".join(f"{ITEM_NAMES.get(e.get('item'), e.get('item'))} {-e['tokens']}" for e in charged),
        "Tokens left after": "" if balance_after is None else balance_after,
        "Real cost (USD)": round(job.cost_usd, 6) if isinstance(job.cost_usd, (int, float)) else "",
        "Image cost (USD)": round(image_cost, 6) if calls else "",
        "AI designer cost (USD)": round(designer_cost, 6) if calls else "",
        "API calls": job.api_calls, "Calls of unknown cost": job.uncosted_calls,
        "Model": job.model or "", "Resolution": job.resolution or "",
        "Chat ID": job.session_id or "" if kind == "design" else "",
        "Change request": ("yes" if job.parent is not None else "no") if kind == "design" else "",
        "Client's message": _text(job.message), "Designer's reply": _text(job.reply),
        "Design title": job.brief.title if job.brief is not None else "",
        "Design brief": _text(job.brief.model_dump(mode="json")) if job.brief is not None else "",
        "Suggestions": _text("; ".join(job.suggestions)),
        "Staff notes": _text(options.get("notes") or ""),
        "Removals": ", ".join(name for key, name in REMOVALS if options.get(key)) if kind != "design" else "",
        "Fidelity": options.get("fidelity") or "",
        "Warnings": _text("\n".join(job.warnings)),
        "Errors": _text("\n".join(filter(None, [job.error] + [
            f"{v.label or f'Image {v.index}'}: {v.error}" for v in job.variants if v.error]))),
        "Reference check": _text(job.reference_analysis.model_dump(mode="json")) if job.reference_analysis else "",
        "Input files": ", ".join(job.inputs), "Output files": ", ".join(outputs),
    }


WHO = {"studio": "Head admin", "partner": "Partner"}  # the ledger keeps "studio" for the head admin
# The events of a bKash payment (app/payments.py). The sheet's script (sheet_script.gs) copies the two checked ones
# from the log tab to its "bKash" tab.
PAY_SENT, PAY_APPROVED, PAY_REJECTED = "bKash payment sent", "bKash payment approved", "bKash payment rejected"
SCRIPT = Path(__file__).with_name("sheet_script.gs")  # the Apps Script, with a place for the secret
SCRIPT_VERSION = 2  # what sheet_script.gs answers as "version"; a script from before 2026-11-24 answers none


def script_text(secret: str) -> str:
    """The Apps Script to paste into the sheet, with this machine's secret in it (the Supreme dashboard's Settings
    hand it to the head admin; nothing else ever shows the secret)."""
    return SCRIPT.read_text(encoding="utf-8").replace("__SECRET__", json.dumps(secret), 1)


def topup_row(entry: dict, customer: str = "", *, event: str | None = None) -> dict:
    """A top-up or take-back of tokens (a token-ledger line)."""
    at_utc, at_bd = _times(entry.get("at"))
    who = WHO.get(entry.get("by"), entry.get("by") or "")
    return {
        "Event": event or ("Tokens added" if entry["tokens"] > 0 else "Tokens taken back"),
        "Time (UTC)": at_utc, "Time (Bangladesh)": at_bd, "Login": who or "Head admin", "Customer": customer,
        "Client ID": entry.get("client", ""), "Tokens added": entry["tokens"],
        "Tokens left after": entry.get("balance", ""),
        "Taka received": entry["taka"] if isinstance(entry.get("taka"), int) else "",
        "Top-up note": _text(entry.get("note") or ""),
        "Added by": who,
    }


def customer_row(brand, by: str) -> dict:
    """A customer made with the dashboards' builder (app/customers.py): who made it, its link, login name and phone.
    Never its password."""
    at_utc, at_bd = _times(brand.made_at)
    return {
        "Event": "Customer added", "Time (UTC)": at_utc, "Time (Bangladesh)": at_bd, "Login": WHO.get(by, by),
        "Customer": brand.name, "Client ID": brand.slug, "Customer phone": brand.phone,
        "Top-up note": f"Entry point {brand.path}, username {brand.login.username if brand.login else ''}",
        "Added by": WHO.get(by, by),
    }


def payment_row(pay: dict, event: str, customer: str = "", *, balance: int | None = None, reason: str = "") -> dict:
    """A bKash payment (app/payments.py): sent by the client, then approved (its tokens added) or rejected."""
    at_utc, at_bd = _times(pay.get("checked_at") if event != PAY_SENT else pay.get("at"))
    who = WHO.get(pay.get("checked_by"), pay.get("checked_by") or "")
    row = {
        "Event": event, "Time (UTC)": at_utc, "Time (Bangladesh)": at_bd,
        "Login": f"Client ({pay.get('client', '')})" if event == PAY_SENT else who,
        "Customer": customer, "Client ID": pay.get("client", ""),
        "Tokens added": pay.get("tokens", "") if event == PAY_APPROVED else 0,
        "Tokens left after": "" if balance is None else balance,
        "Taka received": pay.get("taka", "") if event == PAY_APPROVED else "",
        "Top-up note": f"bKash payment of {pay.get('taka', '')} taka for {pay.get('tokens', '')} tokens",
        "Added by": who if event == PAY_APPROVED else "",
        "bKash TrxID": pay.get("trx", ""), "bKash sender": pay.get("sender", ""),
        "Checked by": who if event != PAY_SENT else "", "Reason": _text(reason),
        # the package the client said it paid for, and when (a rejected payment adds no tokens and no taka)
        "bKash package taka": pay.get("taka", ""), "bKash package tokens": pay.get("tokens", ""),
    }
    row["bKash sent (UTC)"], row["bKash sent (Bangladesh)"] = _times(pay.get("at"))
    return row


def backfill_rows(store, entries: list[dict], client_name, read_calls) -> list[dict]:
    """The rows of every job and top-up from before the sheet log, oldest first (the first start only)."""
    charges: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("type") == "charge":
            charges.setdefault(e.get("job_id"), []).append(e)
    dated: list[tuple[str, dict]] = []
    for job_id in store.list_ids():
        job = store.load(job_id)
        if job is None:
            continue
        mine = charges.get(job.id, [])
        row = job_row(job, "Earlier job", charged=mine, balance_after=mine[-1].get("balance") if mine else None,
                      customer=client_name(job.client) if job.client else "", calls=read_calls(job.id),
                      at=job.updated_at)
        dated.append((job.updated_at or job.created_at or "", row))
    for e in entries:
        if e.get("type") != "charge":
            event = "Earlier top-up" if e["tokens"] > 0 else "Earlier take-back"
            dated.append((e.get("at") or "", topup_row(e, client_name(e.get("client")), event=event)))
    dated.sort(key=lambda pair: pair[0])
    return [row for _, row in dated]


class SheetLog:
    """The outbox and its sender. enqueue() is safe anywhere on the event loop; run() is the sender task."""

    def __init__(self, output_dir: Path, url: str, secret: str, machine: str) -> None:
        self.outbox = Path(output_dir) / OUTBOX
        self.cursor_path = Path(output_dir) / CURSOR
        self.url, self.secret, self.machine = url, secret, machine
        self.fresh = not self.outbox.exists()  # the first start: earlier jobs are queued too (backfill)
        self._wake: asyncio.Event | None = None
        saved = self._saved()
        # sheet_url: the sheet's own address, and script: its script's version, both as the script last said them
        self.status = {"enabled": bool(url and secret), "pending": 0, "last_ok_at": None, "last_error": None,
                       "sheet_url": saved.get("sheet_url"), "script": saved.get("script"),
                       "script_wanted": SCRIPT_VERSION}
        self.status["pending"] = len(self.pending())

    def reload(self) -> None:
        """Read the outbox's state again: main.py calls it right after the server takes the output folder (a server
        still stopping when this one was made may have written to it since)."""
        self.fresh = not self.outbox.exists()
        self.status["pending"] = len(self.pending())

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret)

    def _saved(self) -> dict:
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _offset(self) -> int:
        """How far into the outbox everything is delivered (bytes)."""
        offset = self._saved().get("offset", 0)
        return offset if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0 else 0

    def _save_offset(self, offset: int) -> None:
        tmp = self.cursor_path.with_name(f".{self.cursor_path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps({"offset": offset, "at": utcnow_iso(), "sheet_url": self.status.get("sheet_url"),
                                       "script": self.status.get("script")}), encoding="utf-8")
            os.replace(tmp, self.cursor_path)
        finally:
            tmp.unlink(missing_ok=True)

    def enqueue(self, row: dict) -> None:
        """Keep a row (fsync'd) and wake the sender. Never raises: a job must not fail for the sheet."""
        try:
            row = {"Row ID": f"{self.machine}-{uuid4().hex[:16]}", "Machine": self.machine, **row}
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            append_line(self.outbox, json.dumps(row, ensure_ascii=False, default=str), sync=True)
            self.status["pending"] += 1
            if self._wake is not None:
                self._wake.set()
        except Exception:
            log.exception("sheet: could not keep a row in %s", self.outbox)

    def _waiting(self) -> list[tuple[dict, int]]:
        """The rows after the cursor, oldest first, each with the outbox offset just past its line. A line that
        isn't a row (cut short by a crash) is passed over."""
        try:
            with open(self.outbox, "rb") as fh:
                start = self._offset()
                fh.seek(start)
                data = fh.read()
        except FileNotFoundError:
            return []
        out, pos = [], start
        for raw in data.split(b"\n")[:-1]:  # complete lines only (each ends with a newline)
            pos += len(raw) + 1
            try:
                row = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("Row ID"):
                out.append((row, pos))
        return out

    def pending(self) -> list[dict]:
        """The rows not delivered yet, oldest first."""
        return [row for row, _ in self._waiting()]

    def _post(self, rows: list[dict]) -> list[str]:
        """Send rows to the web app (blocking; a worker thread). The delivered Row IDs, or an exception. Apps Script
        answers a POST with a redirect to its result, which urllib follows with a GET (no body, no secret)."""
        body = json.dumps({"secret": self.secret, "columns": COLUMNS, "rows": rows}, ensure_ascii=False,
                          default=str).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "jersey-hd-studio"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            reason = answer.get("error") if isinstance(answer, dict) else None
            raise RuntimeError(f"the sheet refused the rows ({reason or 'no reason given'})")
        # the script since 2026-11-24 also says its version and the sheet's own address
        version = answer.get("version")
        self.status["script"] = version if isinstance(version, int) and not isinstance(version, bool) else None
        link = answer.get("sheet_url")
        if isinstance(link, str) and link.startswith("https://docs.google.com/spreadsheets/") and len(link) < 300:
            self.status["sheet_url"] = link
        return [str(i) for i in answer.get("added") or []]

    async def check(self) -> dict:
        """Ask the sheet's script which version it is (a request with no rows; nothing is written): the Supreme
        dashboard's "Check the sheet" after a new script was pasted. The status, with the error if it failed."""
        if not self.enabled:
            return dict(self.status)
        try:
            await asyncio.to_thread(self._post, [])
            self.status.update(last_ok_at=utcnow_iso(), last_error=None)
            self._save_offset(self._offset())  # keeps what the script said (its version, the sheet's address)
        except Exception as exc:  # noqa: BLE001 - unreachable or refused: said on the dashboard
            self.status["last_error"] = f"{type(exc).__name__}: {exc}"
        if self._wake is not None:
            self._wake.set()  # anything waiting goes now
        return dict(self.status)

    async def send_pending(self) -> int:
        """Deliver what is waiting, in batches; the number delivered. Raises on the first failure, leaving the cursor
        where the sheet last confirmed everything."""
        delivered = 0
        waiting = self._waiting()
        for start in range(0, len(waiting), BATCH):
            batch = waiting[start:start + BATCH]
            rows = [row for row, _ in batch]
            confirmed = set(await asyncio.to_thread(self._post, rows))
            missing = [row["Row ID"] for row in rows if row["Row ID"] not in confirmed]
            if missing:
                raise RuntimeError(f"the sheet didn't confirm {len(missing)} row(s)")
            self._save_offset(max(batch[-1][1], self._offset()))  # never back: a row confirmed stays confirmed
            delivered += len(rows)
            self.status.update(last_ok_at=utcnow_iso(), last_error=None, pending=len(waiting) - start - len(batch))
        return delivered

    async def run(self) -> None:
        """The sender: delivers whenever a row is kept, and looks again every minute; waits longer after failures."""
        self._wake = asyncio.Event()
        delay = IDLE_S
        while True:
            if self.enabled and self.status["pending"]:
                try:
                    count = await self.send_pending()
                    if count:
                        log.info("sheet: delivered %d row(s)", count)
                    delay = IDLE_S
                except Exception as exc:  # unreachable, refused: kept for the next try
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                    log.warning("sheet: %s (%d row(s) waiting)", self.status["last_error"], self.status["pending"])
                    delay = min(max(delay * 2, 30.0), 600.0)
            self.status["pending"] = len(self._waiting())
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
