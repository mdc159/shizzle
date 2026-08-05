"""Playback evidence and immutable-generation activation ledger.

Revision ID: 0002_playback_telemetry
Revises: 0001_initial
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_playback_telemetry"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "track_generation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "track_id",
            sa.Uuid(),
            sa.ForeignKey("tracks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("from_generation", sa.Integer(), nullable=False),
        sa.Column("to_generation", sa.Integer(), nullable=False),
        sa.Column("detail", JSONType, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_track_generation_events_track_id", "track_generation_events", ["track_id"]
    )

    op.create_table(
        "playback_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "track_id",
            sa.Uuid(),
            sa.ForeignKey("tracks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("app_build", sa.String(length=128), nullable=False),
        sa.Column("browser", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_playback_sessions_track_id", "playback_sessions", ["track_id"])

    op.create_table(
        "playback_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("playback_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "track_id",
            sa.Uuid(),
            sa.ForeignKey("tracks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("client_at_ms", sa.Integer(), nullable=False),
        sa.Column("detail", JSONType, nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_playback_events_session_sequence"
        ),
    )
    op.create_index("ix_playback_events_session_id", "playback_events", ["session_id"])
    op.create_index("ix_playback_events_track_id", "playback_events", ["track_id"])
    op.create_index("ix_playback_events_event", "playback_events", ["event"])


def downgrade() -> None:
    op.drop_index("ix_playback_events_event", table_name="playback_events")
    op.drop_index("ix_playback_events_track_id", table_name="playback_events")
    op.drop_index("ix_playback_events_session_id", table_name="playback_events")
    op.drop_table("playback_events")
    op.drop_index("ix_playback_sessions_track_id", table_name="playback_sessions")
    op.drop_table("playback_sessions")
    op.drop_index("ix_track_generation_events_track_id", table_name="track_generation_events")
    op.drop_table("track_generation_events")
