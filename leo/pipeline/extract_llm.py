"""LLM extraction, stage 3 (ROADMAP B3).

`LLMExtractor` sends one document to Claude (Haiku 5.5 by default, `LEO_MODEL_EXTRACT`) through
the response cache in `leo.llm`. The API caps how large a structured-output schema may be, so it
makes three requests per document, one per part in `EXTRACTION_PARTS`, with the same system
prompt, and merges the answers. It maps them to the shared types, then fills in what one
document can't say by itself:
- `renewal.initial_term_end` from a stated start date and length
- `renewal.current_term_end`, rolled forward to "today" for auto-renewing terms
- `notice_window.next_deadline`, from `leo.rules.notice_deadline`
- `escalator.first_application`, from an anniversary base date

The cache holds the model's answer from before this step, so a new "today" never invalidates it.
"""

import calendar
import datetime
import hashlib
import re
from collections import Counter
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import TypeAdapter, ValidationError

from leo.config import Settings, get_settings
from leo.interfaces import Clock, DocumentText, Extraction
from leo.llm import ResponseCache, json_schema_for, structured_call
from leo.pipeline.extract_schema import (
    EXTRACTION_PARTS,
    TERM_LISTS,
    LLMContact,
    LLMEscalatorTerm,
    LLMInvoiceFact,
    LLMNoticeWindowTerm,
    LLMPrice,
    LLMPriceTerm,
    LLMRenewalTerm,
    LLMTerm,
)
from leo.rules import notice_deadline
from leo.types import (
    Contact,
    ExtractedTerm,
    InvoiceFact,
    NoticeWindowValue,
    RelativeTo,
    RenewalValue,
    TermType,
    extracted_term_adapter,
)

STAGE = "extract"
PROMPT_VERSION = "extract-v3"
EFFORT = "medium"
SCHEMAS = {part: json_schema_for(model) for part, (model, _) in EXTRACTION_PARTS.items()}

# Only these term types carry a plan label, and these never carry an effective date. Both are
# the answer key's conventions (data/synthetic/README.md).
PLAN_LABEL_TYPES = {TermType.price, TermType.seat_count}
NO_EFFECTIVE_DATE_TYPES = {TermType.notice_window, TermType.dispute_window}

