"""Name cleanup for vendor matching. Pure functions, no database.

A card descriptor ("PAYWAVE* LUMENPOST", "KESTREL*CLOUD 844-555-0142") is cleaned before
lookup. Names and aliases are compared in their `match_form`: `keys.normalize_name` without
trailing legal-entity suffixes, so "Halcyon Payroll Services, Inc." and "HALCYON PAYRL SVCS"
are compared as "halcyon payroll services" and "halcyon payrl svcs".
"""

import re

from leo.db.keys import normalize_name

# Payment-processor prefixes that come before the merchant's name. Any other `*` separates
# words ("KESTREL*CLOUD").
_PROCESSOR_PREFIX = re.compile(r"^\s*(?:PAYWAVE|SQ|TST|PAYPAL|SP)\s*\*\s*", re.IGNORECASE)
_PHONE = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")
_STORE_NUMBER = re.compile(r"#\s*\d+")
_SPACES = re.compile(r"\s+")

LEGAL_SUFFIXES = ("inc", "llc", "ltd", "corp", "corporation", "co", "company", "gmbh")
# Applied to normalized names, which are lowercase words separated by single spaces. The
# resolver's SQL runs the same pattern on stored names, so keep it POSIX-compatible.
LEGAL_SUFFIX_PATTERN = r"( (" + "|".join(LEGAL_SUFFIXES) + r"))+$"
_LEGAL_SUFFIX = re.compile(LEGAL_SUFFIX_PATTERN)


def clean_descriptor(descriptor: str) -> str:
    """The merchant part of a card descriptor: no processor prefix, `*`, phone or store
    number. "HARBOR REST SUPPLY #12" -> "HARBOR REST SUPPLY"."""
    s = _PROCESSOR_PREFIX.sub("", descriptor)
    s = s.replace("*", " ")
    s = _PHONE.sub(" ", s)
    s = _STORE_NUMBER.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def match_form(name: str) -> str:
    """`normalize_name` without trailing legal suffixes. A name that is only a suffix keeps it."""
    normalized = normalize_name(name)
    return _LEGAL_SUFFIX.sub("", normalized) or normalized


def descriptor_display_name(descriptor: str) -> str:
    """A vendor name for a descriptor that matches nothing: "Metro Water & Power"."""
    return clean_descriptor(descriptor).title()


# Words a sender adds to a business's name: "Lumenpost Billing" is Lumenpost's billing desk.
_DEPARTMENT = re.compile(
    r"(?:[\s,-]+(?:billing|accounts receivable|accounts|support|team|invoicing|payments"
    r"|notifications))+\s*$",
    re.IGNORECASE,
)
_UPPER_SUFFIX = re.compile(r"\b(Llc|Llp|Lp)\b")


def display_name(name: str) -> str:
    """A new vendor's name as shown, from the name its first document gives (2.3):
    "CORVANA, INC." -> "Corvana, Inc.", "Lumenpost Billing" -> "Lumenpost". An all-caps name of
    two or more words is title-cased (LLC, LLP and LP stay capitals), and trailing department
    words are dropped while a name remains. Lookups still go through the name as written,
    which the resolver keeps as an alias."""
    s = _SPACES.sub(" ", name).strip()
    letters = [c for c in s if c.isalpha()]
    if len(s.split()) >= 2 and letters and all(c.isupper() for c in letters):
        s = _UPPER_SUFFIX.sub(lambda m: m.group(1).upper(), s.title())
    shorter = _DEPARTMENT.sub("", s).strip()
    if _LEGAL_SUFFIX.sub("", normalize_name(shorter)):  # more than a legal suffix is left
        s = shorter
    return s


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def _word_agrees(a: str, b: str) -> bool:
    """Equal, a prefix (`rest` -> `restaurant`), or an abbreviation with the same first
    letter and its letters in order (`payrl` -> `payroll`, `ntwks` -> `networks`). Prefixes
    and abbreviations need at least 3 letters."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) < 3:
        return False
    return long.startswith(short) or (short[0] == long[0] and _is_subsequence(short, long))


def words_agree(a: str, b: str) -> bool:
    """Whether two `match_form` names can name the same business: the first words agree,
    and every word of the shorter name agrees with some word of the longer. A shared word
    alone isn't enough ("metro cafe" vs "metro water and power")."""
    wa, wb = a.split(), b.split()
    if not wa or not wb or not _word_agrees(wa[0], wb[0]):
        return False
    short, long = sorted((wa, wb), key=len)
    return all(any(_word_agrees(w, x) for x in long) for w in short)
