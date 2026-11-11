"""Action drafting, stage 5.

Code decides everything that must be right, and the LLM writes only the prose:
- `action_plan` picks the action type, alternative and pre-step from the finding's kind.
- `build_facts` formats the cited facts (agreement number, amounts, dates, invoice numbers,
  contract clauses) from the finding and the ledger, as named placeholders.
- `recipient` takes a term's `deliver_to`, or else the vendor's contacts.
- `LLMDrafter` asks Claude (Haiku 5.5 by default, `LEO_MODEL_DRAFT`) for an email body that
  writes every fact as a `{placeholder}`, never typed out. `check_body` rejects a body that
  leaves out a required fact, invents a placeholder, types a number itself or has no paragraph
  breaks, and the drafter asks again with the problems listed. After `MAX_ATTEMPTS`, or when a
  call stops early, it falls back to a fixed template, so every finding gets a draft.
- `draft_new` skips findings that already have an action of that type, before any LLM call.

The drafter can't see the answer key, so `check_body` is the runtime stand-in for the key's
`expected_action.must_include`. The eval (`--drafter`) checks the real list.
"""

import datetime
import hashlib
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anthropic

from leo.config import Settings, get_settings
from leo.interfaces import Drafter, Ledger
from leo.llm import ResponseCache, json_schema_for, structured_call
from leo.types import (
    ActionDraft,
    ActionType,
    DisputeWindowValue,
    ExtractedTerm,
    Finding,
    FindingKind,
    InvoiceFact,
    LeoModel,
    NoticeWindowValue,
    PricePer,
    PriceValue,
    TermType,
)

STAGE = "draft"
PROMPT_VERSION = "draft-v6"
EFFORT = "low"
MAX_ATTEMPTS = 3
MAX_TOKENS = 4000


class DraftBody(LeoModel):
    body: str


SCHEMA = json_schema_for(DraftBody)
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
QUOTED_NON_CLAUSE = re.compile(r'"\{((?![a-z_]*_clause\})[a-z_]+)\}')
# Kinds whose action is a billing dispute. Their deadline is the last day *we* may raise the
# dispute, which the vendor doesn't need and the model misread as a deadline for the vendor, so
# it is not a fact in the email. The approval UI shows it.
NO_DEADLINE_FACT = {
    FindingKind.price_cap_exceeded,
    FindingKind.seats_over_commitment,
    FindingKind.escalator_applied_early,
}

# --- Action plan -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionPlan:
    type: ActionType
    alternative: ActionType | None = None
    pre_step: str | None = None


def action_plan(finding: Finding) -> ActionPlan:
    match finding.kind:
        case FindingKind.auto_renewal_deadline:
            return ActionPlan(ActionType.cancellation_notice, ActionType.renegotiation_request)
        case FindingKind.price_increase_notice:
            return ActionPlan(ActionType.negotiation_email)
        case FindingKind.seats_over_commitment:
            extra = finding.computation.extra_seats
            return ActionPlan(
                ActionType.billing_dispute,
                pre_step=(
                    f"Confirm internally that no one added the {extra} extra seats on purpose"
                    " before sending."
                ),
            )
        case _:
            return ActionPlan(ActionType.billing_dispute)


# --- Formatting ------------------------------------------------------------------------------


def money(x: Decimal) -> str:
    return f"${x:,.2f}"


def long_date(d: datetime.date) -> str:
    """ "January 1, 2027": no zero-padded day."""
    return f"{d:%B} {d.day}, {d.year}"


def pct(x: Decimal) -> str:
    return f"{x.normalize():f}%"


_PER = {PricePer.year: "/year", PricePer.month: "/month", PricePer.seat_month: " per seat/month"}


def price(v: PriceValue) -> str | None:
    if v.amount is None:
        return None
    return money(v.amount) + (_PER[v.per] if v.per else "")


def invoice_list(facts: Sequence[InvoiceFact]) -> str | None:
    items = [
        f"{x.invoice_number} ({long_date(x.invoice_date)})"
        if x.invoice_number
        else f"the invoice dated {long_date(x.invoice_date)}"
        for x in sorted(facts, key=lambda x: x.invoice_date)
    ]
    if not items:
        return None
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def clause(term: ExtractedTerm | None, marker: str = "%") -> str | None:
    """The quote that states the rule: the first one containing `marker` (a percent sign by
    default), else the first."""
    if term is None:
        return None
    return next((q for q in term.quotes if marker in q.lower()), term.quotes[0])


