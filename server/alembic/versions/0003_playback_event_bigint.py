"""Store JavaScript epoch milliseconds without 32-bit overflow.

Revision ID: 0003_playback_event_bigint
Revises: 0002_playback_telemetry
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_playback_event_bigint"
down_revision = "0002_playback_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "playback_events",
        "client_at_ms",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "playback_events",
        "client_at_ms",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
