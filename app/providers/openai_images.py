"""OpenAI GPT Image provider: images.generate for text-only requests, images.edit when images are sent."""

from __future__ import annotations

import base64
import binascii
import logging
import math
import re
import time
from dataclasses import dataclass

import openai
from openai.types import ImagesResponse

from app.providers.base import ErrorKind, ImageGenRequest, ImageGenResult, ModelInfo, ProviderError, transport_cause
from app.schemas import CONCRETE_ASPECT_RATIOS

log = logging.getLogger("jersey.providers.openai")

RECOMMENDED_MODEL = "gpt-image-2.5-sunburst"
OFFERED_MODELS = ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2")
SHUT_DOWN_MODELS = frozenset({"gpt-image-1", "dall-e-2", "dall-e-3"})

# "high" is the best fidelity/cost balance. On GPT Image 2.5, "xhigh" and "max" spend ~1.8x and ~4x the
# output tokens of "high" (1024x1024: ~$0.05 high, ~$0.09 xhigh, ~$0.21 max; 3840x2160 at max ~$0.40)
# and roughly double the latency; OpenAI advises them only when "high" misses a quality requirement.
# Models that do not accept the value set here use "high".
QUALITY = "high"

MAX_PROMPT_CHARS = 32_000
MAX_INPUT_IMAGES = 16

# Custom sizes (GPT Image 2 / 2.5): both edges multiples of 16, aspect ratio within 1:3..3:1.
SIZE_STEP = 16
MAX_EDGE = 3840
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400  # 3840x2160; OpenAI calls anything above 2560x1440 experimental
MAX_RATIO = 3.0
LONG_EDGE = {"1K": 1024, "2K": 2048}  # "4K" = the largest size the limits allow for the ratio
STANDARD_SIZES = {"1:1": (1024, 1024), "3:2": (1536, 1024), "2:3": (1024, 1536)}

# Error codes that mean a moderation/safety block rather than a malformed request.
SAFETY_CODES = frozenset(
    {"moderation_blocked", "content_policy_violation", "image_content_policy_violation", "invalid_prompt", "bio_policy"}
)

FORBIDDEN_MESSAGE = (
    "OpenAI refused access to GPT Image for this API key. GPT Image models may require API Organization "
    "Verification: verify the organization in the OpenAI platform settings and check the key's project permissions."
)

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_QUALITIES_25 = ("low", "medium", "high", "xhigh", "max", "auto")
_QUALITIES = ("low", "medium", "high", "auto")
_CUSTOM_NOTE = "Custom sizes up to 3840 px; OpenAI calls outputs above 2560x1440 experimental."
_DEPRECATED_NOTE = "Deprecated: OpenAI shuts this model down on 2026-12-01. Standard sizes only."


@dataclass(frozen=True)
class _Spec:
    label: str
    custom_sizes: bool  # any WxH within the limits above; otherwise only STANDARD_SIZES
    qualities: tuple[str, ...]
    transparent: bool
    input_fidelity: bool  # edits accept input_fidelity="high"
    notes: str


_SPECS: dict[str, _Spec] = {
    "gpt-image-2.5-sunburst": _Spec(
        "GPT Image 2.5 Sunburst (best quality)", True, _QUALITIES_25, True, False,
        "OpenAI's highest-fidelity image model. " + _CUSTOM_NOTE,
    ),
    "gpt-image-2.5-flare": _Spec(
        "GPT Image 2.5 Flare (fast)", True, _QUALITIES_25, True, False,
        "Faster; quality comparable to GPT Image 2. " + _CUSTOM_NOTE,
    ),
    "gpt-image-2": _Spec(
        "GPT Image 2 (previous generation)", True, _QUALITIES, True, False,
        "Transparent backgrounds are a preview feature. " + _CUSTOM_NOTE,
    ),
    # Deprecated models are never offered; they are recognised only when configured as the default.
    "gpt-image-1.5": _Spec("GPT Image 1.5 (deprecated)", False, _QUALITIES, True, True, _DEPRECATED_NOTE),
    "gpt-image-1-mini": _Spec("GPT Image 1 Mini (deprecated)", False, _QUALITIES, False, False, _DEPRECATED_NOTE),
    "chatgpt-image-latest": _Spec("ChatGPT Image (deprecated)", False, _QUALITIES, False, False, _DEPRECATED_NOTE),
}
_UNKNOWN = _Spec("", False, _QUALITIES, False, False, "Model not known to this app: standard sizes only.")


