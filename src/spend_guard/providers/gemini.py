"""Gemini adapter (optional extra `llm-spend-guard[gemini]`).

Authentication comes from the environment through the google-genai SDK: GEMINI_API_KEY, or
Vertex AI (SPEND_GUARD_GEMINI_VERTEX=true) with GOOGLE_CLOUD_PROJECT and Application Default
Credentials. This module never reads, logs or returns credentials.

Usage: `prompt_token_count` is the input; `candidates_token_count` plus `thoughts_token_count`
(reasoning tokens, billed at the output rate) is the output.

Spend guard of its own: a hard per-process call cap (SPEND_GUARD_MAX_LIVE_CALLS, default 0, so
no live call happens unless someone sets it on purpose).
"""

from __future__ import annotations

import threading
from typing import Any

from spend_guard.providers.base import ProviderFailed, ProviderRejected, ProviderResult

REJECTED_BEFORE_WORK = {400, 401, 403, 404, 429}


def _describe(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return type(exc).__name__ + (f" (HTTP {code})" if code else "")


class GeminiProvider:
    def __init__(
        self,
        *,
        max_calls: int,
        vertex: bool = False,
        location: str | None = None,
        thinking_level: str | None = None,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        self.name = "gemini"
        self.max_calls = max_calls
        self.calls = 0
        self.thinking_level = thinking_level.upper() if thinking_level else None
        self._lock = threading.Lock()
        if client is None:
            from google import genai
            from google.genai import types

            http = types.HttpOptions(timeout=int(timeout_s * 1000))
            client = (
                genai.Client(vertexai=True, location=location or None, http_options=http)
                if vertex
                else genai.Client(http_options=http)
            )
        self._client = client

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        from google.genai import types

        with self._lock:
            if self.calls >= self.max_calls:
                raise ProviderRejected(
                    f"live call cap reached ({self.calls}/{self.max_calls}); "
                    "set SPEND_GUARD_MAX_LIVE_CALLS deliberately to allow more"
                )
            self.calls += 1
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_output_tokens,
            thinking_config=(
                types.ThinkingConfig(thinking_level=types.ThinkingLevel(self.thinking_level))
                if self.thinking_level
                else None
            ),
        )
        try:
            resp = self._client.models.generate_content(model=model, contents=prompt, config=config)
        except Exception as exc:
            if getattr(exc, "code", None) in REJECTED_BEFORE_WORK:
                raise ProviderRejected(f"Gemini refused: {_describe(exc)}") from None
            raise ProviderFailed(f"Gemini call failed: {_describe(exc)}") from None
        usage = getattr(resp, "usage_metadata", None)
        if usage is None or usage.prompt_token_count is None:
            raise ProviderFailed("Gemini response has no usage metadata")
        output = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        return ProviderResult(
            text=resp.text or "",
            input_tokens=int(usage.prompt_token_count),
            output_tokens=int(output),
        )
