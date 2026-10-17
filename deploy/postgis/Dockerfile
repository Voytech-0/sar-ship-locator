# Application database: official Postgres plus PostGIS from the PGDG apt repo already configured in the base image.
# The official postgis/postgis images are amd64-only; this builds natively on arm64 and amd64.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 postgresql-16-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
