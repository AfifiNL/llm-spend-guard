"""Builds the guard from environment variables.

SPEND_GUARD_DATABASE_URL     PostgreSQL (without it, a throwaway local server via pgserver)
SPEND_GUARD_PROVIDER         mock (default) | gemini | replay
SPEND_GUARD_MAX_LIVE_CALLS   gemini: hard cap on live calls per process (default 0)
SPEND_GUARD_GEMINI_VERTEX    true: Vertex AI (ADC + GOOGLE_CLOUD_PROJECT); else GEMINI_API_KEY
SPEND_GUARD_GEMINI_LOCATION, SPEND_GUARD_GEMINI_THINKING_LEVEL
SPEND_GUARD_CASSETTE         replay: the recorded calls
SPEND_GUARD_MAX_PRICE_AGE_DAYS  prices older than this are treated as missing (default 31)
SPEND_GUARD_OTEL_CONSOLE     true: print spans to stdout (any OTel exporter can be wired instead)
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from spend_guard.db import make_pool
from spend_guard.gateway import SpendGuard
from spend_guard.ledger import SpendLedger
from spend_guard.providers.base import Provider
from spend_guard.providers.mock import MockProvider
from spend_guard.providers.replay import ReplayProvider

CASSETTE = "evals/fixtures/live-probe.jsonl"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_provider() -> Provider:
    kind = _env("SPEND_GUARD_PROVIDER", "mock")
    if kind == "mock":
        return MockProvider()
    if kind == "replay":
        return ReplayProvider(Path(_env("SPEND_GUARD_CASSETTE", CASSETTE)))
    if kind == "gemini":
        from spend_guard.providers.gemini import GeminiProvider

        return GeminiProvider(
            max_calls=int(_env("SPEND_GUARD_MAX_LIVE_CALLS", "0") or 0),
            vertex=_env("SPEND_GUARD_GEMINI_VERTEX").lower() in {"1", "true", "yes"},
            location=_env("SPEND_GUARD_GEMINI_LOCATION") or None,
            thinking_level=_env("SPEND_GUARD_GEMINI_THINKING_LEVEL") or None,
        )
    raise ValueError(f"unknown SPEND_GUARD_PROVIDER {kind!r}")


def setup_tracing() -> None:
    if _env("SPEND_GUARD_OTEL_CONSOLE").lower() in {"1", "true", "yes"}:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

        tp = TracerProvider()
        tp.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(tp)


def build_guard(provider: Provider | None = None) -> SpendGuard:
    setup_tracing()
    days = int(_env("SPEND_GUARD_MAX_PRICE_AGE_DAYS", "31") or 31)
    ledger = SpendLedger(make_pool(), max_price_age=dt.timedelta(days=days))
    return SpendGuard(ledger, provider or build_provider())
