"""Shared fixtures. Settings are always isolated from real key files, .env and environment."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.config import Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "output_dir": tmp_path / "outputs",
        "enable_mock_provider": True,
        "rate_limit_per_hour": 0,
        "provider_timeout_s": 30.0,
    }
    values.update(overrides)
    return Settings(key_file_dir=tmp_path / "keys", _env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def jersey_png(size: tuple[int, int] = (320, 400)) -> bytes:
    """A small flat 'jersey photo' with stripes and a fake crest, as PNG bytes."""
    w, h = size
    img = Image.new("RGB", size, "#0B1F3A")
    d = ImageDraw.Draw(img)
    for x in range(-h, w, 48):
        d.polygon([(x, 0), (x + 18, 0), (x + 18 + h, h), (x + h, h)], fill="#C8102E")
    d.ellipse((w * 0.15, h * 0.15, w * 0.32, h * 0.29), fill="#FFC72C")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
