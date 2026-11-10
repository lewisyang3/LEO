"""Stage 4: the validator (ROADMAP B4).

`RuleValidator` runs deterministic checks on an extraction:
- the vendor name is present and appears in the document (`vendor_ok`)
- every quote appears in the document exactly (`leo.rules.locate_quote`, as A2's offsets do)
- dates are in range and billing periods are well formed
- amounts, percentages, day counts and seat counts are plausible
- no term conflicts with one of the vendor's prior terms

Prior terms are compared by `RulePriorComparer`, always, and by any extra `PriorComparer`
(`JevPriorComparer`). An item with an issue gets status `needs_review`. Items are never dropped.
When `vendor_ok` is False, every term and invoice fact gets `needs_review` (fail closed).

A prior term is comparable with a new term only when it comes from a different document and
has the same type, plan label, agreement ref and effective date (and, for notice and dispute
windows, the same `applies_to`). A different effective date is a change over time, which the
reconciler handles as supersession (C1), so it is never a conflict here.
"""

import datetime
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel
from typesafe_sdk import Choice, TypeSafeError

from leo.interfaces import Clock, DocumentText, Extraction, Validated, ValidationIssue
from leo.rules import locate_quote
from leo.types import (
    DisputeWindowTerm,
    ExtractedTerm,
    InvoiceFact,
    NoticeWindowTerm,
    TermStatus,
)

log = logging.getLogger(__name__)

ENGINE = "rules"

QUOTE_NOT_FOUND = "quote_not_found"
DATE_UNPARSED = "date_unparsed"
DATE_IMPLAUSIBLE = "date_implausible"
AMOUNT_IMPLAUSIBLE = "amount_implausible"
CONFLICTS_PRIOR = "conflicts_prior"
VENDOR_MISSING = "vendor_missing"
VENDOR_NOT_IN_SOURCE = "vendor_not_in_source"

MIN_DATE = datetime.date(2000, 1, 1)
MAX_DATE = datetime.date(2100, 12, 31)
MAX_MONEY = Decimal(10_000_000)
MAX_PCT = Decimal(100)
MAX_DAYS = 366
MAX_SEATS = 100_000
MAX_RENEWAL_MONTHS = 120

# Value fields ignored when comparing with a prior term: free text, whose wording can differ
# between a document and its duplicate, and fields derived from other fields or from today.
UNCOMPARED_FIELDS = frozenset(
    {
        "renewal_pricing",
        "early_termination_fee",
        "increase_requires",
        "baseline",
        "schedule",
        "deliver_to",
        "next_deadline",
        "current_term_end",
    }
)

_MONTH = re.compile(r"^\d{4}-\d{2}$")
_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _issue(item: str, check: str, message: str) -> ValidationIssue:
    return ValidationIssue(item=item, check=check, message=message)


# --- Vendor and quotes -----------------------------------------------------------------------


def check_vendor(text: str, vendor_name: str | None) -> list[ValidationIssue]:
    """A blank name counts as missing: "" and " " are in almost any text. The name is matched
    stripped, so the worker should store it stripped too (2.1)."""
    name = (vendor_name or "").strip()
    if not name:
        return [_issue("", VENDOR_MISSING, "the extraction has no vendor name")]
    if name.casefold() not in text.casefold():
        return [
            _issue("", VENDOR_NOT_IN_SOURCE, f"vendor name {vendor_name!r} is not in the document")
        ]
    return []


def check_quotes(text: str, extraction: Extraction) -> list[ValidationIssue]:
    issues = []
    for item in (*extraction.terms, *extraction.invoice_facts):
        for i, q in enumerate(item.quotes):
            if locate_quote(text, q) is None:
                issues.append(
                    _issue(item.id, QUOTE_NOT_FOUND, f"quote {i} is not in the document: {q!r}")
                )
    for c in extraction.contacts:
        if locate_quote(text, c.quote) is None:
            issues.append(
                _issue(
                    "",
                    QUOTE_NOT_FOUND,
                    f"contact {c.email} ({c.purpose}): quote is not in the document: {c.quote!r}",
                )
            )
    return issues


# --- Dates -----------------------------------------------------------------------------------


def _dates(item: ExtractedTerm | InvoiceFact) -> list[tuple[str, datetime.date]]:
    if isinstance(item, InvoiceFact):
        return [("invoice_date", item.invoice_date)]
    out = [("effective_date", item.effective_date)]
    out += [(f"value.{k}", v) for k, v in item.value if isinstance(v, datetime.date)]
    return [(k, v) for k, v in out if v is not None]


