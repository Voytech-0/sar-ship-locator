"""PostgreSQL/PostGIS access with plain SQL. `contracts/schema.sql` is the source of truth for the tables.

Every function takes an open connection, so callers decide transaction boundaries. Rows come back as dicts
whose keys already match the API field names; enums and uuids are cast to text in SQL.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

Conn = psycopg.Connection[dict[str, Any]]
Row = dict[str, Any]

JOB_COLUMNS = (
    "id::text AS id, stage::text AS stage, input, status::text AS status, created_at, started_at, "
    "finished_at, error, error_retryable, result"
)
LIVE_JOB_STATUSES = ("queued", "running", "succeeded")  # matches the partial unique index on jobs


class Database:
    """Connection factory: one short-lived connection per unit of work (no pool needed at this scale)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def connection(self) -> Iterator[Conn]:
        """Commits on clean exit, rolls back on error."""
        with psycopg.connect(self._dsn, row_factory=dict_row, options="-c TimeZone=UTC") as conn:
            yield conn

    def ping(self) -> None:
        with self.connection() as conn:
            conn.execute("SELECT 1")


def storable(value: str) -> bool:
    """False for strings PostgreSQL cannot hold: NUL characters and lone surrogates."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --- regions --------------------------------------------------------------------------------------


def ring_to_geojson(ring: Sequence[Sequence[float]]) -> str:
    return json.dumps({"type": "Polygon", "coordinates": [[list(p) for p in ring]]})


def upsert_region(conn: Conn, region_id: str, name: str, ring: Sequence[Sequence[float]]) -> None:
    conn.execute(
        """
        INSERT INTO regions (region_id, name, polygon)
        VALUES (%s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
        ON CONFLICT (region_id) DO UPDATE SET name = EXCLUDED.name, polygon = EXCLUDED.polygon
        """,
        (region_id, name, ring_to_geojson(ring)),
    )


# --- scenes ---------------------------------------------------------------------------------------

_SCENE_SELECT = """
    SELECT s.scene_id, s.region_id, s.acquired_at, s.status::text AS status,
           s.failed_stage::text AS failed_stage, s.error, ST_AsGeoJSON(s.footprint)::jsonb AS footprint,
           s.raster_key, s.discovered_at, s.updated_at,
           CASE WHEN s.status IN ('detected', 'validated')
                  OR EXISTS (SELECT 1 FROM detections d WHERE d.scene_id = s.scene_id)
                THEN (SELECT count(*) FROM detections d WHERE d.scene_id = s.scene_id)::int
           END AS detection_count
    FROM scenes s
"""

_MARGINS_JOIN = (
    "LEFT JOIN unnest(%(rids)s::text[], %(mins)s::int[]) AS rm(region_id, margin_minutes) "
    "ON rm.region_id = s.region_id"
)

# True when every UTC day touched by [acquired_at - margin, acquired_at + margin] has AIS loaded.
_AIS_READY = """
    NOT EXISTS (
        SELECT 1
        FROM generate_series(
            ((s.acquired_at - make_interval(mins => COALESCE(rm.margin_minutes, 0)))
                AT TIME ZONE 'UTC')::date::timestamp,
            ((s.acquired_at + make_interval(mins => COALESCE(rm.margin_minutes, 0)))
                AT TIME ZONE 'UTC')::date::timestamp,
            interval '1 day') AS d(day)
        WHERE NOT EXISTS (
            SELECT 1 FROM ais_loaded_days a WHERE a.region_id = s.region_id AND a.day = d.day::date))
"""


def get_scene(conn: Conn, scene_id: str) -> Row | None:
    return conn.execute(_SCENE_SELECT + " WHERE s.scene_id = %s", (scene_id,)).fetchone()


def list_scenes(
    conn: Conn,
    *,
    region_id: str | None,
    status: str | None,
    ais_ready: bool | None,
    ais_margins: dict[str, int],
    limit: int,
    offset: int,
) -> tuple[list[Row], int]:
    where: list[str] = []
    params: dict[str, Any] = {
        "rids": list(ais_margins),
        "mins": list(ais_margins.values()),
        "limit": limit,
        "offset": offset,
    }
    if region_id is not None:
        where.append("s.region_id = %(region_id)s")
        params["region_id"] = region_id
    if status is not None:
        where.append("s.status = %(status)s::scene_status")
        params["status"] = status
    if ais_ready is not None:
        where.append(_AIS_READY if ais_ready else f"NOT ({_AIS_READY})")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT count(*) AS n FROM scenes s {_MARGINS_JOIN} {clause}", params).fetchone()
    rows = conn.execute(
        f"{_SCENE_SELECT} {_MARGINS_JOIN} {clause} "
        "ORDER BY s.acquired_at DESC, s.scene_id LIMIT %(limit)s OFFSET %(offset)s",
        params,
    ).fetchall()
    assert total is not None
    return rows, int(total["n"])


def insert_scene(
    conn: Conn,
    scene_id: str,
    region_id: str,
    acquired_at: datetime,
    footprint_ring: Sequence[Sequence[float]],
) -> bool:
    """Register a discovered Scene. Returns False if it already exists (discovery is idempotent)."""
    cur = conn.execute(
        """
        INSERT INTO scenes (scene_id, region_id, acquired_at, acquired_start, footprint)
        VALUES (%s, %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
        ON CONFLICT (scene_id) DO NOTHING
        """,
        (scene_id, region_id, acquired_at, acquired_at, ring_to_geojson(footprint_ring)),
    )
    return cur.rowcount == 1


def known_scene_ids(conn: Conn, scene_ids: Sequence[str]) -> set[str]:
    rows = conn.execute("SELECT scene_id FROM scenes WHERE scene_id = ANY(%s)", (list(scene_ids),)).fetchall()
    return {r["scene_id"] for r in rows}


def mark_scene_done(conn: Conn, scene_id: str, status: str, raster_key: str | None = None) -> None:
    """Move a Scene forward and clear any previous failure."""
    conn.execute(
        """
        UPDATE scenes
        SET status = %s::scene_status, failed_stage = NULL, error = NULL,
            raster_key = COALESCE(%s, raster_key), updated_at = now()
        WHERE scene_id = %s
        """,
        (status, raster_key, scene_id),
    )


def get_scene_start(conn: Conn, scene_id: str) -> datetime:
    """The catalogue acquisition time (start of the acquisition); unlike `acquired_at` it never changes."""
    row = conn.execute("SELECT acquired_start FROM scenes WHERE scene_id = %s", (scene_id,)).fetchone()
    assert row is not None
    start: datetime = row["acquired_start"]
    return start


def set_scene_acquired_at(conn: Conn, scene_id: str, acquired_at: datetime) -> None:
    conn.execute("UPDATE scenes SET acquired_at = %s WHERE scene_id = %s", (acquired_at, scene_id))


def mark_scene_failed(conn: Conn, scene_id: str, stage: str, error: str) -> None:
    conn.execute(
        """
        UPDATE scenes SET status = 'failed', failed_stage = %s::stage, error = %s, updated_at = now()
        WHERE scene_id = %s
        """,
        (stage, error, scene_id),
    )


# --- detections, validations, AIS -----------------------------------------------------------------


def list_detections(
    conn: Conn, scene_id: str, detector_name: str | None, detector_version: str | None
) -> list[Row]:
    where = ["d.scene_id = %(scene_id)s"]
    params: dict[str, Any] = {"scene_id": scene_id}
    if detector_name is not None:
        where.append("d.detector_name = %(name)s")
        params["name"] = detector_name
    if detector_version is not None:
        where.append("d.detector_version = %(version)s")
        params["version"] = detector_version
    return conn.execute(
        f"""
        SELECT d.id::text AS id, d.scene_id, d.detector_name, d.detector_version,
               ST_AsGeoJSON(d.geom)::jsonb AS geometry, d.confidence, d.length_m,
               d.shore::text AS shore_class, d.chip_key,
               v.label::text AS label, v.matched_mmsi, v.match_distance_m
        FROM detections d LEFT JOIN validations v ON v.detection_id = d.id
        WHERE {" AND ".join(where)}
        ORDER BY d.created_at, d.id
        """,
        params,
    ).fetchall()


def replace_detections(
    conn: Conn,
    scene_id: str,
    detector_name: str,
    detector_version: str,
    detections: Sequence[Row],
) -> list[str]:
    """Replace one Detector version's Detections for a Scene (re-running a stage is idempotent).

    Each detection dict needs `lon`, `lat`, `confidence`, `shore` and may carry `length_m`, `chip_key`.
    Returns the new detection ids.
    """
    conn.execute(
        "DELETE FROM detections WHERE scene_id = %s AND detector_name = %s AND detector_version = %s",
        (scene_id, detector_name, detector_version),
    )
    ids: list[str] = []
    for d in detections:
        row = conn.execute(
            """
            INSERT INTO detections
                (scene_id, detector_name, detector_version, geom, confidence, length_m, shore, chip_key)
            VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s::shore_class, %s)
            RETURNING id::text AS id
            """,
            (
                scene_id,
                detector_name,
                detector_version,
                d["lon"],
                d["lat"],
                d["confidence"],
                d.get("length_m"),
                d["shore"],
                d.get("chip_key"),
            ),
        ).fetchone()
        assert row is not None
        ids.append(row["id"])
    return ids


def scene_detection_ids(conn: Conn, scene_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text AS id FROM detections WHERE scene_id = %s ORDER BY created_at, id", (scene_id,)
    ).fetchall()
    return [r["id"] for r in rows]


def upsert_validation(
    conn: Conn,
    detection_id: str,
    label: str,
    matched_mmsi: int | None,
    match_distance_m: float | None,
    gate_m: float,
) -> None:
    conn.execute(
        """
        INSERT INTO validations (detection_id, label, matched_mmsi, match_distance_m, gate_m)
        VALUES (%s::uuid, %s::match_label, %s, %s, %s)
        ON CONFLICT (detection_id) DO UPDATE
        SET label = EXCLUDED.label, matched_mmsi = EXCLUDED.matched_mmsi,
            match_distance_m = EXCLUDED.match_distance_m, gate_m = EXCLUDED.gate_m,
            validated_at = now()
        """,
        (detection_id, label, matched_mmsi, match_distance_m, gate_m),
    )


def insert_ais_positions(conn: Conn, positions: Sequence[Row]) -> int:
    """Insert AIS Positions (`mmsi`, `ts`, `lon`, `lat`, optional `sog_knots`/`cog_deg`/`heading`).

    Returns the number of new rows (existing (mmsi, ts) pairs are skipped).
    """
    if not positions:
        return 0
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO ais_positions (mmsi, ts, geom, sog_knots, cog_deg, heading)
        VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)
        ON CONFLICT (mmsi, ts) DO NOTHING
        """,
        [
            (
                p["mmsi"],
                p["ts"],
                p["lon"],
                p["lat"],
                p.get("sog_knots"),
                p.get("cog_deg"),
                p.get("heading"),
            )
            for p in positions
        ],
    )
    return cur.rowcount


