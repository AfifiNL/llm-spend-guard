"""The gateway: every model call goes reserve → call → settle, inside one trace span.

Span `llm.complete` carries the OpenTelemetry GenAI attributes (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) plus `spend.*` attributes: the tenant,
the reserved and the charged amount in micros, the price id and the outcome. Any OpenTelemetry
exporter can ship them; nothing here depends on a vendor.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from spend_guard.errors import BudgetExceeded, NoBudget, NoCheckedPrice
from spend_guard.idempotency import IdempotencyStore, fingerprint
from spend_guard.ledger import SpendLedger, tenant_scope
from spend_guard.providers.base import Provider, ProviderFailed, ProviderRejected

log = logging.getLogger("spend_guard")

# Upper bound on input tokens: a tokenizer that covers at least one byte per token cannot
# produce more tokens than the text has UTF-8 bytes. Chat formatting adds a few tokens per
# message, so a fixed allowance is added. The live probe in the README checks the bound.
FORMAT_ALLOWANCE_TOKENS = 64


def max_input_tokens(system: str, prompt: str) -> int:
    return len(system.encode()) + len(prompt.encode()) + FORMAT_ALLOWANCE_TOKENS


@dataclass(frozen=True)
class Completion:
    request_id: uuid.UUID  # the reservation in the ledger
    text: str
    input_tokens: int
    output_tokens: int
    reserved_micros: int
    cost_micros: int
    replayed: bool = False  # True: an earlier result for the same client request_id

    def to_record(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.request_id),
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reserved_micros": self.reserved_micros,
            "cost_micros": self.cost_micros,
        }

    @classmethod
    def from_record(cls, r: dict[str, Any]) -> Completion:
        return cls(
            uuid.UUID(r["reservation_id"]),
            str(r["text"]),
            int(r["input_tokens"]),
            int(r["output_tokens"]),
            int(r["reserved_micros"]),
            int(r["cost_micros"]),
            replayed=True,
        )


class SpendGuard:
    def __init__(
        self,
        ledger: SpendLedger,
        provider: Provider,
        *,
        tracer: trace.Tracer | None = None,
        max_output_tokens_cap: int = 4096,
    ) -> None:
        self.ledger = ledger
        self.provider = provider
        self.tracer = tracer or trace.get_tracer("spend_guard")
        self.max_output_tokens_cap = max_output_tokens_cap
        self.idempotency = IdempotencyStore(ledger.pool, clock=ledger.clock)

    def complete(
        self,
        tenant: str,
        model: str,
        prompt: str,
        *,
        system: str = "",
        max_output_tokens: int = 512,
        request_id: uuid.UUID | None = None,
    ) -> Completion:
        """With a client `request_id`, the request is charged at most once: a repeat returns
        the stored result (`replayed=True`), a concurrent repeat raises RequestInFlight."""
        if not 0 < max_output_tokens <= self.max_output_tokens_cap:
            raise ValueError(f"max_output_tokens must be 1..{self.max_output_tokens_cap}")
        tenant_scope(tenant)  # invalid ids fail here, before a request id is claimed
        if request_id is None:
            return self._complete(tenant, model, prompt, system, max_output_tokens)
        fp = fingerprint(model, system, prompt, max_output_tokens)
        prior = self.idempotency.begin(tenant, request_id, fp)
        if prior is not None:
            with self.tracer.start_as_current_span("llm.complete") as span:
                span.set_attribute("gen_ai.request.model", model)
                span.set_attribute("spend.tenant", tenant)
                span.set_attribute("spend.client_request_id", str(request_id))
                span.set_attribute("spend.outcome", "replayed")
                span.set_attribute("spend.cost_micros", 0)
            return Completion.from_record(prior)
        try:
            result = self._complete(tenant, model, prompt, system, max_output_tokens)
        except (NoCheckedPrice, NoBudget, BudgetExceeded, ProviderRejected):
            self.idempotency.release(tenant, request_id)  # nothing charged: the id may be reused
            raise
        except BaseException as exc:
            self.idempotency.fail(tenant, request_id, type(exc).__name__)
            raise
        try:
            self.idempotency.complete(tenant, request_id, result.request_id, result.to_record())
        except Exception:
            # The call happened and was settled; only its replay record is missing. Return the
            # result anyway. The key stays in flight and is swept to "failed" later, so a retry
            # is refused rather than charged again.
            log.exception("could not store the result for request %s", request_id)
        return result

    def _complete(
        self, tenant: str, model: str, prompt: str, system: str, max_output_tokens: int
    ) -> Completion:
        with self.tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("gen_ai.system", self.provider.name)
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.request.max_tokens", max_output_tokens)
            span.set_attribute("spend.tenant", tenant)
            ceiling = float(getattr(self.provider, "input_rate_ceiling", 1.0))
            try:
                res = self.ledger.reserve(
                    tenant,
                    model,
                    max_input_tokens=math.ceil(max_input_tokens(system, prompt) * ceiling),
                    max_output_tokens=max_output_tokens,
                )
            except (NoCheckedPrice, NoBudget, BudgetExceeded) as exc:
                outcome = {
                    NoCheckedPrice: "refused_no_price",
                    NoBudget: "refused_no_budget",
                    BudgetExceeded: "refused_budget",
                }[type(exc)]
                span.set_attribute("spend.outcome", outcome)
                span.set_status(Status(StatusCode.ERROR, outcome))
                raise
            span.set_attribute("spend.request_id", str(res.request_id))
            span.set_attribute("spend.reserved_micros", res.reserved_micros)
            span.set_attribute("spend.price_id", res.price.id)
            try:
                out = self.provider.complete(model, system, prompt, max_output_tokens)
            except ProviderRejected as exc:
                self.ledger.release(res.request_id, note=str(exc)[:200])
                span.set_attribute("spend.outcome", "provider_rejected")
                span.set_attribute("spend.cost_micros", 0)
                span.set_status(Status(StatusCode.ERROR, "provider_rejected"))
                raise
            except Exception as exc:
                # Unknown usage: the call may have been billed. Charge the worst case.
                reason = str(exc)[:200] if isinstance(exc, ProviderFailed) else type(exc).__name__
                s = self.ledger.settle_worst_case(res.request_id, note=reason)
                span.set_attribute("spend.outcome", "provider_failed")
                span.set_attribute("spend.cost_micros", s.cost_micros)
                span.set_status(Status(StatusCode.ERROR, "provider_failed"))
                raise
            span.set_attribute("gen_ai.usage.input_tokens", out.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", out.output_tokens)
            if out.unpriced_reason:
                # Served in a way the price book has no price for: charge the full reservation.
                settled = self.ledger.settle_worst_case(res.request_id, note=out.unpriced_reason)
                span.set_attribute("spend.outcome", "unknown_cost")
            else:
                settled = self.ledger.settle(res.request_id, out.input_tokens, out.output_tokens)
                span.set_attribute("spend.outcome", "overrun" if settled.overrun else "ok")
            span.set_attribute("spend.cost_micros", settled.cost_micros)
            return Completion(
                res.request_id,
                out.text,
                out.input_tokens,
                out.output_tokens,
                res.reserved_micros,
                settled.cost_micros,
            )
