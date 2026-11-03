"""Data-model rules as pure functions (ROADMAP 0.4, documented in docs/DATA_MODEL.md)."""

import datetime
from pathlib import Path

from leo.types import DisputeWindowValue, NoticeWindowValue, RelativeTo, RenewalValue

ONE_DAY = datetime.timedelta(days=1)


def read_source_text(path: str | Path) -> str:
    """The text citation offsets index: raw bytes as UTF-8, with no newline translation."""
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """The (start, end) span of the quote's first occurrence, end exclusive. None if absent."""
    start = text.find(quote)
    return None if start == -1 else (start, start + len(quote))


def recommended_send_by(deadline: datetime.date) -> datetime.date:
    """Move a Saturday or Sunday deadline back to the Friday before it."""
    return deadline - datetime.timedelta(days=max(0, deadline.weekday() - 4))


def notice_deadline(notice: NoticeWindowValue, renewal: RenewalValue) -> datetime.date:
    """Last day to give notice: counted back from the term end, or from the renewal date."""
    if notice.days is None or renewal.current_term_end is None:
        raise ValueError("notice deadline needs notice days and the current term end")
    end = renewal.current_term_end
    if notice.relative_to == RelativeTo.renewal_date:
        end += ONE_DAY
    return end - datetime.timedelta(days=notice.days)


def dispute_deadline(window: DisputeWindowValue, invoice_date: datetime.date) -> datetime.date:
    """Last day to dispute an invoice."""
    if window.days is None:
        raise ValueError("dispute deadline needs the window's days")
    return invoice_date + datetime.timedelta(days=window.days)


def price_change_deadline(effective_date: datetime.date) -> datetime.date:
    """Last day to act before a new price takes effect."""
    return effective_date - ONE_DAY
