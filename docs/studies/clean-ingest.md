# Clean end-to-end ingest with detector v6

Task B of `docs/WORKPLAN.md`. Why: the local stack's Scenes were never fetched by the current `fetch` code
(VH, noise-subtracted rasters and `target_height_m` came from experiment files or hand patches).

## Baseline before the clean ingest (2026-11-11, local DB, detector v6, open water)

Read from the dev DB (`detections` joined to `validations`). Matches `AGENTS.md`: 51 matched, 6 unmatched,
3 `dark_ship_candidates`; plus 1 ambiguous. Median / p90 match distance over all matched: 35 m / 68 m.

| Scene (start) | matched | unmatched | ambiguous | median m | p90 m |
|---|---|---|---|---|---|
| 2026-10-12 17:00:44 | 18 | 6 | 0 | 34 | 67 |
| 2026-10-13 05:40:05 | 2 | 0 | 0 | 37 | 54 |
| 2026-10-13 16:52:11 | 13 | 0 | 0 | 35 | 60 |
| 2026-10-13 16:52:36 | 0 | 0 | 0 | | |
| 2026-10-14 05:32:04 | 10 | 0 | 0 | 37 | 101 |
| 2026-10-14 05:32:29 | 3 | 0 | 0 | 33 | 35 |
| 2026-10-15 05:23:38 | 3 | 0 | 1 | 59 | 69 |
| 2026-10-15 05:24:03 | 2 | 0 | 0 | 39 | 54 |
| 2026-10-17 17:08:47 | 0 | 0 | 0 | | |
| **total** | **51** | **6** | **1** | 35 | 68 |

Two things differ from the wording in `AGENTS.md`: the 9 Scenes are 12 to 15 Oct plus one of 17 Oct, and the
17 Oct Scene has no v6 Detections at all (only v2 to v4: 8, 8 and 1 unmatched), so it counts in neither
direction. The 13 Oct 16:52:36 Scene has v6 Detections but none in open water.

`dark_ship_candidates` (v6, all in the 12 Oct evening Scene):

| lon | lat | length m | confidence |
|---|---|---|---|
| 10.8839 | 55.5447 | 114 | 0.94 |
| 10.8752 | 55.5154 | 149 | 0.99 |
| 11.0454 | 55.1146 | 211 | 0.99 |

(Task C's candidates `AGENTS.md` lists by length 105/121/207/109 m belong to v3; the v6 lengths above are
from the stored v6 rows. Candidate 55.3231 N 11.0001 E was dropped by the VH gate in v5.)

## B1: one Scene re-fetched through the real code (2026-11-11)

Scene `S1D_IW_GRDH_1SDV_20261014T053204_..._0CC6` (14 Oct 05:32:04). With the user's yes, its `sigma0.npy`,
`sigma0_vh.npy`, `geo.npz` and `meta.json` were deleted from the store and `scenes.raster_key` was set to NULL
(what retention does; the API refuses `fetch` while the key is set, and deleting only the objects was not
enough). Then `fetch`, `detect`, `validate` through the API, each with `force: true`.

- `fetch`: succeeded, 800 757 152 bytes (VV + VH). `sar_geometry` came back from the annotation with
  `target_height_m` 42.0 (no hand patch).
- `detect` v6: 10 Detections, 0 dropped on land. The old v6 rows were replaced, not duplicated (one v6 batch).
- `validate`: succeeded (result over all versions and shores: 60 matched, 4 ambiguous, 22 unmatched).

| v6, open water | baseline (experiment files) | real re-fetch |
|---|---|---|
| matched | 10 | 10 |
| unmatched | 0 | 0 |
| ambiguous | 0 | 0 |
| median / p90 match distance m | 37 / 101 | 37 / 101 |
| mean VH contrast dB | not stored per version in the baseline query | 32.6 (v5 on experiment files: 25.9) |

No difference in counts or match distances. The VH contrast is about 6.7 dB higher than v5's, in line with the
3-9 dB the noise subtraction should add. The baseline table covered open water only, so near-shore v6 rows of
this Scene were not compared.

