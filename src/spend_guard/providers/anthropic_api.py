"""Anthropic adapter (optional extra `llm-spend-guard[anthropic]`). NEVER called against the real
API during development: it is tested only with fake clients (tests/test_vendor_providers.py).

Usage mapping (Anthropic's Messages API):
  * `usage.input_tokens` counts only the uncached input; prompt caching adds
    `cache_creation_input_tokens` (written to the cache) and `cache_read_input_tokens`.
  * The price book has one input rate, so cached input is converted to input-rate tokens on the
    safe side: cache writes count twice (the highest write multiplier in Anthropic's published
    pricing when this was written; not re-checked) and cache reads count in full (they are billed
    at a fraction). The guard can only over-count caching. This adapter never asks for caching,
    so both fields are normally zero.
  * `usage.output_tokens` is the output, including any extended-thinking tokens.

The SDK's own retries are switched off (`max_retries=0`), as for the other adapters.
"""

from __future__ import annotations

from typing import Any

from spend_guard.providers._common import CallCap, classify
from spend_guard.providers.base import ProviderFailed, ProviderResult

REJECTED_BEFORE_WORK = frozenset({400, 401, 403, 404, 409, 413, 422, 429})
CACHE_WRITE_FACTOR = 2


def input_equivalent(usage: Any) -> int | None:
    base = getattr(usage, "input_tokens", None)
    if not isinstance(base, int):
        return None
    writes = getattr(usage, "cache_creation_input_tokens", None) or 0
    reads = getattr(usage, "cache_read_input_tokens", None) or 0
    return base + CACHE_WRITE_FACTOR * int(writes) + int(reads)


class AnthropicProvider:
    def __init__(self, *, max_calls: int, timeout_s: float = 60.0, client: Any = None) -> None:
        self.name = "anthropic"
        self.cap = CallCap(max_calls, "SPEND_GUARD_MAX_LIVE_CALLS")
        if client is None:
            from anthropic import Anthropic

            client = Anthropic(timeout=timeout_s, max_retries=0)
        self._client = client

    @property
    def calls(self) -> int:
        return self.cap.used

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        self.cap.take()
        kwargs: dict[str, Any] = {"system": system} if system else {}
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
        except Exception as exc:
            raise classify(exc, "Anthropic", REJECTED_BEFORE_WORK) from None
        usage = getattr(resp, "usage", None)
        inp = input_equivalent(usage)
        out = getattr(usage, "output_tokens", None)
        if inp is None or not isinstance(out, int):
            raise ProviderFailed("Anthropic response has no usage")
        text = "".join(
            getattr(block, "text", "")
            for block in (getattr(resp, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        return ProviderResult(text=text, input_tokens=inp, output_tokens=out)
