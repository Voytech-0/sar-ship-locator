"""Keep the imaging geometry of a Scene (platform heading and speed, slant range, incidence).

`fetch` reads it from the product annotation; `validate` needs it after the raster is gone, hence a column.
Fresh databases get the column from schema.sql (revision 0001), hence IF NOT EXISTS.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE scenes ADD COLUMN IF NOT EXISTS sar_geometry jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE scenes DROP COLUMN IF EXISTS sar_geometry")
