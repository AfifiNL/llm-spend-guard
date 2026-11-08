"""The gateway: every model call goes reserve → call → settle, inside one trace span.

Span `llm.complete` carries the OpenTelemetry GenAI attributes (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) plus `spend.*` attributes: the tenant,
the reserved and the charged amount in micros, the price id and the outcome. Any OpenTelemetry
exporter can ship them; nothing here depends on a vendor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from spend_guard.errors import BudgetExceeded, NoBudget, NoCheckedPrice
from spend_guard.ledger import SpendLedger
from spend_guard.providers.base import Provider, ProviderFailed, ProviderRejected

# Upper bound on input tokens: a tokenizer that covers at least one byte per token cannot
# produce more tokens than the text has UTF-8 bytes. Chat formatting adds a few tokens per
# message, so a fixed allowance is added. The live probe in the README checks the bound.
FORMAT_ALLOWANCE_TOKENS = 64


def max_input_tokens(system: str, prompt: str) -> int:
    return len(system.encode()) + len(prompt.encode()) + FORMAT_ALLOWANCE_TOKENS


@dataclass(frozen=True)
class Completion:
    request_id: uuid.UUID
    text: str
    input_tokens: int
    output_tokens: int
    reserved_micros: int
    cost_micros: int


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

    def complete(
        self,
        tenant: str,
        model: str,
        prompt: str,
        *,
        system: str = "",
        max_output_tokens: int = 512,
    ) -> Completion:
        if not 0 < max_output_tokens <= self.max_output_tokens_cap:
            raise ValueError(f"max_output_tokens must be 1..{self.max_output_tokens_cap}")
        with self.tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("gen_ai.system", self.provider.name)
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.request.max_tokens", max_output_tokens)
            span.set_attribute("spend.tenant", tenant)
            try:
                res = self.ledger.reserve(
                    tenant,
                    model,
                    max_input_tokens=max_input_tokens(system, prompt),
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
            settled = self.ledger.settle(res.request_id, out.input_tokens, out.output_tokens)
            span.set_attribute("gen_ai.usage.input_tokens", out.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", out.output_tokens)
            span.set_attribute("spend.cost_micros", settled.cost_micros)
            span.set_attribute("spend.outcome", "overrun" if settled.overrun else "ok")
            return Completion(
                res.request_id,
                out.text,
                out.input_tokens,
                out.output_tokens,
                res.reserved_micros,
                settled.cost_micros,
            )