def _billing_period_ok(period: str) -> bool:
    if _MONTH.match(period):
        try:
            datetime.date.fromisoformat(period + "-01")
        except ValueError:
            return False
        return True
    if m := _RANGE.match(period):
        try:
            start, end = (datetime.date.fromisoformat(x) for x in m.groups())
        except ValueError:
            return False
        return start <= end
    return False


def check_dates(item: ExtractedTerm | InvoiceFact, today: datetime.date) -> list[ValidationIssue]:
    issues = []
    for name, d in _dates(item):
        if not MIN_DATE <= d <= MAX_DATE:
            issues.append(_issue(item.id, DATE_IMPLAUSIBLE, f"{name} {d} is out of range"))
    if isinstance(item, InvoiceFact):
        if item.invoice_date > today:
            issues.append(
                _issue(
                    item.id, DATE_IMPLAUSIBLE, f"invoice_date {item.invoice_date} is after today"
                )
            )
        if not _billing_period_ok(item.billing_period):
            issues.append(
                _issue(
                    item.id,
                    DATE_UNPARSED,
                    f"billing_period {item.billing_period!r} is not YYYY-MM or a start/end range",
                )
            )
    return issues


# --- Amounts ---------------------------------------------------------------------------------


def _range_issue(item: str, name: str, v: Any, lo: Any, hi: Any, lo_open: bool) -> list:
    if v is None:
        return []
    ok = (lo < v if lo_open else lo <= v) and v <= hi
    if ok:
        return []
    bound = f"({lo}, {hi}]" if lo_open else f"[{lo}, {hi}]"
    return [_issue(item, AMOUNT_IMPLAUSIBLE, f"{name} {v} is outside {bound}")]


def check_amounts(item: ExtractedTerm | InvoiceFact) -> list[ValidationIssue]:
    i = item.id
    if isinstance(item, InvoiceFact):
        return _range_issue(i, "amount", item.amount, 0, MAX_MONEY, False) + _range_issue(
            i, "seats_billed", item.seats_billed, 1, MAX_SEATS, False
        )
    issues = []
    values: list[tuple[str, BaseModel]] = [("value", item.value)]
    if item.old_value is not None:
        values.append(("old_value", item.old_value))
    for prefix, value in values:
        for k, v in value:
            name = f"{prefix}.{k}"
            match k:
                case "amount" | "overage_per_unit_month":
                    issues += _range_issue(i, name, v, 0, MAX_MONEY, False)
                case "max_increase_pct" | "pct":
                    issues += _range_issue(i, name, v, 0, MAX_PCT, False)
                case "days":
                    issues += _range_issue(i, name, v, 1, MAX_DAYS, False)
                case "included":
                    issues += _range_issue(i, name, v, 1, MAX_SEATS, False)
                case "renewal_length_months":
                    issues += _range_issue(i, name, v, 1, MAX_RENEWAL_MONTHS, False)
                case "currency" if v is not None and not _CURRENCY.match(v):
                    issues.append(_issue(i, AMOUNT_IMPLAUSIBLE, f"{name} {v!r} is not a code"))
    return issues


# --- Prior terms -----------------------------------------------------------------------------


def _fold(s: str | None) -> str | None:
    return None if s is None else s.strip().casefold()


def comparable(term: ExtractedTerm, prior: ExtractedTerm) -> bool:
    """Whether `prior` states the same thing as `term` for the same period (module docstring)."""
    if prior.document == term.document or prior.status == TermStatus.needs_review:
        return False
    if prior.type != term.type:
        return False
    if _fold(prior.plan_label) != _fold(term.plan_label):
        return False
    if _fold(prior.agreement_ref) != _fold(term.agreement_ref):
        return False
    if prior.effective_date != term.effective_date:
        return False
    if isinstance(term, NoticeWindowTerm | DisputeWindowTerm):
        if prior.value.applies_to != term.value.applies_to:
            return False
    return True


def differing_fields(term: ExtractedTerm, prior: ExtractedTerm) -> list[str]:
    """Structured value fields that are set on both sides and differ."""
    out = []
    for k, v in term.value:
        if k in UNCOMPARED_FIELDS:
            continue
        p = getattr(prior.value, k)
        if v is not None and p is not None and v != p:
            out.append(k)
    return out


@dataclass
class Comparison:
    issues: list[ValidationIssue] = field(default_factory=list)
    error: bool = False  # a call failed; the issues cover only what did answer


class PriorComparer(Protocol):
    name: str

    def compare(self, term: ExtractedTerm, prior_terms: Sequence[ExtractedTerm]) -> Comparison: ...


