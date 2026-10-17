"""Spatial queries work and the GiST indexes are actually used."""

import psycopg

Conn = psycopg.Connection[tuple[object, ...]]

SQUARE = "ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326)"


def _plan(db: Conn, sql: str) -> str:
    return "\n".join(str(r[0]) for r in db.execute(f"EXPLAIN {sql}").fetchall())


def test_footprints_intersecting_a_region(db: Conn) -> None:
    db.execute(
        "INSERT INTO regions VALUES ('gb', 'Great Belt', "
        "ST_GeomFromText('POLYGON((10.7 55.0,11.3 55.0,11.3 55.7,10.7 55.7,10.7 55.0))', 4326))"
    )
    footprints = {
        "overlaps": "POLYGON((10.0 55.3,11.0 55.3,11.0 56.5,10.0 56.5,10.0 55.3))",
        "inside": "POLYGON((10.8 55.1,11.0 55.1,11.0 55.2,10.8 55.2,10.8 55.1))",
        "far": "POLYGON((8.0 54.0,9.0 54.0,9.0 54.5,8.0 54.5,8.0 54.0))",
    }
    for scene_id, wkt in footprints.items():
        db.execute(
            "INSERT INTO scenes (scene_id, region_id, acquired_at, footprint) "
            "VALUES (%s, 'gb', now(), ST_GeomFromText(%s, 4326))",
            (scene_id, wkt),
        )
    rows = db.execute(
        "SELECT s.scene_id FROM scenes s JOIN regions r ON ST_Intersects(s.footprint, r.polygon) "
        "WHERE r.region_id = 'gb' ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == ["inside", "overlaps"]


def test_distance_in_metres_uses_geography_cast(db: Conn) -> None:
    """The match gate is in metres; geography casts give it without projecting by hand."""
    row = db.execute(
        "SELECT ST_Distance(a::geography, b::geography) FROM "
        "(SELECT ST_SetSRID(ST_MakePoint(11.0, 55.0), 4326) AS a, "
        "ST_SetSRID(ST_MakePoint(11.0, 55.0045), 4326) AS b) p"
    ).fetchone()
    assert row is not None
    assert 480 < float(str(row[0])) < 520  # 0.0045 degrees of latitude is about 500 m


def test_ais_geom_index_serves_bbox_queries(db: Conn) -> None:
    db.execute(
        "INSERT INTO ais_positions (mmsi, ts, geom) "
        "SELECT 200000000 + (i % 500), timestamptz '2026-10-14' + i * interval '1 second', "
        "ST_SetSRID(ST_MakePoint(8 + (i % 700) * 0.01, 54 + (i / 700) * 0.01), 4326) "
        "FROM generate_series(0, 49999) i"
    )
    db.execute("ANALYZE ais_positions")
    plan = _plan(
        db, "SELECT * FROM ais_positions WHERE geom && ST_MakeEnvelope(10.70, 55.00, 10.72, 55.02, 4326)"
    )
    assert "ais_positions_geom_idx" in plan, plan


def test_detection_geom_index_serves_bbox_queries(db: Conn) -> None:
    db.execute(f"INSERT INTO regions VALUES ('r', 'R', {SQUARE})")
    db.execute(
        f"INSERT INTO scenes (scene_id, region_id, acquired_at, footprint) VALUES ('s', 'r', now(), {SQUARE})"
    )
    db.execute(
        "INSERT INTO detections (scene_id, detector_name, detector_version, geom, confidence, shore) "
        "SELECT 's', 'ca-cfar', '1', "
        "ST_SetSRID(ST_MakePoint(8 + (i % 700) * 0.01, 54 + (i / 700) * 0.01), 4326), "
        "0.5, 'open_water' FROM generate_series(0, 49999) i"
    )
    db.execute("ANALYZE detections")
    plan = _plan(
        db, "SELECT * FROM detections WHERE geom && ST_MakeEnvelope(10.70, 55.00, 10.72, 55.02, 4326)"
    )
    assert "detections_geom_idx" in plan, plan


def test_ais_time_index_serves_acquisition_window_queries(db: Conn) -> None:
    db.execute(
        "INSERT INTO ais_positions (mmsi, ts, geom) "
        "SELECT 200000000 + (i % 500), timestamptz '2026-10-01' + i * interval '10 seconds', "
        "ST_SetSRID(ST_MakePoint(10.9, 55.3), 4326) FROM generate_series(0, 99999) i"
    )
    db.execute("ANALYZE ais_positions")
    plan = _plan(
        db,
        "SELECT * FROM ais_positions WHERE ts BETWEEN '2026-10-14T05:00:00Z' AND '2026-10-14T06:00:00Z'",
    )
    assert "ais_positions_ts_idx" in plan, plan
