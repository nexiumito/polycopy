"""M8 dry-run realistic fill.

Étend ``my_orders`` avec ``realistic_fill`` (bool) et ``my_positions`` avec
``simulated`` (bool) + ``realized_pnl`` (float nullable). Recrée la contrainte
unique de ``my_positions`` en ``(condition_id, asset_id, simulated)`` pour
permettre la coexistence d'une position réelle et d'une position virtuelle sur
le même marché.

Ajoute un index partiel-équivalent ``ix_my_positions_simulated_open`` sur
``(simulated, closed_at)`` pour accélérer ``list_open_virtual``.

Audit manuel SQLite : ``batch_alter_table(recreate='always')`` recopie la
table — cohérent avec le pattern 0002/0003. Backfill : aucun (defaults).

Revision ID: 0004_m8_dry_run_realistic
Revises: 0003_m5_discovery
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_m8_dry_run_realistic"
down_revision: str | Sequence[str] | None = "0003_m5_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. my_orders : ajout de realistic_fill (default 0).
    with op.batch_alter_table("my_orders", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "realistic_fill",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    # 2. my_positions : ajout simulated + realized_pnl, recréation contrainte
    #    unique en triple clé (condition_id, asset_id, simulated).
    with op.batch_alter_table("my_positions", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "simulated",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        batch.add_column(sa.Column("realized_pnl", sa.Float, nullable=True))
        batch.drop_constraint("uq_my_positions_condition_asset", type_="unique")
        batch.create_unique_constraint(
            "uq_my_positions_condition_asset_simulated",
            ["condition_id", "asset_id", "simulated"],
        )

    # 3. Index pour accélérer list_open_virtual.
    op.create_index(
        "ix_my_positions_simulated_open",
        "my_positions",
        ["simulated", "closed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_my_positions_simulated_open", table_name="my_positions")
    with op.batch_alter_table("my_positions", recreate="always") as batch:
        batch.drop_constraint("uq_my_positions_condition_asset_simulated", type_="unique")
        batch.create_unique_constraint(
            "uq_my_positions_condition_asset",
            ["condition_id", "asset_id"],
        )
        batch.drop_column("realized_pnl")
        batch.drop_column("simulated")
    with op.batch_alter_table("my_orders", recreate="always") as batch:
        batch.drop_column("realistic_fill")
