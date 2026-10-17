"""The Grafana role is read-only, including on tables created after the migration."""

import psycopg
import pytest
from psycopg import errors

Conn = psycopg.Connection[tuple[object, ...]]

SQUARE = "ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326)"

TABLES = ["regions", "scenes", "detections", "validations", "ais_positions", "ais_loaded_days", "jobs"]


@pytest.fixture
def as_grafana(db: Conn) -> Conn:
    db.execute(f"INSERT INTO regions VALUES ('r', 'R', {SQUARE})")
    db.execute("SET ROLE grafana_ro")
    return db


@pytest.mark.parametrize("table", TABLES)
def test_grafana_can_select(as_grafana: Conn, table: str) -> None:
    as_grafana.execute(f"SELECT count(*) FROM {table}").fetchone()


def test_grafana_cannot_write(as_grafana: Conn) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        as_grafana.execute(f"INSERT INTO regions VALUES ('x', 'X', {SQUARE})")
    with pytest.raises(errors.InsufficientPrivilege):
        as_grafana.execute("UPDATE regions SET name = 'changed'")
    with pytest.raises(errors.InsufficientPrivilege):
        as_grafana.execute("DELETE FROM regions")
    with pytest.raises(errors.InsufficientPrivilege):
        as_grafana.execute("TRUNCATE regions")


def test_grafana_cannot_change_schema(as_grafana: Conn) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        as_grafana.execute("CREATE TABLE sneaky (id int)")
    with pytest.raises(errors.InsufficientPrivilege):
        as_grafana.execute("DROP TABLE regions")


def test_grafana_sees_tables_created_later(db: Conn) -> None:
    db.execute("CREATE TABLE added_later (id int)")
    db.execute("SET ROLE grafana_ro")
    db.execute("SELECT * FROM added_later")
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute("INSERT INTO added_later VALUES (1)")


def test_grafana_role_cannot_log_in_directly(db: Conn) -> None:
    row = db.execute("SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = 'grafana_ro'").fetchone()
    assert row == (False, False)
