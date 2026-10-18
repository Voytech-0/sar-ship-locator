# Object storage layout

One bucket in the S3-compatible object store, `sar`. All keys are relative to it. Writers overwrite deterministically, so re-running a stage is safe.

| Key | Written by | Content |
|---|---|---|
| `scenes/{scene_id}/sigma0.npy` | `fetch` | VV band as linear sigma0 (float32 `.npy`), cropped to the Region window; NaN outside the Region polygon. Sentinel-1 GRD has no CRS, so it is not a GeoTIFF |
| `scenes/{scene_id}/geo.npz` | `fetch` | Lon/lat lattice (every 32 px) sampled from the GCP geolocation; `detect` rebuilds `SceneRaster.geolocate` by bilinear interpolation |
| `scenes/{scene_id}/meta.json` | `fetch` | Scene id, acquisition time of the window's centre row, shape, coarse affine, bytes downloaded |
| `scenes/{scene_id}/preview.png` | `detect` | VV preview for the review website: grey + alpha PNG on a north-up lon/lat grid (30 m px), dB-stretched like Chips; alpha 0 outside the Region. Kept after the raster is deleted |
| `scenes/{scene_id}/preview.json` | `detect` | `bounds` `[[south, west], [north, east]]` of `preview.png` (Leaflet `ImageOverlay` order) and `pixel_m` |
| `chips/{scene_id}/{detection_id}.png` | `detect` | Chip: 64x64 px cutout around a Detection, 8-bit, contrast-stretched for review |
| `chips/{scene_id}/{detection_id}.npy` | `detect` | Same Chip as float32 calibrated values, for training |
| `datasets/{version}/manifest.json` | `python -m pipeline.dataset` | Dataset manifest: every example with label, split, position, detector, AIS match and Chip keys, plus counts and the weak-label caveat. Written last, so its presence marks a complete dataset |
| `datasets/{version}/chips.npz` | `python -m pipeline.dataset` | Training bundle: `chips` (N x 64 x 64 float32), `target` (1 matched, 0 unmatched, -1 ambiguous), `split`, `detection_id`; rows line up with the manifest |

## Rules

- `scene_id` and `detection_id` are used verbatim in keys. No other path components are user-controlled.
- Raw GRD products are never stored. Only the cropped raster is, and it is kept for `retention.raster_days` after the Scene's last stage so `detect` can run again (0 = deleted right after `detect`). Expired rasters are removed whenever a `detect` finishes.
- Chips are kept indefinitely. A Chip's label lives in Postgres (`validations`), never in the key.
- `datasets/{version}/` is immutable once written; a new export gets a new `version`.
