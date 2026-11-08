"""OpenAI and Anthropic adapters, tested with fake clients only. They were never called against
the real APIs: these tests pin the usage mapping and the error policy instead."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from spend_guard.providers.anthropic_api import AnthropicProvider
from spend_guard.providers.base import ProviderFailed, ProviderRejected
from spend_guard.providers.openai_api import OpenAIProvider


class Recorder:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StatusError(Exception):
    def __init__(self, status: int | None, message: str = "echoes secret-token-123") -> None:
        super().__init__(message)
        if status is not None:
            self.status_code = status


def openai_chat(result: Any, cap: int = 3) -> tuple[OpenAIProvider, Recorder]:
    rec = Recorder(result)
    client = NS(chat=NS(completions=rec), responses=Recorder(None))
    return OpenAIProvider(max_calls=cap, client=client), rec


CHAT = NS(
    choices=[NS(message=NS(content="hi there"))],
    usage=NS(
        prompt_tokens=120,
        completion_tokens=900,  # includes the reasoning tokens below
        completion_tokens_details=NS(reasoning_tokens=850),
        prompt_tokens_details=NS(cached_tokens=100),
    ),
)


def test_openai_chat_usage_and_request() -> None:
    p, rec = openai_chat(CHAT)
    r = p.complete("m", "be brief", "hello", 1000)
    # Reasoning tokens are part of completion_tokens: not added again. Cached prompt tokens are
    # charged at the full input rate (an over-count, never an under-count).
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi there", 120, 900)
    sent = rec.kwargs[0]
    assert sent["max_completion_tokens"] == 1000
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    p.complete("m", "", "hello", 10)
    assert rec.kwargs[1]["messages"] == [{"role": "user", "content": "hello"}]


def test_openai_responses_api() -> None:
    rec = Recorder(NS(output_text="ok", usage=NS(input_tokens=7, output_tokens=40)))
    p = OpenAIProvider(max_calls=1, api="responses", client=NS(responses=rec))
    r = p.complete("m", "sys", "q", 64)
    assert (r.text, r.input_tokens, r.output_tokens) == ("ok", 7, 40)
    assert rec.kwargs[0] == {
        "model": "m",
        "input": "q",
        "max_output_tokens": 64,
        "instructions": "sys",
    }
    with pytest.raises(ValueError, match="api"):
        OpenAIProvider(max_calls=1, api="other", client=NS())  # type: ignore[arg-type]


def anthropic(result: Any, cap: int = 3) -> tuple[AnthropicProvider, Recorder]:
    rec = Recorder(result)
    return AnthropicProvider(max_calls=cap, client=NS(messages=rec)), rec


def test_anthropic_usage_counts_cache_on_the_safe_side() -> None:
    resp = NS(
        content=[
            NS(type="text", text="part one, "),
            NS(type="tool_use", text="x"),
            NS(type="text", text="part two"),
        ],
        usage=NS(
            input_tokens=50,
            output_tokens=30,
            cache_creation_input_tokens=1000,
            cache_read_input_tokens=400,
        ),
    )
    p, rec = anthropic(resp)
    r = p.complete("m", "sys", "q", 256)
    # 50 uncached + 2 x 1000 cache writes + 400 cache reads, all at the input rate.
    assert r.input_tokens == 50 + 2000 + 400
    assert r.output_tokens == 30
    assert r.text == "part one, part two"
    assert rec.kwargs[0] == {
        "model": "m",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "q"}],
        "system": "sys",
    }


def test_anthropic_without_cache_fields() -> None:
    p, rec = anthropic(
        NS(
            content=[],
            usage=NS(
                input_tokens=5,
                output_tokens=6,
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
            ),
        )
    )
    r = p.complete("m", "", "q", 8)
    assert (r.input_tokens, r.output_tokens, r.text) == (5, 6, "")
    assert "system" not in rec.kwargs[0]


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ProviderRejected),
        (401, ProviderRejected),
        (403, ProviderRejected),
        (404, ProviderRejected),
        (413, ProviderRejected),
        (422, ProviderRejected),
        (429, ProviderRejected),
        (500, ProviderFailed),
        (503, ProviderFailed),
        (529, ProviderFailed),
        (None, ProviderFailed),  # connection reset or timeout: may have been processed
    ],
)
def test_error_policy(vendor: str, status: int | None, expected: type[Exception]) -> None:
    err = StatusError(status)
    p = openai_chat(err)[0] if vendor == "openai" else anthropic(err)[0]
    with pytest.raises(expected) as info:
        p.complete("m", "", "q", 8)
    assert "secret-token-123" not in str(info.value)  # vendor text is never passed on


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_cap_zero_makes_no_call(vendor: str) -> None:
    if vendor == "openai":
        p, rec = openai_chat(CHAT, cap=0)
    else:
        p, rec = anthropic(NS(content=[], usage=NS(input_tokens=1, output_tokens=1)), cap=0)
    with pytest.raises(ProviderRejected, match="cap"):
        p.complete("m", "", "q", 8)
    assert rec.kwargs == []
    assert p.calls == 0


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
def test_missing_usage_is_a_failure(vendor: str) -> None:
    p = (
        openai_chat(NS(choices=[], usage=None))[0]
        if vendor == "openai"
        else anthropic(NS(content=[], usage=None))[0]
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
