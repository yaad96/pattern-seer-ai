"""Claude as the 'AI designer': structured design briefs and reference checks."""

from __future__ import annotations

import base64
from typing import TypeVar

import anthropic
import pydantic

from app.providers.base import ProviderError, transport_cause
from app.providers.text.base import BriefRequest
from app.schemas import BriefResponse, ReferenceAnalysis

# Server-side refusal fallback ("default" routes by refusal category). Only these
# models accept it; older ones would reject the parameter.
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept output_config.effort.
EFFORT_PREFIXES = ("claude-opus-", "claude-fable-", "claude-mythos-", "claude-sonnet-5", "claude-haiku-5")
REFERENCE_CHECK_EFFORT = "low"  # a quick visibility check of one image; briefs use the configured effort

T = TypeVar("T", bound=pydantic.BaseModel)


class ClaudeBriefWriter:
    name = "claude"
    label = "Claude (Anthropic)"

    def __init__(self, api_key: str, model: str, effort: str, timeout_s: float) -> None:
        self.model = model
        self.effort = effort
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    async def write(self, req: BriefRequest) -> BriefResponse:
        return await self._parse(req, BriefResponse, self.effort, "design brief")

    async def analyze_reference(self, req: BriefRequest) -> ReferenceAnalysis:
        return await self._parse(req, ReferenceAnalysis, REFERENCE_CHECK_EFFORT, "reference check")

    def _request_kwargs(self, req: BriefRequest, output_format: type[pydantic.BaseModel], effort: str) -> dict:
        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.mime_type,
                    "data": base64.standard_b64encode(img.data).decode("ascii"),
                },
            }
            for img in req.images
        ]
        content.append({"type": "text", "text": req.user_prompt})
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 16000,
            "system": req.system_prompt,
            "messages": [{"role": "user", "content": content}],
            "output_format": output_format,
        }
        if self.model.startswith(EFFORT_PREFIXES):
            kwargs["output_config"] = {"effort": effort}
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    async def _parse(self, req: BriefRequest, output_format: type[T], effort: str, task: str) -> T:
        """One structured-output call; SDK errors and unusable replies become ProviderError."""
        kwargs = self._request_kwargs(req, output_format, effort)
        params = {k: v for k, v in kwargs.items() if k not in ("system", "messages", "output_format")}
        req.meta.update(endpoint="messages.parse", params={**params, "output_format": output_format.__name__})
        try:
            raw = await self._client.beta.messages.with_raw_response.parse(**kwargs)
            _record_reply(req.meta, raw, self.model)  # before parsing: a reply that fails to parse is billed too
            response = await raw.parse()
        except anthropic.AuthenticationError as exc:
            raise self._error("Claude rejected the API key. Check .anthropic_api_key.", "auth", exc) from exc
        except anthropic.PermissionDeniedError as exc:
            raise self._error("This Anthropic API key is not allowed to use this model.", "auth", exc) from exc
        except anthropic.NotFoundError as exc:
            raise self._error(f"Claude model '{self.model}' was not found.", "bad_request", exc) from exc
        except anthropic.RateLimitError as exc:
            raise self._error("Claude rate limit reached; try again shortly.", "rate_limit", exc, retryable=True) from exc
        except anthropic.BadRequestError as exc:
            raise self._error(f"Claude rejected the request: {exc.message}", "bad_request", exc) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise self._error("Claude is temporarily unavailable.", "unavailable", exc, retryable=True) from exc
            raise self._error(f"Claude API error {exc.status_code}: {exc.message}", "unknown", exc) from exc
        except anthropic.APITimeoutError as exc:
            raise self._error("Claude timed out.", "timeout", exc, retryable=True) from exc
        except anthropic.APIConnectionError as exc:
            raise self._error("Could not reach the Claude API.", "unavailable", exc, retryable=True) from exc
        except pydantic.ValidationError as exc:  # the SDK parses the reply; a refusal or cut-off is not valid JSON
            raise self._error(f"Claude returned an incomplete {task}.", "unknown", exc) from exc

        if response.stop_reason == "refusal":
            raise ProviderError(f"Claude declined to write this {task}.", kind="safety", provider=self.name)
        if response.stop_reason == "max_tokens":
            raise ProviderError(f"Claude's {task} was cut off (max_tokens).", kind="unknown", provider=self.name)
        parsed = response.parsed_output
        if not isinstance(parsed, output_format):
            raise ProviderError(f"Claude returned no usable {task}.", kind="unknown", provider=self.name)
        return parsed

    def _error(self, message: str, kind: str, exc: Exception, *, retryable: bool = False) -> ProviderError:
        cause, not_sent = transport_cause(exc)  # a connection that broke before the request was sent: not billed
        return ProviderError(
            message, kind=kind, retryable=retryable, provider=self.name,  # type: ignore[arg-type]
            detail=str(exc) + (f" <- {cause}" if cause else ""), status_code=getattr(exc, "status_code", None),
            request_id=getattr(exc, "request_id", None), not_sent=not_sent,
        )


def _record_reply(meta: dict, raw, requested_model: str) -> None:
    """The billing facts of a reply for the API call log, read from its raw JSON before the SDK parses it: a reply
    that is cut off or not valid JSON makes parse() raise, but it is billed all the same."""
    try:
        body = raw.http_response.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if getattr(raw, "retries_taken", 0):  # the SDK re-sent the request (a dropped connection, a 5xx or 429 reply)
        meta.setdefault("params", {})["sdk_retries"] = raw.retries_taken
    meta.update(
        model=body.get("model") or requested_model,  # the model that answered (a server-side fallback may differ)
        usage=body.get("usage") or {},
        request_id=raw.request_id,
        stop_reason=body.get("stop_reason"),
        stop_category=(body.get("stop_details") or {}).get("category"),  # a refusal's category decides its billing
    )
