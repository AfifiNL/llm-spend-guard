"""OpenAI and Anthropic adapters, tested with fake clients and the installed SDKs' own exception
classes. They were never called against the real APIs: these tests pin the usage mapping, the
tier checks and the error policy instead."""

from __future__ import annotations

import uuid
from types import SimpleNamespace as NS
from typing import Any

import anthropic
import httpx
import openai
import pytest
from psycopg_pool import ConnectionPool

from conftest import NOW, Clock, setup_prices_and_budgets
from spend_guard.gateway import FORMAT_ALLOWANCE_TOKENS, SpendGuard
from spend_guard.ledger import SpendLedger
from spend_guard.providers.anthropic_api import AnthropicProvider
from spend_guard.providers.base import ProviderFailed, ProviderRejected
from spend_guard.providers.openai_api import OpenAIProvider

REQ = httpx.Request("POST", "https://api.example.invalid/v1")
SECRET = "echoes secret-token-123"


def status_error(mod: Any, cls: str, status: int) -> Exception:
    resp = httpx.Response(status, request=REQ, json={"error": {"message": SECRET}})
    err: Exception = getattr(mod, cls)(SECRET, response=resp, body=None)
    return err


class Recorder:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def openai_chat(result: Any, cap: int = 3) -> tuple[OpenAIProvider, Recorder]:
    rec = Recorder(result)
    return OpenAIProvider(max_calls=cap, client=NS(chat=NS(completions=rec))), rec


def chat_response(tier: str | None = "default", writes: int | None = None) -> Any:
    return NS(
        service_tier=tier,
        choices=[NS(message=NS(content="hi there"))],
        usage=NS(
            prompt_tokens=120,
            completion_tokens=900,  # includes the reasoning tokens below
            completion_tokens_details=NS(reasoning_tokens=850),
            prompt_tokens_details=NS(cached_tokens=20, cache_write_tokens=writes),
        ),
    )


def test_openai_chat_usage_and_request() -> None:
    p, rec = openai_chat(chat_response())
    r = p.complete("m", "be brief", "hello", 1000)
    # Reasoning tokens are part of completion_tokens: not added again. Cached reads are charged
    # at the full input rate (an over-count).
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi there", 120, 900)
    assert r.unpriced_reason is None
    sent = rec.kwargs[0]
    assert sent["max_completion_tokens"] == 1000
    assert sent["service_tier"] == "default"
    assert sent["store"] is False
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    p.complete("m", "", "hello", 10)
    assert rec.kwargs[1]["messages"] == [{"role": "user", "content": "hello"}]


def test_openai_cache_writes_are_charged_at_two_and_a_half_times() -> None:
    p, _ = openai_chat(chat_response(writes=101))
    r = p.complete("m", "", "q", 8)
    # 120 prompt tokens at 1x, plus 1.5x extra for the 101 written to the cache (rounded up).
    assert r.input_tokens == 120 + 152
    assert OpenAIProvider.input_rate_ceiling == 2.5


@pytest.mark.parametrize("tier", ["priority", "flex", "scale", "auto"])
def test_openai_non_standard_tier_is_unpriced(tier: str) -> None:
    r = openai_chat(chat_response(tier=tier))[0].complete("m", "", "q", 8)
    assert r.unpriced_reason is not None
    assert tier in r.unpriced_reason


def test_openai_responses_api() -> None:
    resp = NS(
        output_text="ok",
        service_tier="default",
        usage=NS(
            input_tokens=7,
            output_tokens=40,
            input_tokens_details=NS(cached_tokens=0, cache_write_tokens=2),
        ),
    )
    rec = Recorder(resp)
    p = OpenAIProvider(max_calls=1, api="responses", client=NS(responses=rec))
    r = p.complete("m", "sys", "q", 64)
    assert (r.text, r.input_tokens, r.output_tokens) == ("ok", 7 + 3, 40)
    assert rec.kwargs[0] == {
        "model": "m",
        "input": "q",
        "max_output_tokens": 64,
        "service_tier": "default",
        "store": False,
        "instructions": "sys",
    }
    with pytest.raises(ValueError, match="api"):
        OpenAIProvider(max_calls=1, api="other", client=NS())  # type: ignore[arg-type]


def anthropic_p(
    result: Any, cap: int = 3, geo: str | None = None
) -> tuple[AnthropicProvider, Recorder]:
    rec = Recorder(result)
    return AnthropicProvider(max_calls=cap, inference_geo=geo, client=NS(messages=rec)), rec


