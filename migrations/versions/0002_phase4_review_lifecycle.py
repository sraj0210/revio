"""Phase 4 review generation and provider-write lifecycle.

Revision ID: 0002_phase4_review_lifecycle
Revises: 0001_phase3_durable_queue
"""

from collections.abc import Sequence

from alembic import op

from revio.adapters.persistence.sqlite.schema import PHASE4_SCHEMA_SQL

revision: str = "0002_phase4_review_lifecycle"
down_revision: str | None = "0001_phase3_durable_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in PHASE4_SCHEMA_SQL.split(";"):
        if sql := statement.strip():
            op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("Phase 4 downgrade is unsupported because it would destroy review history")
