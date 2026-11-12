# Work plan (written 2026-11-11)

Three tasks, in this order. Each is one branch and one PR. Read `AGENTS.md` first; it has the rules and all
measurements this plan refers to. Tick the boxes in this file as you go and commit it with each PR. Delete the
file in the last PR.

## Rules for every task

- Start each task from fresh main: `git switch main && git pull`, then `git switch -c <branch>`. PR #8
  (`thermal-noise`) is merged, so the checked-out `thermal-noise` branch is finished.
- If `git fetch` or `git pull` fails with `Permission denied (publickey)`, the SSH agent has no key. Ask the user
  to run `ssh-add`; do not switch the remote to HTTPS.
- `make ci` must pass before every push (needs Docker running). For changes under `dags/`:
  `cd dags && uv sync && uv run pytest && uv run ruff check . && uv run mypy`.
- Open a PR with `gh pr create`. Never merge it; the user merges on GitHub. No `Co-Authored-By` or
  "Generated with" lines in commits or PR bodies.
- Do not change anything in `contracts/` and do not change detector or validation parameters. If a task seems
  to need either, stop and ask the user.
- Report what was measured, including results that are null or worse than before. Do not round a failure into
  a success.
- Stop and ask the user before: deleting Docker volumes, deleting objects in the store, anything listed under
  "Ask first" in a task.

## Task A: housekeeping (branch `housekeeping`)

### A1. Move the study logs out of AGENTS.md

Goal: `AGENTS.md` holds rules, layout, how the pieces fit, the current state and open items, in about 60 lines.
The measurements move to `docs/studies/` without losing a number.

- [x] Create these files and move the text into them (move, do not rewrite; keep every figure and date):

| New file | From AGENTS.md section |
|---|---|
| `docs/studies/unmatched-detections.md` | "Study of the unmatched open-water Detections", up to and including the recall and dark-ship paragraphs |
| `docs/studies/detector-versions.md` | the paragraphs on detector v3, v4 (false-alarm analysis), v5 (VH confirmation), v6 (thermal noise) |
| `docs/studies/match-geometry.md` | "Doppler displacement of moving ships" and the length comparison and first readings from "Exclusion zone and the match-analysis dashboard" |
| `docs/studies/bugs-from-real-runs.md` | "Bugs found by running it for real (fixed)" |

- [x] In `AGENTS.md` keep, in short form:
  - Project, Rules, Layout, How the pieces fit (unchanged).
  - A "Current state" section replacing "Status (2026-10-17)": what runs, the gate (`make ci`, test counts
    taken from an actual run, not copied), and that the local stack's Scenes were patched by hand until Task B
    is done.
  - A "Detector and validation, current" section: one line per active mechanism with its parameter and a link
    to the study file. These must be covered: structures mask, exclusion zone 100 m, CFAR settings, length
    bias 20 m, floors 30 m and 0.7, VH floor 24 dB, noise subtraction, Doppler plus sea-level height 42 m plus
    antenna offset, gate 200 m, how to bump `detector.version` (currently "6").
  - Dataset export, Grafana, Review website: two or three lines each on how to use them; history goes to the
    study files.
  - "Results worth knowing" and "Open items" (drop items marked done).
  - A line near the top: "Measurements and their history are in `docs/studies/`."
- [x] Check nothing was lost: every number in the old file (`git show main:AGENTS.md`) appears in the new
  `AGENTS.md` or in `docs/studies/`. A quick way: extract all numbers from the old file with `grep -o` and
  grep each in the new files; list the ones not found and place them.
- [x] Fix links: `grep -rn "AGENTS.md" docs README.md` and update references to moved sections.

Done when: `AGENTS.md` is about 60 lines, the number check finds nothing missing, `make ci` passes.

### A2. Flaky schemathesis subtest

Facts: `tests/api/test_schemathesis.py` (hypothesis `max_examples=30`, seeded DB rows). One subtest failed once
in a full-suite run and never in 5 reruns of the file alone. That points at state from other tests or at a
rare generated request, not at the test alone.

- [x] Run the whole suite 20 times and keep each log:
  `for i in $(seq 20); do uv run pytest -q -p no:cacheprovider > /tmp/run_$i.log 2>&1 || echo "failed $i"; done`
  (use the session scratch directory instead of `/tmp` if you have one).
- [x] If a run fails: copy the failing request and the hypothesis reproduction lines from the log, reproduce
  it with them, find the cause and fix the cause. Add a regression test with that request.
- [x] If no run fails: do not add retries or loosen checks. Make the next failure diagnosable instead: add
  `print_blob=True` to the test's hypothesis `settings`, and note in `AGENTS.md` Open items "20 full-suite
  runs on 2026-11-xx, not reproduced".

Done when: either the cause is fixed with a regression test, or the 20 clean runs are recorded.

- [ ] Open the PR for Task A (title: "Housekeeping: studies out of AGENTS.md, schemathesis flake").

## Task B: clean end-to-end ingest with detector v6 (branch `clean-ingest`)

Why: the local stack's 9 Scenes were never fetched by the current `fetch` code. VH and the noise-subtracted
rasters came from experiment files, and `target_height_m` was patched into the DB by hand. The production path
(VV + VH + noise LUT + geometry from CDSE) is covered only by synthetic tests.

The `discover` stage takes `since` but no end time, so a clean ingest from 12 Oct also brings in every later
Scene. That is wanted: the 9 old Scenes are the reproduction check here, the later ones are the held-out set
of Task C.

