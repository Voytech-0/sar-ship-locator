"""Keep the catalogue acquisition time next to the Region-window time.

`fetch` replaces `scenes.acquired_at` with the time of the Region's centre row, computed from the catalogue
start. Computing it from the already replaced value made every retry shift the time again, so the catalogue
start is kept in `acquired_start` and never overwritten. Scenes fetched before this migration get
`acquired_start = acquired_at`, which is already the shifted time (only affects development
databases). Fresh databases get the column from schema.sql
(revision 0001), hence IF NOT EXISTS.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE scenes ADD COLUMN IF NOT EXISTS acquired_start timestamptz")
    op.execute("UPDATE scenes SET acquired_start = acquired_at WHERE acquired_start IS NULL")
    op.execute("ALTER TABLE scenes ALTER COLUMN acquired_start SET NOT NULL")
    op.execute("""
        CREATE OR REPLACE FUNCTION scenes_default_acquired_start() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.acquired_start := COALESCE(NEW.acquired_start, NEW.acquired_at);
            RETURN NEW;
        END $$
    """)
    op.execute("DROP TRIGGER IF EXISTS scenes_acquired_start ON scenes")
    op.execute(
        "CREATE TRIGGER scenes_acquired_start BEFORE INSERT ON scenes "
        "FOR EACH ROW EXECUTE FUNCTION scenes_default_acquired_start()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS scenes_acquired_start ON scenes")
    op.execute("DROP FUNCTION IF EXISTS scenes_default_acquired_start()")
    op.execute("ALTER TABLE scenes DROP COLUMN IF EXISTS acquired_start")
