-- Application database schema (PostGIS). Alembic migration 0001 applies this file verbatim.
-- Geometries are WGS 84 (EPSG:4326), [lon, lat]. Timestamps are timestamptz (UTC).

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE TYPE scene_status AS ENUM ('discovered', 'fetched', 'detected', 'validated', 'failed');
CREATE TYPE stage AS ENUM ('discover', 'fetch', 'detect', 'ais-load', 'validate');
CREATE TYPE job_status AS ENUM ('queued', 'running', 'succeeded', 'failed');
CREATE TYPE match_label AS ENUM ('matched', 'unmatched', 'ambiguous');
CREATE TYPE shore_class AS ENUM ('open_water', 'near_shore');

CREATE TABLE regions (
    region_id  text PRIMARY KEY,
    name       text NOT NULL,
    polygon    geometry(Polygon, 4326) NOT NULL
);

CREATE TABLE scenes (
    scene_id      text PRIMARY KEY,                          -- Sentinel-1 product name
    region_id     text NOT NULL REFERENCES regions,
    acquired_at   timestamptz NOT NULL,
    footprint     geometry(Polygon, 4326) NOT NULL,
    status        scene_status NOT NULL DEFAULT 'discovered',
    failed_stage  stage,
    error         text,
    raster_key    text,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT failed_has_stage CHECK ((status = 'failed') = (failed_stage IS NOT NULL))
);
CREATE INDEX scenes_status_idx ON scenes (status, acquired_at DESC);
CREATE INDEX scenes_region_idx ON scenes (region_id, acquired_at DESC);

CREATE TABLE detections (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scene_id         text NOT NULL REFERENCES scenes ON DELETE CASCADE,
    detector_name    text NOT NULL,
    detector_version text NOT NULL,
    geom             geometry(Point, 4326) NOT NULL,
    confidence       real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    length_m         real CHECK (length_m >= 0),
    shore            shore_class NOT NULL,
    chip_key         text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX detections_scene_idx ON detections (scene_id, detector_name, detector_version);
CREATE INDEX detections_geom_idx ON detections USING gist (geom);

-- One row per Detection once its Scene is validated. Re-validation replaces the row.
CREATE TABLE validations (
    detection_id     uuid PRIMARY KEY REFERENCES detections ON DELETE CASCADE,
    label            match_label NOT NULL,
    matched_mmsi     bigint,
    match_distance_m real CHECK (match_distance_m >= 0),
    gate_m           real NOT NULL,                          -- gate in force when validated
    validated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT matched_has_vessel CHECK ((label = 'matched') = (matched_mmsi IS NOT NULL AND match_distance_m IS NOT NULL))
);
CREATE INDEX validations_label_idx ON validations (label);

CREATE TABLE ais_positions (
    mmsi      bigint NOT NULL,
    ts        timestamptz NOT NULL,
    geom      geometry(Point, 4326) NOT NULL,
    sog_knots real,
    cog_deg   real,
    heading   smallint,
    PRIMARY KEY (mmsi, ts)
);
CREATE INDEX ais_positions_ts_idx ON ais_positions (ts);
CREATE INDEX ais_positions_geom_idx ON ais_positions USING gist (geom);

-- UTC days of AIS fully loaded for a Region. Drives `ais_ready` on GET /scenes.
CREATE TABLE ais_loaded_days (
    region_id text NOT NULL REFERENCES regions,
    day       date NOT NULL,
    positions integer NOT NULL CHECK (positions >= 0),
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (region_id, day)
);

-- Job identity: stage + canonical input. Enforces idempotency for live and successful Jobs.
CREATE TABLE jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stage           stage NOT NULL,
    input           jsonb NOT NULL,
    status          job_status NOT NULL DEFAULT 'queued',
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    error_retryable boolean,
    result          jsonb,
    CONSTRAINT failed_has_error CHECK (status <> 'failed' OR error IS NOT NULL)
);
CREATE UNIQUE INDEX jobs_unique_active ON jobs (stage, input) WHERE status IN ('queued', 'running', 'succeeded');
CREATE INDEX jobs_status_idx ON jobs (status, created_at);

-- Read-only role for Grafana.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro NOLOGIN;
    END IF;
END $$;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
