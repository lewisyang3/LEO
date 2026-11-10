"""`.eml` parsing (ROADMAP A3). Pure: no database, no storage.

Headers are decoded with `email.policy.default` (RFC 2047 words become text) and the `Date`
parse is guarded. The body text and headers are metadata only: citation offsets index the raw
stored file text (`leo.rules.read_source_text`), so the pipeline stages keep reading raw text.
`Message-ID` is an attribute, never part of a key (`leo.db.keys`).

Attachments keep their original bytes (`get_payload(decode=True)`, never `get_content()`, which
returns `str` for text parts), so an attached contract hashes the same as the file on its own.
The exception is an attached email (`message/rfc822`): the parser keeps no raw slice, so it is
re-serialized (headers unfolded as sent, CRLF line ends). That is deterministic, but it need not
match the original `.eml` byte for byte, so it may not dedupe against the same file on its own.
Attachments are found at any depth (a signed invoice nests them), and a single-part email
whose only part is an attachment gives that attachment. NUL is stripped from headers and
filenames, since Postgres text columns reject it.
"""

import datetime as dt
import email.policy
import mimetypes
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import PurePath

RFC822 = "message/rfc822"
# Re-serialize an attached email as close to the wire form as the parser allows.
_RFC822_OUT = email.policy.default.clone(refold_source="none", linesep="\r\n")


@dataclass(frozen=True)
class Attachment:
    filename: str  # a bare name, never a path; generated when the part has none
    content_type: str
    raw: bytes


@dataclass(frozen=True)
class ParsedEmail:
    sender: str | None
    recipient: str | None
    subject: str | None
    message_id: str | None
    received_at: dt.datetime | None  # always timezone-aware when set
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_eml(raw: bytes) -> ParsedEmail:
    """Parse an email's raw bytes. Malformed headers, dates or parts never raise."""
    if not raw:
        raise ValueError("empty .eml")
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    return ParsedEmail(
        sender=_header(msg, "From"),
        recipient=_header(msg, "To"),
        subject=_header(msg, "Subject"),
        message_id=_header(msg, "Message-ID"),
        received_at=_date(msg),
        body_text=_body(msg),
        attachments=_attachments(msg),
    )


def _clean(value: str) -> str | None:
    """Strip NUL (Postgres text columns reject it) and surrounding space; blank -> None."""
    return value.replace("\x00", "").strip() or None


def _raw_header(msg: EmailMessage, name: str) -> str | None:
    for key, value in msg.raw_items():
        if key.lower() == name.lower():
            return _clean(str(value))
    return None


def _header(msg: EmailMessage, name: str) -> str | None:
    """The decoded header, or its raw text if the policy can't parse it."""
    try:
        value = msg[name]
    except Exception:  # a header the policy can't parse: keep its raw text
        return _raw_header(msg, name)
    if value is None:
        return None
    return _clean(str(value))


def _date(msg: EmailMessage) -> dt.datetime | None:
    """The `Date` header as an aware datetime; None if missing or malformed. A naive date
    (RFC 5322 `-0000`, or no zone) is taken as UTC."""
    try:
        header = msg["Date"]
        when = getattr(header, "datetime", None)
    except Exception:
        return None
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _strip_html(html: str) -> str:
    p = _TextOnly()
    p.feed(html)
    p.close()
    return "".join(p.parts).strip()


def _text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except Exception:  # unknown charset or bad encoding: decode the bytes leniently
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else ""


def _body(msg: EmailMessage) -> str:
    """The plain-text body, else the HTML body without tags, else ""."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        return ""
    if part is None:
        return ""
    text = _text(part)
    return _strip_html(text) if part.get_content_type() == "text/html" else text


def safe_filename(name: str | None) -> str | None:
    """A bare filename: no NULs, no directories (either separator), no leading dots, no
    blanks."""
    if not name:
        return None
    clean = name.replace("\x00", "").replace("\\", "/")
    bare = PurePath(clean).name.lstrip(".").strip()
    return bare or None


def _attachment_parts(msg: EmailMessage) -> list[EmailMessage]:
    """Every attachment part at any depth, in document order. Recurses into `multipart/*`
    (signed, mixed, related, alternative) but never into an attached `message/rfc822`, which
    is one attachment. The body part is skipped, and so are bare text parts (the body's other
    alternatives) unless marked as an attachment or named."""
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        body = None
    out: list[EmailMessage] = []

    def walk(part: EmailMessage) -> None:
        if part is body:
            return
        if part.get_content_maintype() == "multipart":
            for child in part.iter_parts():
                walk(child)
            return
        if (
            part.get_content_disposition() == "attachment"
            or part.get_filename()
            or part.get_content_type() not in ("text/plain", "text/html")
        ):
            out.append(part)

    try:
        walk(msg)
    except Exception:
        pass  # keep what was found before the bad part
    return out


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    for part in _attachment_parts(msg):
        n = len(out) + 1
        content_type = part.get_content_type()
        try:
            if content_type == RFC822:
                inner = part.get_payload(0)
                raw = (
                    inner.as_bytes(policy=_RFC822_OUT) if isinstance(inner, EmailMessage) else None
                )
                default = f"attached-{n}.eml"
            else:
                raw = part.get_payload(decode=True)
                ext = mimetypes.guess_extension(content_type) or ".bin"
                default = f"attachment-{n}{ext}"
            name = safe_filename(part.get_filename())
        except Exception:
            continue
        if not isinstance(raw, bytes):  # an unreadable part
            continue
        out.append(Attachment(filename=name or default, content_type=content_type, raw=raw))
    return out
