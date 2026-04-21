"""M5_bis competitive eviction schema.

Étend ``target_traders`` avec les colonnes nécessaires à l'eviction
adaptative (``previously_demoted_at``, ``eviction_state_entered_at``,
``eviction_triggering_wallet``) et widen ``status`` de ``String(8)`` à
``String(16)`` pour accueillir ``sell_only`` et ``blacklisted``.
Widen également ``trader_events.from_status`` et ``to_status`` pour les
mêmes raisons (audit trail cohérent).

Data migration idempotente : les rows ``status='paused'`` M5 deviennent
``status='shadow'`` avec ``previously_demoted_at`` posé sur le dernier
``last_scored_at`` connu (fallback ``CURRENT_TIMESTAMP``). Re-run safe —
au 2ᵉ run, ``WHERE status='paused'`` est vide.

Audit manuel requis : SQLite ne supporte pas ALTER COLUMN TYPE, donc
``batch_alter_table(recreate="always")`` copie la table. Pattern
cohérent avec migration 0003 M5.

Cf. spec `docs/specs/M5_bis_competitive_eviction_spec.md` §6.

Revision ID: 0007_m5_bis_eviction
Revises: 0006_m12_trader_daily_pnl
Create Date: 2026-04-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_m5_bis_eviction"
down_revision: str | Sequence[str] | None = "0006_m12_trader_daily_pnl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. target_traders : widen status + 3 nouvelles colonnes eviction.
    with op.batch_alter_table("target_traders", recreate="always") as batch:
        batch.alter_column(
            "status",
            existing_type=sa.String(8),
            type_=sa.String(16),
            existing_nullable=False,
            existing_server_default=sa.text("'active'"),
        )
        batch.add_column(
            sa.Column(
                "previously_demoted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        batch.add_column(
            sa.Column(
                "eviction_state_entered_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        batch.add_column(
            sa.Column(
                "eviction_triggering_wallet",
                sa.String(42),
                nullable=True,
            ),
        )

    # 2. trader_events : widen from_status / to_status pour accueillir
    #    'sell_only' (9) et 'blacklisted' (11).
    with op.batch_alter_table("trader_events", recreate="always") as batch:
        batch.alter_column(
            "from_status",
            existing_type=sa.String(8),
            type_=sa.String(16),
            existing_nullable=True,
        )
        batch.alter_column(
            "to_status",
            existing_type=sa.String(8),
            type_=sa.String(16),
            existing_nullable=True,
        )

    # 3. Data migration : paused → shadow + previously_demoted_at = last_scored_at
    #    (fallback CURRENT_TIMESTAMP si jamais scoré). Idempotent : WHERE status='paused'
    #    est vide au 2e run. active=0 déjà set sur les paused M5 (cf. DecisionEngine
    #    transition_status qui met active=False pour les non-active).
    op.execute(
        """
        UPDATE target_traders
        SET status = 'shadow',
            active = 0,
            previously_demoted_at = COALESCE(last_scored_at, CURRENT_TIMESTAMP)
        WHERE status = 'paused'
        """,
    )

    # 4. Pas de backfill blacklisted : posé dynamiquement au boot par
    #    EvictionScheduler.reconcile_blacklist (Phase B). La migration laisse
    #    ces wallets dans leur status actuel ; le 1er cycle post-upgrade
    #    convergera.


def downgrade() -> None:
    # Reverse data migration : tous les shadow dont previously_demoted_at est
    # posé reviennent en paused. Les sell_only et blacklisted reviennent en
    # shadow (wind-down perdu, acceptable en downgrade).
    op.execute("UPDATE target_traders SET status = 'shadow' WHERE status = 'sell_only'")
    op.execute("UPDATE target_traders SET status = 'shadow' WHERE status = 'blacklisted'")
    op.execute(
        """
        UPDATE target_traders
        SET status = 'paused'
        WHERE status = 'shadow' AND previously_demoted_at IS NOT NULL
        """,
    )

    with op.batch_alter_table("trader_events", recreate="always") as batch:
        batch.alter_column(
            "to_status",
            existing_type=sa.String(16),
            type_=sa.String(8),
            existing_nullable=True,
        )
        batch.alter_column(
            "from_status",
            existing_type=sa.String(16),
            type_=sa.String(8),
            existing_nullable=True,
        )

    with op.batch_alter_table("target_traders", recreate="always") as batch:
        batch.drop_column("eviction_triggering_wallet")
        batch.drop_column("eviction_state_entered_at")
        batch.drop_column("previously_demoted_at")
        batch.alter_column(
            "status",
            existing_type=sa.String(16),
            type_=sa.String(8),
            existing_nullable=False,
            existing_server_default=sa.text("'active'"),
        )
