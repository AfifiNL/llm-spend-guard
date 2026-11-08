"""Money is integer micro-units of the price book's currency (1 USD = 1_000_000 micros), and
prices are per million tokens. Costs round UP to the next micro, so rounding never favours the
caller."""

from __future__ import annotations

import re

MICROS = 1_000_000


def cost_micros(input_tokens: int, output_tokens: int, in_per_mtok: int, out_per_mtok: int) -> int:
    if min(input_tokens, output_tokens, in_per_mtok, out_per_mtok) < 0:
        raise ValueError("tokens and prices must be non-negative")
    numerator = input_tokens * in_per_mtok + output_tokens * out_per_mtok
    return -(-numerator // MICROS)


def fmt(micros: int) -> str:
    """6 decimal places, no float rounding."""
    sign = "-" if micros < 0 else ""
    whole, frac = divmod(abs(micros), MICROS)
    return f"{sign}{whole}.{frac:06d}"


def parse(amount: str) -> int:
    """'1.5' -> 1_500_000. Rejects more than 6 decimals instead of rounding silently."""
    s = amount.strip()
    m = re.fullmatch(r"([+-]?)([0-9]*)(?:\.([0-9]*))?", s, re.ASCII)
    if not m or not (m.group(2) or m.group(3)):
        raise ValueError(f"not an amount: {amount!r}")
    neg = m.group(1) == "-"
    whole, frac = m.group(2), m.group(3) or ""
    if len(frac) > 6:
        raise ValueError(f"at most 6 decimals: {amount!r}")
    value = int(whole or "0") * MICROS + int(frac.ljust(6, "0") or "0")
    return -value if neg else value
