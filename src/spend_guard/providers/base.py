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
    input_tokens: int
    output_tokens: int  # everything billed at the output rate, including reasoning tokens


class Provider(Protocol):
    name: str

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult: ...
