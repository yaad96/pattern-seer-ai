"""OpenAI as the 'AI designer': structured design briefs and reference checks."""

from __future__ import annotations

import base64
from typing import TypeVar

import openai
import pydantic
from openai.types.responses import ParsedResponse

from app.providers.base import ProviderError
from app.providers.openai_images import SAFETY_CODES, map_openai_error
from app.providers.text.base import BriefRequest
from app.schemas import BriefResponse, ReferenceAnalysis

# Reasoning models (GPT-5/6 families, o3/o4) accept reasoning.effort; "low" keeps the brief quick while
# still planning palette and zones. Chat-only models reject the parameter, so it is sent only to these.
REASONING_PREFIXES = ("gpt-6", "gpt-5", "o3", "o4")
REASONING_EFFORT = "low"
MAX_OUTPUT_TOKENS = 16_000  # structured JSON plus reasoning tokens; bounds a runaway response

T = TypeVar("T", bound=pydantic.BaseModel)


class OpenAIBriefWriter:
    name = "openai"
    label = "OpenAI"

    def __init__(self, api_key: str, model: str, timeout_s: float) -> None:
        self.model = model
        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)

    async def write(self, req: BriefRequest) -> BriefResponse:
        return await self._parse(req, BriefResponse, "design brief")

    async def analyze_reference(self, req: BriefRequest) -> ReferenceAnalysis:
        return await self._parse(req, ReferenceAnalysis, "reference check")

    def _request_kwargs(self, req: BriefRequest, text_format: type[pydantic.BaseModel]) -> dict:
        content: list[dict] = [
            {
                "type": "input_image",
                "image_url": f"data:{img.mime_type};base64,{base64.standard_b64encode(img.data).decode('ascii')}",
                "detail": "high",
            }
            for img in req.images
        ]
        content.append({"type": "input_text", "text": req.user_prompt})
        kwargs: dict = {
            "model": self.model,
            "instructions": req.system_prompt,
            "input": [{"role": "user", "content": content}],
            "text_format": text_format,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,  # single-turn call; nothing needs to be kept server-side
        }
        if self.model.startswith(REASONING_PREFIXES) and "-chat" not in self.model:
            kwargs["reasoning"] = {"effort": REASONING_EFFORT}
        return kwargs

    async def _parse(self, req: BriefRequest, text_format: type[T], task: str) -> T:
        """One structured-output call; SDK errors and unusable replies become ProviderError."""
        kwargs = self._request_kwargs(req, text_format)
        params = {k: v for k, v in kwargs.items() if k not in ("instructions", "input", "text_format")}
        params.update(text_format=text_format.__name__, api_host=self._client.base_url.host)
        req.meta.update(endpoint="responses.parse", params=params)
        try:
            raw = await self._client.responses.with_raw_response.parse(**kwargs)
            _record_reply(req.meta, raw, self.model)  # before parsing: a reply that fails to parse is billed too
            response = raw.parse()
        except openai.OpenAIError as exc:
            forbidden = f"This OpenAI API key is not allowed to use model '{self.model}'."
            raise map_openai_error(exc, provider=self.name, model=self.model, forbidden=forbidden) from exc
        except pydantic.ValidationError as exc:  # truncated or malformed JSON, e.g. max_output_tokens reached
            raise ProviderError(
                f"OpenAI returned an incomplete {task}.", kind="unknown", provider=self.name, detail=str(exc)
            ) from exc

        refusal = _refusal(response)
        if refusal is not None:
            raise ProviderError(
                f"OpenAI declined to write this {task}.", kind="safety", provider=self.name, detail=refusal
            )
        if response.error is not None:
            kind = "safety" if response.error.code in SAFETY_CODES else "unknown"
            raise ProviderError(
                f"OpenAI could not write the {task} ({response.error.code}).",
                kind=kind,
                provider=self.name,
                detail=response.error.message,
            )
        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else None
            kind = "safety" if reason == "content_filter" else "unknown"
            message = f"OpenAI's {task} was cut off ({reason or 'incomplete'})."
            raise ProviderError(message, kind=kind, provider=self.name)
        parsed = response.output_parsed
        if not isinstance(parsed, text_format):
            raise ProviderError(f"OpenAI returned no usable {task}.", kind="unknown", provider=self.name)
        return parsed


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
    usage = dict(body.get("usage") or {})
    if usage and body.get("service_tier"):
        usage["service_tier"] = body["service_tier"]  # the processing tier sets the price (app/pricing.py)
    meta.update(
        model=body.get("model") or requested_model,
        usage=usage,
        request_id=raw.request_id,
        stop_reason=body.get("status"),
    )


def _refusal(response: ParsedResponse[pydantic.BaseModel]) -> str | None:
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    return part.refusal
    return None
