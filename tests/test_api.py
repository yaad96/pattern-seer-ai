"""End-to-end API tests against the offline mock image provider."""

import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.pipeline as pipeline_mod
from app.main import create_app
from app.schemas import Job, JobKind, JobStatus
from app.storage import JobStore, new_job_id, utcnow_iso
from tests.conftest import jersey_png, make_settings

TERMINAL = {"succeeded", "partial", "failed"}


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "RETRY_DELAYS_S", (0.01,))


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


def wait_for(client: TestClient, job_id: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    job: dict = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def recreate(client: TestClient, options: dict | None = None, png: bytes | None = None):
    return client.post(
        "/api/recreate",
        files={"image": ("reference.png", png or jersey_png(), "image/png")},
        data={"options": json.dumps(options or {})},
    )


def design(client: TestClient, message: str, options: dict | None = None, **form):
    data = {"message": message, "options": json.dumps(options or {})}
    data.update({k: str(v) for k, v in form.items() if v is not None})
    return client.post("/api/design", data=data)


def test_config_in_demo_mode(client):
    cfg = client.get("/api/config").json()
    assert [p["name"] for p in cfg["providers"]] == ["mock"]
    assert cfg["default_provider"] == "mock"
    assert cfg["brief_writer_enabled"] is False and cfg["brief_writers"] == []
    for key in ("sports", "collars", "sleeves", "recreate_modes", "render_styles", "views", "vector_presets"):
        assert cfg["catalog"][key]
    assert cfg["defaults"]["recreate"]["mode"] == "pattern"
    assert cfg["defaults"]["design"]["view"] == "front_back"


def test_recreate_end_to_end_with_png_and_eps_exports(client):
    r = recreate(client, {"variants": 1, "resolution": "2K"})
    assert r.status_code == 202, r.text
    job = wait_for(client, r.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["kind"] == "recreate" and job["inputs"] == ["input_1.png"]
    assert job["prompt"]["system"] and job["prompt"]["user"]
    v = job["variants"][0]
    assert v["status"] == "succeeded" and v["png"] and v["preview"] and v["raw"]
    assert v["palette"] and all(p["hex"].startswith("#") for p in v["palette"])

    png = client.get(f"/files/{job['id']}/{v['png']}")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(png.content)) as im:
        assert im.size == (v["width"], v["height"])
        assert round(im.info["dpi"][0]) == 300
    dl = client.get(f"/files/{job['id']}/{v['png']}", params={"download": 1})
    assert "attachment" in dl.headers["content-disposition"]
    assert client.get(f"/files/{job['id']}/{v['preview']}").headers["content-type"] == "image/webp"

    vec = client.post(f"/api/jobs/{job['id']}/variants/1/vectorize", json={"preset": "standard"})
    assert vec.status_code == 200, vec.text
    asset = vec.json()
    eps = client.get(f"/files/{job['id']}/{asset['eps']}")
    assert eps.headers["content-type"] == "application/postscript"
    assert eps.content.startswith(b"%!PS-Adobe-3.0 EPSF-3.0")
    assert eps.content.rstrip().endswith(b"%%EOF")
    svg = client.get(f"/files/{job['id']}/{asset['svg']}")
    assert svg.headers["content-type"].startswith("image/svg+xml") and b"viewBox" in svg.content
    assert "content-security-policy" in svg.headers
    assert svg.content.count(b'inkscape:groupmode="layer"') == asset["layers"] == asset["colors"] >= 1
    assert asset["anchors"] > 0
    pdf = client.get(f"/files/{job['id']}/{asset['pdf']}", params={"download": 1})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert "attachment" in pdf.headers["content-disposition"]
    assert pdf.content.startswith(b"%PDF-1.5") and pdf.content.rstrip().endswith(b"%%EOF")
    assert len(pdf.content) == asset["pdf_bytes"]
    again = client.post(f"/api/jobs/{job['id']}/variants/1/vectorize", json={"preset": "standard"}).json()
    assert again["eps"] == asset["eps"]  # cached

    raster = client.post(f"/api/jobs/{job['id']}/variants/1/vectorize", json={"preset": "raster"}).json()
    assert raster["svg"] is None and raster["pdf"] is None
    body = client.get(f"/files/{job['id']}/{raster['eps']}").content
    assert body.startswith(b"%!PS-Adobe-3.0 EPSF-3.0") and b"/FlateDecode" in body

    stored = client.get(f"/api/jobs/{job['id']}").json()
    assert set(stored["variants"][0]["vectors"]) == {"standard", "raster"}


def test_print_upscale_keeps_raw_and_enlarges_final(client):
    job = wait_for(client, recreate(client, {"resolution": "1K", "print_long_edge": 3000}).json()["job_id"])
    v = job["variants"][0]
    assert max(v["width"], v["height"]) == 3000
    assert max(v["raw_width"], v["raw_height"]) < 3000


def test_variants_seed_determinism(client):
    opts = {"variants": 2, "seed": 7, "resolution": "1K"}
    a = wait_for(client, recreate(client, opts).json()["job_id"])
    b = wait_for(client, recreate(client, opts).json()["job_id"])
    assert a["status"] == b["status"] == "succeeded"
    raw = lambda job, i: client.get(f"/files/{job['id']}/{job['variants'][i]['raw']}").content  # noqa: E731
    assert raw(a, 0) == raw(b, 0)  # same prompt + seed -> same image
    assert raw(a, 0) != raw(a, 1)  # variant 2 uses seed + 1


def test_failure_and_retry_hooks(client):
    failed = wait_for(client, recreate(client, {"notes": "[[mock:fail]]"}).json()["job_id"])
    assert failed["status"] == "failed" and failed["error"]
    assert failed["variants"][0]["status"] == "failed"
    retried = wait_for(client, recreate(client, {"notes": "[[mock:retry-once]]"}).json()["job_id"])
    assert retried["status"] == "succeeded"


def test_design_chat_new_then_refine(client):
    first = design(client, "Cricket jersey in maroon and gold with tiger stripes on the sleeves", {"resolution": "1K"})
    assert first.status_code == 202, first.text
    session_id = first.json()["session_id"]
    job1 = wait_for(client, first.json()["job_id"])
    assert job1["status"] == "succeeded"
    assert job1["session_id"] == session_id and job1["message"].startswith("Cricket jersey")
    assert any("AI designer" in w for w in job1["warnings"])  # no text model configured in tests

    second = design(
        client,
        "Make the collar gold",
        {"resolution": "1K"},
        session_id=session_id,
        parent_job_id=job1["id"],
        parent_variant=1,
    )
    assert second.status_code == 202, second.text
    job2 = wait_for(client, second.json()["job_id"])
    assert job2["status"] == "succeeded"
    assert job2["parent"] == {"job_id": job1["id"], "variant": 1}
    assert job2["session_id"] == session_id
    assert job2["aspect_ratio"] == job1["aspect_ratio"]  # canvas kept stable while refining

    listed = client.get("/api/jobs", params={"session_id": session_id}).json()
    assert [j["id"] for j in listed] == [job2["id"], job1["id"]]


def test_design_with_attachment(client):
    r = client.post(
        "/api/design",
        data={"message": "Use this crest colour scheme", "options": json.dumps({"resolution": "1K"})},
        files=[("attachments", ("crest.png", jersey_png((120, 120)), "image/png"))],
    )
    assert r.status_code == 202, r.text
    job = wait_for(client, r.json()["job_id"])
    assert job["inputs"] == ["attach_1.png"]
    assert client.get(f"/files/{job['id']}/attach_1.png").status_code == 200


@pytest.mark.parametrize(
    ("options", "status"),
    [("not json", 400), ("[1, 2]", 400), (json.dumps({"bogus": 1}), 422), (json.dumps({"mode": "nope"}), 422)],
)
def test_recreate_option_validation(client, options, status):
    r = client.post(
        "/api/recreate", files={"image": ("r.png", jersey_png(), "image/png")}, data={"options": options}
    )
    assert r.status_code == status


def test_upload_validation(client, tmp_path):
    r = client.post("/api/recreate", files={"image": ("r.png", b"definitely not an image", "image/png")})
    assert r.status_code == 400
    small = TestClient(create_app(make_settings(tmp_path / "small", max_upload_mb=1)))
    big = b"\x89PNG" + b"0" * (2 * 1024 * 1024)
    assert small.post("/api/recreate", files={"image": ("big.png", big, "image/png")}).status_code == 413


def test_design_validation(client):
    assert design(client, "   ").status_code == 400
    assert design(client, "hi", session_id="not-a-session").status_code == 400
    assert design(client, "hi", parent_job_id="../../etc").status_code == 400
    assert design(client, "hi", parent_job_id=new_job_id(), parent_variant=1).status_code == 404
    too_many = [("attachments", (f"a{i}.png", jersey_png((64, 64)), "image/png")) for i in range(4)]
    r = client.post("/api/design", data={"message": "hi", "options": "{}"}, files=too_many)
    assert r.status_code == 400


def test_lookup_errors(client):
    assert client.get("/api/jobs/20260101-000000-abcdef").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/files/20260101-000000-abcdef/..%2Fjob.json").status_code == 404
    job = wait_for(client, recreate(client, {"resolution": "1K"}).json()["job_id"])
    assert client.get(f"/files/{job['id']}/job.json").status_code == 404  # metadata is not a downloadable file
    assert client.post(f"/api/jobs/{job['id']}/variants/9/vectorize", json={}).status_code == 404
    assert client.post(f"/api/jobs/{job['id']}/variants/1/vectorize", json={"preset": "x"}).status_code == 422


def test_prompt_preview(client):
    r = client.post("/api/prompt-preview", json={"kind": "recreate", "options": {}, "input_ratio": 0.8})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["system"] and body["user"] and body["aspect_ratio"] == "4:5"
    d = client.post("/api/prompt-preview", json={"kind": "design", "options": {}, "message": "Blue hoops"}).json()
    assert d["aspect_ratio"] == "16:9" and "Blue hoops" in d["user"]
    b = client.post("/api/prompt-preview", json={"kind": "brief", "options": {}, "message": "Blue hoops"}).json()
    assert b["system"] and "Blue hoops" in b["user"]
    bad = client.post("/api/prompt-preview", json={"kind": "design", "options": {"view": "sideways"}})
    assert bad.status_code == 422


def test_list_delete_and_index(client):
    job = wait_for(client, recreate(client, {"resolution": "1K"}).json()["job_id"])
    listed = client.get("/api/jobs", params={"kind": "recreate"}).json()
    assert listed[0]["id"] == job["id"] and listed[0]["preview"]
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404
    index = client.get("/")
    assert index.status_code == 200 and "text/html" in index.headers["content-type"]


def test_rate_limit(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, rate_limit_per_hour=1))) as c:
        assert recreate(c, {"resolution": "1K"}).status_code == 202
        limited = recreate(c, {"resolution": "1K"})
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


def test_login_page_protects_app(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, app_password="pw"))) as c:
        assert c.get("/api/config").status_code == 401
        assert c.get("/", follow_redirects=False).headers["location"] == "/login?next=%2F"
        assert c.post("/login", data={"username": "studio", "password": "nope", "next": "/"}).status_code == 401
        ok = c.post("/login", data={"username": "studio", "password": "pw", "next": "//evil.example"}, follow_redirects=False)
        assert ok.status_code == 303 and ok.headers["location"] == "/"  # no open redirect
        assert c.get("/api/config").json()["login_enabled"] is True
        assert c.get("/api/health").status_code == 200


def test_interrupted_jobs_marked_failed_on_startup(tmp_path):
    settings = make_settings(tmp_path)
    store = JobStore(settings.output_dir)
    now = utcnow_iso()
    stuck = Job(id=new_job_id(), kind=JobKind.RECREATE, status=JobStatus.RUNNING, created_at=now, updated_at=now, options={})
    store.save(stuck)
    with TestClient(create_app(settings)) as c:
        job = c.get(f"/api/jobs/{stuck.id}").json()
    assert job["status"] == "failed" and "restarted" in job["error"]
