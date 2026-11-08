"""The live probe: a fixed set of prompts through the full guard, to check the reservation
bound against real usage. Recorded once (`make record-live`); replayed offline in CI through a
real ledger (`make probe-replay`), so the settlement maths runs on real usage numbers for free."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spend_guard.gateway import SpendGuard
from spend_guard.money import fmt, parse
from spend_guard.prices import add_price

MODEL = "gemini-3.6-flash"
SYSTEM = "Answer briefly and plainly."
PROMPTS: list[tuple[str, str, int]] = [
    ("one word", "Reply with the single word: ready", 64),
    ("short fact", "In one sentence, what does a database transaction guarantee?", 128),
    ("list", "List three common causes of slow web pages, one line each.", 256),
    ("explain", "Explain reserve-then-settle budgeting for API calls in about 80 words.", 256),
    (
        "long ask, small cap",
        "Write a detailed 600-word essay on the history of the printing press.",
        128,
    ),
    ("code", "Write a Python function that returns the n-th Fibonacci number iteratively.", 384),
    ("non-English", "Beschrijf in twee zinnen wat een budget is.", 128),
    ("arabic", "اشرح في جملتين ما هي الميزانية.", 128),
    ("json", 'Return JSON {"ok": true, "items": [1, 2, 3]} and nothing else.', 64),
    (
        "reasoning",
        "A train leaves at 09:40 and arrives at 13:05. How long is the trip? Show steps.",
        256,
    ),
]


@dataclass(frozen=True)
class ProbeRow:
    label: str
    max_output_tokens: int
    reserved_micros: int
    cost_micros: int
    input_tokens: int
    output_tokens: int
    input_bound: int


def seed_prices(conn: Any, path: Path) -> None:
    for p in json.loads(path.read_text(encoding="utf-8")):
        add_price(
            conn,
            p["model"],
            parse(p["input_per_mtok"]),
            parse(p["output_per_mtok"]),
            checked_at=dt.datetime.fromisoformat(p["checked_at"].replace("Z", "+00:00")),
            source=p["source"],
            currency=p["currency"],
        )


def run_probe(guard: SpendGuard, tenant: str = "probe") -> list[ProbeRow]:
    from spend_guard.gateway import max_input_tokens

    rows = []
    for label, prompt, cap in PROMPTS:
        c = guard.complete(tenant, MODEL, prompt, system=SYSTEM, max_output_tokens=cap)
        rows.append(
            ProbeRow(
                label,
                cap,
                c.reserved_micros,
                c.cost_micros,
                c.input_tokens,
                c.output_tokens,
                max_input_tokens(SYSTEM, prompt),
            )
        )
    return rows


def table(rows: list[ProbeRow]) -> str:
    out = [
        "| prompt | max out | input tokens (bound) | output tokens | reserved | charged |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        flag = " **over**" if r.output_tokens > r.max_output_tokens else ""
        out.append(
            f"| {r.label} | {r.max_output_tokens} | {r.input_tokens} ({r.input_bound}) | "
            f"{r.output_tokens}{flag} | {fmt(r.reserved_micros)} | {fmt(r.cost_micros)} |"
        )
    total_r = sum(r.reserved_micros for r in rows)
    total_c = sum(r.cost_micros for r in rows)
    out.append(f"| **total** | | | | {fmt(total_r)} | {fmt(total_c)} |")
    return "\n".join(out)
