"""Migration 0001 applies contracts/schema.sql, reverses cleanly, and can be re-applied."""

from pathlib import Path

import psycopg
import pytest
from alembic import command

from .conftest import ROOT, Server, alembic_config

APP_TABLES = {
    "ais_loaded_days",
    "ais_positions",
    "detections",
    "jobs",
    "regions",
    "scenes",
    "validations",
}
APP_TYPES = {"job_status", "match_label", "scene_status", "shore_class", "stage"}


def _objects(conn: psycopg.Connection[tuple[object, ...]]) -> tuple[set[str], set[str], set[str]]:
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    }
    types = {
        r[0]
        for r in conn.execute(
            "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' AND t.typtype = 'e'"
        ).fetchall()
    }
    extensions = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
    return {str(t) for t in tables}, {str(t) for t in types}, {str(e) for e in extensions}


def _columns(conn: psycopg.Connection[tuple[object, ...]]) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ANY(%s) "
        "ORDER BY table_name, ordinal_position",
        (sorted(APP_TABLES),),
    ).fetchall()


def _indexes(conn: psycopg.Connection[tuple[object, ...]]) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s) ORDER BY 1",
        (sorted(APP_TABLES),),
    ).fetchall()


def test_upgrade_creates_application_objects(server: Server, empty_db: str) -> None:
    command.upgrade(alembic_config(server.url(empty_db)), "head")
    with psycopg.connect(server.dsn(empty_db)) as conn:
        tables, types, extensions = _objects(conn)
    assert tables >= APP_TABLES
    assert types == APP_TYPES
    assert {"postgis", "pgcrypto"} <= extensions
    assert "alembic_version" in tables


def test_migration_matches_schema_sql_applied_directly(server: Server, empty_db: str) -> None:
    """The migration must not drift from the contract: same columns and indexes as the raw file."""
    command.upgrade(alembic_config(server.url(empty_db)), "head")
    raw = server.create_db()
    with psycopg.connect(server.dsn(raw), autocommit=True) as conn:
        conn.execute((ROOT / "contracts" / "schema.sql").read_text())
    with psycopg.connect(server.dsn(empty_db)) as migrated, psycopg.connect(server.dsn(raw)) as direct:
        assert _columns(migrated) == _columns(direct)
        assert _indexes(migrated) == _indexes(direct)
        assert _objects(direct)[1] == APP_TYPES


def test_downgrade_removes_everything_it_created(server: Server, empty_db: str) -> None:
    cfg = alembic_config(server.url(empty_db))
    with psycopg.connect(server.dsn(empty_db)) as conn:
        baseline = _objects(conn)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    with psycopg.connect(server.dsn(empty_db)) as conn:
        tables, types, extensions = _objects(conn)
    assert types == baseline[1]
    assert extensions == baseline[2]
    assert tables - {"alembic_version"} == baseline[0]
    assert not (APP_TABLES & tables)


def test_up_down_up_cycle(server: Server, empty_db: str) -> None:
    cfg = alembic_config(server.url(empty_db))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    with psycopg.connect(server.dsn(empty_db)) as conn:
        tables, types, _ = _objects(conn)
        assert tables >= APP_TABLES
        assert types == APP_TYPES
        # The restored schema is usable, including the Grafana role grants.
        conn.execute("SET ROLE grafana_ro")
        assert conn.execute("SELECT count(*) FROM scenes").fetchone() == (0,)


def test_upgrade_head_twice_is_a_noop(server: Server, empty_db: str) -> None:
    cfg = alembic_config(server.url(empty_db))
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    with psycopg.connect(server.dsn(empty_db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0003",)]


def test_downgrade_keeps_role_while_another_database_uses_it(server: Server, empty_db: str) -> None:
    """grafana_ro is cluster-wide; downgrading one database must not break a sibling."""
    other = server.create_db()
    command.upgrade(alembic_config(server.url(other)), "head")
    cfg = alembic_config(server.url(empty_db))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    with psycopg.connect(server.dsn(other)) as conn:
        conn.execute("SET ROLE grafana_ro")
        assert conn.execute("SELECT count(*) FROM regions").fetchone() == (0,)


def test_revision_file_applies_the_contract_path() -> None:
    from importlib import util

    spec = util.spec_from_file_location(
        "rev0001", ROOT / "migrations" / "versions" / "0001_initial_schema.py"
    )
    assert spec and spec.loader
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.SCHEMA_SQL) == ROOT / "contracts" / "schema.sql"
    assert module.SCHEMA_SQL.is_file()


def test_grafana_login_role_is_read_only_and_follows_the_password(
    server: Server, empty_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg.conninfo import make_conninfo

    cfg = alembic_config(server.url(empty_db))
    monkeypatch.setenv("GRAFANA_DB_PASSWORD", "first pw with 'quotes'")
    command.upgrade(cfg, "head")
    try:
        dsn = make_conninfo(server.dsn(empty_db), user="grafana", password="first pw with 'quotes'")
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM scenes").fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM scenes")

        # a later run with a new password rotates it (the role already exists)
        command.downgrade(cfg, "0001")
        monkeypatch.setenv("GRAFANA_DB_PASSWORD", "second")
        command.upgrade(cfg, "head")
        with psycopg.connect(make_conninfo(server.dsn(empty_db), user="grafana", password="second")) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        command.downgrade(cfg, "base")
