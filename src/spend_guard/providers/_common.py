"""Shared by the vendor adapters: a hard per-process call cap and the error classification that
decides whether a failed call is released (nothing billed) or charged at its worst case."""

from __future__ import annotations

import threading

from spend_guard.providers.base import ProviderFailed, ProviderRejected


class CallCap:
    """At most `limit` live calls per process. 0 (the default everywhere) means none at all."""

    def __init__(self, limit: int, env_name: str) -> None:
        self.limit = limit
        self.used = 0
        self.env_name = env_name
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise ProviderRejected(
                    f"live call cap reached ({self.used}/{self.limit}); "
                    f"set {self.env_name} deliberately to allow more"
                )
            self.used += 1


def classify(exc: Exception, vendor: str, rejected_before_work: frozenset[int]) -> Exception:
    """Map a vendor SDK exception to ProviderRejected (the vendor refused the request before
    generating anything: release the reservation) or ProviderFailed (it may have generated and
    billed: charge the worst case). Only the type name and HTTP status are kept, because some
    vendor messages echo request content or key fragments."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    label = type(exc).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "")
    if isinstance(status, int) and status in rejected_before_work:
        return ProviderRejected(f"{vendor} refused: {label}")
    # No status (connection reset, timeout) or a server error: the request may have been
    # processed. Unknown usage is charged at the worst case.
    return ProviderFailed(f"{vendor} call failed: {label}")
