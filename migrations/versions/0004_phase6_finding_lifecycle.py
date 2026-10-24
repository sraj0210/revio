"""Phase 6 deduplication and finding lifecycle.

Revision ID: 0004_phase6_finding_lifecycle
Revises: 0003_phase5_repository_configuration
"""

from collections.abc import Sequence

from alembic import op

from revio.adapters.persistence.sqlite.schema import PHASE6_SCHEMA_SQL

revision: str = "0004_phase6_finding_lifecycle"
down_revision: str | None = "0003_phase5_repository_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in PHASE6_SCHEMA_SQL.split(";"):
        if sql := statement.strip():
            op.execute(sql)


def downgrade() -> None:
    raise RuntimeError(
        "Phase 6 downgrade is unsupported because it would destroy finding-lifecycle history"
    )
