"""OpenAI adapter (optional extra `llm-spend-guard[openai]`). NEVER called against the real API
during development: it is tested only with fake clients and the SDK's own exception classes
(tests/test_vendor_providers.py).

Usage mapping:
  * Chat Completions: `usage.prompt_tokens` is the input; `usage.completion_tokens` the output,
    which already includes reasoning tokens (`completion_tokens_details.reasoning_tokens` is a
    breakdown, not an addition). Responses API: `usage.input_tokens` / `usage.output_tokens`.
  * Prompt caching is automatic at OpenAI. Cache WRITES (`*_tokens_details.cache_write_tokens`)
    are billed above the input rate: OpenAI's prompt-caching guide (read 2026-11-08) gives 1.25x
    for the default lifetime and 2.5x for the 12-hour lifetime on newer models, and no extra
    charge on older ones. The adapter charges every cache-write token at 2.5x, the highest of
    those, and reads `cache_write_tokens` as part of `prompt_tokens` (it sits in the same
    breakdown object as `cached_tokens`, which OpenAI documents as included; the SDK docstring
    does not say so explicitly). Cache READS (`cached_tokens`) are billed at a discount and
    charged here at the full rate.
  * Requests ask for the standard tier (`service_tier="default"`) and are not stored by OpenAI
    (`store=False`). A response that reports another tier is charged at its full reservation:
    the price book has no price for it.

The SDK's own retries are switched off (`max_retries=0`): one call here is one HTTP request.
"""

from __future__ import annotations

from typing import Any, Literal

from spend_guard.providers._common import CallCap, classify
from spend_guard.providers.base import ProviderFailed, ProviderResult

# The API rejected the request itself: nothing was generated.
REJECTED_BEFORE_WORK = frozenset({400, 401, 403, 404, 409, 413, 422, 429})
CACHE_WRITE_FACTOR_TENTHS = 25  # 2.5x the input rate
STANDARD_TIER = "default"


def input_equivalent(prompt_tokens: int, details: Any) -> int:
    """prompt tokens at the input rate, plus the cache-write premium (1.5x extra)."""
    writes = getattr(details, "cache_write_tokens", None) or 0
    premium = int(writes) * (CACHE_WRITE_FACTOR_TENTHS - 10)
    return prompt_tokens + -(-premium // 10)


class OpenAIProvider:
    input_rate_ceiling = CACHE_WRITE_FACTOR_TENTHS / 10

    def __init__(
        self,
        *,
        max_calls: int,
        api: Literal["chat", "responses"] = "chat",
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        if api not in ("chat", "responses"):
            raise ValueError("api must be 'chat' or 'responses'")
        self.name = "openai"
        self.api = api
        self.cap = CallCap(max_calls, "SPEND_GUARD_MAX_LIVE_CALLS")
        if client is None:
            from openai import OpenAI

            client = OpenAI(timeout=timeout_s, max_retries=0)
        self._client = client

    @property
    def calls(self) -> int:
        return self.cap.used

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        self.cap.take()
        try:
            if self.api == "chat":
                messages = ([{"role": "system", "content": system}] if system else []) + [
                    {"role": "user", "content": prompt}
                ]
                resp = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_completion_tokens=max_output_tokens,
                    service_tier=STANDARD_TIER,
                    store=False,
                )
            else:
                kwargs: dict[str, Any] = {"instructions": system} if system else {}
                resp = self._client.responses.create(
                    model=model,
                    input=prompt,
                    max_output_tokens=max_output_tokens,
                    service_tier=STANDARD_TIER,
                    store=False,
                    **kwargs,
                )
        except Exception as exc:
            raise classify(exc, "OpenAI", REJECTED_BEFORE_WORK) from None
        usage = getattr(resp, "usage", None)
        if self.api == "chat":
            inp = getattr(usage, "prompt_tokens", None)
            details = getattr(usage, "prompt_tokens_details", None)
            out = getattr(usage, "completion_tokens", None)
            choices = getattr(resp, "choices", None) or []
            text = (choices[0].message.content or "") if choices else ""
        else:
            inp = getattr(usage, "input_tokens", None)
            details = getattr(usage, "input_tokens_details", None)
            out = getattr(usage, "output_tokens", None)
            text = getattr(resp, "output_text", "") or ""
        if not isinstance(inp, int) or not isinstance(out, int):
            raise ProviderFailed("OpenAI response has no usage")
        tier = getattr(resp, "service_tier", None)
        unpriced = (
            f"served on the {tier!r} tier, which the price book does not price"
            if tier not in (None, STANDARD_TIER)
            else None
        )
        return ProviderResult(
            text=text,
            input_tokens=input_equivalent(inp, details),
            output_tokens=out,
            unpriced_reason=unpriced,
        )
