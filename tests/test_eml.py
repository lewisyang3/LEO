"""`.eml` parser (A3.2). Pure: no database."""

import datetime as dt
import email.parser
import email.utils
from email.message import EmailMessage

import pytest

from leo.eval.holdout import HOLDOUT_DIR
from leo.fixtures import DATA_DIR
from leo.ingest.eml import parse_eml

DATASET_EMLS = sorted([*DATA_DIR.glob("inbox/*.eml"), *HOLDOUT_DIR.glob("inbox/*.eml")])


def _eml(headers: dict[str, str], body: str = "Hello.\n") -> bytes:
    lines = [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


@pytest.mark.parametrize("path", DATASET_EMLS, ids=lambda p: p.name)
def test_dataset_emails_parse_like_their_raw_headers(path):
    raw = path.read_bytes()
    plain = email.parser.BytesHeaderParser().parsebytes(raw)  # the dataset is all ASCII
    parsed = parse_eml(raw)
    assert parsed.sender == plain["From"]
    assert parsed.recipient == plain["To"]
    assert parsed.subject == plain["Subject"]
    assert parsed.message_id == plain["Message-ID"]
    assert parsed.received_at == email.utils.parsedate_to_datetime(plain["Date"])
    assert parsed.body_text.strip()
    assert parsed.attachments == []


def test_dataset_is_covered():
    assert len(DATASET_EMLS) == 20


def test_encoded_words_decode():
    parsed = parse_eml(
        _eml(
            {
                "From": "=?utf-8?q?Caf=C3=A9_Billing?= <billing@cafe.example>",
                "Subject": "=?utf-8?b?UHJpY2UgY2hhbmdlIOKAlCBDYWbDqQ==?=",
            }
        )
    )
    assert parsed.sender == "Café Billing <billing@cafe.example>"
    assert parsed.subject == "Price change — Café"


@pytest.mark.parametrize("date", [None, "not a date", "Thu, 99 Foo 2026 25:61:00 +0000"])
def test_missing_or_bad_date_is_none(date):
    headers = {"Subject": "x"} | ({"Date": date} if date else {})
    assert parse_eml(_eml(headers)).received_at is None


def test_unknown_zone_date_is_utc():
    parsed = parse_eml(_eml({"Date": "Thu, 15 Oct 2026 08:41:00 -0000"}))
    assert parsed.received_at == dt.datetime(2026, 10, 15, 8, 41, tzinfo=dt.UTC)


def test_missing_headers_are_none():
    parsed = parse_eml(b"\r\nJust a body.\r\n")
    assert (parsed.sender, parsed.recipient, parsed.subject, parsed.message_id) == (None,) * 4
    assert parsed.body_text == "Just a body.\r\n"


def test_empty_bytes_raise():
    with pytest.raises(ValueError):
        parse_eml(b"")


def test_html_only_body_is_stripped():
    msg = EmailMessage()
    msg["Subject"] = "html"
    msg.set_content("<p>Price is <b>$339</b></p><style>p{}</style>", subtype="html")
    assert parse_eml(msg.as_bytes()).body_text == "Price is $339"


def _with_attachments() -> tuple[bytes, bytes, bytes, EmailMessage]:
    pdf = b"%PDF-1.4\n\x00\xff binary"
    md = "# Agreement\n\nFee: $339/month — café\n".encode()
    inner = EmailMessage()
    inner["From"] = "billing@lumenpost.example"
    inner["Subject"] = "Original"
    inner.set_content("The forwarded original.\n")
    msg = EmailMessage()
    msg["From"] = "sam@brightwater.example"
    msg["Subject"] = "Fwd: docs"
    msg.set_content("See attached.\n")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="../../order.pdf")
    msg.add_attachment(md, maintype="text", subtype="markdown", filename="terms.md")
    msg.add_attachment(inner)
    msg.add_attachment(b"no name", maintype="application", subtype="octet-stream")
    return pdf, md, msg.as_bytes(), inner