def _family(model: str) -> str:
    """'gpt-image-2.5-sunburst-2026-09-08' -> 'gpt-image-2.5-sunburst' (snapshots follow their alias's rules)."""
    return _SNAPSHOT.sub("", model)


def _spec(model: str) -> _Spec:
    return _SPECS.get(_family(model), _UNKNOWN)


def _ratio(aspect_ratio: str) -> float:
    try:
        width, height = (float(part) for part in aspect_ratio.split(":"))
        return width / height
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid aspect ratio '{aspect_ratio}'.") from exc


def _fit(long_edge: int, ratio: float) -> tuple[int, int]:
    """(width, height) with this long edge; the short edge snapped to the 16 px grid inside the ratio limit."""
    short = max(SIZE_STEP, round(long_edge / max(ratio, 1 / ratio) / SIZE_STEP) * SIZE_STEP)
    while long_edge / short > MAX_RATIO:
        short += SIZE_STEP
    return (long_edge, short) if ratio >= 1 else (short, long_edge)


def custom_size(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    """Size for custom-size models: 1K/2K = 1024/2048 px long edge, 4K = the largest size the limits allow."""
    ratio = min(max(_ratio(aspect_ratio), 1 / MAX_RATIO), MAX_RATIO)
    if resolution == "4K":
        long_edge = MAX_EDGE
        while math.prod(_fit(long_edge, ratio)) > MAX_PIXELS:
            long_edge -= SIZE_STEP
    elif resolution in LONG_EDGE:
        long_edge = LONG_EDGE[resolution]
        while math.prod(_fit(long_edge, ratio)) < MIN_PIXELS:  # e.g. 16:9 at 1024 px is below the minimum
            long_edge += SIZE_STEP
    else:
        raise ValueError(f"Unknown resolution '{resolution}'.")
    return _fit(long_edge, ratio)


def image_size(model: str, aspect_ratio: str, resolution: str) -> str:
    """The `size` parameter ('WxH') for this model, aspect ratio and resolution."""
    if _spec(model).custom_sizes:
        width, height = custom_size(aspect_ratio, resolution)
    else:
        target = math.log(_ratio(aspect_ratio))
        width, height = min(STANDARD_SIZES.values(), key=lambda wh: abs(math.log(wh[0] / wh[1]) - target))
    return f"{width}x{height}"


def model_info(model: str) -> ModelInfo:
    spec = _spec(model)
    family = _family(model)
    label = spec.label or model
    if spec.label and family != model:
        label += f", snapshot {model[len(family) + 1:]}"
    if spec.custom_sizes:
        ratios = tuple(r for r in CONCRETE_ASPECT_RATIOS if 1 / MAX_RATIO <= _ratio(r) <= MAX_RATIO)
        resolutions: tuple[str, ...] = ("1K", "2K", "4K")
    else:
        ratios, resolutions = tuple(STANDARD_SIZES), ("1K",)
    return ModelInfo(
        id=model,
        label=label,
        resolutions=resolutions,
        aspect_ratios=ratios,
        max_input_images=MAX_INPUT_IMAGES,
        supports_transparent=spec.transparent,
        supports_seed=False,
        supports_system_prompt=False,
        notes=spec.notes,
    )


def combined_prompt(system_prompt: str, user_prompt: str) -> str:
    """The Images API has no system field: system rules, a '---' line, then the task (as PromptPair.combined())."""
    return "\n\n---\n\n".join(part for part in (system_prompt.strip(), user_prompt.strip()) if part)


def map_openai_error(exc: openai.OpenAIError, *, provider: str, model: str, forbidden: str) -> ProviderError:
    """Translate an OpenAI SDK exception into a ProviderError whose message is safe to show staff."""

    def error(message: str, kind: ErrorKind, retryable: bool = False) -> ProviderError:
        return ProviderError(
            message, kind=kind, retryable=retryable, provider=provider, detail=_detail(exc),
            status_code=getattr(exc, "status_code", None), request_id=getattr(exc, "request_id", None),
        )

    if isinstance(exc, openai.AuthenticationError):
        return error("OpenAI rejected the API key. Check .openai_api_key.", "auth")
    if isinstance(exc, openai.PermissionDeniedError):
        return error(forbidden, "auth")
    if isinstance(exc, openai.NotFoundError):
        return error(f"OpenAI model '{model}' was not found, or this API key cannot use it.", "bad_request")
    if isinstance(exc, openai.RateLimitError):
        if "insufficient_quota" in (exc.code, exc.type):
            return error("The OpenAI account is out of credit (insufficient_quota). Add credit.", "rate_limit")
        return error("OpenAI rate limit reached; try again shortly.", "rate_limit", retryable=True)
    if isinstance(exc, openai.BadRequestError):
        if exc.code in SAFETY_CODES or (isinstance(exc.body, dict) and "moderation_details" in exc.body):
            blocked = error("OpenAI's safety system blocked this request. Reword it or use a different image.", "safety")
            details = exc.body.get("moderation_details") if isinstance(exc.body, dict) else None
            stage = details.get("moderation_stage") if isinstance(details, dict) else None
            blocked.billing_unknown = stage != "input"  # blocked after the image was made ("output"), or unknown
            return blocked
        return error(f"OpenAI rejected the request: {_api_message(exc)}", "bad_request")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409):  # temporary, like the SDK's own retry rule
            return error("OpenAI is temporarily unavailable.", "unavailable", retryable=True)
        return error(f"OpenAI API error {exc.status_code}: {_api_message(exc)}", "unknown")
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        timeout = isinstance(exc, openai.APITimeoutError)
        failed = error("OpenAI timed out." if timeout else "Could not reach the OpenAI API.",
                       "timeout" if timeout else "unavailable", retryable=True)
        failed.not_sent = transport_cause(exc)[1]  # broke while connecting or sending: never billed
        return failed
    return error("Unexpected error from the OpenAI client.", "unknown")


