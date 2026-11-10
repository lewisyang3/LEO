"""Stage 2: script extractors (ROADMAP B5).

A script is a matcher and parser pair for one known document format, such as a vendor's
invoice template. Scripts are deterministic and make no API call. The worker tries them before
the LLM extractor (stage 3), so a document in a known format never reaches the LLM.

`ScriptExtractor` holds the registry (`SCRIPTS`) and runs the first script that matches. It
returns None ("not mine") when no script matches. It also returns None when a script matches
but can't parse the document (the template changed), so the document falls through to the LLM
instead of getting a wrong extraction. The failure is kept in `records`.

`ChainExtractor` returns the first extractor's answer that isn't None, the same order the
worker's extract loop uses. The eval uses it for "scripts, then the LLM".

The registry lives in code. Storing scripts in `extractor_script`, and generating or promoting
them, is the design doc's script generation (Phase 4).
"""

import datetime
import email.utils
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from leo.interfaces import DocumentText, Extraction, Extractor
from leo.pipeline.extract_llm import normalize_billing_period
from leo.types import InvoiceFact


class ParseError(ValueError):
    """A matched document doesn't fit the script's template."""


@dataclass(frozen=True)
class Script:
    name: str
    version: str
    matches: Callable[[DocumentText], bool]
    parse: Callable[[DocumentText], Extraction]

    @property
    def engine(self) -> str:
        return f"script:{self.name}@{self.version}"


class ScriptExtractor:
    """Stage 2. The first matching script parses the document."""

    engine = "script"

    def __init__(self, scripts: Sequence[Script]) -> None:
        self.scripts = list(scripts)
        self.records: dict[str, dict[str, Any]] = {}  # path -> parse failure

    def extract(self, doc: DocumentText) -> Extraction | None:
        for script in self.scripts:
            if not script.matches(doc):
                continue
            try:
                ex = script.parse(doc)
            except (ParseError, ValueError, ArithmeticError) as e:
                self.records[doc.document.path] = {
                    "script": script.engine,
                    "error": f"{type(e).__name__}: {e}",
                }
                return None
            raw = {**(ex.raw_output or {}), "script": script.name, "version": script.version}
            return ex.model_copy(update={"engine": script.engine, "raw_output": raw})
        return None


class ChainExtractor:
    """The first answer that isn't None, trying each extractor in order."""

    def __init__(self, extractors: Sequence[Extractor]) -> None:
        self.extractors = list(extractors)

    def extract(self, doc: DocumentText) -> Extraction | None:
        for x in self.extractors:
            if (ex := x.extract(doc)) is not None:
                return ex
        return None


# --- Helpers ---------------------------------------------------------------------------------


def _header(text: str, name: str) -> str | None:
    """The first value of a header line, from the header block (before the first blank line)."""
    head = text.split("\n\n", 1)[0]
    m = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", head, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else None


def _body(text: str) -> str:
    return text.split("\n\n", 1)[1] if "\n\n" in text else ""


def _line(text: str, pattern: str) -> re.Match[str]:
    """The match for a whole line. Its group(0) is the line verbatim, for a quote."""
    m = re.search(pattern, text, re.MULTILINE)
    if m is None:
        raise ParseError(f"no line matches {pattern!r}")
    return m


def _date(s: str) -> datetime.date:
    """ "September 1, 2026" """
    try:
        return datetime.datetime.strptime(s.strip(), "%B %d, %Y").date()
    except ValueError as e:
        raise ParseError(f"bad date {s!r}") from e


def _money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _period(start: str, end: str) -> str:
    return normalize_billing_period(f"{_date(start).isoformat()}/{_date(end).isoformat()}") or ""


def _first_body_line(text: str) -> str:
    for line in _body(text).splitlines():
        if line.strip():
            return line.strip()
    raise ParseError("empty body")


def _from_address(doc: DocumentText) -> str:
    sender = _header(doc.text, "From") or ""
    return email.utils.parseaddr(sender)[1].lower()


def _extraction(doc: DocumentText, script: str, vendor: str, fact: dict[str, Any]) -> Extraction:
    fields = {k: str(v) for k, v in fact.items() if k != "quotes" and v is not None}
    return Extraction(
        vendor_name=vendor,
        invoice_facts=[
            InvoiceFact(id="f1", vendor=vendor, document=doc.document.path, **fact),
        ],
        engine=script,  # replaced by ScriptExtractor with "script:<name>@<version>"
        raw_output={"script": script, "fields": fields},
    )


