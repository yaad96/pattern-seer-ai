from types import SimpleNamespace

import pytest

from app.pipeline import JobError, JobManager, _clamp_resolution, _nearest_ratio, design_auto_aspect
from app.providers.base import ModelInfo
from app.schemas import CONCRETE_ASPECT_RATIOS, DesignOptions, DesignView, RenderStyle
from app.storage import JobStore
from tests.conftest import make_settings


@pytest.mark.parametrize(
    ("requested", "supported", "expected"),
    [
        ("2K", ("1K", "2K", "4K"), "2K"),
        ("4K", ("1K", "2K"), "2K"),
        ("1K", ("2K", "4K"), "2K"),
        ("4K", ("1K",), "1K"),
    ],
)
def test_clamp_resolution(requested, supported, expected):
    assert _clamp_resolution(requested, supported) == expected


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(1.0, "1:1"), (0.8, "4:5"), (0.75, "3:4"), (1.78, "16:9"), (2.4, "21:9"), (0.3, "9:16")],
)
def test_nearest_ratio(ratio, expected):
    assert _nearest_ratio(ratio, CONCRETE_ASPECT_RATIOS) == expected


def test_design_auto_aspect():
    assert design_auto_aspect(DesignOptions(render_style=RenderStyle.PATTERN)) == "1:1"
    assert design_auto_aspect(DesignOptions(view=DesignView.FRONT_BACK)) == "16:9"
    assert design_auto_aspect(DesignOptions(view=DesignView.FRONT, render_style=RenderStyle.MOCKUP)) == "4:5"
    assert design_auto_aspect(DesignOptions(view=DesignView.BACK, render_style=RenderStyle.FLAT)) == "3:4"


class FakeProvider:
    name = "fake"
    label = "Fake"

    def models(self):
        return [
            ModelInfo(
                id="fixed-1",
                label="Fixed sizes",
                resolutions=("1K",),
                aspect_ratios=("1:1", "3:2", "2:3"),
                max_input_images=2,
                supports_transparent=False,
            )
        ]

    def default_model(self):
        return "fixed-1"

    async def generate(self, req):  # pragma: no cover - not used
        raise NotImplementedError


def manager(tmp_path) -> JobManager:
    registry = SimpleNamespace(get_image_provider=lambda name: FakeProvider())
    settings = make_settings(tmp_path)
    return JobManager(settings, JobStore(settings.output_dir), registry)  # type: ignore[arg-type]


def test_resolve_clamps_unsupported_requests_with_warnings(tmp_path):
    r = manager(tmp_path)._resolve(None, None, "16:9", 1.0, "4K", "transparent", image_count=1)
    assert (r.aspect_ratio, r.resolution, r.background) == ("3:2", "1K", "opaque")
    assert len(r.warnings) == 3


def test_resolve_auto_ratio_uses_model_ratios(tmp_path):
    r = manager(tmp_path)._resolve(None, None, "auto", 0.7, "1K", "white", image_count=0)
    assert r.aspect_ratio == "2:3" and r.warnings == []


def test_resolve_rejects_unknown_model_and_too_many_images(tmp_path):
    m = manager(tmp_path)
    with pytest.raises(JobError):
        m._resolve(None, "nope", "auto", 1.0, "1K", "white", image_count=0)
    with pytest.raises(JobError):
        m._resolve(None, None, "auto", 1.0, "1K", "white", image_count=3)
