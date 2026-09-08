"""Durable completed-media contributions, separate from source processing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_completed_imports"
down_revision = "0005_job_artist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "completed_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "manifest", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("validation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "completed_import_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("import_id", sa.Uuid(), sa.ForeignKey("completed_imports.id"), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_completed_import_events_import_id", "completed_import_events", ["import_id"]
    )


def downgrade() -> None:
    op.drop_table("completed_import_events")
    op.drop_table("completed_imports")
