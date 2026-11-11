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