# --- Kestrel Cloud Hosting invoice -----------------------------------------------------------

KESTREL_SENDER = "billing@kestrelcloud.example"
_MONTH_DATE = r"[A-Z][a-z]+ \d{1,2}, \d{4}"


def _kestrel_matches(doc: DocumentText) -> bool:
    return _from_address(doc) == KESTREL_SENDER and bool(
        re.search(r"^Invoice: INV-KCH-\S+$", _body(doc.text), re.MULTILINE)
    )


def _kestrel_parse(doc: DocumentText) -> Extraction:
    body = _body(doc.text)
    vendor = _first_body_line(doc.text)
    if vendor != "Kestrel Cloud Hosting, LLC":
        raise ParseError(f"unexpected vendor line {vendor!r}")
    number = _line(body, r"^Invoice: (INV-KCH-\S+)$").group(1)
    date_line = _line(body, rf"^Invoice date: ({_MONTH_DATE})$")
    period = _line(body, rf"^Billing period: ({_MONTH_DATE}) - ({_MONTH_DATE})$")
    total = _line(body, r"^Total due: \$([\d,]+\.\d{2})$")
    # The line item: a description, then its amount, which must equal the total.
    item = _line(body, r"^(\S.*?) {2,}\$([\d,]+\.\d{2})$")
    if _money(item.group(2)) != _money(total.group(1)):
        raise ParseError("line item doesn't equal the total")
    return _extraction(
        doc,
        "kestrel-invoice",
        vendor,
        {
            "invoice_number": number,
            "invoice_date": _date(date_line.group(1)),
            "billing_period": _period(period.group(1), period.group(2)),
            "amount": _money(total.group(1)),
            "quotes": [item.group(0), date_line.group(0)],
        },
    )


KESTREL_INVOICE = Script("kestrel-invoice", "1", _kestrel_matches, _kestrel_parse)


# --- Quillstack receipt ----------------------------------------------------------------------

QUILLSTACK_SENDER = "billing@quillstack.example"


def _quillstack_matches(doc: DocumentText) -> bool:
    body = _body(doc.text)
    return (
        _from_address(doc) == QUILLSTACK_SENDER
        and bool(re.search(r"^Receipt for ", body, re.MULTILINE))
        and bool(re.search(r"^Order Form: QS-OF-\S+$", body, re.MULTILINE))
    )


def _quillstack_parse(doc: DocumentText) -> Extraction:
    body = _body(doc.text)
    vendor = _first_body_line(doc.text)
    if vendor != "Quillstack Inc.":
        raise ParseError(f"unexpected vendor line {vendor!r}")
    # The receipt has no invoice-date line, so the date comes from the email's Date header.
    date_line = _line(doc.text.split("\n\n", 1)[0], r"^Date: (.+)$")
    try:
        sent = email.utils.parsedate_to_datetime(date_line.group(1)).date()
    except (TypeError, ValueError) as e:
        raise ParseError(f"bad Date header {date_line.group(1)!r}") from e
    period = _line(body, rf"^Service period: ({_MONTH_DATE}) - ({_MONTH_DATE})$")
    item = _line(
        body, r"^Quillstack \S.*? - (\d+) seats x \$([\d,]+\.\d{2}) {2,}\$([\d,]+\.\d{2})$"
    )
    seats, per_seat, amount = int(item.group(1)), _money(item.group(2)), _money(item.group(3))
    if seats * per_seat != amount:
        raise ParseError("seats x price doesn't equal the line amount")
    charged = _line(body, r"^Amount charged: \$([\d,]+\.\d{2})\b")
    if _money(charged.group(1)) != amount:
        raise ParseError("line amount doesn't equal the amount charged")
    return _extraction(
        doc,
        "quillstack-receipt",
        vendor,
        {
            "invoice_date": sent,
            "billing_period": _period(period.group(1), period.group(2)),
            "amount": amount,
            "seats_billed": seats,
            "quotes": [item.group(0), date_line.group(0)],
        },
    )


QUILLSTACK_RECEIPT = Script("quillstack-receipt", "1", _quillstack_matches, _quillstack_parse)


# --- Registry --------------------------------------------------------------------------------

SCRIPTS: list[Script] = [KESTREL_INVOICE, QUILLSTACK_RECEIPT]


def script_extractor() -> ScriptExtractor:
    return ScriptExtractor(SCRIPTS)
