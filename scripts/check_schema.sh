#!/usr/bin/env bash
# Applies contracts/schema.sql to an empty PostGIS and probes its key constraints.
set -euo pipefail
cd "$(dirname "$0")/.."

name="sar-schema-check-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$name" -e POSTGRES_PASSWORD=x postgis/postgis:16-3.4 >/dev/null

for _ in $(seq 60); do
  # pg_isready passes during the init-phase restart too; require a real query to succeed twice.
  docker exec "$name" psql -U postgres -tAc 'select 1' >/dev/null 2>&1 && break
  sleep 1
done
sleep 3

psql() { docker exec -i "$name" psql -U postgres -v ON_ERROR_STOP=1 -q "$@"; }
expect_fail() { if psql -c "$1" >/dev/null 2>&1; then echo "FAIL: expected error: $2" >&2; exit 1; fi; }

psql < contracts/schema.sql
psql -c "INSERT INTO regions VALUES ('r','R',ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))',4326));"
expect_fail "INSERT INTO scenes(scene_id,region_id,acquired_at,footprint,status) VALUES ('s','r',now(),ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))',4326),'failed');" \
  "failed Scene without failed_stage"
psql -c "INSERT INTO jobs(stage,input) VALUES ('fetch','{\"scene_id\":\"a\"}');"
expect_fail "INSERT INTO jobs(stage,input) VALUES ('fetch','{\"scene_id\":\"a\"}');" "duplicate live Job"
echo "schema ok"