def upsert_ais_vessels(conn: Conn, vessels: Sequence[Row]) -> None:
    """Static vessel data (`mmsi` plus optional name, ship_type, length_m, width_m); non-empty values win."""
    if not vessels:
        return
    conn.cursor().executemany(
        """
        INSERT INTO ais_vessels (mmsi, name, ship_type, length_m, width_m) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (mmsi) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, ais_vessels.name),
            ship_type = COALESCE(EXCLUDED.ship_type, ais_vessels.ship_type),
            length_m = COALESCE(EXCLUDED.length_m, ais_vessels.length_m),
            width_m = COALESCE(EXCLUDED.width_m, ais_vessels.width_m)
        """,
        [
            (v["mmsi"], v.get("name"), v.get("ship_type"), v.get("length_m"), v.get("width_m"))
            for v in vessels
        ],
    )


def list_ais_vessels(conn: Conn, mmsis: Sequence[int]) -> list[Row]:
    return conn.execute(
        "SELECT mmsi, name, ship_type, length_m, width_m FROM ais_vessels WHERE mmsi = ANY(%s)",
        (list(mmsis),),
    ).fetchall()


def list_ais_positions(conn: Conn, start: datetime, end: datetime) -> list[Row]:
    """AIS Positions with `start <= ts <= end`: mmsi, ts, lon, lat, sog_knots, cog_deg, heading."""
    return conn.execute(
        """
        SELECT mmsi, ts, ST_X(geom) AS lon, ST_Y(geom) AS lat, sog_knots, cog_deg, heading
        FROM ais_positions WHERE ts BETWEEN %s AND %s ORDER BY mmsi, ts
        """,
        (start, end),
    ).fetchall()


