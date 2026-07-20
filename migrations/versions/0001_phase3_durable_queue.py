"""Phase 3 durable queue and installation lifecycle state.

Revision ID: 0001_phase3_durable_queue
Revises: None
"""

from collections.abc import Sequence

from alembic import op

from revio.adapters.persistence.sqlite.schema import SCHEMA_SQL

revision: str = "0001_phase3_durable_queue"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in SCHEMA_SQL.split(";"):
        sql = statement.strip()
        if sql and not sql.startswith("CREATE TABLE IF NOT EXISTS alembic_version"):
            op.execute(sql)


def downgrade() -> None:
    for name in (
        "webhook_delivery_tombstones",
        "installation_states",
        "job_attempts",
        "queue_jobs",
        "webhook_deliveries",
    ):
        op.execute(f"DROP TABLE IF EXISTS {name}")
