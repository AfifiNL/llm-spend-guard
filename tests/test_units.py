from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spend_guard.money import cost_micros, fmt, parse
from spend_guard.providers.base import ProviderFailed, ProviderRejected
from spend_guard.providers.gemini import GeminiProvider
from spend_guard.providers.replay import RecordingProvider, ReplayProvider


def test_cost_rounds_up_and_is_exact() -> None:
    assert cost_micros(1, 0, 750_000, 3_750_000) == 1  # 0.75 micros -> 1
    assert cost_micros(1_000_000, 1_000_000, 750_000, 3_750_000) == 4_500_000
    with pytest.raises(ValueError, match="non-negative"):
        cost_micros(-1, 0, 1, 1)


@pytest.mark.parametrize(
    ("text", "micros"), [("0.75", 750_000), ("3", 3_000_000), (".5", 500_000), ("0.000001", 1)]
)
def test_parse_and_fmt(text: str, micros: int) -> None:
    assert parse(text) == micros
    assert parse(fmt(micros)) == micros


@pytest.mark.parametrize("bad", ["0.0000001", "abc", "1.2.3", "", "+-5", "\u0663", "."])
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match=r"amount|decimals"):
        parse(bad)


class FakeModels:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def generate_content(self, **_: Any) -> Any:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def usage(p: int | None, c: int | None, t: int | None) -> Any:
    return SimpleNamespace(prompt_token_count=p, candidates_token_count=c, thoughts_token_count=t)


def gemini(result: Any, cap: int = 3) -> tuple[GeminiProvider, FakeModels]:
    models = FakeModels(result)
    return GeminiProvider(max_calls=cap, client=SimpleNamespace(models=models)), models


def test_gemini_counts_thinking_as_output() -> None:
    g, _ = gemini(SimpleNamespace(text="ok", usage_metadata=usage(10, 5, 40)))
    r = g.complete("m", "", "p", 64)
    assert (r.input_tokens, r.output_tokens) == (10, 45)


def test_gemini_cap_and_errors() -> None:
    g, models = gemini(SimpleNamespace(text="ok", usage_metadata=usage(1, 1, None)), cap=0)
    with pytest.raises(ProviderRejected, match="cap"):
        g.complete("m", "", "p", 8)
    assert models.calls == 0

    class Err(Exception):
        def __init__(self, code: int) -> None:
            super().__init__("message that may echo secret-token-123")
            self.code = code

    g, _ = gemini(Err(429))
    with pytest.raises(ProviderRejected) as rej:
        g.complete("m", "", "p", 8)
    assert "secret-token-123" not in str(rej.value)
    g, _ = gemini(Err(503))
    with pytest.raises(ProviderFailed):
        g.complete("m", "", "p", 8)
    g, _ = gemini(SimpleNamespace(text="ok", usage_metadata=None))
    with pytest.raises(ProviderFailed, match="usage"):
        g.complete("m", "", "p", 8)


def test_record_and_replay(tmp_path: Path) -> None:
    g, _ = gemini(SimpleNamespace(text="ok", usage_metadata=usage(3, 2, 1)))
    path = tmp_path / "c.jsonl"
    rec = RecordingProvider(g, path, {"model": "m"})
    rec.complete("m", "s", "p", 8)
    rec.save()
    rp = ReplayProvider(path)
    assert rp.complete("m", "s", "p", 8).output_tokens == 3
    with pytest.raises(ProviderFailed, match="re-record"):
        rp.complete("m", "s", "other", 8)
    assert rp.misses == ["other"]