def anthropic_response(**usage: Any) -> Any:
    base = {
        "input_tokens": 50,
        "output_tokens": 30,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "service_tier": "standard",
        "speed": "standard",
        "inference_geo": None,
    }
    return NS(
        content=[
            NS(type="text", text="part one, "),
            NS(type="tool_use", text="x"),
            NS(type="text", text="part two"),
        ],
        usage=NS(**{**base, **usage}),
    )


def test_anthropic_usage_counts_cache_on_the_safe_side() -> None:
    p, rec = anthropic_p(
        anthropic_response(cache_creation_input_tokens=1000, cache_read_input_tokens=400)
    )
    r = p.complete("m", "sys", "q", 256)
    # 50 uncached + 2 x 1000 cache writes + 400 cache reads, all at the input rate.
    assert r.input_tokens == 50 + 2000 + 400
    assert r.output_tokens == 30
    assert r.text == "part one, part two"
    assert r.unpriced_reason is None
    assert rec.kwargs[0] == {
        "model": "m",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "q"}],
        "service_tier": "standard_only",
        "speed": "standard",
        "system": "sys",
    }
    assert AnthropicProvider.input_rate_ceiling == 2.0


@pytest.mark.parametrize(
    ("usage", "word"),
    [
        ({"service_tier": "priority"}, "priority"),
        ({"service_tier": "batch"}, "batch"),
        ({"speed": "fast"}, "fast"),
    ],
)
def test_anthropic_non_standard_service_is_unpriced(usage: dict[str, Any], word: str) -> None:
    r = anthropic_p(anthropic_response(**usage))[0].complete("m", "", "q", 8)
    assert r.unpriced_reason is not None
    assert word in r.unpriced_reason


def test_anthropic_region_is_sent_and_enforced_when_configured() -> None:
    p, rec = anthropic_p(anthropic_response(inference_geo="eu"), geo="eu")
    assert p.complete("m", "", "q", 8).unpriced_reason is None
    assert rec.kwargs[0]["inference_geo"] == "eu"
    other = anthropic_p(anthropic_response(inference_geo="us"), geo="eu")[0]
    assert "region" in (other.complete("m", "", "q", 8).unpriced_reason or "")
    unset, rec2 = anthropic_p(anthropic_response(inference_geo="us"))
    assert unset.complete("m", "", "q", 8).unpriced_reason is None  # not configured: not judged
    assert "inference_geo" not in rec2.kwargs[0]


OPENAI_ERRORS = [
    (status_error(openai, "BadRequestError", 400), ProviderRejected),
    (status_error(openai, "AuthenticationError", 401), ProviderRejected),
    (status_error(openai, "PermissionDeniedError", 403), ProviderRejected),
    (status_error(openai, "NotFoundError", 404), ProviderRejected),
    (status_error(openai, "ConflictError", 409), ProviderRejected),
    (status_error(openai, "APIStatusError", 413), ProviderRejected),
    (status_error(openai, "UnprocessableEntityError", 422), ProviderRejected),
    (status_error(openai, "RateLimitError", 429), ProviderRejected),
    (status_error(openai, "InternalServerError", 500), ProviderFailed),
    (status_error(openai, "InternalServerError", 503), ProviderFailed),
    (openai.APITimeoutError(request=REQ), ProviderFailed),
    (openai.APIConnectionError(request=REQ), ProviderFailed),
]
ANTHROPIC_ERRORS = [
    (status_error(anthropic, "BadRequestError", 400), ProviderRejected),
    (status_error(anthropic, "AuthenticationError", 401), ProviderRejected),
    (status_error(anthropic, "PermissionDeniedError", 403), ProviderRejected),
    (status_error(anthropic, "NotFoundError", 404), ProviderRejected),
    (status_error(anthropic, "ConflictError", 409), ProviderRejected),
    (status_error(anthropic, "RequestTooLargeError", 413), ProviderRejected),
    (status_error(anthropic, "RateLimitError", 429), ProviderRejected),
    (status_error(anthropic, "InternalServerError", 500), ProviderFailed),
    (status_error(anthropic, "OverloadedError", 529), ProviderFailed),
    (anthropic.APITimeoutError(request=REQ), ProviderFailed),
    (anthropic.APIConnectionError(request=REQ), ProviderFailed),
]


@pytest.mark.parametrize(("err", "expected"), OPENAI_ERRORS)
def test_openai_error_policy(err: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected) as info:
        openai_chat(err)[0].complete("m", "", "q", 8)
    assert "secret-token-123" not in str(info.value)  # vendor text is never passed on


@pytest.mark.parametrize(("err", "expected"), ANTHROPIC_ERRORS)
def test_anthropic_error_policy(err: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected) as info:
        anthropic_p(err)[0].complete("m", "", "q", 8)
    assert "secret-token-123" not in str(info.value)


