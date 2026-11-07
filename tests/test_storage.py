import re

import pytest

from app.schemas import Job, JobKind
from app.storage import JobStore, is_valid_filename, is_valid_job_id, new_job_id, new_session_id, utcnow_iso


def make_job(job_id: str) -> Job:
    now = utcnow_iso()
    return Job(id=job_id, kind=JobKind.RECREATE, created_at=now, updated_at=now, options={})


def test_ids_have_expected_shape():
    assert is_valid_job_id(new_job_id())
    assert re.fullmatch(r"[0-9a-f]{32}", new_session_id())


@pytest.mark.parametrize("bad", ["", "..", "../etc", "20260101-000000-zzzzzz", "20260101-000000-abcdef/..", "a" * 30])
def test_invalid_job_ids_rejected(bad):
    assert not is_valid_job_id(bad)


@pytest.mark.parametrize("bad", ["", ".hidden", "../x.png", "a/b.png", "a\\b.png", "x..png", "job.json/"])
def test_invalid_filenames_rejected(bad):
    assert not is_valid_filename(bad)


def test_save_load_list_delete(tmp_path):
    store = JobStore(tmp_path)
    older, newer = "20260101-000000-aaaaaa", "20260102-000000-bbbbbb"
    store.save(make_job(older))
    store.save(make_job(newer))
    assert store.load(older).id == older
    assert store.list_ids() == [newer, older]
    store.write_file(newer, "v1.png", b"png-bytes")
    assert store.read_file(newer, "v1.png") == b"png-bytes"
    assert store.file_path(newer, "../" + older + "/job.json") is None
    assert store.file_path(newer, "missing.png") is None
    assert store.delete(older) is True
    assert store.load(older) is None
    assert store.delete(older) is False


def test_load_rejects_bad_ids_and_corrupt_json(tmp_path):
    store = JobStore(tmp_path)
    assert store.load("../../etc/passwd") is None
    job_id = "20260101-000000-cccccc"
    (store.root / job_id).mkdir()
    (store.root / job_id / "job.json").write_text("{not json")
    assert store.load(job_id) is None


def test_write_file_rejects_unsafe_names(tmp_path):
    store = JobStore(tmp_path)
    with pytest.raises(ValueError):
        store.write_file("20260101-000000-dddddd", "../escape.png", b"x")
    with pytest.raises(ValueError):
        store.job_dir("not-an-id")
