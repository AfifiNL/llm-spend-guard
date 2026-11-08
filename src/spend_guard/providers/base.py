from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderRejected(Exception):
    """The provider refused the request before doing any work (bad request, auth, quota,
    rate limit). Nothing was generated, so the reservation is released."""


class ProviderFailed(Exception):
    """The call failed in a way that may still be billed (timeout, server error after the
    request was sent, unreadable response). The reservation is charged in full."""


@dataclass(frozen=True)
class ProviderResult:
    text: str
    input_tokens: int  # input-rate-equivalent tokens (cache premiums already folded in)
    output_tokens: int  # everything billed at the output rate, including reasoning tokens
    # Set when the response shows it was served in a way the price book does not price (a
    # premium tier, another speed or region): the call is then charged at its full reservation.
    unpriced_reason: str | None = None


class Provider(Protocol):
    """`input_rate_ceiling` (optional attribute, default 1.0): the highest multiple of the input
    rate this provider can bill per input token (cache-write premiums). Reservations scale the
    input bound by it, so a premium the vendor adds on its own cannot exceed a reservation."""

    name: str

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult: ...