### B1. Baseline and a single real fetch (no deletion yet)

- [x] Start the stack: `docker compose up -d --build --wait`. All services healthy.
- [x] Save the baseline to `docs/studies/clean-ingest.md`: for each of the 9 Scenes and detector version 6, the
  open-water counts of matched, unmatched and ambiguous Detections, plus median and p90 match distance, and the
  rows of `dark_ship_candidates`. DB access:
  `docker compose exec -T postgis psql -U sar -d sar -At -c "..."`. Expected totals from `AGENTS.md`: 51
  matched, 6 unmatched, 3 candidates. If the DB disagrees, write down what the DB says and continue.
- [x] Re-fetch one Scene through the real code. `fetch` is refused while the raster exists, so this needs the
  raster of one Scene deleted from the store first. **Ask first** which Scene (suggest the 14 Oct one) and get
  a yes for deleting its `scenes/{id}/sigma0.npy`, `sigma0_vh.npy`, `geo.npz`, `meta.json`.
- [x] Then: `fetch`, `detect`, `validate` (with `"force": true`) for that Scene through the API
  (`POST /v1/stages/...`, see `contracts/openapi.yaml`), and compare its counts with the baseline row. Any
  difference is a finding: record it with the numbers, look for the cause (experiment file vs real fetch),
  and tell the user before B2.

### B2. Fresh stack, ingest through Airflow

- [x] Read `dags/scene_pipeline.py` and `dags/pipeline_dags/common.py`. Check whether a manually triggered run
  can pass `since`. If it cannot, add an optional DAG param `since` (when absent, the existing
  `SINCE_TEMPLATE` behaviour stays) with tests next to `dags/tests/test_manual_runs.py`. This is a `dags/`
  change only; the API contract already has `since`.
- [x] Measure the stored size of one Scene's raster objects and the number of Scenes expected (about 2 per day
  since 12 Oct). Rasters are kept 14 days (`retention.raster_days`). Check free disk covers it with margin;
  if not, stop and ask.
- [x] **Ask first**, then remove the DB and object-store volumes. List them with `docker volume ls` and keep
  the land-polygon volume (rebuilding it needs the Overpass API). Tell the user this deletes the Detections of
  versions 2 to 5, which exist nowhere else.
- [x] `docker compose up -d --build --wait`, then trigger `scene_pipeline` with `since` = 2026-10-12T00:00:00Z.
  Every discovered Scene should reach `detected`. Record failed Scenes with their error; do not retry in a loop.
- [x] Load AIS for every day from 12 Oct to today minus 3 days. Preferred: an Airflow backfill of `ais_load`
  over that range. If that fights the DAG's lag logic, call `POST /v1/stages/ais-load` per day and say so in
  the write-up. Then let the `validation` DAG run, or trigger it.
- [x] Reproduction check in `docs/studies/clean-ingest.md`: the same table as the baseline for the 9 original
  Scenes, side by side, with the differences. State plainly whether 51 / 6 / 3 reproduced.
- [x] Check the observability side on the fresh stack: the three Grafana dashboards show data, Prometheus
  targets are up, no alert fires that should not.
- [x] Update `AGENTS.md` "Current state" (no hand-patched Scenes any more) and open the PR.

Done when: all Scenes since 12 Oct are `validated` or listed as failed with a reason, the reproduction table is
written, and the user has the differences in the PR body.

## Task C: held-out evaluation (branch `heldout-eval`)

Why: every parameter (CFAR pfa and minimum pixels, length bias, the 30 m and 0.7 floors, VH floor 24 dB,
gate 200 m) was fitted on about 59 ships from the 9 Scenes of 12 to 15 Oct. The Scenes after 15 Oct from
Task B have never been used for fitting.

- [ ] Parameters stay frozen. No tuning in this task, whatever the numbers say.
- [ ] Compute for two groups, "fit" (the 9 Scenes of 12 to 15 Oct) and "held-out" (all later Scenes), open
  water, detector v6:
  - matched, unmatched, ambiguous counts, and unmatched per Scene (median and maximum);
  - recall against AIS by length class (under 30 m, 30-60, 60-120, 120 m and longer) with
    `scripts/recall_vs_ais.py` on a site export (`make site`); same vessel filter as in `AGENTS.md`;
  - median and p90 match distance;
  - SAR length against AIS length: median ratio and median absolute error;
  - VH contrast AUC, matched against unmatched;
  - number of `dark_ship_candidates` and a table of them (Scene, position, length, nearest AIS distance).
- [ ] List the Scenes whose unmatched count is far above the median (as 12 Oct evening was). For each, record
  the sea median sigma0 in VV. Do not explain them here; that is a later task.
- [ ] Write `docs/studies/heldout-evaluation.md`: the table fit against held-out, the number of ships and
  Scenes in each group, and a short reading. If held-out is worse, say by how much and which parameter looks
  responsible; propose changes as a list for the user, do not apply them.
- [ ] If the held-out set has fewer than about 100 matched ships, say that the comparison is weak.
- [ ] Update `AGENTS.md` (one paragraph with the headline numbers and a link), delete this file, open the PR.

Done when: the study file exists with both groups, and the PR body states the result in three or four lines.

## After these three (not part of this plan)

Persistence mask for static targets, a classifier in place of the stacked floors, wind against sea sigma0,
an alert on the unmatched rate per Scene, a second tier in the dark-ship rule, GitHub Pages deploy.
