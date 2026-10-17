# SeaweedFS as the S3-compatible object store

Status: accepted

The plan named MinIO, but MinIO no longer publishes public container images (`minio/minio`, `quay.io/minio/minio` and `ghcr.io/minio/minio` all fail to pull, checked 2026-10-17). The pipeline only uses plain S3 calls (put, get, head, delete, create bucket), so compose runs SeaweedFS (`chrislusf/seaweedfs`, Apache-2.0, S3 API on port 8333) and the code and configuration are named after the capability, not the product: `S3ObjectStore`, `STORE_ENDPOINT_URL`, `STORE_ACCESS_KEY`, `STORE_SECRET_KEY`, `STORE_BUCKET`.

## Considered Options

- **RustFS**: pullable and MinIO-compatible, but young and self-described as not production ready.
- **Garage**: solid, but needs a cluster layout step before it accepts data, which complicates a one-command start.
- **Building MinIO from source**: slow image builds and a project that has moved away from container distribution.

## Consequences

Any S3 endpoint works by changing `STORE_ENDPOINT_URL` and the keys, including a managed bucket in a real deployment.
