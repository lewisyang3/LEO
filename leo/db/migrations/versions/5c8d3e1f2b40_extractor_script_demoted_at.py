"""extractor_script.demoted_at: a reviewer's correction demotes a generated script (Phase 4, F3)

Revision ID: 5c8d3e1f2b40
Revises: 4b7e2c9d1a30
Create Date: 2026-11-10 16:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c8d3e1f2b40"
down_revision: str | Sequence[str] | None = "4b7e2c9d1a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extractor_script", sa.Column("demoted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("extractor_script", "demoted_at")
