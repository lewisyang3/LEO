"""Which source items each number on a finding comes from (ROADMAP C2: "click any number").

`finding_numbers` lists the deadline, send-by date and every `impact` and `computation` field.
`SOURCES` maps a field to the linked items it is read from: a term type picks the finding's
terms of that type, and `INVOICE` picks its invoice facts. A field not in the table is derived
from all of them.
"""

import datetime
from decimal import Decimal

from leo.api.schemas import Number
from leo.interfaces import Ledger
from leo.types import Finding, FindingKind, TermType

INVOICE = "invoice_fact"
Spec = tuple[TermType | str, ...]

SOURCES: dict[FindingKind, dict[str, Spec]] = {
    FindingKind.auto_renewal_deadline: {
        "impact.amount": (TermType.price, TermType.renewal),
    },
    FindingKind.price_increase_notice: {
        "impact.monthly": (TermType.price,),
        "impact.annual": (TermType.price,),
        "impact.pct": (TermType.price,),
    },
    FindingKind.price_cap_exceeded: {
        "computation.prior_monthly": (TermType.price,),
        "computation.cap_pct": (TermType.price_cap,),
        "computation.charged_monthly": (INVOICE,),
    },
    FindingKind.seats_over_commitment: {
        "computation.contracted_seats": (TermType.seat_count,),
        "computation.billed_seats": (INVOICE,),
        "computation.price_per_seat_month": (TermType.price,),
    },
    FindingKind.escalator_applied_early: {
        "computation.contract_monthly": (TermType.price,),
        "computation.escalator_pct": (TermType.escalator,),
        "computation.correct_first_application": (TermType.escalator,),
        "computation.applied_from": (INVOICE,),
        "computation.charged_monthly": (INVOICE,),
    },
}


# The impact field equal to `impact.headline_amount` (leo/types.py), per kind.
HEADLINE: dict[FindingKind, str] = {
    FindingKind.auto_renewal_deadline: "impact.amount",
    FindingKind.price_increase_notice: "impact.annual",
    FindingKind.price_cap_exceeded: "impact.full_renewal_term",
    FindingKind.seats_over_commitment: "impact.annualized",
    FindingKind.escalator_applied_early: "impact.if_uncorrected_until_anniversary",
}


def _label(field: str) -> str:
    return field.replace("_pct", " %").replace("_", " ").capitalize()


def _unit(field: str, value: object) -> str:
    if isinstance(value, datetime.date):
        return "date"
    if "pct" in field:
        return "pct"
    if isinstance(value, int):
        return "count"
    return "usd"


def _resolve(spec: Spec, finding: Finding, term_types: dict[str, TermType]) -> list[str]:
    out: list[str] = []
    for part in spec:
        if part == INVOICE:
            out += finding.invoice_facts
        else:
            out += [t for t in finding.terms if term_types.get(t) == part]
    return out


def finding_numbers(finding: Finding, ledger: Ledger) -> list[Number]:
    term_types = {t.id: t.type for t in ledger.terms}
    everything = [*finding.terms, *finding.invoice_facts]
    table = SOURCES[finding.kind]
    numbers: list[Number] = []

    basis = finding.deadline_basis
    deadline_sources = (
        [*basis.terms, *([basis.invoice_fact] if basis.invoice_fact else [])] if basis else []
    )
    for key, label in (("deadline", "Deadline"), ("recommended_send_by", "Send by")):
        value = getattr(finding, key)
        if value is not None:
            numbers.append(
                Number(key=key, label=label, value=value, unit="date", sources=deadline_sources)
            )

    for group in ("impact", "computation"):
        model = getattr(finding, group, None)
        if model is None:
            continue
        for field, value in model.model_dump().items():
            if not isinstance(value, (Decimal, int, datetime.date)) or isinstance(value, bool):
                continue
            key = f"{group}.{field}"
            spec = table.get(key)
            sources = _resolve(spec, finding, term_types) if spec else everything
            numbers.append(
                Number(
                    key=key,
                    label=_label(field),
                    value=value,
                    unit=_unit(field, value),
                    sources=sources or everything,
                    headline=key == HEADLINE[finding.kind],
                )
            )
    return numbers