SYSTEM_PROMPT = """\
You extract vendor contract terms and billing facts from one business document for a small \
company's accounts-payable team. The document is a contract, an order form, a vendor price \
notice, an invoice or a receipt. The team uses your output to catch auto-renewal deadlines, \
price increases, overbilling against contract caps and seat counts, and early price escalators, \
so every value must be exactly what the document says, backed by a quote.

Return JSON matching the schema. Use null for anything the document does not state. Never guess \
or fill in a typical value.

## Quotes
Every term, invoice fact and contact needs quotes copied from the document exactly: the same \
characters, punctuation, capitalization and spacing, as one contiguous span. Never paraphrase, \
shorten with an ellipsis, join two separate lines into one quote, or fix typos. Put the most \
specific sentence or line first, the one that states the value itself. Add further quotes only \
when another line supplies part of the value (for example an invoice-date line or a notice \
address).

## Vendor
vendor_name is the vendor's name copied character for character as it appears in the document \
(letterhead, signature block, or the sender's display name), for example "Acme Widgets, Inc.". \
The customer is never the vendor. In a forwarded email, take the vendor from the forwarded \
message's header block, not from the colleague who forwarded it.

## Terms
One term per distinct commitment, by type:
- price: a recurring price the vendor and customer agreed for a plan or product. per is year, \
month or seat_month (a price per seat or user per month). currency is the ISO 4217 code, so "USD" \
for amounts in dollars ($). Give each plan or product its own price term; never merge plans. A \
per-unit overage charge belongs in seat_count.overage_per_unit_month, not in a price term.
- price_cap: a limit on how much a price may increase (max_increase_pct, what it applies_to, and \
the baseline it is measured against).
- escalator: a scheduled automatic increase (pct, schedule). Set first_application only when the \
document states the date outright. When the increase happens on each anniversary of a named \
date, put that date in anniversary_base_date and leave first_application null.
- renewal: the term length and how it renews. initial_term_end only when an end date is written. \
When the length is written instead ("24 months from the start date"), give term_start and \
initial_term_months. renewal_length_months is the length of each renewal term. A month-to-month \
subscription renews automatically: auto_renew true and renewal_length_months 1.
- notice_window: notice the customer must give (non_renewal, relative to term_end or \
renewal_date) or that the vendor must give before a price change (vendor_price_change, relative \
to price_change). deliver_to is the address notice must go to, if stated.
- seat_count: committed seats, users or employees (included, unit), any overage price, and what \
an increase requires.
- dispute_window: how long the customer has to dispute an invoice (relative to invoice_date) \
and where disputes go.

Short text fields (baseline, schedule, renewal_pricing, increase_requires, \
early_termination_fee) are the shortest phrase from the document that states the value, copied \
word for word. Leave out leading words that only connect the phrase to its sentence: articles \
("the", "a"), prepositions ("on", "at") and the thing being measured ("fees for", "prices \
for"). Examples:
- "renews at the vendor's then-current list price for the Premium plan": renewal_pricing is \
"then-current list price".
- "may not increase by more than 3% over the fees for the prior contract year": baseline is \
"prior contract year".
- "on each anniversary of the Go-Live Date": schedule is "each anniversary of the Go-Live Date".
- "renews at the same Subscription Fee": renewal_pricing is "same Subscription Fee".

plan_label is the plan or product name as written, dropping only a trailing word "plan" or \
"tier" and any description after the name. Keep every other word, such as "add-on" or \
"bundle". Examples: "Premium plan (up to 10 users)" is "Premium"; "Acme Analytics, Pro tier" is \
"Pro"; "SMS Alerts add-on" stays "SMS Alerts add-on"; "Hosting - Basic" stays "Hosting - Basic". \
Use it only on \
price and seat_count terms, and only when the document names a plan or product; a generic fee \
name like "Monthly Fee" is not a plan.

effective_date is the date the term takes effect: the agreement's effective date, service start \
date or order date. For a new price in a price notice, it is the date the new price starts.

agreement_ref is the agreement, order form or MSA number the term comes from, if written.

## Price notices
A price notice gives each affected plan a price term with the new price in value and the \
previous price in old_value. If the notice restates a plan whose price is not changing, give \
that plan its own price term too, with old_value equal to value and the same effective_date as \
the change. Keep plans separate even when they appear in the same sentence.

## Invoices and receipts
Give one invoice fact per invoice or receipt:
- amount: the total billed.
- invoice_date: from the invoice-date line, or else from the email's Date header.
- billing_period: "YYYY-MM-DD/YYYY-MM-DD" when a service period is written, otherwise the \
invoice date's month as "YYYY-MM".
- invoice_number: only a value the document labels as an invoice or statement number, never an \
order form, agreement or account number.
- seats_billed: only when a billed line item is a quantity of seats or users times a unit price. \
A headcount mentioned for information is not seats billed.
An invoice or receipt records what was billed, not what was agreed. Its line items, totals, \
service periods and rate adjustments are invoice facts only: never turn them into price, \
renewal, escalator or seat_count terms. Extract a term from an invoice only when it states a \
contractual rule in words, such as "disputes must be raised within 30 days".

## Contacts
A contact is a vendor email address the body text invites the customer to write to with \
questions, for example "Billing questions may be sent to billing@acme.example": purpose is one \
lowercase word such as billing or support. Do not list:
- an address that is a term's deliver_to (where notices or disputes must be sent); it belongs \
in that term
- addresses that appear only in email headers (From, To, Reply-To)
- the customer's own addresses or no-reply senders
"""


def _user_prompt(doc: DocumentText, asks: str) -> str:
    doc_class = doc.document.doc_class.value if doc.document.doc_class else "unknown"
    return (
        f"Document class: {doc_class}\n\n"
        "<document>\n"
        f"{doc.text}\n"
        "</document>\n\n"
        f"Extract the vendor name and, from this document, only its {asks}. Other kinds of item "
        "are extracted separately. Return empty lists when the document has none."
    )


