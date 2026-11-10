"""2.1 pipeline writes: fail-closed items and the per-tenant reconcile job

- `contract_term.vendor_id` and `invoice_fact.vendor_id` are nullable: an item whose vendor
  name failed validation is stored as `needs_review` with no vendor (`leo/interfaces.py`).
- `ck_invoice_period` lets a `needs_review` fact keep a period that doesn't parse.
- `job.document_id` is nullable for the per-tenant reconcile job, and only for it.

Revision ID: b21a9e0c4d17
Revises: 0f19fbbdb783
Create Date: 2026-11-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b21a9e0c4d17"
down_revision: str | Sequence[str] | None = "0f19fbbdb783"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PERIOD = (
    "billing_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'"
    " OR billing_period ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
)
NEW_PERIOD = f"status = 'needs_review' OR {OLD_PERIOD}"
JOB_DOCUMENT = "document_id IS NOT NULL OR stage = 'reconcile'"


def upgrade() -> None:
    op.alter_column("contract_term", "vendor_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("invoice_fact", "vendor_id", existing_type=sa.UUID(), nullable=True)
    op.drop_constraint("ck_invoice_period", "invoice_fact", type_="check")
    op.create_check_constraint("ck_invoice_period", "invoice_fact", NEW_PERIOD)
    op.alter_column("job", "document_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint("ck_job_document", "job", JOB_DOCUMENT)


def downgrade() -> None:
    op.drop_constraint("ck_job_document", "job", type_="check")
    op.execute("DELETE FROM job WHERE document_id IS NULL")
    op.alter_column("job", "document_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint("ck_invoice_period", "invoice_fact", type_="check")
    op.create_check_constraint("ck_invoice_period", "invoice_fact", OLD_PERIOD)
    op.alter_column("invoice_fact", "vendor_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("contract_term", "vendor_id", existing_type=sa.UUID(), nullable=False)
