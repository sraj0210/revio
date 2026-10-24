"""Phase 5 durable normalized repository configuration.

Revision ID: 0003_phase5_repository_configuration
Revises: 0002_phase4_review_lifecycle
"""

from collections.abc import Sequence

from alembic import op

from revio.adapters.persistence.sqlite.schema import PHASE5_SCHEMA_SQL

revision: str = "0003_phase5_repository_configuration"
down_revision: str | None = "0002_phase4_review_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in PHASE5_SCHEMA_SQL.split(";"):
        if sql := statement.strip():
            op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("Phase 5 downgrade is unsupported because it would destroy policy history")
