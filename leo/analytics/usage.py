"""Collect actual API invocations within a worker stage; never count cache hits as spend.

A context variable scopes calls to the executing stage without modifying B2/B3/B4 interfaces.
The worker persists collected calls even when the business transaction rolls back.
No prices are guessed: configure per-million token rates in the environment, or cost is NULL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class UsageCall:
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    status: str


_calls: ContextVar[list[UsageCall] | None] = ContextVar("leo_b6_calls", default=None)


@contextmanager
def collect_usage() -> Iterator[list[UsageCall]]:
    items: list[UsageCall] = []
    token = _calls.set(items)
    try:
        yield items
    finally:
        _calls.reset(token)


def _token(obj: Any, field: str) -> int | None:
    value = obj.get(field) if isinstance(obj, dict) else getattr(obj, field, None)
    return int(value) if value is not None else None


def estimated_cost(provider: str, model: str, inp: int | None, out: int | None) -> Decimal | None:
    """Per million tokens, e.g. LEO_B6_CLAUDE_INPUT_PER_M=...; no implicit price list.

    Optional exact model override: LEO_B6_CLAUDE_<MODEL>_INPUT_PER_M, where the model
    slug is uppercase alphanumeric with other characters replaced by underscores.
    """
    import re

    suffix = re.sub(r"[^A-Z0-9]+", "_", model.upper()).strip("_")
    prefix = f"LEO_B6_{provider.upper()}"
    def rate(kind: str) -> Decimal | None:
        s = os.getenv(f"{prefix}_{suffix}_{kind}_PER_M") or os.getenv(f"{prefix}_{kind}_PER_M")
        try:
            r = Decimal(s) if s is not None else None
        except InvalidOperation:
            return None
        return r if r is not None and r >= 0 else None

    ir, orate = rate("INPUT"), rate("OUTPUT")
    # Jev's System One documentation states output tokens are currently free.
    if provider == "jev" and orate is None:
        orate = Decimal(0)
    if inp is None or out is None or ir is None or orate is None:
        return None
    return ((inp * ir + out * orate) / Decimal(1_000_000)).quantize(Decimal("0.00000001"))


def record_usage(
    provider: str, model: str, usage: Any = None, status: str = "success"
) -> None:
    items = _calls.get()
    if items is None:
        return  # direct SDK usage outside a worker is not attributed to a job
    inp = _token(usage, "input_tokens") if usage is not None else None
    out = _token(usage, "output_tokens") if usage is not None else None
    cost = estimated_cost(provider, model, inp, out)
    items.append(UsageCall(provider, model, inp, out, cost, status))