def list_detection_points(conn: Conn, scene_id: str) -> list[Row]:
    """Every Detection of a Scene (all Detector versions): `id`, `lon`, `lat`, `length_m`."""
    return conn.execute(
        """
        SELECT id::text AS id, ST_X(geom) AS lon, ST_Y(geom) AS lat, length_m
        FROM detections WHERE scene_id = %s ORDER BY created_at, id
        """,
        (scene_id,),
    ).fetchall()


def scene_status_counts(conn: Conn) -> list[Row]:
    return conn.execute(
        "SELECT region_id, status::text AS status, count(*)::int AS n FROM scenes GROUP BY 1, 2"
    ).fetchall()


def jobs_in_flight_counts(conn: Conn) -> list[Row]:
    return conn.execute(
        "SELECT stage::text AS stage, count(*)::int AS n FROM jobs "
        "WHERE status IN ('queued', 'running') GROUP BY 1"
    ).fetchall()


def last_scene_acquired(conn: Conn) -> list[Row]:
    return conn.execute("SELECT region_id, max(acquired_at) AS t FROM scenes GROUP BY 1").fetchall()


def latest_ais_day(conn: Conn) -> list[Row]:
    return conn.execute("SELECT region_id, max(day) AS d FROM ais_loaded_days GROUP BY 1").fetchall()


