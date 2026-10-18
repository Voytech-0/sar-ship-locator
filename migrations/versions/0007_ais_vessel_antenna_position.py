"""Antenna position on the hull (AIS A, B, C, D) in `ais_vessels`.

The AIS position is the antenna, the SAR Detection is near the hull centre; for large ships the two differ
by tens of metres. Fresh databases get the columns from schema.sql (revision 0001), hence IF NOT EXISTS.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("to_bow_m", "to_stern_m", "to_port_m", "to_starboard_m")


def upgrade() -> None:
    for column in COLUMNS:
        op.execute(f"ALTER TABLE ais_vessels ADD COLUMN IF NOT EXISTS {column} real CHECK ({column} >= 0)")


def downgrade() -> None:
    for column in COLUMNS:
        op.execute(f"ALTER TABLE ais_vessels DROP COLUMN IF EXISTS {column}")
