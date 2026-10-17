"""Every CHECK, UNIQUE, FK and type constraint in contracts/schema.sql rejects bad data."""

import json
from typing import Any

import psycopg
import pytest
from psycopg import errors

Conn = psycopg.Connection[tuple[object, ...]]

SQUARE = "ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326)"


def add_region(db: Conn, region_id: str = "r") -> None:
    db.execute(f"INSERT INTO regions VALUES (%s, 'Region', {SQUARE})", (region_id,))


def add_scene(
    db: Conn,
    scene_id: str = "s",
    status: str = "discovered",
    failed_stage: str | None = None,
    error: str | None = None,
) -> None:
    if not db.execute("SELECT 1 FROM regions WHERE region_id = 'r'").fetchone():
        add_region(db)
    db.execute(
        "INSERT INTO scenes (scene_id, region_id, acquired_at, footprint, status, failed_stage, error) "
        f"VALUES (%s, 'r', '2026-10-14T05:32:04Z', {SQUARE}, %s::scene_status, %s::stage, %s)",
        (scene_id, status, failed_stage, error),
    )


def add_detection(db: Conn, scene_id: str = "s", **overrides: Any) -> str:
    row: dict[str, Any] = {"confidence": 0.9, "length_m": 120.0, "shore": "open_water"} | overrides
    found = db.execute(
        "INSERT INTO detections "
        "(scene_id, detector_name, detector_version, geom, confidence, length_m, shore) "
        "VALUES (%s, 'ca-cfar', '1', ST_SetSRID(ST_MakePoint(0.5, 0.5), 4326), %s, %s, %s::shore_class) "
        "RETURNING id::text",
        (scene_id, row["confidence"], row["length_m"], row["shore"]),
    ).fetchone()
    assert found is not None
    return str(found[0])


def add_job(
    db: Conn, stage: str = "fetch", input_: dict[str, Any] | None = None, status: str = "queued"
) -> None:
    error = "boom" if status == "failed" else None
    db.execute(
        "INSERT INTO jobs (stage, input, status, error) VALUES (%s::stage, %s, %s::job_status, %s)",
        (stage, json.dumps(input_ or {"scene_id": "a"}), status, error),
    )


# --- scenes -------------------------------------------------------------------------------------------------


def test_failed_scene_requires_failed_stage(db: Conn) -> None:
    with pytest.raises(errors.CheckViolation, match="failed_has_stage"):
        add_scene(db, status="failed")


def test_failed_stage_requires_failed_status(db: Conn) -> None:
    with pytest.raises(errors.CheckViolation, match="failed_has_stage"):
        add_scene(db, status="detected", failed_stage="fetch")


def test_failed_scene_with_stage_is_accepted(db: Conn) -> None:
    add_scene(db, status="failed", failed_stage="fetch", error="corrupt product")


def test_scene_needs_a_known_region(db: Conn) -> None:
    add_region(db)
    with pytest.raises(errors.ForeignKeyViolation):
        db.execute(
            f"INSERT INTO scenes (scene_id, region_id, acquired_at, footprint) "
            f"VALUES ('x', 'missing', now(), {SQUARE})"
        )


def test_scene_status_is_an_enum(db: Conn) -> None:
    with pytest.raises(errors.InvalidTextRepresentation):
        add_scene(db, status="bogus")


def test_scene_id_is_unique(db: Conn) -> None:
    add_scene(db)
    with pytest.raises(errors.UniqueViolation):
        add_scene(db)


# --- geometry typing ----------------------------------------------------------------------------------------


def test_footprint_must_be_a_polygon_in_4326(db: Conn) -> None:
    add_region(db)
    with pytest.raises(psycopg.Error, match=r"(?i)geometry"):
        db.execute(
            "INSERT INTO scenes (scene_id, region_id, acquired_at, footprint) "
            "VALUES ('x', 'r', now(), ST_SetSRID(ST_MakePoint(0, 0), 4326))"
        )
    with pytest.raises(psycopg.Error, match=r"(?i)srid"):
        db.execute(
            "INSERT INTO scenes (scene_id, region_id, acquired_at, footprint) "
            "VALUES ('x', 'r', now(), ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 3857))"
        )


def test_detection_geometry_must_be_a_point(db: Conn) -> None:
    add_scene(db)
    with pytest.raises(psycopg.Error, match=r"(?i)geometry"):
        db.execute(
            "INSERT INTO detections (scene_id, detector_name, detector_version, geom, confidence, shore) "
            f"VALUES ('s', 'd', '1', {SQUARE}, 0.5, 'open_water')"
        )


# --- detections ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 5])
def test_confidence_must_be_between_0_and_1(db: Conn, confidence: float) -> None:
    add_scene(db)
    with pytest.raises(errors.CheckViolation, match="confidence"):
        add_detection(db, confidence=confidence)


@pytest.mark.parametrize("confidence", [0, 0.5, 1])
def test_confidence_bounds_are_inclusive(db: Conn, confidence: float) -> None:
    add_scene(db)
    add_detection(db, confidence=confidence)


def test_length_cannot_be_negative_but_may_be_null(db: Conn) -> None:
    add_scene(db)
    with pytest.raises(errors.CheckViolation, match="length_m"):
        add_detection(db, length_m=-1)
    add_detection(db, length_m=None)