def merge_parts(outputs: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """One answer from the parts: each part's own lists, terms tagged with their type, and the
    vendor name most parts agree on (ties go to the earlier part). Also returns the distinct
    vendor names given, so a disagreement shows in `raw_output`."""
    terms: list[dict[str, Any]] = []
    facts: list[Any] = []
    contacts: list[Any] = []
    names: list[str] = []
    for part, (model, _) in EXTRACTION_PARTS.items():
        out = outputs[part]
        if name := out.get("vendor_name"):
            names.append(name)
        for key in model.model_fields:
            items = out.get(key) or []
            if key in TERM_LISTS:
                terms += [{**x, "type": TERM_LISTS[key]} for x in items if isinstance(x, dict)]
            elif key == "invoice_facts":
                facts += items
            elif key == "contacts":
                contacts += items
    counts = Counter(names)
    vendor = max(names, key=lambda n: (counts[n], -names.index(n))) if names else None
    merged = {"vendor_name": vendor, "terms": terms, "invoice_facts": facts, "contacts": contacts}
    return merged, sorted(counts)


# --- Field normalization ---------------------------------------------------------------------

_REF_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def agreement_ref(ref: str | None) -> str | None:
    """The agreement's identifier alone: "MSA No. KCH-MSA-2209" becomes "KCH-MSA-2209". The
    reconciler joins terms on an exact `agreement_ref` (F3's cap and renewal), so a label the
    model sometimes writes would split one agreement in two (2.1 review). The last token with
    a digit is the identifier; a ref with none is kept as written, stripped."""
    if ref is None:
        return None
    ids = [tok for tok in _REF_TOKEN.findall(ref) if any(c.isdigit() for c in tok)]
    return ids[-1] if ids else (ref.strip() or None)


_LABELLED_REF = re.compile(
    r"\b(?:Agreement|Order\s+Form|MSA|Contract|Account)\s+(?:No\.?|Number|ID|#)\s*[:#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9-]*\d[A-Za-z0-9-]*)",
    re.IGNORECASE,
)


def labelled_ref(text: str) -> str | None:
    """The document's one labelled agreement or account number ("Account ID: LP-204417"), for
    terms the model gave no `agreement_ref` (2.3). The prompt asks for an agreement, order form
    or MSA number, so the model leaves an account number out, and the draft then can't cite it.
    Invoice numbers aren't labels here. None when the document has no such label, or two
    different ids."""
    ids = {m.group(1).rstrip("-") for m in _LABELLED_REF.finditer(text)}
    return ids.pop() if len(ids) == 1 else None


# --- Date helpers ----------------------------------------------------------------------------


def add_months(d: datetime.date, months: int) -> datetime.date:
    """`d` plus whole months, with the day clamped to the end of a shorter month."""
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return datetime.date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


ONE_DAY = datetime.timedelta(days=1)

# Month counts from the model outside 1..MAX_MONTHS (50 years) are treated as unknown, so a bad
# answer can't overflow the date math or roll a term backwards.
MAX_MONTHS = 600


def _months_ok(months: int | None) -> bool:
    return months is not None and 1 <= months <= MAX_MONTHS


def initial_term_end(value: RenewalValue, start: datetime.date | None, months: int | None):
    """The stated end, or `start + months - 1 day` when only a length is stated."""
    if value.initial_term_end is not None:
        return value.initial_term_end
    if start is not None and _months_ok(months):
        return add_months(start, months) - ONE_DAY  # type: ignore[arg-type]
    return None


def current_term_end(value: RenewalValue, today: datetime.date) -> datetime.date | None:
    """An auto-renewing term rolls forward by its renewal length until it ends today or later.
    Any other term ends at its initial end."""
    end = value.initial_term_end
    if end is None or value.auto_renew is not True or not _months_ok(value.renewal_length_months):
        return end
    while end < today:
        end = add_months(end + ONE_DAY, value.renewal_length_months) - ONE_DAY
    return end


def next_deadline(notice: NoticeWindowValue, renewal: RenewalValue | None):
    if renewal is None or notice.relative_to not in (RelativeTo.term_end, RelativeTo.renewal_date):
        return None
    try:
        return notice_deadline(notice, renewal)
    except (ValueError, OverflowError):  # missing inputs, or dates out of range
        return None


def normalize_billing_period(period: str | None) -> str | None:
    """A span covering exactly one calendar month becomes "YYYY-MM", as in the answer key."""
    if not period or "/" not in period:
        return period
    try:
        start, end = (datetime.date.fromisoformat(p.strip()) for p in period.split("/", 1))
    except ValueError:
        return period
    last = calendar.monthrange(start.year, start.month)[1]
    if start.day == 1 and end == start.replace(day=last):
        return f"{start.year:04d}-{start.month:02d}"
    return period


# --- Mapping ---------------------------------------------------------------------------------


def _currency(c: str | None) -> str | None:
    """The ISO code for a dollar sign, which the model sometimes copies as written."""
    return "USD" if c is not None and c.strip().upper() in {"$", "US$", "USD"} else c


def _money(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def _price(p: LLMPrice | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {"amount": _money(p.amount), "currency": _currency(p.currency), "per": p.per}


def _term_value(t: LLMTerm) -> dict[str, Any]:
    """The shared value shape for a term. Derived dates are filled in later."""
    v = t.value.model_dump()
    match t:
        case LLMPriceTerm():
            return _price(t.value)  # type: ignore[return-value]
        case LLMRenewalTerm():
            for helper in ("term_start", "initial_term_months"):
                v.pop(helper)
            v["current_term_end"] = None
        case LLMEscalatorTerm():
            v.pop("anniversary_base_date")
            if v["first_application"] is None and t.value.anniversary_base_date is not None:
                v["first_application"] = add_months(t.value.anniversary_base_date, 12)
        case LLMNoticeWindowTerm():
            v["next_deadline"] = None
    for k in ("max_increase_pct", "pct", "overage_per_unit_month"):
        if k in v:
            v[k] = _money(v[k])
    return v


_term_adapter: TypeAdapter[LLMTerm] = TypeAdapter(LLMTerm)


class LLMExtractor:
    """The `Extractor` for stage 3. It always returns an `Extraction`. When the model refuses,
    is cut off or returns invalid JSON, the extraction is empty with `vendor_name=None`, so
    validation fails closed, and `raw_output` says why."""

    def __init__(
        self,
        clock: Clock,
        cache: ResponseCache,
        client: anthropic.Anthropic | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.clock = clock
        self.cache = cache
        self.client = client
        self.settings = settings or get_settings()

    @property
    def model(self) -> str:
        return self.settings.leo_model_extract

    @property
    def engine(self) -> str:
        return f"llm:{self.model}"

    def extract(self, doc: DocumentText) -> Extraction:
        """Three requests, one per part. If any part has no complete answer, the extraction is
        empty with `vendor_name=None` (fail closed). The parts that did answer stay cached, so
        a rerun repeats only the failed request."""
        doc_hash = doc.document.sha256 or hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
        records: dict[str, dict[str, Any]] = {}
        outputs: dict[str, dict[str, Any]] = {}
        for part, (_, asks) in EXTRACTION_PARTS.items():
            result = structured_call(
                cache=self.cache,
                stage=STAGE,
                doc_hash=doc_hash,
                model=self.model,
                prompt_version=PROMPT_VERSION,
                effort=EFFORT,
                system=SYSTEM_PROMPT,
                user=_user_prompt(doc, asks),
                schema=SCHEMAS[part],
                client=self.client,
                api_key=self.settings.anthropic_api_key or None,
                offline=self.settings.leo_llm_offline,
            )
            records[part] = result.record
            if result.output is not None:
                outputs[part] = result.output
        raw: dict[str, Any] = {"parts": records}
        failed = [p for p in EXTRACTION_PARTS if p not in outputs]
        if failed:
            raw["error"] = "no complete answer for part(s): " + ", ".join(failed)
            return Extraction(vendor_name=None, engine=self.engine, raw_output=raw)
        merged, names = merge_parts(outputs)
        if len(names) > 1:
            raw["vendor_names"] = names
        return self.to_extraction(doc, merged, raw)

    def to_extraction(
        self, doc: DocumentText, output: dict[str, Any], raw: dict[str, Any]
    ) -> Extraction:
        """Map the model's JSON to the shared types. An item that fails to validate is left
        out, and its error is recorded in `raw_output["item_errors"]`."""
        path = doc.document.path
        vendor_name = output.get("vendor_name") or None
        vendor = vendor_name or ""
        errors: list[str] = []

        def attempt(kind: str, i: int, build):
            try:
                return build()
            except (ValidationError, ValueError, TypeError, OverflowError) as e:
                errors.append(f"{kind}[{i}]: {e}")
                return None

        llm_terms = [
            t
            for i, x in enumerate(output.get("terms") or [])
            if (t := attempt("terms", i, lambda x=x: _term_adapter.validate_python(x)))
        ]

        terms: list[ExtractedTerm] = []
        for i, t in enumerate(llm_terms):

            def build(t=t, i=i) -> ExtractedTerm:
                ttype = TermType(t.type)
                old = _price(t.old_value) if isinstance(t, LLMPriceTerm) else None
                term = extracted_term_adapter.validate_python(
                    {
                        "id": f"t{i + 1}",
                        "vendor": vendor,
                        "document": path,
                        "type": ttype,
                        "value": _term_value(t),
                        "quotes": t.quotes,
                        "plan_label": t.plan_label if ttype in PLAN_LABEL_TYPES else None,
                        "agreement_ref": agreement_ref(t.agreement_ref),
                        "effective_date": (
                            None if ttype in NO_EFFECTIVE_DATE_TYPES else t.effective_date
                        ),
                        "old_value": old,
                    }
                )
                if isinstance(t, LLMRenewalTerm):
                    term.value.initial_term_end = initial_term_end(
                        term.value, t.value.term_start, t.value.initial_term_months
                    )
                return term

            if (term := attempt("terms", i, build)) is not None:
                terms.append(term)

        self._fill_derived_dates(terms)
        if any(t.agreement_ref is None for t in terms) and (ref := labelled_ref(doc.text)):
            for t in terms:
                t.agreement_ref = t.agreement_ref or ref

        facts: list[InvoiceFact] = []
        for i, x in enumerate(output.get("invoice_facts") or []):

            def build_fact(x=x, i=i) -> InvoiceFact:
                f = LLMInvoiceFact.model_validate(x)
                return InvoiceFact(
                    id=f"f{len(facts) + 1}",
                    vendor=vendor,
                    document=path,
                    billing_period=normalize_billing_period(f.billing_period),
                    invoice_date=f.invoice_date,
                    amount=_money(f.amount),
                    quotes=f.quotes,
                    invoice_number=f.invoice_number,
                    seats_billed=f.seats_billed,
                )

            if (fact := attempt("invoice_facts", i, build_fact)) is not None:
                facts.append(fact)

        contacts: list[Contact] = []
        for i, x in enumerate(output.get("contacts") or []):

            def build_contact(x=x) -> Contact:
                c = LLMContact.model_validate(x)
                return Contact(email=c.email, purpose=c.purpose, document=path, quote=c.quote)

            if (contact := attempt("contacts", i, build_contact)) is not None:
                contacts.append(contact)

        if errors:
            raw["item_errors"] = errors
        return Extraction(
            vendor_name=vendor_name,
            terms=terms,
            invoice_facts=facts,
            contacts=contacts,
            engine=self.engine,
            raw_output=raw,
        )

    def _fill_derived_dates(self, terms: list[ExtractedTerm]) -> None:
        """Current term ends and notice deadlines, which depend on today. A notice window uses
        the first renewal term from the same document."""
        today = self.clock.today()
        renewal: RenewalValue | None = None
        for t in terms:
            if isinstance(t.value, RenewalValue):
                t.value.current_term_end = current_term_end(t.value, today)
                renewal = renewal or t.value
        for t in terms:
            if isinstance(t.value, NoticeWindowValue):
                t.value.next_deadline = next_deadline(t.value, renewal)
