#!/usr/bin/env python3
"""Check that data/synthetic/expected.json is consistent with the dataset.

- Every quote appears exactly once in its document (the stage-4 provenance rule),
  and its character offsets are printed for use as quote_start / quote_end.
- Every referenced document, term, invoice fact and transaction exists.
- Every transaction maps to a known vendor.
- Finding arithmetic matches the transaction amounts.
- The exact-duplicate pair is byte-identical; other documents are not.
- (v2) Term values have exactly their type's shape, supersession links agree,
  invoice dates parse, vendor contacts are cited, and every finding's anchor,
  deadline, send-by date and recipient follow from the cited terms.

Usage: python3 scripts/validate_answer_key.py [--offsets] [--key PATH]
  --key PATH  validate a different copy of expected.json (documents still come from ROOT)
"""
import csv
import hashlib
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "synthetic"
errors = []


def err(msg):
    errors.append(msg)


def find_quote(text, quote):
    """Return (count, start offset of the first match or -1). Offsets index the raw file text."""
    return text.count(quote), text.find(quote)


def iso(s):
    return date.fromisoformat(s)


def back_to_friday(d):
    """Move a Saturday or Sunday deadline back to the Friday before it."""
    return d - timedelta(days=max(0, d.weekday() - 4))


ENUMS = {
    "per": {"year", "month", "seat_month"},
    "relative_to": {"term_end", "renewal_date", "price_change", "invoice_date"},
    "applies_to": {"non_renewal", "vendor_price_change", "invoice", "renewal_term"},
    "unit": {"users", "seats", "employees"},
}
VALUE_SHAPES = {
    "price": {"amount", "currency", "per"},
    "price_cap": {"max_increase_pct", "applies_to", "baseline"},
    "escalator": {"pct", "schedule", "first_application"},
    "renewal": {"auto_renew", "initial_term_end", "current_term_end", "renewal_length_months",
                "renewal_pricing", "early_termination_fee"},
    "notice_window": {"days", "relative_to", "applies_to", "deliver_to", "next_deadline"},
    "seat_count": {"included", "unit", "overage_per_unit_month", "increase_requires"},
    "dispute_window": {"days", "relative_to", "applies_to", "deliver_to"},
}


def check_shape(label, ttype, value):
    want = VALUE_SHAPES.get(ttype)
    if want is None:
        err(f"{label}: unknown term type {ttype}")
        return
    if set(value) != want:
        err(f"{label}: {ttype} value keys {sorted(value)} != {sorted(want)}")
    for k, allowed in ENUMS.items():
        if k in value and value[k] is not None and value[k] not in allowed:
            err(f"{label}: {k}={value[k]!r} not in {sorted(allowed)}")


def notice_deadline(notice, renewal):
    """Notice deadline from a notice_window value and its renewal value."""
    end = iso(renewal["current_term_end"])
    if notice["relative_to"] == "renewal_date":
        end += timedelta(days=1)
    return end - timedelta(days=notice["days"])


