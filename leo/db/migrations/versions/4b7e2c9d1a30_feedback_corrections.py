"""feedback loop: corrections and rejections (Phase 4)

- `contract_term.corrected_at`, `invoice_fact.corrected_at`: a reviewer's correction or
  rejection, which re-extraction never overwrites.
- A `rejected` status on terms and invoice facts. `ck_invoice_period` lets a rejected fact keep
  a period that doesn't parse.
- `feedback_event` gains `invoice_fact_id`, `document_id` and `engine`.

Revision ID: 4b7e2c9d1a30
Revises: 985708103b25
Create Date: 2026-11-10 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4b7e2c9d1a30"
down_revision: str | Sequence[str] | None = "985708103b25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PATTERN = (
    "billing_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'"
    " OR billing_period ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
)
OLD_PERIOD = f"status = 'needs_review' OR {PATTERN}"
NEW_PERIOD = f"status IN ('needs_review', 'rejected') OR {PATTERN}"
OLD_TERM_STATUS = "status IN ('current', 'superseded', 'needs_review')"
NEW_TERM_STATUS = "status IN ('current', 'superseded', 'needs_review', 'rejected')"
OLD_FACT_STATUS = "status IN ('current', 'needs_review')"
NEW_FACT_STATUS = "status IN ('current', 'needs_review', 'rejected')"


def _check(name: str, table: str, sql: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, sql)


def upgrade() -> None:
    for table in ("contract_term", "invoice_fact"):
        op.add_column(table, sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True))
    _check("ck_contract_term_status", "contract_term", NEW_TERM_STATUS)
    _check("ck_invoice_fact_status", "invoice_fact", NEW_FACT_STATUS)
    _check("ck_invoice_period", "invoice_fact", NEW_PERIOD)

    op.add_column("feedback_event", sa.Column("invoice_fact_id", sa.Uuid(), nullable=True))
    op.add_column("feedback_event", sa.Column("document_id", sa.Uuid(), nullable=True))
    op.add_column("feedback_event", sa.Column("engine", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_feedback_event_invoice_fact_id",
        "feedback_event",
        "invoice_fact",
        ["tenant_id", "invoice_fact_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_feedback_event_document_id",
        "feedback_event",
        "source_document",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_feedback_event_document_id", "feedback_event", type_="foreignkey")
    op.drop_constraint("fk_feedback_event_invoice_fact_id", "feedback_event", type_="foreignkey")
    for col in ("engine", "document_id", "invoice_fact_id"):
        op.drop_column("feedback_event", col)
    op.execute("DELETE FROM feedback_event")
    op.execute("UPDATE contract_term SET status = 'needs_review' WHERE status = 'rejected'")
    op.execute("UPDATE invoice_fact SET status = 'needs_review' WHERE status = 'rejected'")
    _check("ck_invoice_period", "invoice_fact", OLD_PERIOD)
    _check("ck_invoice_fact_status", "invoice_fact", OLD_FACT_STATUS)
    _check("ck_contract_term_status", "contract_term", OLD_TERM_STATUS)
    for table in ("contract_term", "invoice_fact"):
        op.drop_column(table, "corrected_at")
