"""Runtime settings: operator overrides for routing and RunPod concurrency.

Revision ID: 0007_runtime_settings
Revises: 0006_worker_nodes
Create Date: 2026-11-19

Self-contained on purpose (no imports from shizzle_server), like 0001.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_runtime_settings"
down_revision = "0006_worker_nodes"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # Operator-controlled overrides read by the orchestrator at each routing
    # decision; a missing row falls back to the environment Settings value.
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", JSONType, nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
