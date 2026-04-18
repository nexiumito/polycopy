"""M12 trader daily pnl snapshots.

Ajoute la table ``trader_daily_pnl`` (append-only, dédup unique
``(wallet_address, date)``) qui persiste l'equity curve quotidienne d'un
wallet. Source de Sortino / Calmar / consistency dans le scoring v2 (cf. spec
M12 §3.2, §3.6). Zéro modification sur les tables existantes M5/M11 —
migration strictement additive.

Revision ID: 0006_m12_trader_daily_pnl
Revises: 0005_m11_latency_samples
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_m12_trader_daily_pnl"
down_revision: str | Sequence[str] | None = "0005_m11_latency_samples"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trader_daily_pnl",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(42), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("equity_usdc", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "realized_pnl_day",
            sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "unrealized_pnl_day",
            sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "positions_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("snapshotted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "wallet_address",
            "date",
            name="uq_trader_daily_pnl_wallet_date",
        ),
    )
    op.create_index(
        "ix_trader_daily_pnl_wallet_address",
        "trader_daily_pnl",
        ["wallet_address"],
    )
    op.create_index("ix_trader_daily_pnl_date", "trader_daily_pnl", ["date"])
    op.create_index(
        "ix_trader_daily_pnl_wallet_date",
        "trader_daily_pnl",
        ["wallet_address", "date"],
    )


def downgrade() -> None:
    op.drop_index("ix_trader_daily_pnl_wallet_date", table_name="trader_daily_pnl")
    op.drop_index("ix_trader_daily_pnl_date", table_name="trader_daily_pnl")
    op.drop_index(
        "ix_trader_daily_pnl_wallet_address",
        table_name="trader_daily_pnl",
    )
    op.drop_table("trader_daily_pnl")
