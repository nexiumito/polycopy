"""Baseline M3 schema.

Crée les tables M1/M2/M3 telles qu'elles existaient avant M4 (avant le
tightening `PnlSnapshot`). Les utilisateurs qui ont déjà une DB héritée de M3
peuvent marquer cette revision comme appliquée via ``alembic stamp head`` (cf.
``docs/setup.md`` §10) ; ``init_db`` le fait automatiquement s'il détecte les
tables présentes mais pas de ``alembic_version``.

Revision ID: 0001_baseline_m3
Revises:
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_baseline_m3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "target_traders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(42), nullable=False),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_target_traders_wallet_address",
        "target_traders",
        ["wallet_address"],
        unique=True,
    )

    op.create_table(
        "detected_trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tx_hash", sa.String(66), nullable=False),
        sa.Column("target_wallet", sa.String(42), nullable=False),
        sa.Column("condition_id", sa.String(66), nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("size", sa.Float, nullable=False),
        sa.Column("usdc_size", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("slug", sa.String, nullable=True),
        sa.Column("raw_json", sa.JSON, nullable=False),
    )
    op.create_index("ix_detected_trades_tx_hash", "detected_trades", ["tx_hash"], unique=True)
    op.create_index("ix_detected_trades_target_wallet", "detected_trades", ["target_wallet"])
    op.create_index("ix_detected_trades_condition_id", "detected_trades", ["condition_id"])
    op.create_index("ix_detected_trades_timestamp", "detected_trades", ["timestamp"])

    op.create_table(
        "strategy_decisions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("detected_trade_id", sa.Integer, nullable=False),
        sa.Column("tx_hash", sa.String(66), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("my_size", sa.Float, nullable=True),
        sa.Column("my_price", sa.Float, nullable=True),
        sa.Column("slippage_pct", sa.Float, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pipeline_state", sa.JSON, nullable=False),
    )
    op.create_index(
        "ix_strategy_decisions_detected_trade_id",
        "strategy_decisions",
        ["detected_trade_id"],
    )
    op.create_index("ix_strategy_decisions_tx_hash", "strategy_decisions", ["tx_hash"])
    op.create_index("ix_strategy_decisions_decided_at", "strategy_decisions", ["decided_at"])

    op.create_table(
        "my_orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_tx_hash", sa.String(66), nullable=False),
        sa.Column("clob_order_id", sa.String(66), nullable=True),
        sa.Column("condition_id", sa.String(66), nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("size", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("tick_size", sa.Float, nullable=False),
        sa.Column("neg_risk", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("order_type", sa.String(4), nullable=False, server_default="FOK"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("taking_amount", sa.String(64), nullable=True),
        sa.Column("making_amount", sa.String(64), nullable=True),
        sa.Column("transaction_hashes", sa.JSON, nullable=False),
        sa.Column("error_msg", sa.String(256), nullable=True),
        sa.Column("simulated", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_my_orders_source_tx_hash", "my_orders", ["source_tx_hash"])
    op.create_index("ix_my_orders_clob_order_id", "my_orders", ["clob_order_id"])
    op.create_index("ix_my_orders_condition_id", "my_orders", ["condition_id"])

    op.create_table(
        "my_positions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("condition_id", sa.String(66), nullable=False),
        sa.Column("asset_id", sa.String, nullable=False),
        sa.Column("size", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column("avg_price", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("condition_id", "asset_id", name="uq_my_positions_condition_asset"),
    )
    op.create_index("ix_my_positions_condition_id", "my_positions", ["condition_id"])
    op.create_index("ix_my_positions_asset_id", "my_positions", ["asset_id"])

    op.create_table(
        "pnl_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_usdc", sa.Float, nullable=True),
        sa.Column("realized_pnl", sa.Float, nullable=True),
        sa.Column("unrealized_pnl", sa.Float, nullable=True),
        sa.Column("drawdown_pct", sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("pnl_snapshots")
    op.drop_index("ix_my_positions_asset_id", table_name="my_positions")
    op.drop_index("ix_my_positions_condition_id", table_name="my_positions")
    op.drop_table("my_positions")
    op.drop_index("ix_my_orders_condition_id", table_name="my_orders")
    op.drop_index("ix_my_orders_clob_order_id", table_name="my_orders")
    op.drop_index("ix_my_orders_source_tx_hash", table_name="my_orders")
    op.drop_table("my_orders")
    op.drop_index("ix_strategy_decisions_decided_at", table_name="strategy_decisions")
    op.drop_index("ix_strategy_decisions_tx_hash", table_name="strategy_decisions")
    op.drop_index("ix_strategy_decisions_detected_trade_id", table_name="strategy_decisions")
    op.drop_table("strategy_decisions")
    op.drop_index("ix_detected_trades_timestamp", table_name="detected_trades")
    op.drop_index("ix_detected_trades_condition_id", table_name="detected_trades")
    op.drop_index("ix_detected_trades_target_wallet", table_name="detected_trades")
    op.drop_index("ix_detected_trades_tx_hash", table_name="detected_trades")
    op.drop_table("detected_trades")
    op.drop_index("ix_target_traders_wallet_address", table_name="target_traders")
    op.drop_table("target_traders")
