"""Transactions, plans and invoice matching (ROADMAP A5).

- `CsvTransactionSource` reads `transactions.csv`. A Ramp connection would implement the same
  `TransactionSource` interface.
- `sync_transactions` upserts each charge with its vendor from the resolver.
- `match_plans` and `match_invoice_facts` recompute the derived links from what is stored.
  They are the only writers of `transaction.plan_label` and
  `invoice_fact.matches_transaction`, so a rerun gives the same result.
"""

import csv
import datetime
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update

from leo.db import keys
from leo.db.models import ContractTerm, InvoiceFact
from leo.db.models import Transaction as TxnRow
from leo.db.repo import Repo
from leo.interfaces import TransactionSource, VendorResolver
from leo.types import TermStatus, TermType, Transaction

SOURCE = "csv"
INVOICE_MATCH_DAYS = 7  # an invoice and its charge are at most this far apart


class CsvTransactionSource:
    def __init__(self, path: Path) -> None:
        self.path = path

    def transactions(self) -> list[Transaction]:
        with self.path.open(newline="") as f:
            return [Transaction.model_validate(row) for row in csv.DictReader(f)]


def sync_transactions(
    repo: Repo, source: TransactionSource, resolver: VendorResolver
) -> dict[str, UUID]:
    """Upsert every charge with its resolved vendor; return external id -> row id. A
    descriptor that matches no vendor creates one (Sunridge, Metro)."""
    out: dict[str, UUID] = {}
    for t in source.transactions():
        out[t.id] = repo.upsert_transaction(
            keys.txn(SOURCE, t.id),
            vendor_id=UUID(resolver.vendor_for_descriptor(t.descriptor)),
            source=SOURCE,
            external_id=t.id,
            descriptor=t.descriptor,
            card_last4=t.card_last4,
            seats_billed=t.seats_billed,
            amount=t.amount,
            currency=t.currency,
            date=t.date,
        )
    return out


def _amount(value: dict | None) -> Decimal | None:
    amount = (value or {}).get("amount")
    return None if amount is None else Decimal(str(amount))


def match_plans(repo: Repo) -> None:
    """Label each charge of a vendor with two or more plans with the plan whose price, in
    force on the charge date, equals the amount. In force means the latest `effective_date`
    on or before the charge, for that plan label. No match, or more than one, gives NULL.
    Superseded prices count: they were in force before their replacement."""
    s, t = repo.session, repo.tenant_id
    terms = s.execute(
        select(
            ContractTerm.vendor_id,
            ContractTerm.plan_label,
            ContractTerm.effective_date,
            ContractTerm.value,
        ).where(
            ContractTerm.tenant_id == t,
            ContractTerm.type == TermType.price,
            ContractTerm.status != TermStatus.needs_review,
            ContractTerm.plan_label.is_not(None),
        )
    ).all()
    plans: dict[UUID, dict[str, list[tuple[datetime.date, Decimal | None]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in terms:
        plans[row.vendor_id][row.plan_label].append(
            (row.effective_date or datetime.date.min, _amount(row.value))
        )

    txns = s.execute(
        select(TxnRow.id, TxnRow.vendor_id, TxnRow.date, TxnRow.amount).where(TxnRow.tenant_id == t)
    ).all()
    for txn in txns:
        label = None
        vendor_plans = plans.get(txn.vendor_id, {})
        if len(vendor_plans) >= 2:
            hits = []
            for plan, prices in vendor_plans.items():
                in_force = [p for p in prices if p[0] <= txn.date]
                if in_force and max(in_force, key=lambda p: p[0])[1] == txn.amount:
                    hits.append(plan)
            label = hits[0] if len(hits) == 1 else None
        s.execute(update(TxnRow).where(TxnRow.id == txn.id).values(plan_label=label))


def match_invoice_facts(repo: Repo) -> None:
    """Match each invoice fact to a charge of the same vendor and amount, at most
    `INVOICE_MATCH_DAYS` from the invoice date. The nearest wins, and a charge matches at
    most one fact. Facts are matched in invoice-date order."""
    s, t = repo.session, repo.tenant_id
    txns = s.execute(
        select(TxnRow.id, TxnRow.vendor_id, TxnRow.date, TxnRow.amount)
        .where(TxnRow.tenant_id == t)
        .order_by(TxnRow.date, TxnRow.id)
    ).all()
    facts = s.execute(
        select(InvoiceFact.id, InvoiceFact.vendor_id, InvoiceFact.invoice_date, InvoiceFact.amount)
        .where(InvoiceFact.tenant_id == t)
        .order_by(InvoiceFact.invoice_date, InvoiceFact.id)
    ).all()
    claimed: set[UUID] = set()
    for f in facts:
        candidates = [
            (abs((x.date - f.invoice_date).days), x.date, x.id)
            for x in txns
            if x.id not in claimed
            and x.vendor_id == f.vendor_id
            and x.amount == f.amount
            and abs((x.date - f.invoice_date).days) <= INVOICE_MATCH_DAYS
        ]
        match = min(candidates)[2] if candidates else None
        if match is not None:
            claimed.add(match)
        s.execute(
            update(InvoiceFact).where(InvoiceFact.id == f.id).values(matches_transaction=match)
        )
