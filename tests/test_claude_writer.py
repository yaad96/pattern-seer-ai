import asyncio
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.providers.base import InputImage, ProviderError
from app.providers.text.base import BriefRequest
from app.providers.text.claude import FALLBACK_BETA, ClaudeBriefWriter
from app.schemas import BriefResponse, DesignBrief

BRIEF = DesignBrief(
    title="Royal Bengal Strike",
    sport="cricket",
    garment="short-sleeve polo-collar cricket jersey",
    fit="athletic fit",
    collar="polo collar, gold tipping",
    sleeves="short raglan sleeves",
    cuffs_and_hem="ribbed cuffs, straight hem",
    base_color="maroon #6D1A36",
    palette=[],
    pattern="gold tiger-stripe brush strokes on the sides",
    zones=[],
    text_elements=[],
    logos=[],
    style_keywords=["aggressive"],
    mood="fierce",
    avoid=[],
    assumptions=[],
)
RESPONSE = BriefResponse(brief=BRIEF, changes=[], reply="Here is your design.", suggestions=["Add a gold collar"])
REQUEST = BriefRequest(
    system_prompt="SYSTEM",
    user_prompt="USER",
    images=[InputImage(data=b"\x89PNG-fake", mime_type="image/png", label="inspiration")],
)


class FakeRaw:
    """Like the SDK's raw response: the reply's JSON and request id, then parse()."""

    def __init__(self, result):
        self.result, self.request_id = result, "req_test"
        body = {"model": "claude-opus-5-5", "stop_reason": getattr(result, "stop_reason", None),
                "usage": {"input_tokens": 10, "output_tokens": 5}}
        self.http_response = SimpleNamespace(json=lambda: body)

    async def parse(self):
        return self.result


class FakeMessages:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []
        self.with_raw_response = SimpleNamespace(parse=self._raw_parse)  # the writer reads the raw reply first

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result

    async def _raw_parse(self, **kwargs):
        return FakeRaw(await self.parse(**kwargs))


def make_writer(model="claude-opus-5-5", result=None, exc=None):
    writer = ClaudeBriefWriter(api_key="sk-ant-test", model=model, effort="medium", timeout_s=10)
    fake = FakeMessages(result=result, exc=exc)
    writer._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
    return writer, fake


def ok(stop_reason="end_turn", parsed=RESPONSE):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed)


def test_request_shape_for_opus_5_5():
    writer, fake = make_writer(result=ok())
    result = asyncio.run(writer.write(REQUEST))
    assert result == RESPONSE
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"] == "SYSTEM"
    assert kw["output_format"] is BriefResponse
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "temperature" not in kw and "thinking" not in kw
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "USER"}


def test_no_fallback_or_effort_for_older_models():
    writer, fake = make_writer(model="claude-haiku-4-5", result=ok())
    asyncio.run(writer.write(REQUEST))
    kw = fake.calls[0]
    assert "fallbacks" not in kw and "betas" not in kw and "output_config" not in kw


@pytest.mark.parametrize(
    ("stop_reason", "parsed", "kind"),
    [("refusal", None, "safety"), ("max_tokens", None, "unknown"), ("end_turn", None, "unknown")],
)
def test_unusable_responses(stop_reason, parsed, kind):
    writer, _ = make_writer(result=ok(stop_reason=stop_reason, parsed=parsed))
    with pytest.raises(ProviderError) as info:
        asyncio.run(writer.write(REQUEST))
    assert info.value.kind == kind


REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(cls, code):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (status_error(anthropic.AuthenticationError, 401), "auth", False),
        (status_error(anthropic.PermissionDeniedError, 403), "auth", False),
        (status_error(anthropic.NotFoundError, 404), "bad_request", False),
        (status_error(anthropic.RateLimitError, 429), "rate_limit", True),
        (status_error(anthropic.BadRequestError, 400), "bad_request", False),
        (status_error(anthropic.InternalServerError, 500), "unavailable", True),
        (anthropic.APITimeoutError(request=REQ), "timeout", True),
        (anthropic.APIConnectionError(request=REQ), "unavailable", True),
    ],
)
def test_error_mapping(exc, kind, retryable):
    writer, _ = make_writer(exc=exc)
    with pytest.raises(ProviderError) as info:
        asyncio.run(writer.write(REQUEST))
    assert info.value.kind == kind
    assert info.value.retryable is retryable
    assert info.value.provider == "claude"
    assert "sk-ant-test" not in info.value.message
