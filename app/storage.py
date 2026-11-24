"""On-disk job storage.

Layout: <output_dir>/jobs/<job_id>/job.json plus that job's image/vector files.
Job ids start with a UTC timestamp, so sorting ids newest-first sorts jobs too.
Every id and filename coming from a URL is validated against a strict pattern
before it touches the filesystem.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.schemas import Job

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUNNING_RE = re.compile(rb'"status":\s*"(?:queued|running)"')  # a job's own status (an image's is never these)


def append_line(path: Path, line: str, *, sync: bool = False) -> None:
    """Append one line of text to `path` (created if missing), in one write. A last line cut short (the process killed,
    or the disk full, while it was written) is ended first, so the new line is never glued to it: each stays readable
    on its own. `sync`: fsync before returning (the token ledger, the sheet outbox).

    A failed fsync is logged, not raised: the line is already in the file, so the caller must go on as if it were
    written (the token ledger keeps its memory equal to the file; raising made it charge the same image again)."""
    data = line.rstrip("\n").encode("utf-8") + b"\n"
    fh = open(path, "a+b")  # O_APPEND: the write always goes to the end
    try:
        if fh.seek(0, os.SEEK_END):
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.write(data)
        fh.flush()  # from here on the line is in the file
    except BaseException:
        try:
            fh.close()
        except OSError:
            pass
        raise
    try:
        if sync:
            os.fsync(fh.fileno())
    except OSError:
        log.exception("could not fsync %s (the line is written; it may not survive a power cut)", path)
    try:
        fh.close()
    except OSError:  # the same: the line was handed to the system already
        log.exception("could not close %s after writing a line to it", path)


class OutputsInUse(RuntimeError):
    """Another server process already uses this output folder."""


class OutputsLock:
    """The output folder, held by this server process (lock_outputs); close() lets it go."""

    def __init__(self, fds: list[int]) -> None:
        self._fds = fds

    def close(self) -> None:
        while self._fds:
            try:
                os.close(self._fds.pop())
            except OSError:
                pass


def lock_outputs(output_dir: Path) -> OutputsLock | None:
    """Take the output folder for this server process, so a second server started on the same folder (e.g. ./run.sh
    while one is running) stops before it marks the first one's running jobs as interrupted, or charges their tokens
    a second time. Held until close() or the end of the process (the OS frees it then). None where file locks aren't
    available (not macOS or Linux).

    Two locks: the folder itself, which can't be deleted while it holds files (a lock file could be, and a second
    server would then lock a new file of the same name: 2026-11-23 review CHAOS-5), and server.lock, the one servers
    from before that review take, so either version keeps the other one out."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - the app runs on macOS and Linux
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    fds: list[int] = []
    try:
        fds.append(os.open(output_dir, os.O_RDONLY))
        fds.append(os.open(output_dir / "server.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644))
        for fd in fds:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        OutputsLock(fds).close()
        raise OutputsInUse(
            f"Another Jersey HD Studio server is already running on {output_dir}. Stop it first (Ctrl+C in its "
            "window), then start this one again."
        ) from exc
    return OutputsLock(fds)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_job_id() -> str:
    """'YYYYMMDD-HHMMSS-mmmrrr': milliseconds then 3 random hex chars, so ids sort by creation time."""
    now = datetime.now(timezone.utc)
    return f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}{secrets.token_hex(2)[:3]}"


def new_session_id() -> str:
    return secrets.token_hex(16)


def is_valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id))


def is_valid_filename(name: str) -> bool:
    return bool(FILENAME_RE.match(name)) and ".." not in name


class JobStore:
    def __init__(self, output_dir: Path) -> None:
        self.root = Path(output_dir) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        if not is_valid_job_id(job_id):
            raise ValueError(f"invalid job id: {job_id!r}")
        return self.root / job_id

    def file_path(self, job_id: str, filename: str) -> Path | None:
        """Path of an existing file inside a job directory, or None."""
        if not is_valid_job_id(job_id) or not is_valid_filename(filename):
            return None
        path = self.root / job_id / filename
        return path if path.is_file() else None

    # -- job records -------------------------------------------------------

    def save(self, job: Job) -> None:
        d = self.job_dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        data = job.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".job-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, d / "job.json")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load(self, job_id: str) -> Job | None:
        if not is_valid_job_id(job_id):
            return None
        path = self.root / job_id / "job.json"
        if not path.is_file():
            return None
        try:
            return Job.model_validate_json(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):  # deleted between the check and the read: gone
            return None

    def may_be_running(self, job_id: str) -> bool:
        """A quick look at a job's record, without parsing it: whether it may say queued or running (start-up
        recovery reads every job this way and parses only those)."""
        try:
            data = (self.root / job_id / "job.json").read_bytes()
        except OSError:
            return False
        return RUNNING_RE.search(data) is not None

    def list_ids(self) -> list[str]:
        """All job ids, newest first."""
        ids = [p.name for p in self.root.iterdir() if p.is_dir() and is_valid_job_id(p.name)]
        return sorted(ids, reverse=True)

    def delete(self, job_id: str) -> bool:
        d = self.job_dir(job_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True

    # -- files -------------------------------------------------------------

    def write_image(self, job_id: str, filename: str, img, **save_params) -> Path:
        """Save a PIL image straight into an existing job folder (atomically, like write_file), without holding the
        encoded file in memory: used for the 26-100 MB print PNGs. A job deleted meanwhile raises FileNotFoundError
        instead of getting its folder back."""
        if not is_valid_filename(filename):
            raise ValueError(f"invalid filename: {filename!r}")
        d = self.job_dir(job_id)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                img.save(fh, **save_params)
            os.replace(tmp, d / filename)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return d / filename

    def write_file(self, job_id: str, filename: str, data: bytes) -> Path:
        if not is_valid_filename(filename):
            raise ValueError(f"invalid filename: {filename!r}")
        d = self.job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def read_file(self, job_id: str, filename: str) -> bytes | None:
        path = self.file_path(job_id, filename)
        try:
            return path.read_bytes() if path else None
        except FileNotFoundError:  # deleted between the check and the read: gone
            return None

    def append_line(self, job_id: str, filename: str, line: str) -> None:
        """Add one line of text to a job file, creating it if needed (the append-only API call log)."""
        if not is_valid_filename(filename):
            raise ValueError(f"invalid filename: {filename!r}")
        if "\n" in line or "\r" in line:
            raise ValueError("a log line must not contain a line break")
        d = self.job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        with open(path, "a+b") as fh:
            fh.seek(0, os.SEEK_END)
            ended = fh.tell() == 0
            if not ended:  # a line cut short earlier (e.g. a full disk) must not swallow this one
                fh.seek(-1, os.SEEK_END)
                ended = fh.read(1) == b"\n"
            fh.write((b"" if ended else b"\n") + line.encode("utf-8") + b"\n")
