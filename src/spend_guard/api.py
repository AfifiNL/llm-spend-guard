"""HTTP API. `/v1/complete` runs a call through the guard. Every error body is
`{"detail": {"code": <stable machine-readable code>, "message": <text for people>}}`; clients
branch on `code`, never on the message (see the README's table of codes).

Prices, budgets and keys are managed with the CLI only, so the network API cannot raise a limit
or mint a key.

Two ways in, both `Authorization: Bearer ...`:
  * an API key (`llm-spend-guard key-create <tenant>`): the key decides the tenant. A request
    that names another tenant is refused (403); the tenant may be left out of the body.
  * the shared token SPEND_GUARD_API_TOKEN, for one trusted caller: it names the tenant in each
    request and can use any of them. Unset it once every client has its own key.
A refused credential gets 401. Only when no shared token is configured and no active key exists
(checked at most every 10 seconds) does the API answer 503 instead: nothing could ever pass."""

from __future__ import annotations

import hmac
import os
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from spend_guard import keys
from spend_guard.errors import (
    BudgetExceeded,
    NoBudget,
    NoCheckedPrice,
    RequestIdConflict,
    RequestIdSpent,
    RequestInFlight,
)
from spend_guard.gateway import SpendGuard
from spend_guard.money import fmt
from spend_guard.providers.base import ProviderFailed, ProviderRejected


class CompleteIn(BaseModel):
    tenant: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$"
    )
    request_id: uuid.UUID | None = None
    model: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    system: str = Field(default="", max_length=20_000)
    max_output_tokens: int = Field(default=512, gt=0, le=4096)

    @field_validator("prompt", "system")
    @classmethod
    def _encodable(cls, v: str) -> str:
        try:
            v.encode()
        except UnicodeEncodeError:
            raise ValueError("text must be valid Unicode (no lone surrogates)") from None
        return v


RETRY_IN_FLIGHT_S = 5
KEY_CHECK_EVERY_S = 10.0


def error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message}, headers=headers)


@dataclass(frozen=True)
class Caller:
    tenant: str | None  # bound tenant (API key), or None for the shared token

    def resolve(self, requested: str | None) -> str:
        if self.tenant is None:
            if requested is None:
                raise error(422, "tenant_required", "tenant is required with the shared token")
            return requested
        if requested is not None and requested != self.tenant:
            raise error(403, "tenant_mismatch", "this API key belongs to another tenant")
        return self.tenant


def _keys_exist(request: Request) -> bool:
    """Whether any active key exists, cached so anonymous traffic does not query each time.
    Once true it stays true for the process (a refused credential gets 401 either way)."""
    st = request.app.state
    now = time.monotonic()
    if st.keys_exist or now - st.keys_checked_at < KEY_CHECK_EVERY_S:
        return bool(st.keys_exist)
    guard: SpendGuard = st.guard
    with guard.ledger.pool.connection() as conn:
        st.keys_exist = keys.active_keys(conn) > 0
    st.keys_checked_at = now
    return bool(st.keys_exist)


def _caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> Caller:
    guard: SpendGuard = request.app.state.guard
    api_token: str | None = request.app.state.api_token
    scheme, _, given = (authorization or "").partition(" ")
    if scheme == "Bearer" and given:
        if api_token and hmac.compare_digest(given.encode(), api_token.encode()):
            return Caller(tenant=None)
        with guard.ledger.pool.connection() as conn:
            tenant = keys.tenant_for(conn, given)
        if tenant is not None:
            request.app.state.keys_exist = True
            return Caller(tenant=tenant)
    if not api_token and not _keys_exist(request):
        raise error(503, "api_disabled", "no SPEND_GUARD_API_TOKEN and no active API key")
    raise error(401, "unauthorized", "invalid or missing credential")


CallerDep = Annotated[Caller, Depends(_caller)]


def create_app(guard: SpendGuard, *, api_token: str | None) -> FastAPI:
    app = FastAPI(title="llm-spend-guard", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the input back: it may be large, private or not even encodable.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        body = {"code": "invalid_request", "message": "the request is invalid", "errors": errors}
        return JSONResponse({"detail": body}, status_code=422)

    app.state.guard = guard
    app.state.api_token = api_token
    app.state.keys_exist = False
    app.state.keys_checked_at = float("-inf")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/complete")
    def complete(body: CompleteIn, caller: CallerDep) -> dict[str, Any]:
        tenant = caller.resolve(body.tenant)
        try:
            c = guard.complete(
                tenant,
                body.model,
                body.prompt,
                system=body.system,
                max_output_tokens=body.max_output_tokens,
                request_id=body.request_id,
            )
        except BudgetExceeded as exc:
            # No amounts: the global scope's spend is not every tenant's business.
            raise error(402, "budget_exceeded", f"daily budget reached for {exc.scope}") from None
        except NoCheckedPrice as exc:
            raise error(409, "no_checked_price", str(exc)) from None
        except NoBudget as exc:
            raise error(403, "no_budget", str(exc)) from None
        except RequestInFlight as exc:
            raise error(
                409, "request_in_flight", str(exc), {"Retry-After": str(RETRY_IN_FLIGHT_S)}
            ) from None
        except RequestIdSpent as exc:
            raise error(409, "request_id_spent", str(exc)) from None
        except RequestIdConflict as exc:
            raise error(409, "request_id_conflict", str(exc)) from None
        except ValueError as exc:  # an invalid tenant id the pattern let through ("*" is not)
            raise error(422, "invalid_tenant", str(exc)) from None
        except ProviderRejected as exc:
            raise error(502, "provider_rejected", str(exc)) from None
        except ProviderFailed as exc:
            raise error(502, "provider_failed", str(exc)) from None
        return {
            "request_id": str(body.request_id or c.request_id),
            "reservation_id": str(c.request_id),
            "replayed": c.replayed,
            "tenant": tenant,
            "text": c.text,
            "usage": {"input_tokens": c.input_tokens, "output_tokens": c.output_tokens},
            "reserved": fmt(c.reserved_micros),
            "cost": fmt(c.cost_micros),
            # What this HTTP call added to the ledger: zero for a replay. Sum this, not `cost`.
            "charged_now": fmt(0 if c.replayed else c.cost_micros),
        }

    @app.get("/v1/budgets/{tenant}")
    def budget(tenant: str, caller: CallerDep) -> list[dict[str, Any]]:
        caller.resolve(tenant)
        try:
            rows = guard.ledger.status(tenant)
        except NoBudget as exc:
            raise error(403, "no_budget", str(exc)) from None
        except ValueError as exc:
            raise error(422, "invalid_tenant", str(exc)) from None
        return [
            {
                "scope": s.scope,
                "limit": fmt(s.limit_micros),
                "reserved": fmt(s.reserved_micros),
                "settled": fmt(s.settled_micros),
                "remaining": fmt(s.remaining_micros),
            }
            for s in rows
        ]

    return app


def app_from_env() -> FastAPI:  # pragma: no cover - wiring only
    from spend_guard.factory import build_guard

    return create_app(build_guard(), api_token=os.environ.get("SPEND_GUARD_API_TOKEN") or None)
