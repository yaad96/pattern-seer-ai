"""What each paid API call cost, worked out from the token counts the provider returned with it (`usage`).

The providers bill per token, so usage x the published per-token price is the charge. Prices are USD per 1M tokens,
copied from the official pricing pages on PRICES_AS_OF (SOURCES). Every call log record keeps the price row it was
charged with, so a later price change never rewrites an old record. When a price changes, edit the tables here.

A call without usage is priced by what happened to it (`outcome`):
- the offline demo model (mock) is free;
- "error_reply", the API answered with an error status: nothing was made. Anthropic documents that failed requests
  aren't charged; OpenAI does not say, so it is assumed (and noted in the record);
- "not_sent", stopped before the request went out (e.g. a prompt over the length limit): free;
- "no_reply" (timeout, dropped connection, cancelled) and "ok" without token counts: unknown. Both providers may
  charge a request that kept running after the client gave up.
"""

from __future__ import annotations

PRICES_AS_OF = "2026-11-03"
SOURCES = (
    "https://developers.openai.com/api/docs/pricing",
    "https://platform.claude.com/docs/en/about-claude/pricing",
)

# GPT Image through the Images API (images.edit / images.generate), Standard processing. Cached-input rates apply
# only to the Responses API image tool, never to these calls. Text output of the 2.5 models is not billed.
IMAGE_PRICES: dict[str, dict[str, float]] = {
    "gpt-image-2.5-sunburst": {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0, "text_output": 0.0},
    "gpt-image-2.5-flare": {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0, "text_output": 0.0},
    "gpt-image-2": {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0, "text_output": 0.0},
    "gpt-image-1.5": {"text_input": 5.0, "image_input": 8.0, "image_output": 32.0, "text_output": 10.0},
    "chatgpt-image-latest": {"text_input": 5.0, "image_input": 8.0, "image_output": 32.0, "text_output": 10.0},
    "gpt-image-1-mini": {"text_input": 2.0, "image_input": 2.5, "image_output": 8.0, "text_output": 0.0},
}

# Claude (Messages API), global inference, standard speed. Thinking tokens are part of output_tokens.
CLAUDE_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.2},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_write_1h": 4.0, "cache_read": 0.1},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_write_1h": 4.0, "cache_read": 0.2},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 0.25},
}
CLAUDE_US_ONLY = 1.1  # usage.inference_geo "us"
# A refusal that arrives before any output is billed only in these categories (stop_details.category).
CLAUDE_BILLED_REFUSALS = frozenset({"bio", "frontier_llm", "reasoning_extraction"})

# OpenAI text models (Responses API), short context (<= 272K input tokens), by usage.service_tier.
OPENAI_TEXT_PRICES: dict[str, dict[str, float]] = {
    "gpt-6.1-sol": {"input": 2.0, "cached_input": 0.1, "cache_write_30m": 2.5, "cache_write_12h": 5.0, "output": 10.0},
}
OPENAI_TIER = {"default": 1.0, "auto": 1.0, "standard": 1.0, "flex": 0.5, "priority": 2.0, "ultrafast": 6.0}
# OpenAI's regional (data residency) endpoints cost 10% more for models released on or after 2026-03-05.
OPENAI_REGIONAL_HOSTS = frozenset({"us.api.openai.com", "eu.api.openai.com", "ae.api.openai.com"})
OPENAI_REGIONAL_UPLIFT = 1.1
OPENAI_UPLIFT_MODELS = frozenset({"gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2", "gpt-6.1-sol"})

_MILLION = 1_000_000


def call_cost(
    provider: str,
    model: str | None,
    usage: dict | None,
    *,
    outcome: str,
    stop_reason: str | None = None,
    stop_category: str | None = None,
    requested_model: str | None = None,
    api_host: str | None = None,
) -> tuple[float | None, dict | None]:
    """(cost in USD, or None when it can't be known; the price row used, stored with the call). `outcome` is "ok",
    "error_reply", "not_sent" or "no_reply" (see the module docstring)."""
    if provider == "mock":
        return 0.0, {"model": model, "note": "offline demo model, no charge"}
    if not usage:
        if outcome == "error_reply":
            note = "error reply: nothing generated, not billed"
            return 0.0, {"model": model, "note": note if provider == "claude" else f"{note} (assumed)"}
        if outcome == "not_sent":
            return 0.0, {"model": model, "note": "not sent (the connection broke before the request went out): no charge"}
        return None, None
    if provider == "openai" and _rates(IMAGE_PRICES, model):
        return _regional(_image_cost(model or "", usage), model, api_host)
    if provider == "claude" and _rates(CLAUDE_PRICES, model):
        return _claude_cost(model or "", usage, stop_reason, stop_category, requested_model or model or "")
    if provider == "openai" and _rates(OPENAI_TEXT_PRICES, model):
        return _regional(_openai_text_cost(model or "", usage), model, api_host)
    return None, None


def _regional(priced: tuple[float | None, dict | None], model: str | None, api_host: str | None):
    """OpenAI's +10% for a regional endpoint (the host the call went to is in the call's settings)."""
    cost, row = priced
    base = next((m for m in OPENAI_UPLIFT_MODELS if model == m or (model or "").startswith(m + "-")), None)
    if cost is None or row is None or api_host not in OPENAI_REGIONAL_HOSTS or base is None:
        return priced
    row["multiplier"] = round(row.get("multiplier", 1.0) * OPENAI_REGIONAL_UPLIFT, 6)
    row["note"] = f"regional endpoint {api_host}: +10%"
    return cost * OPENAI_REGIONAL_UPLIFT, row


