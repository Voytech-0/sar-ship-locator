# fake-sources

A tiny stand-in for the two external HTTP sources, serving the fixtures in `tests/fixtures/`. Standard library only.

| Path | Mimics | Notes |
|---|---|---|
| `GET /odata/v1/Products` | CDSE OData catalogue | one record for the fixture Scene in the real response shape; `$top` and the `ContentDate/Start gt <iso>` part of `$filter` are honoured, the rest of the filter is ignored |
| `GET /aisdk-2026-10-14.zip` | DMA daily AIS archive | zip containing `aisdk-2026-10-14.csv`; any other day answers 404 like the real bucket does inside its publication lag |
| `GET /healthz` | n/a | `{"status": "ok"}` |

Not served here: ranged GETs of the Scene TIFF. Use an S3-compatible store seeded from `tests/fixtures/s3/` (MinIO in compose, moto in unit tests); see `tests/fixtures/README.md`.

## Run

Standalone (from anywhere in the repo):

```sh
PORT=8080 python deploy/fake-sources/app.py
curl localhost:8080/healthz
```

Docker (build context is the repo root):

```sh
docker build -f deploy/fake-sources/Dockerfile -t fake-sources .
docker run --rm -p 8080:8080 fake-sources
```

Environment: `PORT` (default 8080), `FIXTURES_DIR` (default `tests/fixtures`; the image sets `/fixtures`).

Compose service sketch (for whoever owns the compose file):

```yaml
fake-sources:
  build: {context: ., dockerfile: deploy/fake-sources/Dockerfile}
  ports: ["8080:8080"]
```
