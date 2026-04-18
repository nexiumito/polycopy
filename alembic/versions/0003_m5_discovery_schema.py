"""M5 discovery schema.

Étend ``target_traders`` avec le lifecycle ``status`` / ``pinned`` / compteur
d'hystérésis / timestamps d'audit, et crée les 2 tables append-only
``trader_scores`` et ``trader_events``.

Backfill : tous les traders pré-M5 avec ``active=1`` sont marqués ``pinned=1``
+ ``status='pinned'`` (ils viennent forcément de ``TARGET_WALLETS`` env, sont
donc la whitelist autoritaire de l'utilisateur, et ne doivent jamais être
demote-ables par M5).

Audit manuel requis : SQLite supporte l'ajout de colonnes via
``batch_alter_table`` (copie table) — cohérent avec le pattern 0002 M4.

Revision ID: 0003_m5_discovery
Revises: 0002_m4_pnl_snapshot
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_m5_discovery"
down_revision: str | Sequence[str] | None = "0002_m4_pnl_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. target_traders : ajout des 7 nouvelles colonnes via batch (SQLite).
    with op.batch_alter_table("target_traders", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "status",
                sa.String(8),
                nullable=False,
                server_default=sa.text("'active'"),
            ),
        )
        batch.add_column(
            sa.Column(
                "pinned",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        batch.add_column(
            sa.Column(
                "consecutive_low_score_cycles",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        batch.add_column(sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_scored_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("scoring_version", sa.String(16), nullable=True))

    # 2. Backfill (cf. spec §14.5 #9) :
    #    - active=1 → pinned (viennent de TARGET_WALLETS env, whitelist user autoritaire)
    #    - active=0 → paused (wallets retirés manuellement, pas pinned par défaut)
    op.execute(
        "UPDATE target_traders SET pinned = 1, status = 'pinned' WHERE active = 1",
    )
    op.execute(
        "UPDATE target_traders SET status = 'paused' WHERE active = 0",
    )

    # 3. Index sur status (queries dashboard + decision_engine).
    op.create_index("ix_target_traders_status", "target_traders", ["status"])

    # 4. Table trader_scores (append-only, audit + historique formule).
    op.create_table(
        "trader_scores",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("target_trader_id", sa.Integer, nullable=False),
        sa.Column("wallet_address", sa.String(42), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("scoring_version", sa.String(16), nullable=False),
        sa.Column("cycle_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "low_confidence",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("metrics_snapshot", sa.JSON, nullable=False),
    )
    op.create_index(
        "ix_trader_scores_target_trader_id",
        "trader_scores",
        ["target_trader_id"],
    )
    op.create_index(
        "ix_trader_scores_wallet_address",
        "trader_scores",
        ["wallet_address"],
    )
    op.create_index("ix_trader_scores_cycle_at", "trader_scores", ["cycle_at"])
    op.create_index(
        "ix_trader_scores_wallet_cycle",
        "trader_scores",
        ["wallet_address", "cycle_at"],
    )

    # 5. Table trader_events (append-only, audit trail des décisions).
    op.create_table(
        "trader_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(42), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_status", sa.String(8), nullable=True),
        sa.Column("to_status", sa.String(8), nullable=True),
        sa.Column("score_at_event", sa.Float, nullable=True),
        sa.Column("scoring_version", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("event_metadata", sa.JSON, nullable=True),
    )
    op.create_index(
        "ix_trader_events_wallet_address",
        "trader_events",
        ["wallet_address"],
    )
    op.create_index("ix_trader_events_at", "trader_events", ["at"])
    op.create_index(
        "ix_trader_events_wallet_at",
        "trader_events",
        ["wallet_address", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trader_events_wallet_at", table_name="trader_events")
    op.drop_index("ix_trader_events_at", table_name="trader_events")
    op.drop_index("ix_trader_events_wallet_address", table_name="trader_events")
    op.drop_table("trader_events")
    op.drop_index("ix_trader_scores_wallet_cycle", table_name="trader_scores")
    op.drop_index("ix_trader_scores_cycle_at", table_name="trader_scores")
    op.drop_index("ix_trader_scores_wallet_address", table_name="trader_scores")
    op.drop_index("ix_trader_scores_target_trader_id", table_name="trader_scores")
    op.drop_table("trader_scores")
    op.drop_index("ix_target_traders_status", table_name="target_traders")
    with op.batch_alter_table("target_traders", recreate="always") as batch:
        batch.drop_column("scoring_version")
        batch.drop_column("last_scored_at")
        batch.drop_column("promoted_at")
        batch.drop_column("discovered_at")
        batch.drop_column("consecutive_low_score_cycles")
        batch.drop_column("pinned")
        batch.drop_column("status")