def get_chip_key(conn: Conn, detection_id: str) -> str | None:
    """The object store key of a Detection's Chip PNG, or None (unknown id, or no Chip)."""
    row = conn.execute("SELECT chip_key FROM detections WHERE id = %s::uuid", (detection_id,)).fetchone()
    key: str | None = row["chip_key"] if row else None
    return key


def set_chip_keys(conn: Conn, chip_keys: dict[str, str]) -> None:
    for detection_id, key in chip_keys.items():
        conn.execute("UPDATE detections SET chip_key = %s WHERE id = %s::uuid", (key, detection_id))


def mark_ais_day_loaded(conn: Conn, region_id: str, day: date, positions: int) -> None:
    conn.execute(
        """
        INSERT INTO ais_loaded_days (region_id, day, positions) VALUES (%s, %s, %s)
        ON CONFLICT (region_id, day) DO UPDATE SET positions = EXCLUDED.positions, loaded_at = now()
        """,
        (region_id, day, positions),
    )


# --- jobs -----------------------------------------------------------------------------------------


def find_live_job(conn: Conn, stage: str, input_: Row) -> Row | None:
    return conn.execute(
        f"SELECT {JOB_COLUMNS} FROM jobs WHERE stage = %s::stage AND input = %s AND status::text = ANY(%s)",
        (stage, Jsonb(input_), list(LIVE_JOB_STATUSES)),
    ).fetchone()


def insert_job(conn: Conn, stage: str, input_: Row) -> tuple[Row, bool]:
    """Create a queued Job unless a live one exists for the same (stage, input). Returns (job, created).

    The partial unique index makes this race-safe: if a concurrent submit wins, we return its Job.
    """
    existing = find_live_job(conn, stage, input_)
    if existing is not None:
        return existing, False
    try:
        with conn.transaction():  # savepoint, so a unique violation leaves the outer transaction usable
            row = conn.execute(
                f"INSERT INTO jobs (stage, input) VALUES (%s::stage, %s) RETURNING {JOB_COLUMNS}",
                (stage, Jsonb(input_)),
            ).fetchone()
    except errors.UniqueViolation:
        existing = find_live_job(conn, stage, input_)
        assert existing is not None
        return existing, False
    assert row is not None
    return row, True


def get_job(conn: Conn, job_id: str) -> Row | None:
    return conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s::uuid", (job_id,)).fetchone()


def claim_next_job(conn: Conn) -> Row | None:
    """Atomically move the oldest queued Job to running (SKIP LOCKED lets workers share the queue)."""
    return conn.execute(
        f"""
        UPDATE jobs SET status = 'running', started_at = now()
        WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING {JOB_COLUMNS}
        """
    ).fetchone()


def finish_job_succeeded(conn: Conn, job_id: str, result: Row) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'succeeded', finished_at = now(), result = %s WHERE id = %s::uuid",
        (Jsonb(result), job_id),
    )


def finish_job_failed(conn: Conn, job_id: str, error: str, retryable: bool) -> None:
    conn.execute(
        """
        UPDATE jobs SET status = 'failed', finished_at = now(), error = %s, error_retryable = %s
        WHERE id = %s::uuid
        """,
        (error, retryable, job_id),
    )


def running_jobs(conn: Conn) -> list[Row]:
    return conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE status = 'running'").fetchall()
