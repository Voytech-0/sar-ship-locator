"""Detection recall against AIS, from an exported site (`make site`).

For every exported Scene: the AIS vessels whose image position lies on valid SAR data (alpha of
`preview.png`) and 400 m or more from mapped land or structures, and the share of them that has a matched
open-water Detection, by AIS length class. Run it from the repository root:

    uv run python scripts/recall_vs_ais.py [site/data] [data/land/great-belt.geojson]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

M_LAT = 110_574.0
LAND_CLEARANCE_M = 400.0
CLASSES = [(0, 30), (30, 60), (60, 120), (120, 500)]


def main() -> None:
    site = Path(sys.argv[1] if len(sys.argv) > 1 else "site/data")
    land_path = Path(sys.argv[2] if len(sys.argv) > 2 else "data/land/great-belt.geojson")
    land = [shape(f["geometry"]) for f in json.loads(land_path.read_text())["features"]]
    tree = STRtree(land)

    rows: list[tuple[bool, float]] = []
    for scene_file in sorted(site.glob("*/scene.json")):
        scene = json.loads(scene_file.read_text())
        if not scene.get("preview"):
            continue
        alpha = np.array(Image.open(scene_file.parent / "preview.png").convert("LA"))[:, :, 1]
        (south, west), (north, east) = scene["preview"]["bounds"]

        def has_data(
            lon: float,
            lat: float,
            alpha: np.ndarray = alpha,
            b: tuple[float, ...] = (south, west, north, east),
        ) -> bool:
            s, w, n, e = b
            r, c = int((n - lat) / (n - s) * alpha.shape[0]), int((lon - w) / (e - w) * alpha.shape[1])
            return (
                0 <= r < alpha.shape[0]
                and 0 <= c < alpha.shape[1]
                and bool(alpha[max(r - 3, 0) : r + 4, max(c - 3, 0) : c + 4].min() > 0)
            )

        matched = {
            d["matched_mmsi"]
            for d in scene["detections"]
            if d["label"] == "matched" and d["shore"] == "open_water"
        }
        for vessel in scene["ais"]:
            point = Point(vessel["image_lon"], vessel["image_lat"])
            if (
                not has_data(point.x, point.y)
                or land[int(tree.nearest(point))].distance(point) * M_LAT < LAND_CLEARANCE_M
            ):
                continue
            rows.append((vessel["mmsi"] in matched, vessel.get("length_m") or 0.0))

    print(f"AIS vessels on valid data, {LAND_CLEARANCE_M:.0f} m+ from land: {len(rows)}")
    for lo, hi in CLASSES:
        group = [hit for hit, length in rows if lo <= length < hi]
        if group:
            share = 100 * sum(group) / len(group)
            print(
                f"  AIS length {lo:3d}-{hi:3d} m: {sum(group):2d} of {len(group):2d} detected ({share:.0f} %)"
            )


if __name__ == "__main__":
    main()
