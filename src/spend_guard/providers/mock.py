"""Deterministic offline provider: the demo, the tests and CI spend nothing."""

from __future__ import annotations

from spend_guard.providers.base import ProviderResult


def _tokens(text: str) -> int:
    # Roughly 4 bytes per token for English; the mock only needs to be deterministic.
    return max(1, -(-len(text.encode()) // 4))


class MockProvider:
    name = "mock"

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        words = f"Mock answer from {model} to: {prompt}".split()
        text = " ".join(words[: max(1, max_output_tokens)])
        return ProviderResult(
            text=text,
            input_tokens=_tokens(system) + _tokens(prompt),
            output_tokens=min(_tokens(text), max_output_tokens),
        )
