"""Initial schema: applies contracts/schema.sql verbatim.

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "contracts" / "schema.sql"

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS jobs, ais_loaded_days, ais_positions, validations, detections, scenes, regions CASCADE;
DROP TYPE IF EXISTS shore_class, match_label, job_status, stage, scene_status;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM grafana_ro;
DROP OWNED BY grafana_ro;
-- The role is cluster-wide: drop it only if no other database still depends on it.
DO $$ BEGIN
    DROP ROLE IF EXISTS grafana_ro;
EXCEPTION WHEN dependent_objects_still_exist THEN
    RAISE NOTICE 'grafana_ro is still used by another database, keeping it';
END $$;
DROP EXTENSION IF EXISTS pgcrypto;
DROP EXTENSION IF EXISTS postgis;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(SCHEMA_SQL.read_text())


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
