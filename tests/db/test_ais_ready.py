"""The schema can answer GET /scenes?ais_ready= (see queries.py for the semantics under test)."""

import psycopg

from .queries import scenes_by_ais_readiness

Conn = psycopg.Connection[tuple[object, ...]]

SQUARE = "ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326)"


def _scene(db: Conn, scene_id: str, acquired_at: str, region: str = "r") -> None:
    db.execute(
        f"INSERT INTO scenes (scene_id, region_id, acquired_at, footprint) VALUES (%s, %s, %s, {SQUARE})",
        (scene_id, region, acquired_at),
    )


def _load_day(db: Conn, day: str, region: str = "r") -> None:
    db.execute("INSERT INTO ais_loaded_days (region_id, day, positions) VALUES (%s, %s, 1)", (region, day))


def _ids(db: Conn, ais_ready: bool | None, margin: int = 30) -> list[str]:
    rows = db.execute(scenes_by_ais_readiness(ais_ready), {"margin_minutes": margin}).fetchall()
    return [str(r[0]) for r in rows]


def _setup(db: Conn) -> None:
    for region in ("r", "other"):
        db.execute(f"INSERT INTO regions VALUES (%s, %s, {SQUARE})", (region, region))


def test_scene_is_ready_once_its_day_is_loaded(db: Conn) -> None:
    _setup(db)
    _scene(db, "noon", "2026-10-14T12:00:00Z")
    assert _ids(db, True) == []
    assert _ids(db, False) == ["noon"]
    _load_day(db, "2026-10-14")
    assert _ids(db, True) == ["noon"]
    assert _ids(db, False) == []


def test_margin_across_midnight_needs_the_neighbouring_day(db: Conn) -> None:
    _setup(db)
    _scene(db, "late", "2026-10-14T23:50:00Z")  # +30 min reaches 15 Oct
    _scene(db, "early", "2026-10-14T00:10:00Z")  # -30 min reaches 13 Oct
    _load_day(db, "2026-10-14")
    assert _ids(db, True) == []
    _load_day(db, "2026-10-15")
    assert _ids(db, True) == ["late"]
    _load_day(db, "2026-10-13")
    assert _ids(db, True) == ["late", "early"]


def test_zero_margin_only_needs_the_acquisition_day(db: Conn) -> None:
    _setup(db)
    _scene(db, "late", "2026-10-14T23:59:00Z")
    _load_day(db, "2026-10-14")
    assert _ids(db, True, margin=0) == ["late"]
    assert _ids(db, True, margin=30) == []


def test_loaded_days_of_another_region_do_not_count(db: Conn) -> None:
    _setup(db)
    _scene(db, "mine", "2026-10-14T12:00:00Z", region="r")
    _load_day(db, "2026-10-14", region="other")
    assert _ids(db, True) == []
    _load_day(db, "2026-10-14", region="r")
    assert _ids(db, True) == ["mine"]


def test_days_are_utc_not_session_time_zone(db: Conn) -> None:
    _setup(db)
    _scene(db, "s", "2026-10-14T23:50:00Z")
    _load_day(db, "2026-10-14")
    _load_day(db, "2026-10-15")
    db.execute("SET TIME ZONE 'Pacific/Auckland'")
    assert _ids(db, True) == ["s"]
    db.execute("SET TIME ZONE 'America/Los_Angeles'")
    assert _ids(db, True) == ["s"]


def test_ready_and_not_ready_partition_all_scenes(db: Conn) -> None:
    _setup(db)
    for i, day in enumerate(["2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]):
        _scene(db, f"s{i}", f"{day}T12:00:00Z")
    _load_day(db, "2026-10-11")
    _load_day(db, "2026-10-13")
    ready, waiting, everything = _ids(db, True), _ids(db, False), _ids(db, None)
    assert sorted(ready + waiting) == sorted(everything)
    assert set(ready) == {"s1", "s3"}
    assert everything == ["s3", "s2", "s1", "s0"]  # newest first