def _n(counts: dict | None, key: str) -> int:
    """A token count from a usage object: 0 when it is missing or null (raw replies may say `null`)."""
    value = (counts or {}).get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _rates(table: dict[str, dict[str, float]], model: str | None) -> dict[str, float] | None:
    """The model's price row; a dated snapshot ("gpt-image-2.5-sunburst-2026-09-08") uses its model's row."""
    if not model:
        return None
    if model in table:
        return table[model]
    base = max((key for key in table if model.startswith(key + "-")), key=len, default=None)
    return table[base] if base else None


def _row(model: str, rates: dict[str, float], **extra) -> dict:
    return {"model": model, "per_1m_tokens": dict(rates), "as_of": PRICES_AS_OF, **extra}


def _image_cost(model: str, usage: dict) -> tuple[float, dict]:
    """Input text and image tokens plus output image (and text) tokens, each at its own rate."""
    rates = _rates(IMAGE_PRICES, model) or {}
    given = usage.get("input_tokens_details")
    made = usage.get("output_tokens_details") or {"image_tokens": _n(usage, "output_tokens")}  # 2.5 makes images only
    if given is None and _n(usage, "input_tokens"):
        return None, None  # no text/image split: the input can't be priced exactly
    split_in = _n(given, "text_tokens") + _n(given, "image_tokens")
    split_out = _n(made, "image_tokens") + _n(made, "text_tokens")
    if split_in != _n(usage, "input_tokens") or split_out != _n(usage, "output_tokens"):
        return None, None  # the parts don't add up to the totals: a token kind this table doesn't know
    cost = (
        _n(given, "text_tokens") * rates["text_input"]
        + _n(given, "image_tokens") * rates["image_input"]
        + _n(made, "image_tokens") * rates["image_output"]
        + _n(made, "text_tokens") * rates["text_output"]
    ) / _MILLION
    return cost, _row(model, rates)


def _claude_cost(
    model: str, usage: dict, stop_reason: str | None, stop_category: str | None, requested_model: str
) -> tuple[float | None, dict | None]:
    """Each attempt at its own model's rates (a server-side fallback lists them in usage.iterations); input_tokens is
    the uncached part, and thinking is inside output_tokens. An attempt declined before any output is billed only
    in some refusal categories: a hand-off to the fallback model is assumed free (a jersey picture can't fall in a
    billed category), and the last attempt follows the reply's own stop_details.category."""
    if usage.get("speed") == "fast":
        return None, None  # fast mode has its own prices; the app never asks for it
    attempts = usage.get("iterations") or [usage]
    billed = []
    for number, attempt in enumerate(attempts, start=1):
        last = number == len(attempts)
        if not _n(attempt, "output_tokens") and (not last or stop_reason == "refusal"):
            if not last or stop_category not in CLAUDE_BILLED_REFUSALS:
                continue  # declined before any output: not billed
        billed.append(attempt)
    if not billed:
        return 0.0, {"model": model, "note": f"refused before any output ({stop_category or 'no category'}): not billed"}
    total, used = 0.0, {}
    for attempt in billed:
        name = attempt.get("model") or (requested_model if attempt.get("type") == "message" else model)
        rates = _rates(CLAUDE_PRICES, name)
        if rates is None:
            return None, None
        cache = attempt.get("cache_creation") or {}  # by TTL; without it, every cache write is priced as 5 minutes
        write_5m = _n(cache, "ephemeral_5m_input_tokens") if cache else _n(attempt, "cache_creation_input_tokens")
        total += (
            _n(attempt, "input_tokens") * rates["input"]
            + write_5m * rates["cache_write_5m"]
            + _n(cache, "ephemeral_1h_input_tokens") * rates["cache_write_1h"]
            + _n(attempt, "cache_read_input_tokens") * rates["cache_read"]
            + _n(attempt, "output_tokens") * rates["output"]
        ) / _MILLION
        used[name] = rates
    multiplier = CLAUDE_US_ONLY if usage.get("inference_geo") == "us" else 1.0
    row = _row(model, used.get(model) or next(iter(used.values())), multiplier=multiplier)
    if len(used) > 1:
        row["note"] = "server-side fallback: each attempt at its own model's rates"
    return total * multiplier, row


def _openai_text_cost(model: str, usage: dict) -> tuple[float | None, dict | None]:
    """Uncached input, cache reads, cache writes (by TTL) and output; reasoning tokens are inside output_tokens."""
    rates = _rates(OPENAI_TEXT_PRICES, model) or {}
    tier = OPENAI_TIER.get(str(usage.get("service_tier") or "default"))
    if tier is None or _n(usage, "input_tokens") > 272_000:
        return None, None  # an unknown tier, or long-context prices: not used by the app
    given = usage.get("input_tokens_details") or {}
    cached, writes = _n(given, "cached_tokens"), _n(given, "cache_write_tokens")
    by_ttl = given.get("cache_write_tokens_details") or {}
    write_12h = _n(by_ttl, "ttl_12h_tokens")
    write_30m = _n(by_ttl, "ttl_30m_tokens") if "ttl_30m_tokens" in by_ttl else max(0, writes - write_12h)
    ordinary = max(0, _n(usage, "input_tokens") - cached - writes)
    cost = (
        ordinary * rates["input"]
        + cached * rates["cached_input"]
        + write_30m * rates["cache_write_30m"]
        + write_12h * rates["cache_write_12h"]
        + _n(usage, "output_tokens") * rates["output"]
    ) / _MILLION
    return cost * tier, _row(model, rates, multiplier=tier)
