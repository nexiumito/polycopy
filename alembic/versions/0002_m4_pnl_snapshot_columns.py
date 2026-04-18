"""M4 PnlSnapshot columns.

Ajoute ``open_positions_count``, ``cash_pnl_total``, ``is_dry_run``, resserre
les nullables et indexe ``timestamp``.

Revision ID: 0002_m4_pnl_snapshot
Revises: 0001_baseline_m3
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_m4_pnl_snapshot"
down_revision: str | Sequence[str] | None = "0001_baseline_m3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite n'accepte pas ALTER COLUMN standard — utiliser `batch_alter_table`
    # qui copie la table. Voir specs/M4-monitoring.md §7.6.
    with op.batch_alter_table("pnl_snapshots", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "open_positions_count",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        batch.add_column(sa.Column("cash_pnl_total", sa.Float, nullable=True))
        batch.add_column(
            sa.Column(
                "is_dry_run",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        batch.alter_column(
            "timestamp",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch.alter_column(
            "total_usdc",
            existing_type=sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        )
        batch.alter_column(
            "realized_pnl",
            existing_type=sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        )
        batch.alter_column(
            "unrealized_pnl",
            existing_type=sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        )
        batch.alter_column(
            "drawdown_pct",
            existing_type=sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        )
    op.create_index("ix_pnl_snapshots_timestamp", "pnl_snapshots", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_pnl_snapshots_timestamp", table_name="pnl_snapshots")
    with op.batch_alter_table("pnl_snapshots", recreate="always") as batch:
        batch.drop_column("is_dry_run")
        batch.drop_column("cash_pnl_total")
        batch.drop_column("open_positions_count")
        batch.alter_column("timestamp", existing_type=sa.DateTime(timezone=True), nullable=True)
        batch.alter_column("total_usdc", existing_type=sa.Float, nullable=True)
        batch.alter_column("realized_pnl", existing_type=sa.Float, nullable=True)
        batch.alter_column("unrealized_pnl", existing_type=sa.Float, nullable=True)
        batch.alter_column("drawdown_pct", existing_type=sa.Float, nullable=True)
