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
    acquired_at   timestamptz NOT NULL,                      -- time of the Region's rows once fetched (see acquired_start)
    acquired_start timestamptz NOT NULL,                     -- catalogue time (start of the acquisition), never changes
    footprint     geometry(Polygon, 4326) NOT NULL,
    status        scene_status NOT NULL DEFAULT 'discovered',
    failed_stage  stage,
    error         text,
    raster_key    text,
    sar_geometry  jsonb,  -- imaging geometry (SarGeometry) from the fetch stage, used by validation
    discovered_at timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT failed_has_stage CHECK ((status = 'failed') = (failed_stage IS NOT NULL))
);
-- The catalogue time defaults to acquired_at on insert (fetch later moves acquired_at, never acquired_start).
CREATE FUNCTION scenes_default_acquired_start() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.acquired_start := COALESCE(NEW.acquired_start, NEW.acquired_at);
    RETURN NEW;
END $$;
CREATE TRIGGER scenes_acquired_start BEFORE INSERT ON scenes
    FOR EACH ROW EXECUTE FUNCTION scenes_default_acquired_start();
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

-- Static vessel data from the AIS archive (one row per MMSI, newest non-empty values win).
CREATE TABLE ais_vessels (
    mmsi      bigint PRIMARY KEY,
    name      text,
    ship_type text,
    length_m  real CHECK (length_m > 0),
    width_m   real CHECK (width_m > 0),
    -- Antenna position on the hull (AIS A, B, C, D): metres to the bow, stern, port side and starboard side.
    to_bow_m       real CHECK (to_bow_m >= 0),
    to_stern_m     real CHECK (to_stern_m >= 0),
    to_port_m      real CHECK (to_port_m >= 0),
    to_starboard_m real CHECK (to_starboard_m >= 0)
);

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

-- Detections worth a second look: confident, ship-sized, in open water, and no AIS vessel at their position.
-- A candidate only, not proof (small vessels, AIS gaps and clutter remain possible). Thresholds are a starting
-- point chosen from three real scenes; tighten or relax them as more validated data accumulates.
CREATE OR REPLACE VIEW dark_ship_candidates AS
SELECT d.id AS detection_id, d.scene_id, s.acquired_at, ST_X(d.geom) AS lon, ST_Y(d.geom) AS lat, d.geom,
       d.confidence, d.length_m, d.detector_name, d.detector_version, d.chip_key
FROM detections d
JOIN validations v ON v.detection_id = d.id
JOIN scenes s ON s.scene_id = d.scene_id
WHERE v.label = 'unmatched' AND d.shore = 'open_water' AND d.length_m >= 100 AND d.confidence >= 0.9::real;

-- Read-only role for Grafana.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro NOLOGIN;
    END IF;
END $$;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
