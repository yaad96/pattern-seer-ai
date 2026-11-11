"""The dashboard's site switch (2026-11-11, the owner's "killswitch"): while the site is closed, only the studio login
can use it. Everyone else (the demo login, anyone signed out, a tab already open) gets what a site that is not there
gives: HTTP 404 with an empty body, so the browser shows its own "page can't be found / HTTP ERROR 404" screen (the
owner: "it should resemble as closely as possible to ERROR 404 pages when truly theres 404"). API calls get
FastAPI's own 404 answer. The server keeps running, so the studio login can open the site again from the dashboard.
The state lives in <output_dir>/site_closed.json, so a restart or a deploy keeps it."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.storage import utcnow_iso

log = logging.getLogger(__name__)

FILE = "site_closed.json"
CLOSED_STATUS = 404
# exactly FastAPI's answer for a path that does not exist; api.js reloads the client demo page on it, so an open tab
# turns into the browser's 404 screen too (the app's own 404s all say something else, e.g. "Job not found.")
CLOSED_DETAIL = "Not Found"


class SiteSwitch:
    """Open or closed, as last set on the dashboard. Read on every request (in memory), written only when switched."""

    def __init__(self, output_dir: Path) -> None:
        self.path = Path(output_dir) / FILE
        self.state = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"closed": False, "since": None}
        except (OSError, ValueError):  # unreadable: stay closed, as a kill switch should; the dashboard can open it
            log.exception("could not read %s; the site stays closed until it is switched on the dashboard", self.path)
            return {"closed": True, "since": None}
        return {"closed": bool(data.get("closed")), "since": data.get("since")}

    @property
    def closed(self) -> bool:
        return bool(self.state["closed"])

    def set(self, closed: bool) -> dict:
        state = {"closed": bool(closed), "since": utcnow_iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.path)
        self.state = state
        log.warning("the site was %s on the dashboard", "CLOSED" if closed else "opened")
        return dict(state)