class RulePriorComparer:
    name = "rules"

    def compare(self, term: ExtractedTerm, prior_terms: Sequence[ExtractedTerm]) -> Comparison:
        issues = []
        for prior in prior_terms:
            if comparable(term, prior) and (diff := differing_fields(term, prior)):
                issues.append(
                    _issue(
                        term.id,
                        CONFLICTS_PRIOR,
                        f"differs from {prior.document} ({prior.id}) on {', '.join(diff)}",
                    )
                )
        return Comparison(issues)


JEV_QUESTION = "relation"
# Only pairs with the same effective date reach Jev (`comparable`), so a dated change isn't a
# choice. With a "changes" choice, Jev split a same-date $299 vs $339 pair about evenly between
# "changes" and "conflicts" (checked live 2026-11-04); without it, it answers 0.97 "conflicts".
JEV_CRITERIA = {
    "restates": "The new term states the same terms as the prior term.",
    "conflicts": (
        "The new term contradicts the prior term: for the same plan and the same effective "
        "date, they state different terms."
    ),
    "unrelated": "The new term is about something different from the prior term.",
}
CONFLICT_THRESHOLD = 0.6
_STATE_FIELDS = {"type", "plan_label", "agreement_ref", "effective_date", "value", "quotes"}


def jev_state(term: ExtractedTerm, prior: ExtractedTerm) -> dict[str, Any]:
    """The JSON Jev reads: both terms without ids, vendor or status, JSON-native values."""
    return {
        "new_term": term.model_dump(mode="json", include=_STATE_FIELDS),
        "prior_term": prior.model_dump(mode="json", include=_STATE_FIELDS),
    }


class JevPriorComparer:
    """Asks Jev, once per comparable prior term, how the new term relates to it. A
    `conflicts` answer at `CONFLICT_THRESHOLD` or above is an issue."""

    name = "jev"

    def __init__(self, client: Any, threshold: float = CONFLICT_THRESHOLD) -> None:
        self.client = client  # a typesafe_sdk.TypeSafeClient (`leo.jev.jev_client`)
        self.threshold = threshold

    def compare(self, term: ExtractedTerm, prior_terms: Sequence[ExtractedTerm]) -> Comparison:
        out = Comparison()
        for prior in prior_terms:
            if not comparable(term, prior):
                continue
            state = jev_state(term, prior)
            try:
                resp = self.client.system_one(
                    state=state,
                    questions={JEV_QUESTION: Choice(criteria=JEV_CRITERIA)},
                )
                answer = resp.choices[JEV_QUESTION]
            except (TypeSafeError, KeyError) as e:
                log.warning("Jev comparison failed for %s vs %s: %r", term.id, prior.id, e)
                out.error = True
                continue
            p = answer.probabilities.get("conflicts", 0.0)
            if answer.choice == "conflicts" and p >= self.threshold:
                probs = json.dumps(answer.probabilities, sort_keys=True)
                out.issues.append(
                    _issue(
                        term.id,
                        CONFLICTS_PRIOR,
                        f"Jev: conflicts with {prior.document} ({prior.id}), {probs}",
                    )
                )
        return out


# --- Validator -------------------------------------------------------------------------------


class RuleValidator:
    """The `Validator` for stage 4. `extra` comparers (Jev) run alongside the rule comparison."""

    def __init__(self, clock: Clock, extra: Sequence[PriorComparer] = ()) -> None:
        self.clock = clock
        self.extra = list(extra)

    def validate(
        self, doc: DocumentText, extraction: Extraction, prior_terms: Sequence[ExtractedTerm]
    ) -> Validated:
        text = doc.text
        today = self.clock.today()
        vendor_issues = check_vendor(text, extraction.vendor_name)
        issues = list(vendor_issues)
        issues += check_quotes(text, extraction)
        for item in (*extraction.terms, *extraction.invoice_facts):
            issues += check_dates(item, today)
            issues += check_amounts(item)

        errored: set[str] = set()
        comparers = [RulePriorComparer(), *self.extra]
        for term in extraction.terms:
            for c in comparers:
                result = c.compare(term, prior_terms)
                issues += result.issues
                if result.error:
                    errored.add(c.name)

        vendor_ok = not vendor_issues
        flagged = {i.item for i in issues if i.item}

        def review(x):
            if vendor_ok and x.id not in flagged:
                return x
            return x.model_copy(update={"status": TermStatus.needs_review})

        validated = extraction.model_copy(
            update={
                "terms": [review(t) for t in extraction.terms],
                "invoice_facts": [review(f) for f in extraction.invoice_facts],
            }
        )
        engine = "+".join(
            [ENGINE] + [c.name + ("(error)" if c.name in errored else "") for c in self.extra]
        )
        return Validated(extraction=validated, issues=issues, engine=engine, vendor_ok=vendor_ok)
