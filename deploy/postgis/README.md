# Application database image

`Dockerfile` builds Postgres 16 plus PostGIS 3 on top of the official `postgres:16-bookworm` image, using the PGDG apt repository that image already configures.

## Why not `postgis/postgis`

The official `postgis/postgis` images (checked 2026-10-17: `16-3.4`, `16-3.5`, `17-3.5`, `18-3.6`, `latest`) publish **amd64 only**. On Apple Silicon they run under emulation, which is slow and prints platform warnings. `postgres:16-bookworm` is multi-arch (amd64, arm64, and others), and Debian packages PostGIS for both, so this image builds natively on either. Third-party multi-arch PostGIS images exist (e.g. `imresamu/postgis`), but a build from the official base keeps the supply chain short.

Verified on arm64: `docker build deploy/postgis` produces an `arm64` image and the full `tests/db` gate passes against it.

## Schema

The image contains no schema. `contracts/schema.sql` creates the PostGIS and pgcrypto extensions and every table; Alembic migration `0001` applies it verbatim (`alembic upgrade head`, URL from `DATABASE_URL`, e.g. `postgresql+psycopg://user:password@host:5432/dbname`). Run migrations as a role allowed to `CREATE EXTENSION` (the image's `POSTGRES_USER` is a superuser).

Migration `0001` reads `contracts/schema.sql` from the repository, so any image or job that runs `alembic` must contain both `migrations/` and `contracts/`.

## Grafana access

`schema.sql` creates `grafana_ro` as a `NOLOGIN` group role with `SELECT` on all tables, present and future. Grafana needs a login, so compose must create one as a member, e.g.

```sql
CREATE ROLE grafana LOGIN PASSWORD '...' IN ROLE grafana_ro;
```

`grafana_ro` is cluster-wide. Downgrading one database keeps it while another database still grants to it.

## Gate

```sh
uv run pytest tests/db
```

Builds this image, starts one container, migrates a template database once, and clones it per test. Skips cleanly without Docker.