def test_attachments_keep_original_bytes():
    pdf, md, raw, inner = _with_attachments()
    parsed = parse_eml(raw)
    assert parsed.body_text.strip() == "See attached."
    a = parsed.attachments
    assert [(x.filename, x.content_type) for x in a] == [
        ("order.pdf", "application/pdf"),  # directories stripped
        ("terms.md", "text/markdown"),
        ("attached-3.eml", "message/rfc822"),
        ("attachment-4.bin", "application/octet-stream"),
    ]
    assert a[0].raw == pdf
    assert a[1].raw == md  # bytes, not str: hashes the same as the file on its own
    assert parse_eml(a[2].raw).subject == "Original"
    assert a[3].raw == b"no name"
    assert parse_eml(raw) == parsed  # deterministic


def test_nested_attachments_are_found():
    """A signed invoice: multipart/signed > multipart/mixed > [text, invoice.pdf], + sig."""
    inner = EmailMessage()
    inner.set_content("Invoice attached.\n")
    inner.add_attachment(
        b"%PDF nested", maintype="application", subtype="pdf", filename="invoice.pdf"
    )
    signed = EmailMessage()
    signed["Subject"] = "Signed invoice"
    signed.make_mixed()
    signed.set_type("multipart/signed")
    signed.set_payload([inner])
    sig = EmailMessage()
    sig.set_content(b"sig", maintype="application", subtype="pkcs7-signature", filename="smime.p7s")
    signed.attach(sig)
    parsed = parse_eml(signed.as_bytes())
    assert parsed.body_text.strip() == "Invoice attached."
    assert [(a.filename, a.raw) for a in parsed.attachments] == [
        ("invoice.pdf", b"%PDF nested"),
        ("smime.p7s", b"sig"),
    ]


def test_alternative_body_is_not_an_attachment():
    msg = EmailMessage()
    msg.set_content("Plain body.\n")
    msg.add_alternative("<p>HTML body.</p>", subtype="html")
    msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="a.pdf")
    parsed = parse_eml(msg.as_bytes())
    assert parsed.body_text.strip() == "Plain body."
    assert [a.filename for a in parsed.attachments] == ["a.pdf"]


def test_single_part_attachment_email():
    raw = (
        b"Subject: scan\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=x.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0x\r\n"
    )
    parsed = parse_eml(raw)
    assert parsed.body_text == ""
    assert [(a.filename, a.raw) for a in parsed.attachments] == [("x.pdf", b"%PDF-1")]


def test_nul_is_stripped_from_headers_and_filenames():
    raw = (
        b"Subject: hi\x00there\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nBody.\r\n"
        b"--b\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="ev\x00il.pdf"\r\n\r\n%PDF\r\n'
        b"--b--\r\n"
    )
    parsed = parse_eml(raw)
    assert parsed.subject == "hithere"
    assert [a.filename for a in parsed.attachments] == ["evil.pdf"]


def test_attached_email_keeps_its_header_lines():
    """An attached `message/rfc822` is re-serialized: long headers aren't refolded and lines
    end in CRLF, so it stays as close to the original `.eml` as the parser allows."""
    subject = "Price change " + "x" * 90  # longer than the 78-column fold
    inner = f"From: a@b.example\r\nSubject: {subject}\r\n\r\nhello\r\n".encode()
    outer = (
        b"From: x@y.example\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=BB\r\n\r\n"
        b"--BB\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--BB\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment; filename=fw.eml\r\n\r\n" + inner + b"--BB--\r\n"
    )
    (att,) = parse_eml(outer).attachments
    assert att.raw == inner.removesuffix(b"\r\n")  # the boundary's CRLF belongs to the boundary
    assert parse_eml(att.raw).subject == subject
    assert parse_eml(outer).attachments[0].raw == att.raw  # deterministic
