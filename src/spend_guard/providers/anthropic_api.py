"""Anthropic adapter (optional extra `llm-spend-guard[anthropic]`). NEVER called against the real
API during development: it is tested only with fake clients and the SDK's own exception classes
(tests/test_vendor_providers.py).

Usage mapping (Messages API):
  * `usage.input_tokens` counts only uncached input; prompt caching adds
    `cache_creation_input_tokens` (writes) and `cache_read_input_tokens` (reads).
  * Anthropic's prompt-caching page (read 2026-11-08) prices cache writes at 1.25x (5-minute
    TTL) or 2x (1-hour TTL) the input rate and reads at 0.1x or less. The adapter charges writes
    at 2x, the highest of those, and reads at the full input rate: caching can be over-counted,
    not under-counted. On the Messages API caching is opt-in (`cache_control`), which this
    adapter never sends, so both fields are normally zero.
  * `usage.output_tokens` is the output, including any extended-thinking tokens.
  * Requests ask for the standard tier only (`service_tier="standard_only"`) and standard speed.
    A response reporting another tier or speed is charged at its full reservation. Region:
    with `inference_geo` set, it is sent and the response must report the same region; without
    it, the workspace default applies and the reported region is not checked (region pricing
    is not modelled).

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
    input_rate_ceiling = float(CACHE_WRITE_FACTOR)

    def __init__(
        self,
        *,
        max_calls: int,
        inference_geo: str | None = None,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        self.name = "anthropic"
        self.inference_geo = inference_geo
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
        if self.inference_geo:
            kwargs["inference_geo"] = self.inference_geo
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                messages=[{"role": "user", "content": prompt}],
                service_tier="standard_only",
                speed="standard",
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
        return ProviderResult(
            text=text, input_tokens=inp, output_tokens=out, unpriced_reason=self._unpriced(usage)
        )

    def _unpriced(self, usage: Any) -> str | None:
        tier = getattr(usage, "service_tier", None)
        if tier not in (None, "standard"):
            return f"served on the {tier!r} tier, which the price book does not price"
        speed = getattr(usage, "speed", None)
        if speed not in (None, "standard"):
            return f"served at {speed!r} speed, which the price book does not price"
        geo = getattr(usage, "inference_geo", None)
        if self.inference_geo and geo not in (None, self.inference_geo):
            return f"served in region {geo!r}, not the configured {self.inference_geo!r}"
        return None
