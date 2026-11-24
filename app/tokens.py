"""Our own tokens (2026-11-23): what a client's jobs cost in tokens, each client's purse, and the permanent ledger.

The owner set a price per image: the front panel, a back drawn from the client's own picture, a back that is the front
mirrored or copied (free: no AI image), the sleeve, and a Design message (text-to-image). The prices are business
information, kept in the git-ignored client_assets/tokens.json next to the clients; without that file nothing is
charged and nothing is blocked.

- Only jobs made with a client page's own login are charged (Job.client).
- Each image that came out is charged once, when its job ends. A failed image is free: OpenAI didn't bill the
  failed attempts (checked against the bill on 2026-11-23), and "generate again" is charged when it works.
- Before a job starts its price is held, so two quick jobs can't overspend a purse. A job the purse can't cover is
  refused (HTTP 402). Holds live in memory: a restart drops them along with the jobs it interrupts.
- The head admin adds tokens or takes them back (never more than the purse has free), always with a note; since
  2026-11-24 the partner login can add too (not take back), and a bKash payment that is approved adds its tokens
  (app/payments.py).
- Every change goes to <output_dir>/token_ledger.jsonl, append-only and never rewritten, with the balance after it.
  A balance is the sum of that file. Deleting a job in History doesn't touch its charges.

Concurrency: one client may use many devices at once; they all reach the one server process, whose event loop runs
each of hold, bind, settle and add to the end without interruption (no await inside), so two requests can't both
spend the same tokens. One server process per output folder is enforced at start-up (storage.lock_outputs)."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.storage import append_line, utcnow_iso

log = logging.getLogger(__name__)

LEDGER = "token_ledger.jsonl"
PRICES_FILE = "tokens.json"
ITEMS = ("front", "back_picture", "back_same", "sleeve", "design")
MAX_PRICE = 100_000
MAX_CHANGE = 10_000_000  # one top-up or correction
MAX_TAKA = 10_000_000  # what one top-up records as paid, in whole taka


@dataclass(frozen=True)
class Prices:
    front: int
    back_picture: int  # a back drawn from the client's own picture
    back_same: int  # a back that is the front mirrored or copied
    sleeve: int
    design: int  # one Design message (text-to-image)

    def of(self, item: str) -> int:
        return int(getattr(self, item))

    def as_dict(self) -> dict[str, int]:
        return {item: self.of(item) for item in ITEMS}


def load_prices(folder: Path) -> Prices | None:
    """The owner's prices from `folder`/tokens.json ({"prices": {"front": <tokens>, ...}}), or None (tokens off). A broken
    file is logged and turns tokens off; it never stops the app."""
    path = folder / PRICES_FILE
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8-sig"))["prices"]
        values = {item: raw[item] for item in ITEMS}
        if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_PRICE for v in values.values()):
            raise ValueError(f"every price must be a whole number from 0 to {MAX_PRICE}")
        return Prices(**values)
    except Exception as exc:
        log.error("tokens are off: %s can't be used (%s: %s)", path, type(exc).__name__, exc)
        return None


class NotEnoughTokens(Exception):
    def __init__(self, need: int, available: int) -> None:
        super().__init__(f"Not enough tokens: this needs {need}, you have {available}. "
                         "Please ask the head admin to add more.")
        self.need, self.available = need, available


def item_of(job, variant) -> str | None:
    """What one image of a job is, for its price: None for images client pages don't make (pattern, flat jersey,
    HD replica)."""
    if getattr(job.kind, "value", job.kind) == "design":
        return "design"
    if variant.panel == "front":
        return "front"
    if variant.panel == "back":
        if variant.source in ("mirror", "copy"):
            return "back_same"
        if variant.source is None:  # not made yet (a retry): as the job asked for it
            options = job.options or {}
            if "front" in (options.get("panels") or ("front",)) and options.get("back") in ("mirror", "copy"):
                return "back_same"
        return "back_picture"
    if variant.panel == "sleeve":
        return "sleeve"
    return None


def panel_items(panels: list[str], back_same: bool) -> list[str]:
    """The items of a panel job asked for: `panels` in order, the back as drawn or made from the front."""
    return [("back_same" if back_same else "back_picture") if p == "back" else p for p in panels]


class TokenBook:
    """The purses: balances, holds and the ledger. Used on the event-loop thread only (routes and JobManager); the
    report reads the file from a worker thread, which only ever sees whole lines or skips a cut one."""

    def __init__(self, output_dir: Path, prices: Prices | None) -> None:
        self.path = Path(output_dir) / LEDGER
        self.prices = prices
        self._holds: dict[str, tuple[str, int]] = {}  # a hold id, then the job id -> (client, tokens)
        self.reload()

    def reload(self) -> None:
        """Build the balances from the ledger file; each change is then added as it is written. main.py calls it again
        right after the server takes the output folder (storage.lock_outputs): a server that was still stopping when
        this one was made may have written to the ledger since (2026-11-23 review CHAOS-1). Holds stay."""
        self._balance: dict[str, int] = defaultdict(int)
        self._charged: set[tuple[str, int]] = set()
        self._requests: dict[str, dict] = {}  # a top-up's request id -> its ledger line (the same form sent again)
        self._entries = self._read()
        for entry in self._entries:
            self._apply(entry)

    @property
    def enabled(self) -> bool:
        return self.prices is not None

    # ---- reading ----

    def entries(self) -> list[dict]:
        """Every ledger line, oldest first (a copy: safe to read from a worker thread)."""
        return list(self._entries)

    def clients(self) -> list[str]:
        """Every client with ledger history."""
        return list(self._balance)

    def _read(self) -> list[dict]:
        try:  # a byte cut mid-character (a crash) spoils only its own line, never the file
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        out = []
        for line in text.split("\n"):  # not splitlines(): a note may hold U+2028 and the like, written raw
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:  # a line cut short: skip it
                continue
            tokens = entry.get("tokens") if isinstance(entry, dict) else None
            if isinstance(entry.get("client"), str) and isinstance(tokens, int) and not isinstance(tokens, bool):
                out.append(entry)
        return out

    def _apply(self, entry: dict) -> None:
        self._balance[entry["client"]] += entry["tokens"]
        if entry.get("type") == "charge":
            self._charged.add((entry.get("job_id"), entry.get("variant")))
        if isinstance(entry.get("request_id"), str):
            self._requests[entry["request_id"]] = entry

    def by_request(self, request_id: str | None) -> dict | None:
        """The ledger line a top-up form's request id already made (its answer was lost and it was sent again)."""
        return self._requests.get(request_id) if request_id else None

    def balance(self, client: str) -> int:
        return self._balance.get(client, 0)

    def held(self, client: str) -> int:
        return sum(tokens for who, tokens in self._holds.values() if who == client)

    def available(self, client: str) -> int:
        return self.balance(client) - self.held(client)

    # ---- holds (before a job starts) ----

    def hold(self, client: str, tokens: int) -> str:
        """Hold `tokens` of `client`'s purse for a job about to start; NotEnoughTokens when it can't cover them."""
        available = self.available(client)
        if tokens > available:
            log.info("tokens: refused a %d-token job for %s (%d available)", tokens, client, available)
            raise NotEnoughTokens(tokens, max(0, available))
        key = f"hold:{uuid4().hex}"
        self._holds[key] = (client, tokens)
        return key

    def bind(self, key: str, job_id: str) -> None:
        """The job now exists: its hold is kept under its id until the job ends (settle), added to any hold the job
        already has."""
        if key in self._holds:
            client, tokens = self._holds.pop(key)
            _, before = self._holds.get(job_id, (client, 0))
            self._holds[job_id] = (client, before + tokens)

    def release(self, key: str) -> None:
        self._holds.pop(key, None)

    # ---- changes (each one a ledger line) ----

    def settle(self, job) -> list[dict]:
        """A job ended: drop its hold and charge each image of it that came out, is its client's to pay
        (Job.charge_variants) and isn't charged yet."""
        self.release(job.id)
        if not self.enabled or not getattr(job, "client", None):
            return []
        chargeable = set(getattr(job, "charge_variants", None) or [])
        charged = []
        for variant in job.variants:
            if (variant.status != "succeeded" or variant.index not in chargeable
                    or (job.id, variant.index) in self._charged):
                continue
            item = item_of(job, variant)
            if item is None:
                continue
            price = self.prices.of(item)
            charged.append(self._append({"type": "charge", "client": job.client, "tokens": -price, "job_id": job.id,
                                         "variant": variant.index, "item": item, "price": price}))
        return charged

    def add(self, client: str, tokens: int, note: str, by: str, taka: int | None = None,
            request_id: str | None = None, extra: dict | None = None) -> dict:
        """Tokens added to (or, when negative, taken from) `client`'s purse by `by` ("studio" = the head admin,
        "partner"), with a note, and with a top-up what the client paid for them (`taka`, whole taka; None = not
        recorded, 0 = free tokens).
        `request_id`: the form's own id, kept on the line; the same id again returns that line and adds nothing (the
        answer to the first press was lost: 2026-11-23 review CHAOS-4). An approved bKash payment uses "pay-<id>".
        `extra`: more to keep on the line (a bKash payment's id and TrxID)."""
        done = self.by_request(request_id)
        if done is not None:
            if done["client"] != client or done["tokens"] != tokens:
                raise ValueError("This form was already used for another change. Reload the page and try again.")
            return done
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens == 0 or abs(tokens) > MAX_CHANGE:
            raise ValueError(f"The number of tokens must be a whole number other than 0, at most {MAX_CHANGE}.")
        if taka is not None and (not isinstance(taka, int) or isinstance(taka, bool) or not 0 <= taka <= MAX_TAKA):
            raise ValueError(f"The taka received must be a whole number from 0 to {MAX_TAKA}.")
        if taka is not None and tokens < 0:
            raise ValueError("Taka received goes with tokens added, not with tokens taken back.")
        note = (note or "").strip()
        if not note:
            raise ValueError("Write a note: what the tokens are for.")
        free = self.available(client)
        if tokens < 0 and -tokens > free:  # a take-back: never below zero, nor into what running jobs hold
            held = self.held(client)
            raise ValueError(f"Only {max(0, free)} tokens can be taken back now"
                             + (f" ({held} are set aside for jobs still running)." if held else "."))
        entry = {"type": "topup" if tokens > 0 else "adjust", "client": client, "tokens": tokens, "note": note, "by": by}
        if taka is not None:
            entry["taka"] = taka
        if request_id:
            entry["request_id"] = request_id
        for key, value in (extra or {}).items():
            entry.setdefault(key, value)
        return self._append(entry)

    def _append(self, entry: dict) -> dict:
        entry = {"id": uuid4().hex, "at": utcnow_iso(), **entry}
        entry["balance"] = self.balance(entry["client"]) + entry["tokens"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # on disk (fsync'd) before the balance changes in memory; a line cut short by a crash stays on its own line
        append_line(self.path, json.dumps(entry, ensure_ascii=False), sync=True)
        self._entries.append(entry)
        self._apply(entry)
        log.info("tokens: %s %s%d for %s (balance %d)%s", entry["type"], "+" if entry["tokens"] > 0 else "",
                 entry["tokens"], entry["client"], entry["balance"],
                 f" job {entry['job_id']} {entry['item']}" if entry["type"] == "charge" else f" by {entry.get('by')}")
        return entry
