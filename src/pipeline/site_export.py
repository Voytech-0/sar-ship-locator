"""Export validated Scenes as static files for the review website (`site/`).

    uv run python -m pipeline.site_export                 # newest validated Scene
    uv run python -m pipeline.site_export --latest 3      # newest three
    uv run python -m pipeline.site_export --scene S1A_... # one Scene

Writes `<out>/index.json` and, per Scene, `<out>/<scene_id>/scene.json`, `preview.png` and `chips/<id>.png`.
`scene.json` holds the Detections, the AIS vessels at acquisition time with a short track, and a summary.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from contracts.protocols import AisPosition, ObjectStore
from pipeline import db
from pipeline.algorithms.validator import NearestNeighbourValidator
from pipeline.db import Database, Row

TRACK_MINUTES = 10  # AIS track shown either side of the acquisition time
BOUNDS_MARGIN_DEG = 0.02


def _stats(detections: list[dict[str, Any]]) -> dict[str, Any]:
    open_water = [d for d in detections if d["shore"] == "open_water"]
    labels = Counter(d["label"] for d in open_water)
    dists = [d["match_distance_m"] for d in open_water if d["label"] == "matched"]
    return {
        "detections": len(detections),
        "open_water": dict(labels),
        "match_rate": labels["matched"] / len(open_water) if open_water else None,
        "median_match_distance_m": statistics.median(dists) if dists else None,
    }


def export_scene(database: Database, store: ObjectStore, scene_id: str, out: Path) -> dict[str, Any]:
    with database.connection() as conn:
        scene = db.get_scene(conn, scene_id)
        assert scene is not None
        when: datetime = scene["acquired_at"]
        rows = conn.execute(
            """
            SELECT d.id::text AS id, ST_X(d.geom) AS lon, ST_Y(d.geom) AS lat, d.confidence, d.length_m,
                   d.shore::text AS shore, d.chip_key, v.label::text AS label, v.matched_mmsi,
                   v.match_distance_m
            FROM detections d LEFT JOIN validations v ON v.detection_id = d.id
            WHERE d.scene_id = %s ORDER BY d.id
            """,
            (scene_id,),
        ).fetchall()
        margin = timedelta(minutes=TRACK_MINUTES)
        raw = db.list_ais_positions(conn, when - margin, when + margin)

    scene_dir = out / scene_id
    (scene_dir / "chips").mkdir(parents=True, exist_ok=True)
    preview = None
    if store.exists(f"scenes/{scene_id}/preview.png"):
        (scene_dir / "preview.png").write_bytes(store.get(f"scenes/{scene_id}/preview.png"))
        bounds = json.loads(store.get(f"scenes/{scene_id}/preview.json"))["bounds"]
        preview = {"url": "preview.png", "bounds": bounds}

    detections = []
    for r in rows:
        chip = None
        if r["chip_key"] and store.exists(r["chip_key"]):
            (scene_dir / "chips" / f"{r['id']}.png").write_bytes(store.get(r["chip_key"]))
            chip = f"chips/{r['id']}.png"
        detections.append({**{k: r[k] for k in r if k != "chip_key"}, "chip": chip})

    positions = [
        AisPosition(p["mmsi"], p["ts"], (p["lon"], p["lat"]), p["sog_knots"], p["cog_deg"], p["heading"])
        for p in raw
    ]
    south = west = -1e9
    north = east = 1e9
    if preview:
        (south, west), (north, east) = preview["bounds"]
    vessels = []
    for mmsi, (lon, lat) in NearestNeighbourValidator().tracks_at(positions, when).items():
        m = BOUNDS_MARGIN_DEG
        if not (south - m <= lat <= north + m and west - m <= lon <= east + m):
            continue
        own = [p for p in positions if p.mmsi == mmsi]
        track = [[p.position[0], p.position[1], round((p.ts - when).total_seconds())] for p in own]
        sog = next((p.sog_knots for p in own if p.sog_knots is not None), None)
        vessels.append({"mmsi": mmsi, "lon": lon, "lat": lat, "sog_knots": sog, "track": track})

    doc = {
        "scene_id": scene_id,
        "acquired_at": when.isoformat(),
        "preview": preview,
        "stats": _stats(detections),
        "detections": detections,
        "ais": vessels,
    }
    (scene_dir / "scene.json").write_text(json.dumps(doc, separators=(",", ":")))
    return doc


def latest_validated(database: Database, n: int) -> list[Row]:
    with database.connection() as conn:
        return conn.execute(
            "SELECT scene_id FROM scenes WHERE status = 'validated' ORDER BY acquired_at DESC LIMIT %s",
            (n,),
        ).fetchall()


def main(argv: Sequence[str] | None = None) -> None:
    from pipeline.adapters import S3ObjectStore, create_store_client

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="site/data", type=Path)
    ap.add_argument("--latest", type=int, default=1, help="newest N validated Scenes")
    ap.add_argument("--scene", action="append", help="export this Scene id (repeatable)")
    args = ap.parse_args(argv)

    env = os.environ
    store = S3ObjectStore(
        create_store_client(env["STORE_ENDPOINT_URL"], env["STORE_ACCESS_KEY"], env["STORE_SECRET_KEY"]),
        bucket=env.get("STORE_BUCKET", "sar"),
    )
    database = Database(env["PIPELINE_DATABASE_URL"])
    scenes = [{"scene_id": s} for s in args.scene] if args.scene else latest_validated(database, args.latest)
    if not scenes:
        raise SystemExit("no validated Scene to export")
    args.out.mkdir(parents=True, exist_ok=True)
    docs = [export_scene(database, store, s["scene_id"], args.out) for s in scenes]
    index = {
        "default": docs[0]["scene_id"],
        "scenes": [{"scene_id": d["scene_id"], "acquired_at": d["acquired_at"]} for d in docs],
    }
    (args.out / "index.json").write_text(json.dumps(index, indent=1))
    for d in docs:
        print(f"{d['scene_id']}: {d['stats']['detections']} detections, {len(d['ais'])} AIS vessels")


if __name__ == "__main__":
    main()