Side effect: starting the old stack woke the Airflow DAGs, which began ingesting later Scenes (and queued
their fetches ahead of this one) into the old DB. They are wiped in B2.

## B2: fresh stack, ingest through Airflow (2026-11-12)

Procedure. With the user's yes, `docker compose down`, then removed the volumes `sar_pg-data`, `sar_store-data`,
`sar_airflow-db-data`, `sar_airflow-logs` (kept `sar_land-data`). This deleted the Detections of versions 2 to 5 and the
old DB's 48 Scenes. `docker compose up -d --build --wait`, then:

- `scene_pipeline` got an optional DAG param `since` (`dags/scene_pipeline.py`, `SINCE_TEMPLATE`; a manual run could
  not pass it before; tests in `dags/tests/test_manual_runs.py`). Triggered with
  `airflow dags trigger scene_pipeline --conf '{"since":"2026-10-12T00:00:00Z"}'`. A scheduled run of the new stack was
  already running and the manual one queued behind it (`max_active_runs=1`).
- Result: 49 Scenes discovered (12 Oct to 12 Nov), all reached `detected`, none failed. Sizes: one Scene's VV + VH
  raster was 800 MB downloaded; at the end the object store volume was 7.7 GB (rasters are kept 14 days),
  Postgres 2.9 GB, free disk 157 GB.
- AIS: `airflow backfill create --dag-id ais_load --from-date 2026-10-15 --to-date 2026-11-12`, which loads the
  day 3 days behind each run date plus 2 older days (idempotent): 31 UTC days, 10 Oct to 9 Nov, all succeeded. The
  `validation` DAG validates Scenes whose AIS is loaded; three 6 Nov Scenes became ready after its last
  asset-triggered run, so it was triggered once more by hand. Result: 45 `validated`, 4 `detected` (10 to 12 Nov,
  their AIS day is not published yet).

### Reproduction check: the 9 original Scenes, detector v6, open water

| Scene (start) | baseline m / u / a | clean ingest m / u / a | median / p90 m (both) |
|---|---|---|---|
| 2026-10-12 17:00:44 | 18 / 6 / 0 | 18 / 6 / 0 | 34 / 67 |
| 2026-10-13 05:40:05 | 2 / 0 / 0 | 2 / 0 / 0 | 37 / 54 |
| 2026-10-13 16:52:11 | 13 / 0 / 0 | 13 / 0 / 0 | 35 / 60 |
| 2026-10-13 16:52:36 | 0 / 0 / 0 | 0 / 0 / 0 | |
| 2026-10-14 05:32:04 | 10 / 0 / 0 | 10 / 0 / 0 | 37 / 101 |
| 2026-10-14 05:32:29 | 3 / 0 / 0 | 3 / 0 / 0 | 33 / 35 |
| 2026-10-15 05:23:38 | 3 / 0 / 1 | 3 / 0 / 1 | 59 / 69 |
| 2026-10-15 05:24:03 | 2 / 0 / 0 | 2 / 0 / 0 | 39 / 54 |
| 2026-10-17 17:08:47 | 0 / 0 / 0 | 0 / 0 / 0 | |
| **total** | **51 / 6 / 1** | **51 / 6 / 1** | 35 / 68 |

51 matched, 6 unmatched and 3 candidates reproduced exactly, per Scene and in the match distances. The same three
`dark_ship_candidates` appear (lon, lat, length): 11.0454, 55.1146, 211 m; 10.8752, 55.5154, 149 m; 10.8839,
55.5447, 114 m. The whole DB now has 5 candidates in 3 Scenes, so 2 come from later Scenes (Task C looks at those).
This run used the real fetch path for every Scene (VV + VH + noise LUT + geometry from CDSE), so the experiment-file
and hand-patch shortcuts of the old stack gave the same numbers.

### Observability on the fresh stack

- Prometheus: target `pipeline-api` up; the 4 alert rules are loaded and none fires.
- Grafana (anonymous read, every panel query run through `/api/ds/query` with the dashboard's default variables):
  Detections 7 of 7 panels return rows, Match analysis 13 of 13, Pipeline health 10 of 10. `sar_scenes` agrees with
  the DB (45 validated, 4 detected).
