"""Keep the VH contrast of a Detection (peak VH in the cluster over the surrounding sea, dB).

Null for Detections made without a VH band. Fresh databases get the column from schema.sql (revision 0001),
hence IF NOT EXISTS.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS vh_contrast_db real")


def downgrade() -> None:
    op.execute("ALTER TABLE detections DROP COLUMN IF EXISTS vh_contrast_db")
