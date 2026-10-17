"""Reference SQL for queries whose shape the database must support, written against contracts/schema.sql.

`GET /scenes?ais_ready=` (contracts/openapi.yaml): a Scene is AIS-ready when every UTC day touched by
[acquired_at - margin, acquired_at + margin] has a row in `ais_loaded_days` for the Scene's Region. The margin
is the Region's `validation.ais_margin_minutes`. The API stream owns the production implementation; this is
the tested reference for what the schema must be able to answer.
"""

AIS_READY_PREDICATE = """
NOT EXISTS (
    SELECT 1
    FROM generate_series(
        ((s.acquired_at - make_interval(mins => %(margin_minutes)s)) AT TIME ZONE 'UTC')::date,
        ((s.acquired_at + make_interval(mins => %(margin_minutes)s)) AT TIME ZONE 'UTC')::date,
        interval '1 day'
    ) AS needed(day)
    LEFT JOIN ais_loaded_days l ON l.region_id = s.region_id AND l.day = needed.day::date
    WHERE l.day IS NULL
)
"""


def scenes_by_ais_readiness(ais_ready: bool | None) -> str:
    """SQL listing scene ids, newest acquisition first. Named parameter: `margin_minutes`."""
    where = {
        None: "TRUE",
        True: AIS_READY_PREDICATE,
        False: f"NOT ({AIS_READY_PREDICATE})",
    }[ais_ready]
    return f"SELECT s.scene_id FROM scenes s WHERE {where} ORDER BY s.acquired_at DESC, s.scene_id"
