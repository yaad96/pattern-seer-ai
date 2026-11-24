"""bKash payments, checked by hand (2026-11-24; the owner: "lets do manual").

A client buys tokens from its own page: it picks a package, sends the money with bKash (Send Money, to the personal
number the head admin set on the Supreme dashboard) and types the TrxID bKash gave it. That makes a payment request,
waiting. The head admin or the partner looks the TrxID up in the bKash app and approves it (the tokens are then added
to the client's purse, app/tokens.py) or rejects it with a reason the client sees.

- The number and the packages are the head admin's settings, <output_dir>/payment_settings.json. Without a number or
  without a package, nothing can be bought.
- Every request and every decision is a line of <output_dir>/payments.jsonl, append-only (fsync'd) and never
  rewritten; a request's state is what its lines add up to.
- A TrxID can be waiting or approved only once, for any client; a rejected one can be sent again.
- Approving writes the top-up to the token ledger first, under the request id "pay-<id>", then the decision here.
  The ledger takes a request id once, so two people pressing Approve add the tokens once; and a crash between the
  two lines is repaired at the next start (PayBook.repair).

Used on the event-loop thread only (the routes), like the token ledger."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.storage import append_line, utcnow_iso

log = logging.getLogger(__name__)

FILE = "payments.jsonl"
SETTINGS_FILE = "payment_settings.json"
MAX_PACKAGES = 6
MAX_WAITING = 3  # payment requests one client may have waiting at once
MAX_A_DAY = 20  # and may send in 24 hours, whatever became of them
MAX_TOKENS = 10_000_000
MAX_TAKA = 10_000_000
REASONS = ("not_found", "amount", "used", "other")
REASON_TEXT = {  # as the dashboards and the sheet say it; the client's page has its own words (customerboard.js)
    "not_found": "TrxID not found in bKash",
    "amount": "The amount does not match",
    "used": "This TrxID was used before",
}
NUMBER_RE = re.compile(r"01[3-9]\d{8}")  # a Bangladeshi mobile number, as bKash shows it
TRX_RE = re.compile(r"[A-Z0-9]{8,16}")  # bKash's TrxID: 10 letters and digits today; some room either way


class PayError(Exception):
    """A request that can't be taken or decided, with the HTTP status that says why."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def clean_number(value: str) -> str:
    """'01712-345678', '+880 1712 345678' -> '01712345678'; '' when it isn't a mobile number."""
    digits = re.sub(r"[\s\-()+]", "", value or "")
    if digits.startswith("880"):
        digits = digits[2:]
    return digits if NUMBER_RE.fullmatch(digits) else ""


def clean_trx(value: str) -> str:
    """A TrxID as typed (spaces, lower case) -> as bKash writes it; '' when it can't be one."""
    trx = re.sub(r"\s", "", value or "").upper()
    return trx if TRX_RE.fullmatch(trx) else ""


def reason_text(pay: dict) -> str:
    """Why a payment was rejected, in English: the standard reason, or what was written."""
    return REASON_TEXT.get(pay.get("reason") or "", pay.get("reason_text") or "")


