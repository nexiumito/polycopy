"""M17 cross-layer integrity — pnl_snapshots.execution_mode + trader_events.wallet_address nullable.

Bundle 2 changements structurels MD.3 + MD.7 du bundle M17 :

- ``pnl_snapshots.execution_mode`` (NOT NULL, default ``'live'``) :
  segregation des baselines drawdown par mode (audit C-003). Backfill
  in-place depuis le flag ``is_dry_run`` historique (1=dry_run, 0=live).
  CHECK constraint ``execution_mode IN ('simulation', 'dry_run', 'live')``.
- ``trader_events.wallet_address`` : passe NOT NULL → NULL pour autoriser
  les events système (audit H-005 — kill switch écrit
  ``TraderEvent(wallet_address=None, event_type='kill_switch')``). Le
  NULL signale un event non-attaché à un wallet spécifique.

Migration **additive + relaxation contrainte**. Defaults safe :
- Rows existantes ``pnl_snapshots`` backfillées via ``UPDATE`` SQL pendant
  l'upgrade, en cohérence avec ``is_dry_run`` historique.
- Rows existantes ``trader_events`` conservent leur ``wallet_address``
  non-NULL (la relaxation n'invalide pas les rows existantes).

SQLite-friendly via ``batch_alter_table`` (cohérent migrations 0007 / 0009).

Le downgrade refuse de re-NOT-NULL ``trader_events.wallet_address`` s'il
existe des rows avec ``wallet_address=NULL`` — sinon la contrainte casse
des données système. Erreur claire avec count.

Cf. spec :
:doc:`docs/specs/M17-cross-layer-integrity.md` §11.

Note : on saute le numéro 0008 (réservé par M15 pour un éventuel fix MC
futur, cf. docstring 0009). M17 prend 0010 avec
``down_revision='0009_m15_anti_toxic_lifecycle'`` — chain linéaire.

Revision ID: 0010_m17_pnl_snapshot_execution_mode
Revises: 0009_m15_anti_toxic_lifecycle
Create Date: 2026-04-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_m17_pnl_snapshot_execution_mode"
down_revision: str | Sequence[str] | None = "0009_m15_anti_toxic_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- MD.3 : pnl_snapshots.execution_mode + CHECK constraint ------------
    with op.batch_alter_table("pnl_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "execution_mode",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'live'"),
            ),
        )
        batch.create_check_constraint(
            "ck_pnl_snapshots_execution_mode",
            "execution_mode IN ('simulation', 'dry_run', 'live')",
        )

    # Backfill in-place depuis is_dry_run (cohérent legacy M4..M16).
    op.execute(
        sa.text(
            "UPDATE pnl_snapshots SET execution_mode = 'dry_run' WHERE is_dry_run = 1",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE pnl_snapshots SET execution_mode = 'live' WHERE is_dry_run = 0",
        ),
    )

    # --- MD.7 : trader_events.wallet_address NOT NULL → NULL ---------------
    with op.batch_alter_table("trader_events") as batch:
        batch.alter_column(
            "wallet_address",
            existing_type=sa.String(42),
            nullable=True,
        )


def downgrade() -> None:
    # --- MD.7 : trader_events.wallet_address NULL → NOT NULL (safe) -------
    # Refuse si des rows ont wallet_address=NULL — sinon downgrade casse la
    # contrainte sur des données système (kill_switch events post-M17).
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM trader_events WHERE wallet_address IS NULL",
        ),
    ).scalar_one()
    if null_count and null_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0010 : {null_count} system-level "
            "trader_events rows have wallet_address=NULL. Delete them or "
            "restore them to a wallet first.",
        )
    with op.batch_alter_table("trader_events") as batch:
        batch.alter_column(
            "wallet_address",
            existing_type=sa.String(42),
            nullable=False,
        )

    # --- MD.3 : drop execution_mode + CHECK -------------------------------
    with op.batch_alter_table("pnl_snapshots") as batch:
        batch.drop_constraint(
            "ck_pnl_snapshots_execution_mode",
            type_="check",
        )
        batch.drop_column("execution_mode")