def main():
    show_offsets = "--offsets" in sys.argv
    key_path = Path(sys.argv[sys.argv.index("--key") + 1]) if "--key" in sys.argv else ROOT / "expected.json"
    key = json.loads(key_path.read_text())

    docs = {d["path"]: d for d in key["documents"]}
    on_disk = {str(p.relative_to(ROOT)) for p in ROOT.glob("contracts/*")} | {
        str(p.relative_to(ROOT)) for p in ROOT.glob("inbox/*")
    }
    for p in on_disk - docs.keys():
        err(f"file on disk not in answer key: {p}")
    for p in docs.keys() - on_disk:
        err(f"answer key lists missing file: {p}")

    vendors = {v["id"] for v in key["vendors"]}
    for d in docs.values():
        if d["vendor"] is not None and d["vendor"] not in vendors:
            err(f"{d['path']}: unknown vendor {d['vendor']}")

    txns = {}
    with open(ROOT / "transactions.csv", newline="") as f:
        for row in csv.DictReader(f):
            txns[row["id"]] = row
    tmap = key["transaction_vendor_map"]
    for tid in txns:
        if tmap.get(tid) not in vendors:
            err(f"{tid}: not mapped to a known vendor")

    # Provenance: each quote must occur exactly once in its document.
    texts = {p: (ROOT / p).read_text() for p in on_disk}
    for group in ("terms", "invoice_facts"):
        for item in key[group]:
            doc = item["document"]
            if doc not in texts:
                err(f"{item['id']}: missing document {doc}")
                continue
            if item["vendor"] != docs[doc]["vendor"]:
                err(f"{item['id']}: vendor differs from its document's vendor")
            for q in item["quotes"]:
                n, s = find_quote(texts[doc], q)
                if n != 1:
                    err(f"{item['id']}: quote found {n}x in {doc}: {q!r}")
                elif show_offsets:
                    print(f"{item['id']:28} {doc}:{s}-{s + len(q)}")

    # Vendor contacts: cited exactly once, and the email is inside the quote.
    contacts = {}
    for v in key["vendors"]:
        for c in v.get("contacts", []):
            contacts.setdefault(v["id"], set()).add(c["email"])
            label = f"{v['id']} contact {c['email']}"
            if c["document"] not in texts:
                err(f"{label}: missing document {c['document']}")
                continue
            n, s = find_quote(texts[c["document"]], c["quote"])
            if n != 1:
                err(f"{label}: quote found {n}x in {c['document']}: {c['quote']!r}")
            elif show_offsets:
                print(f"{label:28} {c['document']}:{s}-{s + len(c['quote'])}")
            if c["email"] not in c["quote"]:
                err(f"{label}: email not inside its quote")

    term_ids = {t["id"] for t in key["terms"]}
    fact_ids = {i["id"] for i in key["invoice_facts"]}
    terms = {t["id"]: t for t in key["terms"]}
    for t in key["terms"]:
        for ref in ("supersedes", "superseded_by"):
            if ref in t and t[ref] not in term_ids:
                err(f"{t['id']}: {ref} -> unknown term {t[ref]}")
        check_shape(t["id"], t["type"], t["value"])
        dt = t["value"].get("deliver_to")
        if dt is not None and not any(dt in q for q in t["quotes"]):
            err(f"{t['id']}: deliver_to {dt} is not inside any of its quotes")
        if "old_value" in t:
            check_shape(f"{t['id']}.old_value", "price", t["old_value"])
        if t.get("status") not in (None, "superseded"):
            err(f"{t['id']}: unknown status {t['status']!r}")
        if t.get("status") == "superseded" and "superseded_by" not in t:
            err(f"{t['id']}: superseded without superseded_by")
        if "superseded_by" in t and terms.get(t["superseded_by"], {}).get("supersedes") != t["id"]:
            err(f"{t['id']}: superseded_by {t['superseded_by']} does not point back")
        if "supersedes" in t and terms.get(t["supersedes"], {}).get("superseded_by") != t["id"]:
            err(f"{t['id']}: supersedes {t['supersedes']} does not point back")

    # Notice deadlines follow from the renewal term with the same agreement.
    renewals = {t["agreement_ref"]: t for t in key["terms"] if t["type"] == "renewal"}
    for t in key["terms"]:
        if t["type"] != "notice_window" or t["value"].get("next_deadline") is None:
            continue
        r = renewals.get(t.get("agreement_ref"))
        if r is None:
            err(f"{t['id']}: no renewal term for {t.get('agreement_ref')}")
        elif notice_deadline(t["value"], r["value"]) != iso(t["value"]["next_deadline"]):
            err(f"{t['id']}: next_deadline should be {notice_deadline(t['value'], r['value'])}")
    facts = {i["id"]: i for i in key["invoice_facts"]}
    for i in key["invoice_facts"]:
        try:
            iso(i["invoice_date"])
        except (KeyError, TypeError, ValueError):
            err(f"{i['id']}: missing or invalid invoice_date")
        tx = txns.get(i["matches_transaction"])
        if tx is None:
            err(f"{i['id']}: unknown transaction")
        elif Decimal(tx["amount"]) != Decimal(str(i["amount"])):
            err(f"{i['id']}: amount {i['amount']} != {tx['id']} {tx['amount']}")

    for f in key["findings"] + key["non_findings"]:
        for t in f.get("terms", []):
            if t not in term_ids:
                err(f"{f['id']}: unknown term {t}")
        for i in f.get("invoice_facts", []):
            if i not in fact_ids:
                err(f"{f['id']}: unknown invoice fact {i}")
        for t in f.get("transactions", []):
            if t not in txns:
                err(f"{f['id']}: unknown transaction {t}")
            elif f["vendor"] and tmap[t] != f["vendor"]:
                err(f"{f['id']}: {t} belongs to {tmap[t]}, not {f['vendor']}")
        for d in f.get("documents", []) + f.get("linked_duplicates", []):
            if d not in docs:
                err(f"{f['id']}: unknown document {d}")

    # Finding-only fields: anchor, deadline rule, send-by date, recipient.
    today = iso(key["demo_today"])
    for f in key["findings"]:
        fid, fterms, ftx = f["id"], f.get("terms", []), f.get("transactions", [])
        a = f.get("anchor")
        if not a or a.get("term") not in fterms:
            err(f"{fid}: anchor term missing or not in terms")
        elif a.get("transaction") is not None and a["transaction"] not in ftx:
            err(f"{fid}: anchor transaction not in transactions")
        b, dl, send = f.get("deadline_basis"), f.get("deadline"), f.get("recommended_send_by")
        if dl is None:
            if b is not None or send is not None:
                err(f"{fid}: no deadline, so deadline_basis and recommended_send_by must be null")
        else:
            computed = None
            bterms = [terms[t] for t in (b or {}).get("terms", []) if t in terms]
            if not b or any(t not in fterms for t in b.get("terms", [])):
                err(f"{fid}: deadline_basis terms missing or not in terms")
            elif b.get("invoice_fact") and b["invoice_fact"] not in f.get("invoice_facts", []):
                err(f"{fid}: deadline_basis invoice_fact not in invoice_facts")
            else:
                types = {t["type"]: t for t in bterms}
                if "dispute_window" in types and b.get("invoice_fact"):
                    computed = iso(facts[b["invoice_fact"]]["invoice_date"]) + timedelta(days=types["dispute_window"]["value"]["days"])
                elif "notice_window" in types and "renewal" in types:
                    computed = notice_deadline(types["notice_window"]["value"], types["renewal"]["value"])
                elif len(bterms) == 1 and bterms[0]["type"] == "price":
                    computed = iso(bterms[0]["effective_date"]) - timedelta(days=1)
                else:
                    err(f"{fid}: no deadline rule for basis types {sorted(types)}")
            if computed is not None and computed != iso(dl):
                err(f"{fid}: deadline {dl} != computed {computed}")
            if f.get("days_until_deadline") != (iso(dl) - today).days:
                err(f"{fid}: days_until_deadline should be {(iso(dl) - today).days}")
            if send is None or iso(send) != back_to_friday(iso(dl)):
                err(f"{fid}: recommended_send_by should be {back_to_friday(iso(dl))}")
        to = f.get("expected_action", {}).get("to")
        allowed = {terms[t]["value"].get("deliver_to") for t in fterms if t in terms} | contacts.get(f["vendor"], set())
        allowed.discard(None)
        if to is None:
            err(f"{fid}: expected_action.to is missing")
        elif to not in allowed:
            err(f"{fid}: expected_action.to {to!r} is not a cited deliver_to or vendor contact")

    # Arithmetic for the reconciliation findings.
    amt = lambda t: Decimal(txns[t]["amount"])
    F = {f["id"]: f for f in key["findings"]}
    k = F["F3"]["computation"]
    if amt("txn_0019") * Decimal("1.05") != Decimal(str(k["max_allowed_monthly"])):
        err("F3: max_allowed_monthly is not prior * 1.05")
    if (amt("txn_0025") - Decimal(str(k["max_allowed_monthly"]))) * 2 != Decimal(str(F["F3"]["impact"]["overcharged_to_date"])):
        err("F3: overcharged_to_date mismatch")
    q = F["F4"]["computation"]
    if int(txns["txn_0036"]["seats_billed"]) != q["billed_seats"]:
        err("F4: billed seats mismatch")
    if amt("txn_0036") - amt("txn_0030") != Decimal(str(q["overcharge_monthly"])):
        err("F4: overcharge mismatch")
    h = F["F5"]["computation"]
    if amt("txn_0035") - amt("txn_0029") != Decimal(str(h["overcharge_monthly"])):
        err("F5: overcharge mismatch")
    lp = F["F2"]["impact"]
    if (Decimal("339") - amt("txn_0032")) != Decimal(str(lp["monthly"])):
        err("F2: monthly delta mismatch")
    to_date = sum(Decimal(str(F[x]["impact"]["overcharged_to_date"])) for x in ("F3", "F4", "F5"))
    if to_date != Decimal(str(key["totals"]["overcharged_to_date"])):
        err(f"totals: overcharged_to_date should be {to_date}")

    # Dedup: exact-duplicate pairs are byte-identical, and nothing else is.
    hashes = {}
    for p in sorted(on_disk):
        hashes.setdefault(hashlib.sha256((ROOT / p).read_bytes()).hexdigest(), []).append(p)
    groups = [sorted(g) for g in hashes.values() if len(g) > 1]
    expected = [sorted([d["path"], d["exact_duplicate_of"]]) for d in docs.values() if "exact_duplicate_of" in d]
    if sorted(groups) != sorted(expected):
        err(f"byte-identical groups {groups} != expected {expected}")

    if errors:
        print(f"\n{len(errors)} problem(s):")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print(f"OK: {len(docs)} documents, {len(term_ids)} terms, {len(fact_ids)} invoice facts, "
          f"{len(txns)} transactions, {len(F)} findings, {len(key['non_findings'])} non-findings, "
          f"{sum(len(c) for c in contacts.values())} contacts.")


if __name__ == "__main__":
    main()
