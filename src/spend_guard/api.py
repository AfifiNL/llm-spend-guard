"""HTTP API. `/v1/complete` runs a call through the guard; refusals map to distinct status codes:
402 budget exceeded, 409 no checked price or a request_id conflict, 403 no budget configured or
a tenant the caller may not use, 502 provider error. Prices, budgets and keys are managed with
the CLI only, so the network API cannot raise a limit or mint a key.

Two ways in, both `Authorization: Bearer ...`:
  * an API key (`llm-spend-guard key-create <tenant>`): the key decides the tenant. A request
    that names another tenant is refused (403); the tenant may be left out of the body.
  * the shared token SPEND_GUARD_API_TOKEN, for one trusted caller: it names the tenant in each
    request and can use any of them. Unset it once every client has its own key.
Fail-closed: with no shared token and no active key, every request is refused (503)."""

from __future__ import annotations

import hmac
import os
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


@dataclass(frozen=True)
class Caller:
    tenant: str | None  # bound tenant (API key), or None for the shared token

    def resolve(self, requested: str | None) -> str:
        if self.tenant is None:
            if requested is None:
                raise HTTPException(422, "tenant is required with the shared token")
            return requested
        if requested is not None and requested != self.tenant:
            raise HTTPException(403, "this API key belongs to another tenant")
        return self.tenant


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
            return Caller(tenant=tenant)
    if not api_token:
        with guard.ledger.pool.connection() as conn:
            if keys.active_keys(conn) == 0:
                raise HTTPException(
                    503, "API disabled: no SPEND_GUARD_API_TOKEN and no active API key"
                )
    raise HTTPException(401, "invalid token")


CallerDep = Annotated[Caller, Depends(_caller)]


def create_app(guard: SpendGuard, *, api_token: str | None) -> FastAPI:
    app = FastAPI(title="llm-spend-guard", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the input back: it may be large, private or not even encodable.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    app.state.guard = guard
    app.state.api_token = api_token

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
            raise HTTPException(402, f"daily budget reached for {exc.scope}") from None
        except NoCheckedPrice as exc:
            raise HTTPException(409, str(exc)) from None
        except NoBudget as exc:
            raise HTTPException(403, str(exc)) from None
        except (RequestInFlight, RequestIdConflict) as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:  # an invalid tenant id the pattern let through ("*" is not)
            raise HTTPException(422, str(exc)) from None
        except (ProviderRejected, ProviderFailed) as exc:
            raise HTTPException(502, str(exc)) from None
        return {
            "request_id": str(body.request_id or c.request_id),
            "reservation_id": str(c.request_id),
            "replayed": c.replayed,
            "tenant": tenant,
            "text": c.text,
            "usage": {"input_tokens": c.input_tokens, "output_tokens": c.output_tokens},
            "reserved": fmt(c.reserved_micros),
            "cost": fmt(c.cost_micros),
        }

    @app.get("/v1/budgets/{tenant}")
    def budget(tenant: str, caller: CallerDep) -> list[dict[str, Any]]:
        caller.resolve(tenant)
        try:
            rows = guard.ledger.status(tenant)
        except NoBudget as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
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