def ledger_rows(pool: ConnectionPool[Any]) -> list[tuple[str, int, int]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, amount_micros, released_micros FROM ledger ORDER BY id"
        ).fetchall()
    return [(r[0], int(r[1]), int(r[2])) for r in rows]


@pytest.mark.parametrize(
    ("err", "expected", "charged"),
    [
        (openai.APITimeoutError(request=REQ), ProviderFailed, True),
        (openai.APIConnectionError(request=REQ), ProviderFailed, True),
        (status_error(openai, "InternalServerError", 500), ProviderFailed, True),
        (status_error(openai, "ConflictError", 409), ProviderRejected, False),
        (status_error(openai, "RateLimitError", 429), ProviderRejected, False),
    ],
)
def test_gateway_charges_the_worst_case_exactly_once(
    pool: ConnectionPool[Any],
    clock: Clock,
    err: Exception,
    expected: type[Exception],
    charged: bool,
) -> None:
    setup_prices_and_budgets(pool)
    guard = SpendGuard(SpendLedger(pool, clock=clock), openai_chat(err)[0])
    with pytest.raises(expected):
        guard.complete("acme", "m1", "hello", max_output_tokens=10, request_id=uuid.uuid4())
    rows = ledger_rows(pool)
    (reserve,) = [r for r in rows if r[0] == "reserve"]
    closing = [r for r in rows if r[0] != "reserve"]
    assert len(closing) == 1
    if charged:
        assert closing[0] == ("settle", reserve[1], reserve[1])  # full worst case, once
    else:
        assert closing[0] == ("release", 0, reserve[1])
    assert guard.ledger.reconcile(NOW.date()) == []


def test_openai_reservation_covers_cache_write_premium(
    pool: ConnectionPool[Any], clock: Clock
) -> None:
    setup_prices_and_budgets(pool)  # 1.0 in / 2.0 out per Mtok: 1 / 2 micros per token
    guard = SpendGuard(SpendLedger(pool, clock=clock), openai_chat(chat_response(writes=120))[0])
    c = guard.complete("acme", "m1", "hello", max_output_tokens=1000)
    bound = len(b"hello") + FORMAT_ALLOWANCE_TOKENS
    assert c.reserved_micros == -(-bound * 25 // 10) + 2 * 1000
    assert c.cost_micros == 120 + 180 + 2 * 900  # every prompt token was a cache write


def test_unpriced_response_is_charged_its_reservation(
    pool: ConnectionPool[Any], clock: Clock
) -> None:
    setup_prices_and_budgets(pool)
    guard = SpendGuard(
        SpendLedger(pool, clock=clock), openai_chat(chat_response(tier="priority"))[0]
    )
    c = guard.complete("acme", "m1", "hello", max_output_tokens=1000)
    assert c.text == "hi there"
    assert c.cost_micros == c.reserved_micros
    with pool.connection() as conn:
        note = conn.execute("SELECT note FROM ledger WHERE kind = 'settle'").fetchone()
    assert note is not None
    assert "priority" in note[0]


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_cap_zero_makes_no_call(vendor: str) -> None:
    if vendor == "openai":
        p, rec = openai_chat(chat_response(), cap=0)
    else:
        p, rec = anthropic_p(anthropic_response(), cap=0)
    with pytest.raises(ProviderRejected, match="cap"):
        p.complete("m", "", "q", 8)
    assert rec.kwargs == []
    assert p.calls == 0


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_missing_usage_is_a_failure(vendor: str) -> None:
    p = (
        openai_chat(NS(choices=[], usage=None))[0]
        if vendor == "openai"
        else anthropic_p(NS(content=[], usage=None))[0]
    )
    with pytest.raises(ProviderFailed, match="usage"):
        p.complete("m", "", "q", 8)


def test_factory_defaults_to_no_live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from spend_guard.factory import build_provider

    # Placeholder values so the SDK clients can be constructed; no request is ever sent.
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-key")
    monkeypatch.delenv("SPEND_GUARD_MAX_LIVE_CALLS", raising=False)
    for kind in ("openai", "anthropic"):
        monkeypatch.setenv("SPEND_GUARD_PROVIDER", kind)
        provider = build_provider()
        assert provider.name == kind
        with pytest.raises(ProviderRejected, match="cap"):
            provider.complete("m", "", "q", 8)
    monkeypatch.setenv("SPEND_GUARD_PROVIDER", "openai")
    monkeypatch.setenv("SPEND_GUARD_OPENAI_API", "nope")
    with pytest.raises(ValueError, match="SPEND_GUARD_OPENAI_API"):
        build_provider()
