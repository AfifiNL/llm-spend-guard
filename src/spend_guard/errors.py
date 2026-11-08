"""Every refusal has its own type, so callers (and the HTTP API) can tell them apart."""

from __future__ import annotations


class SpendGuardError(Exception):
    """Base class."""


class NoCheckedPrice(SpendGuardError):
    """The model has no price in the price book, or its price was checked too long ago."""


class NoBudget(SpendGuardError):
    """No budget is configured for a scope the call needs (fail closed)."""


class BudgetExceeded(SpendGuardError):
    """The worst-case cost of this call would take a scope past its daily limit."""

    def __init__(self, scope: str, spent: int, requested: int, limit: int) -> None:
        from spend_guard.money import fmt

        super().__init__(
            f"{scope}: {fmt(spent)} spent or reserved + {fmt(requested)} worst case for this"
            f" call > {fmt(limit)} daily limit"
        )
        self.scope = scope
        self.spent = spent
        self.requested = requested
        self.limit = limit


class UnknownReservation(SpendGuardError):
    """Settle or release of a reservation that does not exist or is already closed."""


class RequestInFlight(SpendGuardError):
    """A request with the same client request_id is still running."""


class RequestIdConflict(SpendGuardError):
    """The tenant already used this client request_id for a different request."""


class RequestIdSpent(SpendGuardError):
    """The request with this id ended without a result that can be replayed (a failure that
    may have been billed, or an abandoned request). Retry with a new request_id."""
