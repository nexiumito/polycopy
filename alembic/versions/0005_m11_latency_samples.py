"""M11 pipeline latency samples.

Ajoute la table ``trade_latency_samples`` (append-only, purge 7 jours) pour
l'instrumentation par stage introduite en M11. Pas de modification sur les
tables existantes — migration strictement additive.

Revision ID: 0005_m11_latency_samples
Revises: 0004_m8_dry_run_realistic
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_m11_latency_samples"
down_revision: str | Sequence[str] | None = "0004_m8_dry_run_realistic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_latency_samples",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.String(32), nullable=False),
        sa.Column("stage_name", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_trade_latency_samples_trade_id",
        "trade_latency_samples",
        ["trade_id"],
    )
    op.create_index(
        "ix_trade_latency_samples_stage_ts",
        "trade_latency_samples",
        ["stage_name", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_latency_samples_stage_ts",
        table_name="trade_latency_samples",
    )
    op.drop_index(
        "ix_trade_latency_samples_trade_id",
        table_name="trade_latency_samples",
    )
    op.drop_table("trade_latency_samples")
