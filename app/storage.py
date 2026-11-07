"""On-disk job storage.

Layout: <output_dir>/jobs/<job_id>/job.json plus that job's image/vector files.
Job ids start with a UTC timestamp, so sorting ids newest-first sorts jobs too.
Every id and filename coming from a URL is validated against a strict pattern
before it touches the filesystem.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.schemas import Job

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


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
        except ValueError:
            return None

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
        return path.read_bytes() if path else None

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
