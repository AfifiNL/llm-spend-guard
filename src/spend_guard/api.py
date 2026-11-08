"""HTTP API. `/v1/complete` runs a call through the guard; refusals map to distinct status codes:
402 budget exceeded, 409 no checked price, 403 no budget configured, 502 provider error.
Prices and budgets are managed with the CLI only, so the network API cannot raise a limit.
Fail-closed: without SPEND_GUARD_API_TOKEN every request is refused.

The tenant is whatever the caller sends: anyone holding the token can spend any tenant's budget,
and a new tenant id gets the default tenant budget. The global budget is the hard ceiling. Tie
tenants to credentials in front of this API before exposing it to more than one client."""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from spend_guard.errors import BudgetExceeded, NoBudget, NoCheckedPrice
from spend_guard.gateway import SpendGuard
from spend_guard.money import fmt
from spend_guard.providers.base import ProviderFailed, ProviderRejected


class CompleteIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
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


def create_app(guard: SpendGuard, *, api_token: str | None) -> FastAPI:
    app = FastAPI(title="llm-spend-guard", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the input back: it may be large, private or not even encodable.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not api_token:
            raise HTTPException(503, "API disabled: SPEND_GUARD_API_TOKEN is not set")
        scheme, _, given = (authorization or "").partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(given.encode(), api_token.encode()):
            raise HTTPException(401, "invalid token")

    Auth = Depends(auth)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/complete", dependencies=[Auth])
    def complete(body: CompleteIn) -> dict[str, Any]:
        try:
            c = guard.complete(
                body.tenant,
                body.model,
                body.prompt,
                system=body.system,
                max_output_tokens=body.max_output_tokens,
            )
        except BudgetExceeded as exc:
            # No amounts: the global scope's spend is not every tenant's business.
            raise HTTPException(402, f"daily budget reached for {exc.scope}") from None
        except NoCheckedPrice as exc:
            raise HTTPException(409, str(exc)) from None
        except NoBudget as exc:
            raise HTTPException(403, str(exc)) from None
        except (ProviderRejected, ProviderFailed) as exc:
            raise HTTPException(502, str(exc)) from None
        return {
            "request_id": str(c.request_id),
            "text": c.text,
            "usage": {"input_tokens": c.input_tokens, "output_tokens": c.output_tokens},
            "reserved": fmt(c.reserved_micros),
            "cost": fmt(c.cost_micros),
        }

    @app.get("/v1/budgets/{tenant}", dependencies=[Auth])
    def budget(tenant: str) -> list[dict[str, Any]]:
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