class PayBook:
    """The payment requests and the bKash settings."""

    def __init__(self, output_dir: Path) -> None:
        self.path = Path(output_dir) / FILE
        self.settings_path = Path(output_dir) / SETTINGS_FILE
        self.reload()

    def reload(self) -> None:
        """Read both files again: main.py calls it right after the server takes the output folder, like the ledger."""
        self._pays: dict[str, dict] = {}  # id -> the request as it stands, in the order sent
        self._forms: dict[str, str] = {}  # a buy form's request id -> the payment it made
        for line in self._read():
            self._apply(line)
        self._settings = self._load_settings()

    # ---- the bKash number and the packages (the head admin's) ----

    def _load_settings(self) -> dict:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            number, packages = self._checked(raw.get("number") or "", raw.get("packages") or [])
        except FileNotFoundError:
            return {"number": "", "packages": [], "updated_at": None, "updated_by": None}
        except (OSError, ValueError, AttributeError, TypeError, PayError):
            log.exception("payments: %s can't be used; buying is off until the settings are saved again",
                          self.settings_path)
            return {"number": "", "packages": [], "updated_at": None, "updated_by": None}
        return {"number": number, "packages": packages, "updated_at": raw.get("updated_at"),
                "updated_by": raw.get("updated_by")}

    @staticmethod
    def _checked(number: str, packages: list) -> tuple[str, list[dict]]:
        cleaned = clean_number(number) if number.strip() else ""
        if number.strip() and not cleaned:
            raise PayError("Enter the bKash number as 11 digits, like 01XXXXXXXXX.")
        if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
            raise PayError(f"At most {MAX_PACKAGES} packages.")
        out = []
        for package in packages:
            tokens, taka = (package or {}).get("tokens"), (package or {}).get("taka")
            ok = all(isinstance(v, int) and not isinstance(v, bool) for v in (tokens, taka))
            if not ok or not 1 <= tokens <= MAX_TOKENS or not 1 <= taka <= MAX_TAKA:
                raise PayError("A package needs a number of tokens and a price in taka, both whole numbers above 0.")
            if any(p["tokens"] == tokens for p in out):
                raise PayError(f"There are two packages of {tokens} tokens.")
            out.append({"tokens": tokens, "taka": taka})
        return cleaned, sorted(out, key=lambda p: p["tokens"])

    @property
    def settings(self) -> dict:
        return {**self._settings, "packages": [dict(p) for p in self._settings["packages"]]}

    @property
    def enabled(self) -> bool:
        """Whether a client can buy: there is a number to pay to and at least one package."""
        return bool(self._settings["number"] and self._settings["packages"])

    def set_settings(self, number: str, packages: list, by: str) -> dict:
        number, packages = self._checked(number, packages)
        settings = {"number": number, "packages": packages, "updated_at": utcnow_iso(), "updated_by": by}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_name(f".{self.settings_path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.settings_path)
        finally:
            tmp.unlink(missing_ok=True)
        self._settings = settings
        log.warning("payments: the bKash settings were changed by %s (%d package(s), a number %s)", by, len(packages),
                    "set" if number else "not set")
        return self.settings

    # ---- reading ----

    def _read(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        out = []
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:  # a line cut short by a crash: skip it
                continue
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry.get("event") in (
                    "sent", "approved", "rejected"):
                out.append(entry)
        return out

    def _apply(self, line: dict) -> None:
        if line["event"] == "sent":
            pay = {key: line.get(key) for key in ("id", "client", "tokens", "taka", "trx", "sender", "at")}
            if not all(isinstance(pay[key], str) for key in ("client", "trx")) or not all(
                    isinstance(pay[key], int) and not isinstance(pay[key], bool) for key in ("tokens", "taka")):
                return
            pay.update(status="waiting", checked_at=None, checked_by=None, reason=None, reason_text=None, balance=None)
            self._pays[pay["id"]] = pay
            if isinstance(line.get("request_id"), str):
                self._forms[line["request_id"]] = pay["id"]
            return
        pay = self._pays.get(line["id"])
        if pay is None or pay["status"] != "waiting":  # decided once; a second decision line changes nothing
            return
        pay.update(status=line["event"], checked_at=line.get("at"), checked_by=line.get("by"),
                   reason=line.get("reason"), reason_text=line.get("reason_text"), balance=line.get("balance"))

    def _append(self, line: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        append_line(self.path, json.dumps(line, ensure_ascii=False), sync=True)  # on disk before memory changes
        self._apply(line)

    def get(self, pay_id: str) -> dict | None:
        pay = self._pays.get(pay_id)
        return dict(pay) if pay is not None else None

    def all(self) -> list[dict]:
        """Every payment request, newest first (copies)."""
        return [dict(pay) for pay in reversed(self._pays.values())]

    def of_client(self, client: str) -> list[dict]:
        return [pay for pay in self.all() if pay["client"] == client]

    def waiting(self) -> int:
        return sum(pay["status"] == "waiting" for pay in self._pays.values())

    def by_form(self, request_id: str | None) -> dict | None:
        """The request a buy form's id already made (the form was sent again after its answer was lost)."""
        done = self._forms.get(request_id) if request_id else None
        return dict(self._pays[done]) if done is not None else None

    # ---- changes (each one a line) ----

    def submit(self, client: str, tokens: int, trx: str, sender: str, request_id: str | None = None) -> dict:
        """A client says it paid for the package of `tokens` tokens: a request, waiting to be checked."""
        done = self._forms.get(request_id) if request_id else None
        if done is not None:  # the same form again (its answer was lost): the request it already made
            pay = self._pays[done]
            if pay["client"] != client:
                raise PayError("This form was already used. Reload the page and try again.", 409)
            return dict(pay)
        if not self.enabled:
            raise PayError("Tokens can't be bought here right now. Please contact the head admin.", 409)
        package = next((p for p in self._settings["packages"] if p["tokens"] == tokens), None)
        if package is None:
            raise PayError("That package is no longer offered. Reload the page and choose again.", 409)
        cleaned = clean_trx(trx)
        if not cleaned:
            raise PayError("Enter the TrxID exactly as bKash shows it: 8 to 16 letters and digits.")
        number = clean_number(sender)
        if not number:
            raise PayError("Enter the bKash number you paid from as 11 digits, like 01XXXXXXXXX.")
        if any(p["trx"] == cleaned and p["status"] in ("waiting", "approved") for p in self._pays.values()):
            raise PayError("This TrxID was already sent. One TrxID can be used once.", 409)
        if sum(p["client"] == client and p["status"] == "waiting" for p in self._pays.values()) >= MAX_WAITING:
            raise PayError(f"{MAX_WAITING} payments of yours are waiting to be checked. Please wait for those first.", 409)
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        if sum(p["client"] == client and (p["at"] or "") >= day_ago for p in self._pays.values()) >= MAX_A_DAY:
            raise PayError("Too many payments were sent today. Please contact the head admin.", 429)
        line = {"event": "sent", "id": f"p{uuid4().hex[:12]}", "at": utcnow_iso(), "client": client,
                "tokens": package["tokens"], "taka": package["taka"], "trx": cleaned, "sender": number}
        if request_id:
            line["request_id"] = request_id
        self._append(line)
        log.info("payments: %s sent %s for %d tokens (%d taka)", client, cleaned, line["tokens"], line["taka"])
        return dict(self._pays[line["id"]])

    def decide(self, pay_id: str, status: str, by: str, *, reason: str | None = None, text: str = "",
               balance: int | None = None) -> dict:
        """Record that a waiting request was approved or rejected. The caller adds the tokens of an approved one."""
        pay = self._pays.get(pay_id)
        if pay is None:
            raise PayError("No such payment.", 404)
        if pay["status"] != "waiting":
            raise PayError(f"This payment was already {pay['status']}.", 409)
        line = {"event": status, "id": pay_id, "at": utcnow_iso(), "by": by}
        if status == "rejected":
            text = (text or "").strip()
            if reason not in REASONS or (reason == "other" and not text):
                raise PayError("Say why: choose a reason, or write one.")
            line["reason"] = reason
            if reason == "other":
                line["reason_text"] = text[:200]
        elif balance is not None:
            line["balance"] = balance
        self._append(line)
        log.info("payments: %s %s by %s (%s, %s)", pay_id, status, by, pay["client"], pay["trx"])
        return dict(self._pays[pay_id])

    def repair(self, ledger_request) -> int:
        """At start-up: a waiting request whose top-up is already in the token ledger (the server stopped between the
        two lines) is approved. `ledger_request`: TokenBook.by_request."""
        fixed = 0
        for pay in list(self._pays.values()):
            entry = ledger_request(f"pay-{pay['id']}") if pay["status"] == "waiting" else None
            if entry is not None:
                self.decide(pay["id"], "approved", entry.get("by") or "studio", balance=entry.get("balance"))
                fixed += 1
        return fixed
