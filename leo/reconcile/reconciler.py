"""Reconciliation rules, stage 5 (ROADMAP C1).

`RuleReconciler` turns a `Ledger` into findings and supersessions. It is pure: it never mutates
the ledger and takes `today` from the caller. There is one rule per `FindingKind`:

- `auto_renewal_deadline` (F1): an auto-renewing term whose non-renewal notice deadline falls
  inside the lookahead.
- `price_increase_notice` (F2): a recorded price change on a plan that raises the price and
  hasn't taken effect yet. A restated price (same value) is not a change.
- `price_cap_exceeded` (F3): renewal-term invoices above the prior price plus the cap.
- `seats_over_commitment` (F4): invoices billing more seats than the seat commitment.
- `escalator_applied_early` (F5): an escalated price billed before the escalator's first
  application.

Inputs:
- Terms and invoice facts with status `needs_review` or `rejected` are ignored.
- `superseded` terms are kept as price history. Supersession is computed from the data, never
  read from `status`.

Price history is grouped per vendor and `plan_label`, and `effective_date` decides which price
applies on a given day (`price_on`). A finding's `plan_label` is set only when its vendor has
more than one plan, the same convention as `Ledger.transaction_plan_map`.

Deadlines use `leo.rules` (see docs/DATA_MODEL.md, "Deadlines"). A billing finding (F3–F5)
takes the earliest dispute deadline still open among its offending invoices, or None when the
vendor has no dispute window.

F3 and F5 compare a per-month price with invoice totals, so they skip any other `per`.
"""

import datetime
from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal

from leo.interfaces import Ledger, Reconciliation, Supersession
from leo.rules import (
    dispute_deadline,
    notice_deadline,
    price_change_deadline,
    recommended_send_by,
)
from leo.types import (
    EXCLUDED_STATUSES,
    Anchor,
    AppliesTo,
    AutoRenewalFinding,
    AutoRenewalImpact,
    DeadlineBasis,
    DisputeWindowTerm,
    EscalatorAppliedEarlyFinding,
    EscalatorComputation,
    EscalatorImpact,
    EscalatorTerm,
    ExtractedTerm,
    Finding,
    FindingKind,
    InvoiceFact,
    NoticeWindowTerm,
    PriceCapComputation,
    PriceCapExceededFinding,
    PriceCapImpact,
    PriceCapTerm,
    PriceIncreaseFinding,
    PriceIncreaseImpact,
    PricePer,
    PriceTerm,
    RelativeTo,
    RenewalTerm,
    SeatCountTerm,
    SeatsComputation,
    SeatsImpact,
    SeatsOverCommitmentFinding,
    Transaction,
    Vendor,
)

LOOKAHEAD_DAYS = 60
CENT = Decimal("0.01")
TENTH = Decimal("0.1")
HUNDRED = Decimal(100)
TWELVE = Decimal(12)


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def q1(x: Decimal) -> Decimal:
    return x.quantize(TENTH, rounding=ROUND_HALF_UP)


def pct_change(old: Decimal, new: Decimal) -> Decimal:
    """Percent change, as the key stores it (13.4, not 0.134)."""
    return q1((new - old) / old * HUNDRED)


def finding_id(anchor: Anchor, kind: FindingKind) -> str:
    """`v1:finding:{anchor term}:{anchor txn or -}:{kind}` (ROADMAP A2). Built from the
    ledger's ids, which are natural keys once the repository supplies them."""
    return f"v1:finding:{anchor.term}:{anchor.transaction or '-'}:{kind.value}"


