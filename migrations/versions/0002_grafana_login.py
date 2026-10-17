"""Login role for Grafana: `grafana`, a member of the read-only `grafana_ro`.

contracts/schema.sql creates grafana_ro as NOLOGIN. When GRAFANA_DB_PASSWORD is set, this creates the LOGIN
role Grafana's PostGIS datasource connects as. Without the variable nothing is created.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    password = os.environ.get("GRAFANA_DB_PASSWORD")
    if not password:
        return
    conn = op.get_bind()
    literal = conn.execute(sa.text("SELECT quote_literal(:p)"), {"p": password}).scalar_one()
    exists = conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'grafana'")).first()
    if exists:
        op.execute(f"ALTER ROLE grafana LOGIN PASSWORD {literal}")
    else:
        op.execute(f"CREATE ROLE grafana LOGIN PASSWORD {literal} IN ROLE grafana_ro")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            DROP ROLE IF EXISTS grafana;
        EXCEPTION WHEN dependent_objects_still_exist THEN
            RAISE NOTICE 'grafana is still used by another database, keeping it';
        END $$;
    """)
