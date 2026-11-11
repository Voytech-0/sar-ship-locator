# Tooling history: dataset export, Grafana, review website

Moved from AGENTS.md (2026-11-11), text unchanged.

## Dataset export (2026-10-17)

`python -m pipeline.dataset <version>` (module `pipeline/dataset.py`) writes `datasets/<version>/manifest.json` and `chips.npz` to the object store. Labels are weak AIS labels (matched = 1, unmatched = 0, ambiguous left out by default); versions are immutable; splits are allocated over whole scenes (`assign_splits`: 70/15/15, floor with a minimum of one scene per split from three scenes up). A first attempt hashed each scene independently and, with only 5 scenes, put all of them in `train`; the allocation fixed that. First real export `v2`: 747 examples from 5 scenes (train 679, validation 39, test 29; 69 matched in all). Dataset `v1` in the local object store has the flawed all-train split and can be deleted.

## Grafana (2026-10-17)

`deploy/grafana/build_dashboards.py` generates both dashboards (regenerate with `uv run python deploy/grafana/build_dashboards.py`, then restart Grafana). All panel queries were checked through Grafana's query API against real data. The Detections dashboard has: per-scene counts, label split, a map of all Detections coloured by validation outcome, a map and table of the dark-ship candidates, and a chip gallery with Label and Shore filters. Chips are PNGs in the object store, which a browser cannot reach, so pipeline-api serves them at `GET /v1/detections/{id}/chip` and the tables show them as image cells; the constant dashboard variable `api_url` (default http://localhost:8000) is where the browser finds the API. The default Grafana basemap is CARTO, which now returns watermarked "API key required" tiles, so the maps use OpenStreetMap.
## Review website (2026-10-17)

`site/index.html` (Leaflet, no build step) shows a validated Scene: VV preview overlay (toggle, opacity), Detections by validation outcome with chips, dark-ship candidates, AIS vessels at acquisition time with +-10 min tracks, match links, filters, summary. Static: `site/data/` is git-ignored and made by `python -m pipeline.site_export` (`make site`; from Docker: `docker compose run --rm -v $PWD/site/data:/out pipeline-api python -m pipeline.site_export --out /out`), then `cd site && python3 -m http.server 8099` (8080 is Airflow). `detect` now writes `scenes/{id}/preview.png` + `preview.json` (30 m lat/lon grid, kept after the raster is deleted; storage.md updated). VV only for now: an RGB VV/VH ratio composite needs a VH fetch (contract + ADR-0004 change, not decided). Only the 14 Oct Scene was re-ingested with previews. Not done: GitHub Pages deploy, committed demo snapshot, preview size check on a full-overlap Scene (this one is a thin strip, 224 KB; cap is 5 MB).
