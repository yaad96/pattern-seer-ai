"""Application settings.

API keys are read ONLY from two plain-text files in the project folder:

    .openai_api_key      (image generation; optional AI designer)
    .anthropic_api_key   (optional AI designer: Claude)

Paste the bare key into a file to enable that provider; leave it empty to
disable it. Environment variables and .env entries are never used for keys.
Restart the server after changing a key file.

Non-secret settings may be overridden with JERSEY_-prefixed environment
variables or .env entries, e.g. JERSEY_OPENAI_IMAGE_MODEL=... (the prefix keeps
unrelated variables such as CLAUDE_EFFORT from leaking in).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# provider -> key file name (inside the key directory, normally the project folder)
KEY_FILES: dict[str, str] = {
    "openai": ".openai_api_key",
    "anthropic": ".anthropic_api_key",
}

_ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?[A-Z][A-Z0-9_]*\s*=\s*(.*)$")


def read_key_file(path: Path) -> str:
    """The key stored in `path`, or "" if the file is missing/empty.

    Tolerates a trailing newline, surrounding quotes, '#' comment lines and an
    accidental 'NAME=value' / 'export NAME=value' paste.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.match(line)
        if match:
            line = match.group(1).strip()
        return line.strip("'\"").strip()
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JERSEY_", env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Image generation ---
    default_image_provider: Literal["auto", "openai", "mock"] = "auto"
    openai_image_model: str = "gpt-image-2.5-sunburst"
    # The offline mock provider is always offered when no real provider is configured;
    # set this to also offer it alongside OpenAI (useful for demos/tests).
    enable_mock_provider: bool = False

    # --- 'AI designer' brief writer (text LLM that expands the client's words) ---
    brief_writer: Literal["auto", "claude", "openai", "off"] = "auto"
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    openai_text_model: str = "gpt-6.1-sol"

    # --- Server / safety limits ---
    output_dir: Path = BASE_DIR / "outputs"
    app_username: str = "studio"
    app_password: str = ""  # set to require HTTP Basic auth for the whole app
    # a second login that opens only the client demo page /customerep (and only the jobs made with it); off when empty
    demo_username: str = "demo"
    demo_password: str = ""
    trust_proxy_headers: bool = False  # use X-Forwarded-For for rate limiting behind a proxy
    cookie_secure: bool = False  # mark the login cookie Secure (HTTPS only); true on the online server
    cpu_jobs: int = 2  # heavy image tasks at once (print-size PNGs, tracing); 1 on a 1 GB server
    # make each finished panel job's print PNGs and detailed EPS in the background, so the downloads and ZIPs are
    # ready (or nearly) when asked for; on for the online server, where tracing is slow
    prepare_downloads: bool = False
    # /panels, the studio's production-panel page (studio login), which the studio page links to; on here and on the
    # online server (the owner, 2026-11-07). JERSEY_PANELS_PAGE=false turns it and the link off
    panels_page: bool = True
    max_upload_mb: int = 20
    max_attachments: int = 3
    max_message_chars: int = 4000
    max_concurrent_generations: int = 4
    rate_limit_per_hour: int = 120  # generation requests per client IP per hour (0 = unlimited)
    provider_timeout_s: float = 300.0
    print_dpi: int = 300  # DPI written into PNGs and used for EPS physical size
    model_input_long_edge: int = 2048  # input images are downscaled to this before upload

    # Keys live in private attributes, so no settings source (env, .env, init) can set them.
    _api_keys: dict[str, str] = PrivateAttr(default_factory=dict)
    _key_dir: Path = PrivateAttr(default=BASE_DIR)

    def __init__(self, *, key_file_dir: Path | None = None, **values: Any) -> None:
        super().__init__(**values)
        self._key_dir = Path(key_file_dir) if key_file_dir is not None else BASE_DIR
        self._api_keys = {
            provider: read_key_file(self._key_dir / filename) for provider, filename in KEY_FILES.items()
        }

    @property
    def openai_api_key(self) -> str:
        return self._api_keys.get("openai", "")

    @property
    def anthropic_api_key(self) -> str:
        return self._api_keys.get("anthropic", "")

    @property
    def key_status(self) -> dict[str, str]:
        """For logs/UI: which key files hold a key. Never contains the keys themselves."""
        return {
            provider: (f"loaded from {filename}" if self._api_keys.get(provider) else f"{filename} is empty")
            for provider, filename in KEY_FILES.items()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