# --- Facts -----------------------------------------------------------------------------------


@dataclass
class Facts:
    """Placeholder name -> formatted value. `required` must appear as placeholders in the body,
    and `phrases` must appear in the rendered body."""

    values: dict[str, str]
    required: list[str]
    phrases: list[str] = field(default_factory=list)

    def digest(self, *extra: str) -> str:
        blob = json.dumps(
            [self.values, self.required, self.phrases, *extra], sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class _Lookup:
    def __init__(self, finding: Finding, ledger: Ledger) -> None:
        by_id = {t.id: t for t in ledger.terms}
        self.terms = [by_id[t] for t in finding.terms if t in by_id]
        self.anchor = by_id.get(finding.anchor.term)
        facts = {x.id: x for x in ledger.invoice_facts}
        self.invoice_facts = [facts[x] for x in finding.invoice_facts if x in facts]
        self.by_id = by_id

    def of_type(self, t: TermType) -> ExtractedTerm | None:
        if self.anchor is not None and self.anchor.type == t:
            return self.anchor
        return next((x for x in self.terms if x.type == t), None)

    def agreement_ref(self) -> str | None:
        for t in (self.anchor, *self.terms):
            if t is not None and t.agreement_ref:
                return t.agreement_ref
        return None


def vendor_name(finding: Finding, ledger: Ledger) -> str:
    v = next((v for v in ledger.vendors if v.id == finding.vendor), None)
    return v.canonical_name if v else finding.vendor


def build_facts(finding: Finding, ledger: Ledger, sender: str = "") -> Facts:
    """The cited facts for a finding. A fact the ledger doesn't have is left out, and is then
    not required."""
    look = _Lookup(finding, ledger)
    ref = look.agreement_ref()
    values: dict[str, str | None] = {
        "vendor": vendor_name(finding, ledger),
        "sender": sender,
        "agreement": f"Agreement No. {ref}" if ref else "our agreement",
        "deadline": (
            long_date(finding.deadline)
            if finding.deadline and finding.kind not in NO_DEADLINE_FACT
            else None
        ),
    }
    required = ["agreement"]
    phrases: list[str] = []

    match finding.kind:
        case FindingKind.auto_renewal_deadline:
            renewal = look.of_type(TermType.renewal)
            end = renewal.value.current_term_end if renewal else None  # type: ignore[union-attr]
            notice = look.of_type(TermType.notice_window)
            values |= {
                "term_end": long_date(end) if end else None,
                "notice_clause": notice.quotes[0] if notice else None,
                "renewal_amount": money(finding.impact.amount),
            }
            required += ["term_end"]
            phrases += ["non-renewal"]
        case FindingKind.price_increase_notice:
            new = look.of_type(TermType.price)
            old_value = new.old_value if new else None
            if new is not None and old_value is None and new.supersedes:
                old_term = look.by_id.get(new.supersedes)
                old_value = old_term.value if old_term else None  # type: ignore[assignment]
            eff = new.effective_date if new else None
            values |= {
                "plan": finding.plan_label or (new.plan_label if new else None),
                "old_price": price(old_value) if isinstance(old_value, PriceValue) else None,
                "new_price": price(new.value) if new else None,  # type: ignore[arg-type]
                "effective_date": long_date(eff) if eff else None,
                "increase_pct": pct(finding.impact.pct),
                "monthly_increase": money(finding.impact.monthly),
                "annual_impact": money(finding.impact.annual),
            }
            required += ["old_price", "new_price"]
        case FindingKind.price_cap_exceeded:
            c = finding.computation
            values |= {
                "cap_clause": clause(look.of_type(TermType.price_cap)),
                "cap_pct": pct(c.cap_pct),
                "prior_monthly": money(c.prior_monthly),
                "max_allowed_monthly": money(c.max_allowed_monthly),
                "charged_monthly": money(c.charged_monthly),
                "actual_increase_pct": pct(c.actual_increase_pct),
                "overcharge_monthly": money(c.overcharge_monthly),
                "overcharged_to_date": money(finding.impact.overcharged_to_date),
                "invoices": invoice_list(look.invoice_facts),
            }
            required += ["cap_clause", "max_allowed_monthly", "overcharged_to_date", "invoices"]
        case FindingKind.seats_over_commitment:
            c = finding.computation
            seats = look.of_type(TermType.seat_count)
            values |= {
                "contracted_seats": str(c.contracted_seats),
                "billed_seats": str(c.billed_seats),
                "extra_seats": str(c.extra_seats),
                "price_per_seat": money(c.price_per_seat_month) + " per seat per month",
                "overcharge_monthly": money(c.overcharge_monthly),
                "overcharged_to_date": money(finding.impact.overcharged_to_date),
                "annualized": money(finding.impact.annualized),
                "increase_requires": seats.value.increase_requires if seats else None,  # type: ignore[union-attr]
                # A clause to quote, so the model doesn't quote `increase_requires` (2.3).
                "seat_clause": clause(seats, "increase"),
                "invoices": invoice_list(look.invoice_facts),
            }
            required += ["contracted_seats", "billed_seats", "overcharged_to_date"]
        case FindingKind.escalator_applied_early:
            c = finding.computation
            values |= {
                "escalator_clause": clause(look.of_type(TermType.escalator)),
                "escalator_pct": pct(c.escalator_pct),
                "first_application": long_date(c.correct_first_application),
                "applied_from": long_date(c.applied_from),
                "contract_monthly": money(c.contract_monthly),
                "charged_monthly": money(c.charged_monthly),
                "overcharge_monthly": money(c.overcharge_monthly),
                "overcharged_to_date": money(finding.impact.overcharged_to_date),
                "invoices": invoice_list(look.invoice_facts),
            }
            required += ["first_application", "overcharged_to_date", "invoices"]

    present = {k: v for k, v in values.items() if v is not None}
    return Facts(values=present, required=[r for r in required if r in present], phrases=phrases)


# --- Recipient -------------------------------------------------------------------------------


def recipient(finding: Finding, ledger: Ledger, action_type: ActionType) -> str:
    """A notice or dispute window's `deliver_to` (the deadline's terms first, then the anchor,
    then the rest), else a vendor contact ("notices" preferred for a cancellation, "billing"
    otherwise), else "". The approval UI must not approve a draft with no recipient."""
    by_id = {t.id: t for t in ledger.terms}
    basis = finding.deadline_basis.terms if finding.deadline_basis else []
    for tid in dict.fromkeys([*basis, finding.anchor.term, *finding.terms]):
        t = by_id.get(tid)
        if t is not None and isinstance(t.value, NoticeWindowValue | DisputeWindowValue):
            if t.value.deliver_to:
                return t.value.deliver_to
    vendor = next((v for v in ledger.vendors if v.id == finding.vendor), None)
    if vendor is None or not vendor.contacts:
        return ""
    want = "notices" if action_type == ActionType.cancellation_notice else "billing"
    preferred = [c for c in vendor.contacts if c.purpose == want]
    return (preferred or vendor.contacts)[0].email


def subject(plan: ActionPlan, facts: Facts) -> str:
    agreement = facts.values["agreement"]
    match plan.type:
        case ActionType.cancellation_notice:
            return f"Notice of non-renewal: {agreement}"
        case ActionType.negotiation_email:
            plan_name = facts.values.get("plan")
            return (
                f"{plan_name} plan price change: {agreement}"
                if plan_name
                else (f"Price change: {agreement}")
            )
        case ActionType.renegotiation_request:
            return f"Renewal terms: {agreement}"
        case _:
            return f"Billing dispute: {agreement}"


# --- Body: render, check, template -----------------------------------------------------------


def render(body: str, facts: Facts) -> str:
    """Fill in known placeholders. Unknown ones are left as written (`check_body` flags them)."""
    return PLACEHOLDER.sub(lambda m: facts.values.get(m.group(1), m.group(0)), body)


def check_body(body: Any, facts: Facts) -> list[str]:
    """Problems with a model-written body, before rendering. Empty means it passes."""
    if not isinstance(body, str) or not body.strip():
        return ["the body is empty"]
    problems = []
    used = set(PLACEHOLDER.findall(body))
    missing = [r for r in facts.required if r not in used]
    if missing:
        problems.append("missing required placeholders: " + ", ".join(f"{{{m}}}" for m in missing))
    unknown = sorted(used - facts.values.keys())
    if unknown:
        problems.append("unknown placeholders: " + ", ".join(f"{{{u}}}" for u in unknown))
    typed = sorted(set(re.findall(r"\S*\d\S*", LIST_MARKER.sub("", PLACEHOLDER.sub("", body)))))
    if typed:
        problems.append("numbers typed out instead of placeholders: " + ", ".join(typed[:5]))
    quoted = sorted(set(QUOTED_NON_CLAUSE.findall(body)))
    if quoted:
        problems.append(
            "only _clause placeholders may be in quotation marks: "
            + ", ".join(f"{{{q}}}" for q in quoted)
        )
    stutter = sorted(
        name
        for name in used & facts.values.keys()
        if len(words := facts.values[name].split()) >= 2
        and re.search(re.escape(" ".join(words[:2])) + r"\s*\{" + name + r"\}", body)
    )
    if stutter:
        problems.append(
            "the words before these placeholders repeat their values; write the placeholder"
            " alone: " + ", ".join(f"{{{n}}}" for n in stutter)
        )
    if "\n\n" not in body.strip():
        problems.append("no paragraph breaks; separate paragraphs with a blank line")
    rendered = render(body, facts)
    for p in facts.phrases:
        if p not in rendered:
            problems.append(f"missing the phrase {p!r}")
    return problems


_SIGN_OFF = "\n\nThank you,\n{sender}"


def template_body(finding: Finding, facts: Facts) -> str:
    """The fallback body, written with placeholders. It uses only facts that are present."""
    v = facts.values

    def opt(text: str, *names: str) -> str:
        return text if all(n in v for n in names) else ""

    hello = "Hello {vendor} team,\n\n"
    match finding.kind:
        case FindingKind.auto_renewal_deadline:
            body = (
                "This is our written notice of non-renewal of {agreement}."
                + opt(
                    " The current term ends on {term_end}, and we do not wish to renew it.",
                    "term_end",
                )
                + opt(
                    " We are sending this before {deadline}, as the agreement requires.", "deadline"
                )
                + "\n\nPlease confirm that you have received this notice"
                + opt(" and that the agreement will end on {term_end}", "term_end")
                + "."
            )
        case FindingKind.price_increase_notice:
            body = (
                "We received your notice that "
                + opt("the {plan} plan under ", "plan")
                + "{agreement} will change"
                + opt(" from {old_price}", "old_price")
                + opt(" to {new_price}", "new_price")
                + opt(" starting {effective_date}", "effective_date")
                + opt(
                    ", an increase of {increase_pct} ({annual_impact} a year)",
                    "increase_pct",
                    "annual_impact",
                )
                + ".\n\nBefore the new price takes effect, we would like to discuss keeping"
                + opt(" our current rate of {old_price}", "old_price")
                + (" our current rate" if "old_price" not in v else "")
                + ", or other options such as a longer commitment at a lower price."
                " Please let us know who we can speak with."
            )
        case FindingKind.price_cap_exceeded:
            body = (
                "We are disputing the charges"
                + opt(" on {invoices}", "invoices")
                + " under {agreement}."
                + opt(' The agreement states: "{cap_clause}"', "cap_clause")
                + " Our prior rate was {prior_monthly} per month, so a renewal term may charge"
                " at most {max_allowed_monthly} per month. We have been billed {charged_monthly}"
                " per month, an overcharge of {overcharge_monthly} per month and"
                " {overcharged_to_date} to date.\n\nPlease credit {overcharged_to_date} and bill"
                " {max_allowed_monthly} per month going forward."
                + opt(
                    " We are raising this within the dispute window, which closes on {deadline}.",
                    "deadline",
                )
            )
        case FindingKind.seats_over_commitment:
            body = (
                "We were billed for {billed_seats} seats"
                + opt(" on {invoices}", "invoices")
                + ", but {agreement} covers {contracted_seats} seats at {price_per_seat}."
                + opt(
                    " Adding seats requires a {increase_requires}, and we have not signed one.",
                    "increase_requires",
                )
                + " The {extra_seats} extra seats add {overcharge_monthly} a month, or"
                " {overcharged_to_date} to date.\n\nPlease credit {overcharged_to_date} and bill"
                " {contracted_seats} seats going forward."
            )
        case FindingKind.escalator_applied_early:
            body = (
                "Our invoice"
                + opt(" {invoices}", "invoices")
                + " applies a {escalator_pct} increase, billing {charged_monthly} per month"
                " instead of {contract_monthly}."
                + opt(' Under {agreement}, "{escalator_clause}"', "escalator_clause")
                + " The first increase is due on {first_application}, so it was applied too early."
                " That is an overcharge of {overcharge_monthly} per month, or"
                " {overcharged_to_date} to date.\n\nPlease credit {overcharged_to_date} and bill"
                " {contract_monthly} per month until {first_application}."
                + opt(
                    " We are raising this within the dispute window, which closes on {deadline}.",
                    "deadline",
                )
            )
    return hello + body + _SIGN_OFF


# --- LLM drafter -----------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write short, professional emails for a small business's accounts-payable team to send to a \
vendor: a notice of non-renewal, a request to renegotiate, a reply to a price-change notice, or \
a billing dispute. A person reviews every email before it is sent.

The facts are given to you as named placeholders. Write each fact only as its placeholder, \
in curly braces exactly as given, for example {agreement} or {overcharged_to_date}. Code fills \
them in after you write. Never type a number, amount, percentage, date, invoice number or \
agreement number yourself, not even a number of days, and never copy one from the summary. \
If a number has no placeholder, leave it out. Use only the placeholders \
listed, and use every required one at least once.

Placeholders ending in "_clause" are exact quotes from the agreement, sometimes only part of a \
sentence. Put each one in double quotes after an introduction such as 'The agreement states:' \
so the sentence still reads correctly, and don't name a section number. {invoices} already \
includes each invoice's date, so don't repeat the date next to it. A placeholder's value is \
a complete phrase: don't write its first words before it (write 'on {invoices}', not 'on the \
invoice {invoices}'). Only "_clause" placeholders \
are quotes: never put any other placeholder in quotation marks or after 'The agreement states:'. \
If there is no "_clause" placeholder, don't quote the agreement at all.

Write in plain, firm, polite language. Say clearly what happened, what the agreement says and \
what you are asking the vendor to do. Don't threaten, apologize, speculate or add facts that \
aren't given. Start with a greeting to the {vendor} team and end with a sign-off from {sender} \
alone, with no title, department or name added. Separate paragraphs with a blank line. No \
subject line: the subject is set separately. Return JSON with one field, "body".\
"""

_ASKS = {
    ActionType.cancellation_notice: (
        "Give formal written notice of non-renewal: the business does not want the agreement "
        'to renew. Use the words "non-renewal". Ask the vendor to confirm receipt.'
    ),
    ActionType.renegotiation_request: (
        "Ask to renegotiate the renewal terms before the agreement renews."
    ),
    ActionType.negotiation_email: (
        "Reply to the price-change notice. Ask to keep the current price or discuss options "
        "before the new price takes effect."
    ),
    ActionType.billing_dispute: (
        "Dispute the charges: explain what the agreement allows, what was billed and the "
        "overcharge, and ask for a credit and a corrected bill going forward."
    ),
}


def user_prompt(
    finding: Finding, plan: ActionPlan, facts: Facts, problems: Sequence[str] = (), attempt: int = 1
) -> str:
    """`attempt` and `problems` appear only on a retry, so the first request stays the same. The
    attempt number keeps a retry with the same problems from replaying the last cached answer."""
    lines = [
        f"Action: {plan.type.value}",
        f"Task: {_ASKS[plan.type]}",
        f"Summary (context only; do not copy numbers from it): {finding.title}",
    ]
    if finding.deadline_notes:
        lines.append(f"Deadline notes (context only): {finding.deadline_notes}")
    lines += ["", "Placeholders (name: the value code will fill in):"]
    lines += [f"- {{{k}}}: {v}" for k, v in facts.values.items()]
    lines += ["", "Required placeholders: " + ", ".join(f"{{{r}}}" for r in facts.required)]
    if facts.phrases:
        lines.append("Required words: " + ", ".join(f'"{p}"' for p in facts.phrases))
    if problems:
        lines += ["", f"Attempt {attempt}. Your previous draft had these problems. Fix them:"]
        lines += [f"- {p}" for p in problems]
    return "\n".join(lines)


class LLMDrafter:
    """The `Drafter` for stage 5. `records[finding_id]` keeps each draft's attempt records,
    their problems and whether the fallback template was used, for the eval and for storing the
    raw model output."""

    def __init__(
        self,
        cache: ResponseCache,
        client: anthropic.Anthropic | None = None,
        settings: Settings | None = None,
        sender: str = "",
    ) -> None:
        self.cache = cache
        self.client = client
        self.settings = settings or get_settings()
        self.sender = sender
        self.records: dict[str, dict[str, Any]] = {}

    @property
    def model(self) -> str:
        return self.settings.leo_model_draft

    @property
    def engine(self) -> str:
        return f"llm:{self.model}"

    def draft(self, finding: Finding, ledger: Ledger) -> ActionDraft:
        plan = action_plan(finding)
        facts = build_facts(finding, ledger, self.sender)
        attempts: list[dict[str, Any]] = []
        problems: list[str] = []
        body: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = structured_call(
                cache=self.cache,
                stage=STAGE,
                doc_hash=facts.digest(finding.id, plan.type.value),
                model=self.model,
                prompt_version=PROMPT_VERSION,
                effort=EFFORT,
                system=SYSTEM_PROMPT,
                user=user_prompt(finding, plan, facts, problems, attempt),
                schema=SCHEMA,
                client=self.client,
                api_key=self.settings.anthropic_api_key or None,
                offline=self.settings.leo_llm_offline,
                max_tokens=MAX_TOKENS,
            )
            attempts.append(result.record)
            if result.output is None:  # refused or cut off: not cached, so don't loop on it
                problems = [result.record.get("error", "no complete answer")]
                break
            raw = result.output.get("body")
            problems = check_body(raw, facts)
            if not problems:
                body = render(raw, facts)
                break
        fallback = body is None
        if body is None:
            body = render(template_body(finding, facts), facts)
        self.records[finding.id] = {
            "engine": self.engine,
            "attempts": attempts,
            "problems": problems,
            "fallback": fallback,
        }
        return ActionDraft(
            finding=finding.id,
            type=plan.type,
            to=recipient(finding, ledger, plan.type),
            subject=subject(plan, facts),
            body=body,
            pre_step=plan.pre_step,
            alternative=plan.alternative,
        )


class TemplateDrafter:
    """The same draft with the fallback template and no LLM call."""

    engine = "template"  # stored on the action; the approval queue flags it

    def __init__(self, sender: str = "") -> None:
        self.sender = sender

    def draft(self, finding: Finding, ledger: Ledger) -> ActionDraft:
        plan = action_plan(finding)
        facts = build_facts(finding, ledger, self.sender)
        return ActionDraft(
            finding=finding.id,
            type=plan.type,
            to=recipient(finding, ledger, plan.type),
            subject=subject(plan, facts),
            body=render(template_body(finding, facts), facts),
            pre_step=plan.pre_step,
            alternative=plan.alternative,
        )


# --- No redraft ------------------------------------------------------------------------------


def draft_new(
    drafter: Drafter,
    findings: Sequence[Finding],
    ledger: Ledger,
    existing: Collection[tuple[str, ActionType]],
) -> list[ActionDraft]:
    """Drafts for the findings whose action of their planned type isn't in `existing`: the
    (finding id, action type) pairs not to redraft. The worker passes the actions the user
    decided or edited (`Repo.action_pairs`), so an unedited draft follows its finding."""
    out = []
    for f in findings:
        if (f.id, action_plan(f).type) in existing:
            continue
        out.append(drafter.draft(f, ledger))
    return out