def _api_message(exc: openai.APIStatusError) -> str:
    """The API's own error text (exc.message adds an 'Error code: 400 - ' prefix)."""
    body = exc.body
    if isinstance(body, dict) and isinstance(body.get("message"), str) and body["message"]:
        return body["message"]
    return exc.message


def _detail(exc: openai.OpenAIError) -> str:
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code} (request id {exc.request_id}): {exc.body!r}"
    cause = transport_cause(exc)[0]
    return f"{type(exc).__name__}: {exc}" + (f" <- {cause}" if cause else "")


class OpenAIImageProvider:
    name = "openai"
    label = "OpenAI GPT Image"

    def __init__(self, api_key: str, default_model: str, timeout_s: float) -> None:
        if _family(default_model) in SHUT_DOWN_MODELS:
            log.warning("OpenAI shut down %s; using %s instead", default_model, RECOMMENDED_MODEL)
            default_model = RECOMMENDED_MODEL
        self._default_model = default_model
        ids = [default_model, *(m for m in OFFERED_MODELS if m != default_model)]
        self._models = [model_info(m) for m in ids]
        # no SDK re-sends: every request goes through the pipeline's own retry, so each one is logged and priced
        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=0)

    def models(self) -> list[ModelInfo]:
        return list(self._models)

    def default_model(self) -> str:
        return self._default_model

    async def generate(self, req: ImageGenRequest) -> ImageGenResult:
        model = req.model or self._default_model
        params = self._params(model, req)
        endpoint = "images.edit" if req.images else "images.generate"
        raw_api = self._client.images.with_raw_response
        call = raw_api.edit if req.images else raw_api.generate
        meta = {k: v for k, v in params.items() if k not in ("prompt", "image")}
        # the host matters for the price: OpenAI's regional endpoints (us./eu./ae.api.openai.com) cost 10% more
        meta.update(endpoint=endpoint, input_images=len(req.images), api_host=self._client.base_url.host)
        req.meta.clear()
        req.meta.update(meta)  # from here on the request counts as sent
        started = time.perf_counter()
        try:
            raw = await call(**params)
            response = raw.parse()
        except openai.OpenAIError as exc:
            error = map_openai_error(exc, provider=self.name, model=model, forbidden=FORBIDDEN_MESSAGE)
            error.request_meta = meta
            log.warning("OpenAI %s failed for %s: %s", endpoint, model, error.detail)
            raise error from exc
        latency = time.perf_counter() - started
        if raw.retries_taken:  # the SDK re-sent the request after a dropped connection or a 5xx/429 reply
            meta["sdk_retries"] = raw.retries_taken
        if raw.headers.get("openai-processing-ms"):
            meta["openai_processing_ms"] = raw.headers["openai-processing-ms"]
        usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
        request_id = raw.request_id or getattr(response, "_request_id", None)
        try:
            data = self._decode(response)
        except ProviderError as exc:  # a reply without a usable image is still billed: keep what the call log needs
            exc.request_meta, exc.request_id, exc.usage = meta, request_id, usage
            raise
        return ImageGenResult(
            data=data,
            mime_type=_MIME_TYPES.get(response.output_format or "png", "image/png"),
            provider=self.name,
            model=model,
            latency_s=latency,
            usage=usage,
            request_meta=meta,
            request_id=request_id,
        )

    def _params(self, model: str, req: ImageGenRequest) -> dict:
        spec = _spec(model)
        prompt = combined_prompt(req.system_prompt, req.user_prompt)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise self._bad_request(
                f"The prompt is too long for GPT Image ({len(prompt):,} characters; the limit is {MAX_PROMPT_CHARS:,})."
            )
        if len(req.images) > MAX_INPUT_IMAGES:
            raise self._bad_request(f"GPT Image accepts at most {MAX_INPUT_IMAGES} input images.")
        try:
            size = image_size(model, req.aspect_ratio, req.resolution)
        except ValueError as exc:
            raise self._bad_request(str(exc)) from exc
        params: dict = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "quality": QUALITY if QUALITY in spec.qualities else "high",
            "output_format": "png",
            "n": 1,
        }
        if spec.transparent:
            params["background"] = req.background
        if req.images:
            params["image"] = [
                (f"image_{i}.{_EXTENSIONS.get(img.mime_type, 'png')}", img.data, img.mime_type)
                for i, img in enumerate(req.images, start=1)
            ]
            if spec.input_fidelity:
                params["input_fidelity"] = "high"
        else:
            params["moderation"] = "auto"  # the SDK's images.edit has no moderation parameter
        return params

    def _decode(self, response: ImagesResponse) -> bytes:
        image = response.data[0] if response.data else None
        if image is None or not image.b64_json:
            raise ProviderError("OpenAI returned no image.", kind="no_image", provider=self.name)
        try:
            return base64.b64decode(image.b64_json, validate=True)
        except binascii.Error as exc:
            raise ProviderError(
                "OpenAI returned an unreadable image.", kind="no_image", provider=self.name, detail=str(exc)
            ) from exc

    def _bad_request(self, message: str) -> ProviderError:
        return ProviderError(message, kind="bad_request", provider=self.name)
