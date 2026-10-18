"""Static AIS vessel data (name, type, length, width) in `ais_vessels`.

Fresh databases get the table from schema.sql (revision 0001), hence IF NOT EXISTS.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ais_vessels (
            mmsi      bigint PRIMARY KEY,
            name      text,
            ship_type text,
            length_m  real CHECK (length_m > 0),
            width_m   real CHECK (width_m > 0)
        )
    """)
    op.execute("GRANT SELECT ON ais_vessels TO grafana_ro")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ais_vessels")
