"""Provider contracts shared by the image generators and the brief writers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx2

ErrorKind = Literal["auth", "rate_limit", "safety", "no_image", "bad_request", "timeout", "unavailable", "unknown"]


class ProviderError(Exception):
    """A provider call failed. `message` is safe to show to staff in the UI."""

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind = "unknown",
        retryable: bool = False,
        provider: str = "",
        detail: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        request_meta: dict | None = None,
        usage: dict | None = None,
        billing_unknown: bool = False,
        not_sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retryable = retryable
        self.provider = provider
        self.detail = detail  # raw provider text for logs and the job's API call log; never shown to clients
        self.status_code = status_code  # the HTTP status, when the API answered
        self.request_id = request_id  # the provider's id for this request (quote it to their support)
        self.request_meta = request_meta  # the parameters that were sent, when the request was built
        self.usage = usage  # token counts when the API replied without a usable result (still billed)
        # an error reply that may still be charged, e.g. an image blocked by OpenAI's output moderation after it was made
        self.billing_unknown = billing_unknown
        # the connection failed before the request was fully sent (connect or write error): it can't have been billed
        self.not_sent = not_sent

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.provider or 'provider'}:{self.kind}] {self.message}"


# Transport failures while connecting or sending: the API never received the whole request.
_BEFORE_SENT = (
    httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.WriteError, httpx2.WriteTimeout,
    httpx2.LocalProtocolError, httpx2.ProxyError, httpx2.UnsupportedProtocol,
)


def transport_cause(exc: BaseException) -> tuple[str, bool]:
    """(the underlying network error as text, whether it happened before the request was fully sent) for an SDK
    connection or timeout error. The SDKs raise these `from` the HTTP client's own exception."""
    cause = exc.__cause__ or exc.__context__
    if cause is None:
        return "", False
    return f"{type(cause).__name__}: {cause}", isinstance(cause, _BEFORE_SENT)


@dataclass(frozen=True)
class InputImage:
    """An image sent to a model. `data` is already normalized (RGB/RGBA PNG unless noted)."""

    data: bytes
    mime_type: str = "image/png"
    # "reference"      - the client's low-res reference photo/mockup (recreate jobs)
    # "back_reference" - the client's picture of the back (panel jobs whose back differs)
    # "current_design" - the design being refined (design refinement turns), or the finished front panel
    # "inspiration"    - extra images the client attached (logos, mood references)
    label: Literal["reference", "back_reference", "current_design", "inspiration"] = "reference"
    source: str | None = None  # the job file it was made from (input_1.png, front_raw.png, ...), for the call log


@dataclass
class ImageGenRequest:
    system_prompt: str
    user_prompt: str
    images: list[InputImage] = field(default_factory=list)  # empty = text-to-image
    aspect_ratio: str = "1:1"  # always concrete here (never "auto")
    resolution: str = "2K"  # "1K" | "2K" | "4K"; providers clamp to what the model supports
    background: Literal["opaque", "transparent"] = "opaque"
    seed: int | None = None  # used only where the provider supports it
    model: str | None = None  # None = provider default
    # filled by the provider just before the request goes out (the parameters sent), so the call log can tell a
    # request that was sent but never answered (a timeout: may be billed) from one that was never sent
    meta: dict = field(default_factory=dict)


@dataclass
class ImageGenResult:
    data: bytes  # encoded image bytes exactly as returned by the provider
    mime_type: str  # e.g. "image/png", "image/jpeg"
    provider: str
    model: str
    text: str = ""  # any text the model returned alongside the image
    latency_s: float = 0.0
    usage: dict = field(default_factory=dict)  # the provider's token counts, as returned (prices: app/pricing.py)
    request_meta: dict = field(default_factory=dict)  # effective params sent (size, quality, ...)
    request_id: str | None = None  # the provider's id for this request


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    resolutions: tuple[str, ...]  # subset of ("1K", "2K", "4K")
    aspect_ratios: tuple[str, ...]  # concrete ratios the model accepts natively
    max_input_images: int
    supports_transparent: bool = False
    supports_seed: bool = False
    supports_system_prompt: bool = False
    notes: str = ""


class ImageProvider(Protocol):
    name: str  # "openai" | "mock"
    label: str  # UI label

    def models(self) -> list[ModelInfo]: ...

    def default_model(self) -> str: ...

    async def generate(self, req: ImageGenRequest) -> ImageGenResult: ...
