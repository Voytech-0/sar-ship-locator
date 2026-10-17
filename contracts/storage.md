# Object storage layout

One MinIO bucket, `sar`. All keys are relative to it. Writers overwrite deterministically, so re-running a stage is safe.

| Key | Written by | Content |
|---|---|---|
| `scenes/{scene_id}/vv.tif` | `fetch` | VV band (sigma0-ready DN), cropped to the Region, GeoTIFF, EPSG:4326 or UTM 32N as delivered |
| `scenes/{scene_id}/meta.json` | `fetch` | Acquisition time, orbit, polarisations, calibration LUT reference, crop window |
| `chips/{scene_id}/{detection_id}.png` | `detect` | Chip: 64x64 px cutout around a Detection, 8-bit, contrast-stretched for review |
| `chips/{scene_id}/{detection_id}.npy` | `detect` | Same Chip as float32 calibrated values, for training |
| `datasets/{version}/manifest.json` | export command | Dataset manifest: list of Chip keys with labels and provenance |

## Rules

- `scene_id` and `detection_id` are used verbatim in keys. No other path components are user-controlled.
- Raw GRD products are never stored. Only the cropped raster is, and it may be deleted once the Scene is `detected` (config flag `retention.delete_raster_after_detect`).
- Chips are kept indefinitely. A Chip's label lives in Postgres (`validations`), never in the key.
- `datasets/{version}/` is immutable once written; a new export gets a new `version`.
