"""Natural-key builders (docs/DATA_MODEL.md).

Every key carries the `v1:` scheme prefix. Child keys embed the parent's key, so a wiped
database rebuilds with the same keys. Email keys never use the Message-ID, sender, subject or
any extracted value; spans come from `leo.rules.span_key`.
"""

import hashlib
import re
from pathlib import PurePath

PREFIX = "v1"
EML_SUFFIX = ".eml"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def gmail(account_id: str, message_id: str) -> str:
    """`account_id` is our own connected-account id, never the email address."""
    return f"{PREFIX}:gmail:{account_id}:{message_id}"


def eml_hash(raw: bytes) -> str:
    return f"{PREFIX}:eml-hash:{_sha256(raw)}"


def file(raw: bytes) -> str:
    return f"{PREFIX}:file:{_sha256(raw)}"


def is_eml(path: str | PurePath) -> bool:
    return PurePath(path).suffix.lower() == EML_SUFFIX


def document_key(path: str | PurePath, raw: bytes) -> str:
    """An uploaded or synthetic `.eml` keys on its bytes as an email; anything else as a file."""
    return eml_hash(raw) if is_eml(path) else file(raw)


def term(doc_key: str, term_type: str, span: str) -> str:
    return f"{PREFIX}:term:{doc_key}:{term_type}:{span}"


def invoice_fact(doc_key: str, span: str) -> str:
    return f"{PREFIX}:invoice-fact:{doc_key}:{span}"


def finding(anchor_term_key: str, anchor_txn_key: str | None, kind: str) -> str:
    return f"{PREFIX}:finding:{anchor_term_key}:{anchor_txn_key or '-'}:{kind}"


def txn(source: str, external_id: str) -> str:
    return f"{PREFIX}:txn:{source}:{external_id}"


def job(doc_key: str, stage: str) -> str:
    return f"{PREFIX}:job:{doc_key}:{stage}"


def tenant_job(tenant_id: str, stage: str) -> str:
    """A job that runs over the whole tenant (2.1's reconcile), not one document."""
    return f"{PREFIX}:job:tenant:{tenant_id}:{stage}"


def action(finding_key: str, action_type: str) -> str:
    return f"{PREFIX}:action:{finding_key}:{action_type}"


_NON_ALNUM = re.compile(r"[\W_]+")  # Unicode-aware, so "Café" keeps its é


def normalize_name(name: str) -> str:
    """Vendor name or alias for uniqueness: casefold, `&` -> `and`, punctuation runs -> one
    space. "Amazon Web Services, Inc." and "amazon web services inc" are equal."""
    folded = name.casefold().replace("&", " and ")
    return _NON_ALNUM.sub(" ", folded).strip()
