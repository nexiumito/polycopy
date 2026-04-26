"""M15 anti-toxic lifecycle — source_wallet_address + is_probation.

Ajoute deux colonnes structurelles requises par le bundle M15 (MB.1 + MB.6) :

- ``my_positions.source_wallet_address`` (nullable, ``String(42)``) : trace le
  wallet polymarket source qui a déclenché la copie. Indispensable au
  collecteur ``MetricsCollectorV2._compute_internal_pnl_score`` (MB.1).
  Index composite ``(source_wallet_address, closed_at, simulated)`` pour
  garder le coût d'une query/cycle/wallet négligeable.
- ``target_traders.is_probation`` (NOT NULL, default ``False``) : flag de
  probation fractional-Kelly (MB.6). Quand ``True``, ``PositionSizer``
  multiplie ``my_size`` par ``probation_size_multiplier`` (default 0.25).

Migration **strictement additive**. Defaults safe — rows existantes M3..M14
conservent ``source_wallet_address=NULL`` (le collecteur les ignore : cold-
start naturel post-merge) et ``is_probation=False`` (sizing normal). Aucun
backfill rétroactif (versioning sacré scoring + absence de mapping fiable
pour les positions historiques sans ``source_wallet_address`` explicite).

SQLite-friendly via ``batch_alter_table`` (cohérent migrations 0003 / 0007).

Cf. spec :
:doc:`docs/specs/M15-anti-toxic-lifecycle.md` §11.1.

Note : on saute la révision 0008 (jamais émise — MC.x n'a pas introduit de
migration). Le numéro 0009 est conservé pour réserver 0008 à un éventuel
fix MC futur sans renumérotation de M15.

Revision ID: 0009_m15_anti_toxic_lifecycle
Revises: 0007_m5_bis_eviction
Create Date: 2026-04-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_m15_anti_toxic_lifecycle"
# down_revision pointe sur la dernière migration livrée. À la date de M15,
# la dernière migration en place est 0007 (M5_bis eviction). MC.x n'a pas
# introduit de migration sur la branche actuelle — on ancre directement
# sur 0007 pour rester linéaire jusqu'à ce que MC ship une migration 0008.
down_revision: str | Sequence[str] | None = "0007_m5_bis_eviction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # MB.1 — my_positions.source_wallet_address (nullable, indexed).
    with op.batch_alter_table("my_positions") as batch:
        batch.add_column(
            sa.Column("source_wallet_address", sa.String(42), nullable=True),
        )
    op.create_index(
        "ix_my_positions_source_wallet_closed",
        "my_positions",
        ["source_wallet_address", "closed_at", "simulated"],
        unique=False,
    )

    # MB.6 — target_traders.is_probation (NOT NULL, default False).
    with op.batch_alter_table("target_traders") as batch:
        batch.add_column(
            sa.Column(
                "is_probation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    op.drop_index(
        "ix_my_positions_source_wallet_closed",
        table_name="my_positions",
    )
    with op.batch_alter_table("my_positions") as batch:
        batch.drop_column("source_wallet_address")
    with op.batch_alter_table("target_traders") as batch:
        batch.drop_column("is_probation")
