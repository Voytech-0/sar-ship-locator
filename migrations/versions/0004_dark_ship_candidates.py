"""The dark_ship_candidates view (see contracts/schema.sql).

Fresh databases get the view from schema.sql (revision 0001), hence CREATE OR REPLACE.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17
"""

import re
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "contracts" / "schema.sql"


def upgrade() -> None:
    view = re.search(r"CREATE OR REPLACE VIEW dark_ship_candidates AS.*?;\n", SCHEMA_SQL.read_text(), re.S)
    assert view, "dark_ship_candidates view not found in contracts/schema.sql"
    op.execute(view.group(0))


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS dark_ship_candidates")
