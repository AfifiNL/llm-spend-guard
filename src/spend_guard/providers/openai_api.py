"""OpenAI adapter (optional extra `llm-spend-guard[openai]`). NEVER called against the real API
during development: it is tested only with fake clients (tests/test_vendor_providers.py).

Usage mapping (OpenAI's own definitions):
  * Chat Completions: `usage.prompt_tokens` is the input; `usage.completion_tokens` is the
    output and already includes reasoning tokens (`completion_tokens_details.reasoning_tokens`
    is a breakdown, not an addition).
  * Responses API: `usage.input_tokens` / `usage.output_tokens`, the latter including reasoning.
  * Cached input (`*_tokens_details.cached_tokens`) is billed by OpenAI at a discount; the guard
    charges it at the full input rate, so it over-counts cached prompts and never under-counts.

The SDK's own retries are switched off (`max_retries=0`): one call here is one HTTP request, so
the call cap and the worst-case charge for a failed call stay meaningful.
"""

from __future__ import annotations

from typing import Any, Literal

from spend_guard.providers._common import CallCap, classify
from spend_guard.providers.base import ProviderFailed, ProviderResult

# The API rejected the request itself: nothing was generated.
REJECTED_BEFORE_WORK = frozenset({400, 401, 403, 404, 409, 413, 422, 429})


class OpenAIProvider:
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
                    model=model, messages=messages, max_completion_tokens=max_output_tokens
                )
            else:
                kwargs: dict[str, Any] = {"instructions": system} if system else {}
                resp = self._client.responses.create(
                    model=model, input=prompt, max_output_tokens=max_output_tokens, **kwargs
                )
        except Exception as exc:
            raise classify(exc, "OpenAI", REJECTED_BEFORE_WORK) from None
        usage = getattr(resp, "usage", None)
        if self.api == "chat":
            inp = getattr(usage, "prompt_tokens", None)
            out = getattr(usage, "completion_tokens", None)
            choices = getattr(resp, "choices", None) or []
            text = (choices[0].message.content or "") if choices else ""
        else:
            inp = getattr(usage, "input_tokens", None)
            out = getattr(usage, "output_tokens", None)
            text = getattr(resp, "output_text", "") or ""
        if not isinstance(inp, int) or not isinstance(out, int):
            raise ProviderFailed("OpenAI response has no usage")
        return ProviderResult(text=text, input_tokens=inp, output_tokens=out)