def test_on_land_is_not_a_storable_shore_class(db: Conn) -> None:
    add_scene(db)
    with pytest.raises(errors.InvalidTextRepresentation):
        add_detection(db, shore="on_land")


def test_detection_needs_an_existing_scene(db: Conn) -> None:
    with pytest.raises(errors.ForeignKeyViolation):
        add_detection(db, scene_id="nope")


def test_deleting_a_scene_cascades_to_detections_and_validations(db: Conn) -> None:
    add_scene(db)
    det = add_detection(db)
    db.execute("INSERT INTO validations (detection_id, label, gate_m) VALUES (%s, 'unmatched', 500)", (det,))
    db.execute("DELETE FROM scenes WHERE scene_id = 's'")
    assert db.execute("SELECT count(*) FROM detections").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM validations").fetchone() == (0,)


# --- validations --------------------------------------------------------------------------------------------


def _validate(db: Conn, det: str, label: str, mmsi: int | None, distance: float | None) -> None:
    db.execute(
        "INSERT INTO validations (detection_id, label, matched_mmsi, match_distance_m, gate_m) "
        "VALUES (%s, %s::match_label, %s, %s, 500)",
        (det, label, mmsi, distance),
    )


def test_matched_requires_mmsi_and_distance(db: Conn) -> None:
    add_scene(db)
    det = add_detection(db)
    for mmsi, distance in [(None, 10.0), (219000001, None), (None, None)]:
        with pytest.raises(errors.CheckViolation, match="matched_has_vessel"):
            _validate(db, det, "matched", mmsi, distance)
    _validate(db, det, "matched", 219000001, 10.0)


@pytest.mark.parametrize("label", ["unmatched", "ambiguous"])
def test_unmatched_and_ambiguous_must_not_carry_a_vessel(db: Conn, label: str) -> None:
    add_scene(db)
    det = add_detection(db)
    with pytest.raises(errors.CheckViolation, match="matched_has_vessel"):
        _validate(db, det, label, 219000001, 10.0)
    _validate(db, det, label, None, None)


def test_match_distance_cannot_be_negative(db: Conn) -> None:
    add_scene(db)
    det = add_detection(db)
    with pytest.raises(errors.CheckViolation):
        _validate(db, det, "matched", 219000001, -5.0)


def test_one_validation_per_detection(db: Conn) -> None:
    add_scene(db)
    det = add_detection(db)
    _validate(db, det, "unmatched", None, None)
    with pytest.raises(errors.UniqueViolation):
        _validate(db, det, "unmatched", None, None)


# --- ais ----------------------------------------------------------------------------------------------------


def test_ais_position_is_unique_per_vessel_and_time(db: Conn) -> None:
    sql = (
        "INSERT INTO ais_positions (mmsi, ts, geom) "
        "VALUES (219000001, '2026-10-14T05:32:00Z', ST_SetSRID(ST_MakePoint(10.9, 55.3), 4326))"
    )
    db.execute(sql)
    with pytest.raises(errors.UniqueViolation):
        db.execute(sql)


def test_ais_loaded_days_primary_key_and_count(db: Conn) -> None:
    add_region(db)
    db.execute("INSERT INTO ais_loaded_days (region_id, day, positions) VALUES ('r', '2026-10-14', 10)")
    with pytest.raises(errors.UniqueViolation):
        db.execute("INSERT INTO ais_loaded_days (region_id, day, positions) VALUES ('r', '2026-10-14', 11)")
    with pytest.raises(errors.CheckViolation):
        db.execute("INSERT INTO ais_loaded_days (region_id, day, positions) VALUES ('r', '2026-10-15', -1)")
    with pytest.raises(errors.ForeignKeyViolation):
        db.execute("INSERT INTO ais_loaded_days (region_id, day, positions) VALUES ('nope', '2026-10-16', 1)")


# --- jobs ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_duplicate_live_job_is_rejected(db: Conn, status: str) -> None:
    add_job(db, status=status)
    with pytest.raises(errors.UniqueViolation, match="jobs_unique_active"):
        add_job(db)


def test_a_failed_job_does_not_block_a_retry(db: Conn) -> None:
    add_job(db, status="failed")
    add_job(db, status="queued")  # the retry
    with pytest.raises(errors.UniqueViolation, match="jobs_unique_active"):
        add_job(db)  # but only one live Job at a time
    add_job(db, status="failed")  # further failed attempts may pile up


def test_job_identity_ignores_json_key_order(db: Conn) -> None:
    add_job(db, "detect", {"scene_id": "a", "detector": {"name": "ca-cfar", "version": "1"}})
    with pytest.raises(errors.UniqueViolation, match="jobs_unique_active"):
        add_job(db, "detect", {"detector": {"version": "1", "name": "ca-cfar"}, "scene_id": "a"})


def test_same_input_in_a_different_stage_is_a_different_job(db: Conn) -> None:
    add_job(db, "fetch")
    add_job(db, "detect")


def test_failed_job_must_record_an_error(db: Conn) -> None:
    with pytest.raises(errors.CheckViolation, match="failed_has_error"):
        db.execute("INSERT INTO jobs (stage, input, status) VALUES ('fetch', '{}', 'failed')")


def test_job_stage_is_an_enum(db: Conn) -> None:
    with pytest.raises(errors.InvalidTextRepresentation):
        db.execute("INSERT INTO jobs (stage, input) VALUES ('load', '{}')")