def short_date(d: datetime.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _money(x: Decimal) -> str:
    return f"${x:,.2f}"


def _date_key(t: ExtractedTerm) -> tuple[datetime.date, str]:
    return (t.effective_date or datetime.date.min, t.id)


def price_on(group: Sequence[PriceTerm], day: datetime.date) -> PriceTerm | None:
    """The price that applies on `day`: the latest `effective_date` on or before it. A term
    with no effective date counts as always in effect."""
    applicable = [t for t in group if t.effective_date is None or t.effective_date <= day]
    return max(applicable, key=_date_key, default=None)


def _same_price(a: PriceTerm, b: PriceTerm) -> bool:
    return a.value == b.value


def supersessions_in(group: Sequence[PriceTerm]) -> list[Supersession]:
    """Within one plan's price history, in effective-date order: a price that differs from the
    current one supersedes it. An equal price is a restatement and changes nothing."""
    out: list[Supersession] = []
    head: PriceTerm | None = None
    for t in sorted(group, key=_date_key):
        if head is not None and _same_price(head, t):
            continue
        if head is not None:
            out.append(Supersession(old=head.id, new=t.id))
        head = t
    return out


class _Vendor:
    """One vendor's usable terms, invoice facts and charges."""

    def __init__(self, vid: str, ledger: Ledger, terms: list[ExtractedTerm]) -> None:
        self.id = vid
        self.ledger = ledger
        self.terms = terms
        self.facts = sorted(
            (
                f
                for f in ledger.invoice_facts
                if f.vendor == vid and f.status not in EXCLUDED_STATUSES
            ),
            key=lambda f: (f.invoice_date, f.id),
        )
        groups: dict[str | None, list[PriceTerm]] = defaultdict(list)
        for t in self.of(PriceTerm):
            groups[t.plan_label].append(t)
        self.price_groups = {k: sorted(v, key=_date_key) for k, v in groups.items()}
        self.multi_plan = len([k for k in self.price_groups if k is not None]) > 1
        self.txns = {t.id: t for t in ledger.transactions}

    def of[T](self, cls: type[T]) -> list[T]:
        return [t for t in self.terms if isinstance(t, cls)]

    def plan(self, plan_label: str | None) -> str | None:
        """The finding's `plan_label`: set only when the vendor has more than one plan."""
        return plan_label if self.multi_plan else None

    def prices(self, plan_label: str | None) -> list[PriceTerm]:
        return self.price_groups.get(plan_label, [])

    def charges(self, plan_label: str | None) -> list[Transaction]:
        out = [
            t
            for t in self.ledger.transactions
            if self.ledger.transaction_vendor_map.get(t.id) == self.id
            and (not self.multi_plan or self.ledger.transaction_plan_map.get(t.id) == plan_label)
        ]
        return sorted(out, key=lambda t: (t.date, t.id))

    def facts_for(self, plan_label: str | None) -> list[InvoiceFact]:
        if not self.multi_plan:
            return self.facts
        return [
            f
            for f in self.facts
            if f.matches_transaction is not None
            and self.ledger.transaction_plan_map.get(f.matches_transaction) == plan_label
        ]

    def latest_charge(self, plan_label: str | None, today: datetime.date) -> list[str]:
        past = [t for t in self.charges(plan_label) if t.date <= today]
        return [past[-1].id] if past else []

    def billing_transactions(
        self, plan_label: str | None, offenders: Sequence[InvoiceFact]
    ) -> list[str]:
        """The last charge before the first offending invoice, then the offenders' charges."""
        matched = [f.matches_transaction for f in offenders if f.matches_transaction]
        first = self.txns.get(matched[0]) if matched else None
        cutoff = first.date if first else offenders[0].invoice_date
        before = [t for t in self.charges(plan_label) if t.date < cutoff]
        return ([before[-1].id] if before else []) + matched

    def dispute(
        self, offenders: Sequence[InvoiceFact], today: datetime.date
    ) -> tuple[DisputeWindowTerm | None, datetime.date | None, InvoiceFact | None]:
        """The vendor's invoice dispute window, and the earliest dispute deadline still open
        among the offending invoices (with its invoice)."""
        windows = [
            w
            for w in self.of(DisputeWindowTerm)
            if w.value.days is not None and w.value.relative_to == RelativeTo.invoice_date
        ]
        if not windows:
            return None, None, None
        window = windows[0]
        open_ = [
            (d, f)
            for f in offenders
            if (d := dispute_deadline(window.value, f.invoice_date)) >= today
        ]
        if not open_:
            return window, None, None
        d, f = min(open_, key=lambda x: (x[0], x[1].id))
        return window, d, f


def _notes(text: str, deadline: datetime.date | None) -> str:
    if deadline is None:
        return text
    send_by = recommended_send_by(deadline)
    if send_by != deadline:
        text += (
            f" {short_date(deadline)} is a {deadline:%A}, so send by"
            f" {send_by:%A} {short_date(send_by)}."
        )
    return text


def _common(
    v: _Vendor,
    kind: FindingKind,
    title: str,
    anchor_term: ExtractedTerm,
    anchor_txn: str | None,
    terms: Iterable[ExtractedTerm | None],
    transactions: list[str],
    facts: Sequence[InvoiceFact],
    plan_label: str | None,
    deadline: datetime.date | None,
    basis_terms: Sequence[ExtractedTerm],
    basis_fact: InvoiceFact | None,
    notes: str,
) -> dict:
    anchor = Anchor(term=anchor_term.id, transaction=anchor_txn)
    documents = list(dict.fromkeys([anchor_term.document, *(f.document for f in facts)]))
    linked = [d.path for d in v.ledger.documents if d.probable_duplicate_of in documents]
    return {
        "id": finding_id(anchor, kind),
        "kind": kind,
        "vendor": v.id,
        "title": title,
        "terms": list(dict.fromkeys(t.id for t in terms if t is not None)),
        "transactions": list(dict.fromkeys(transactions)),
        "documents": documents,
        "invoice_facts": [f.id for f in facts],
        "linked_duplicates": linked,
        "plan_label": plan_label,
        "anchor": anchor,
        "deadline": deadline,
        "recommended_send_by": recommended_send_by(deadline) if deadline else None,
        "deadline_basis": (
            DeadlineBasis(
                terms=[t.id for t in basis_terms],
                invoice_fact=basis_fact.id if basis_fact else None,
            )
            if deadline
            else None
        ),
        "deadline_notes": _notes(notes, deadline),
    }


def _vendor_name(ledger: Ledger, vid: str) -> str:
    v: Vendor | None = next((v for v in ledger.vendors if v.id == vid), None)
    return v.canonical_name if v else vid


def _annual(p: PriceTerm | None) -> Decimal | None:
    if p is None or p.value.amount is None:
        return None
    match p.value.per:
        case PricePer.year:
            return q2(p.value.amount)
        case PricePer.month:
            return q2(p.value.amount * TWELVE)
    return None


# --- Rules -----------------------------------------------------------------------------------


def auto_renewal(v: _Vendor, today: datetime.date, lookahead: int) -> list[Finding]:
    out: list[Finding] = []
    for notice in v.of(NoticeWindowTerm):
        if notice.value.applies_to != AppliesTo.non_renewal:
            continue
        renewal = next(
            (
                r
                for r in v.of(RenewalTerm)
                if r.agreement_ref == notice.agreement_ref
                and r.value.auto_renew is True
                and r.value.current_term_end is not None
            ),
            None,
        )
        if renewal is None:
            continue
        try:
            deadline = notice_deadline(notice.value, renewal.value)
        except ValueError:
            continue
        if not today <= deadline <= today + datetime.timedelta(days=lookahead):
            continue
        prices = [
            p
            for p in (price_on(g, today) for g in v.price_groups.values())
            if p is not None and p.agreement_ref == notice.agreement_ref
        ]
        price = prices[0] if prices else None
        plan = price.plan_label if price else None
        amount = _annual(price) or Decimal(0)
        months = renewal.value.renewal_length_months
        basis = f"{months}-month renewal commitment." if months else "Renewal commitment."
        if renewal.value.renewal_pricing:
            basis += f" Renewal pricing: {renewal.value.renewal_pricing}."
        term_end = renewal.value.current_term_end
        assert term_end is not None
        name = _vendor_name(v.ledger, v.id)
        out.append(
            AutoRenewalFinding(
                **_common(
                    v,
                    FindingKind.auto_renewal_deadline,
                    f"{name} auto-renews after {short_date(term_end)} unless notice is given"
                    f" by {short_date(deadline)}",
                    notice,
                    None,
                    [renewal, notice, price],
                    v.latest_charge(plan, today),
                    [],
                    v.plan(plan),
                    deadline,
                    [notice, renewal],
                    None,
                    f"Non-renewal notice is due {notice.value.days} days before the term ends"
                    f" on {short_date(term_end)}.",
                ),
                impact=AutoRenewalImpact(amount=amount, basis=basis),
            )
        )
    return out


def price_increases(v: _Vendor, today: datetime.date) -> tuple[list[Finding], list[Supersession]]:
    findings: list[Finding] = []
    supersessions: list[Supersession] = []
    by_id = {t.id: t for t in v.of(PriceTerm)}
    for plan, group in v.price_groups.items():
        for s in supersessions_in(group):
            supersessions.append(s)
            old, new = by_id[s.old], by_id[s.new]
            if (
                old.value.amount is None
                or new.value.amount is None
                or old.value.amount <= 0
                or new.value.amount <= old.value.amount
                or old.value.per != new.value.per
                or old.value.currency != new.value.currency
                or new.effective_date is None
            ):
                continue
            deadline = price_change_deadline(new.effective_date)
            if deadline < today:
                continue
            delta = new.value.amount - old.value.amount
            match new.value.per:
                case PricePer.month:
                    monthly, annual = q2(delta), q2(delta * TWELVE)
                case PricePer.year:
                    monthly, annual = q2(delta / TWELVE), q2(delta)
                case _:
                    continue
            pct = pct_change(old.value.amount, new.value.amount)
            rules = [
                n
                for n in v.of(NoticeWindowTerm)
                if n.value.applies_to == AppliesTo.vendor_price_change
                and (n.agreement_ref is None or n.agreement_ref == new.agreement_ref)
            ]
            name = _vendor_name(v.ledger, v.id)
            label = f"{name} {plan}" if plan else name
            findings.append(
                PriceIncreaseFinding(
                    **_common(
                        v,
                        FindingKind.price_increase_notice,
                        f"{label} rises {pct}% ({_money(old.value.amount)} to"
                        f" {_money(new.value.amount)}) on {short_date(new.effective_date)}",
                        new,
                        None,
                        [old, new, *rules],
                        v.latest_charge(plan, today),
                        [],
                        v.plan(plan),
                        deadline,
                        [new],
                        None,
                        "Cancel or renegotiate before the new price takes effect on"
                        f" {short_date(new.effective_date)}.",
                    ),
                    impact=PriceIncreaseImpact(monthly=monthly, annual=annual, pct=pct),
                )
            )
    return findings, supersessions


def price_caps(v: _Vendor, today: datetime.date) -> list[Finding]:
    out: list[Finding] = []
    for cap in v.of(PriceCapTerm):
        cap_pct = cap.value.max_increase_pct
        if cap.value.applies_to != AppliesTo.renewal_term or cap_pct is None:
            continue
        renewal = next(
            (
                r
                for r in v.of(RenewalTerm)
                if r.agreement_ref == cap.agreement_ref and r.value.initial_term_end
            ),
            None,
        )
        if renewal is None:
            continue
        term_end = renewal.value.initial_term_end
        assert term_end is not None
        for plan, group in v.price_groups.items():
            prior = price_on(group, term_end)
            if (
                prior is None
                or prior.value.amount is None
                or prior.value.amount <= 0
                or prior.value.per != PricePer.month
                or prior.agreement_ref != (cap.agreement_ref)
            ):
                continue
            base = prior.value.amount
            max_allowed = q2(base * (1 + cap_pct / HUNDRED))
            offenders = [
                f for f in v.facts_for(plan) if f.invoice_date > term_end and f.amount > max_allowed
            ]
            if not offenders:
                continue
            charged = offenders[0].amount
            overcharge = q2(charged - max_allowed)
            to_date = q2(sum((f.amount - max_allowed for f in offenders), Decimal(0)))
            full = q2(overcharge * (renewal.value.renewal_length_months or 12))
            window, deadline, basis_fact = v.dispute(offenders, today)
            name = _vendor_name(v.ledger, v.id)
            actual = pct_change(base, charged)
            out.append(
                PriceCapExceededFinding(
                    **_common(
                        v,
                        FindingKind.price_cap_exceeded,
                        f"{name} renewal raised the price {actual}%, above the"
                        f" {q1(cap_pct)}% contractual cap",
                        cap,
                        offenders[0].matches_transaction,
                        [prior, cap, renewal, window],
                        v.billing_transactions(plan, offenders),
                        offenders,
                        v.plan(plan),
                        deadline,
                        [window] if window else [],
                        basis_fact,
                        _billing_notes(window, basis_fact),
                    ),
                    impact=PriceCapImpact(
                        overcharged_to_date=to_date,
                        remaining_renewal_term=max(q2(full - to_date), Decimal(0)),
                        full_renewal_term=full,
                    ),
                    computation=PriceCapComputation(
                        prior_monthly=q2(base),
                        cap_pct=cap_pct,
                        max_allowed_monthly=max_allowed,
                        charged_monthly=q2(charged),
                        actual_increase_pct=actual,
                        overcharge_monthly=overcharge,
                    ),
                )
            )
    return out


def seats_over(v: _Vendor, today: datetime.date) -> list[Finding]:
    out: list[Finding] = []
    for seats in v.of(SeatCountTerm):
        included = seats.value.included
        if included is None:
            continue
        plan = seats.plan_label
        offenders = [
            f for f in v.facts_for(plan) if f.seats_billed is not None and f.seats_billed > included
        ]
        if not offenders:
            continue
        price = price_on(v.prices(plan), offenders[0].invoice_date)
        per_seat = seats.value.overage_per_unit_month
        if per_seat is None and price is not None and price.value.per == PricePer.seat_month:
            per_seat = price.value.amount
        if per_seat is None:
            continue
        billed = offenders[0].seats_billed
        assert billed is not None
        extra = billed - included
        overcharge = q2(extra * per_seat)
        to_date = q2(
            sum((((f.seats_billed or 0) - included) * per_seat for f in offenders), Decimal(0))
        )
        window, deadline, basis_fact = v.dispute(offenders, today)
        name = _vendor_name(v.ledger, v.id)
        unit = seats.value.unit.value if seats.value.unit else "seats"
        out.append(
            SeatsOverCommitmentFinding(
                **_common(
                    v,
                    FindingKind.seats_over_commitment,
                    f"{name} billed {billed} {unit}; the commitment covers {included}",
                    seats,
                    offenders[0].matches_transaction,
                    [seats, price, window],
                    v.billing_transactions(plan, offenders),
                    offenders,
                    v.plan(plan),
                    deadline,
                    [window] if window else [],
                    basis_fact,
                    _billing_notes(window, basis_fact)
                    + f" Every month it stays unresolved adds {_money(overcharge)}.",
                ),
                impact=SeatsImpact(overcharged_to_date=to_date, annualized=q2(overcharge * 12)),
                computation=SeatsComputation(
                    contracted_seats=included,
                    billed_seats=billed,
                    extra_seats=extra,
                    price_per_seat_month=q2(per_seat),
                    overcharge_monthly=overcharge,
                ),
            )
        )
    return out


def _months_between(a: datetime.date, b: datetime.date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def escalators_early(v: _Vendor, today: datetime.date) -> list[Finding]:
    out: list[Finding] = []
    for esc in v.of(EscalatorTerm):
        pct, first = esc.value.pct, esc.value.first_application
        if pct is None or first is None or pct <= 0:
            continue
        plan = esc.plan_label
        group = v.prices(plan)
        offenders: list[tuple[InvoiceFact, PriceTerm]] = []
        for f in v.facts_for(plan):
            base = price_on(group, f.invoice_date)
            if (
                f.invoice_date < first
                and base is not None
                and base.value.amount is not None
                and base.value.per == PricePer.month
                and f.amount == q2(base.value.amount * (1 + pct / HUNDRED))
            ):
                offenders.append((f, base))
        if not offenders:
            continue
        facts = [f for f, _ in offenders]
        first_fact, base = offenders[0]
        assert base.value.amount is not None
        contract = q2(base.value.amount)
        overcharge = q2(first_fact.amount - contract)
        to_date = q2(sum((f.amount - b.value.amount for f, b in offenders), Decimal(0)))
        months = max(_months_between(first_fact.invoice_date, first), 0)
        window, deadline, basis_fact = v.dispute(facts, today)
        name = _vendor_name(v.ledger, v.id)
        out.append(
            EscalatorAppliedEarlyFinding(
                **_common(
                    v,
                    FindingKind.escalator_applied_early,
                    f"{name} applied its {q1(pct)}% increase on"
                    f" {short_date(first_fact.invoice_date)}; the contract says"
                    f" {short_date(first)}",
                    esc,
                    first_fact.matches_transaction,
                    [base, esc, window],
                    v.billing_transactions(plan, facts),
                    facts,
                    v.plan(plan),
                    deadline,
                    [window] if window else [],
                    basis_fact,
                    _billing_notes(window, basis_fact),
                ),
                impact=EscalatorImpact(
                    overcharged_to_date=to_date,
                    if_uncorrected_until_anniversary=q2(overcharge * months),
                ),
                computation=EscalatorComputation(
                    contract_monthly=contract,
                    charged_monthly=q2(first_fact.amount),
                    escalator_pct=pct,
                    correct_first_application=first,
                    applied_from=first_fact.invoice_date,
                    overcharge_monthly=overcharge,
                ),
            )
        )
    return out


def _billing_notes(window: DisputeWindowTerm | None, fact: InvoiceFact | None) -> str:
    if window is None:
        return "No dispute window is stated."
    if fact is None:
        return f"The {window.value.days}-day dispute window has closed on every invoice."
    return (
        f"There is a {window.value.days}-day dispute window from the"
        f" {short_date(fact.invoice_date)} invoice date."
    )


# --- The reconciler --------------------------------------------------------------------------


class RuleReconciler:
    def __init__(self, lookahead_days: int = LOOKAHEAD_DAYS) -> None:
        self.lookahead_days = lookahead_days

    def reconcile(self, ledger: Ledger, today: datetime.date) -> Reconciliation:
        terms: dict[str, list[ExtractedTerm]] = defaultdict(list)
        for t in ledger.terms:
            if t.vendor and t.status not in EXCLUDED_STATUSES:
                terms[t.vendor].append(t)
        findings: list[Finding] = []
        supersessions: list[Supersession] = []
        for vid in sorted(terms):
            v = _Vendor(vid, ledger, terms[vid])
            findings += auto_renewal(v, today, self.lookahead_days)
            increases, supers = price_increases(v, today)
            findings += increases
            supersessions += supers
            findings += price_caps(v, today)
            findings += seats_over(v, today)
            findings += escalators_early(v, today)
        return Reconciliation(findings=findings, supersessions=supersessions)
