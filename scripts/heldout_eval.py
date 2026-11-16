"""Fit group against held-out group (docs/studies/heldout-evaluation.md). Open water, detector v6.

Fit = the 9 Scenes the parameters were fitted on (acquired up to 2026-10-17 18:00 UTC), held-out = every later
validated Scene. Reads the dev DB through `docker compose exec postgis psql`; prints Markdown tables.

    uv run python scripts/heldout_eval.py
"""

from __future__ import annotations

import csv
import io
import subprocess
from collections import defaultdict

import numpy as np

CUT = "2026-10-17 18:00+00"
SQL = f"""
COPY (
  SELECT s.scene_id, s.acquired_at < '{CUT}' AS fit, v.label::text, v.match_distance_m, d.length_m,
         d.confidence, d.vh_contrast_db, a.length_m AS ais_length_m
  FROM scenes s
  LEFT JOIN detections d ON d.scene_id = s.scene_id AND d.detector_version = '6' AND d.shore = 'open_water'
  LEFT JOIN validations v ON v.detection_id = d.id
  LEFT JOIN ais_vessels a ON a.mmsi = v.matched_mmsi
  WHERE s.status = 'validated'
) TO STDOUT CSV HEADER
"""


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """P(random matched value > random unmatched value), ties count half."""
    return float(
        ((pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum())
        / (len(pos) * len(neg))
    )


def f(x: str) -> float | None:
    return float(x) if x != "" else None


def main() -> None:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgis", "psql", "-U", "sar", "-d", "sar", "-c", SQL],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = list(csv.DictReader(io.StringIO(out)))
    for group, is_fit in (("fit", "t"), ("held-out", "f")):
        g = [r for r in rows if r["fit"] == is_fit]
        scenes = sorted({r["scene_id"] for r in g})
        det = [r for r in g if r["label"]]
        per_scene: dict[str, int] = defaultdict(int)
        for s in scenes:
            per_scene[s] = 0
        for r in det:
            if r["label"] == "unmatched":
                per_scene[r["scene_id"]] += 1
        counts = {k: sum(r["label"] == k for r in det) for k in ("matched", "unmatched", "ambiguous")}
        m = [r for r in det if r["label"] == "matched"]
        dist = np.array([f(r["match_distance_m"]) for r in m], dtype=float)
        pair = [(f(r["length_m"]), f(r["ais_length_m"])) for r in m if r["ais_length_m"]]
        sar = np.array([p[0] for p in pair])
        ais = np.array([p[1] for p in pair])
        unm = [r for r in det if r["label"] == "unmatched"]
        vh_m = np.array([f(r["vh_contrast_db"]) for r in m if r["vh_contrast_db"]], dtype=float)
        vh_u = np.array([f(r["vh_contrast_db"]) for r in unm if r["vh_contrast_db"]], dtype=float)
        u = np.array(sorted(per_scene.values()))
        worst = max(per_scene, key=lambda k: per_scene[k])
        err = np.abs(sar - ais)
        ulen = np.median([f(r["length_m"]) for r in unm])
        uconf = np.median([f(r["confidence"]) for r in unm])
        print(f"### {group}: {len(scenes)} Scenes")
        print("matched {matched}, unmatched {unmatched}, ambiguous {ambiguous}".format(**counts))
        print(f"unmatched per Scene: median {np.median(u):.1f}, max {u.max()} ({worst[:32]})")
        print(f"match distance m: median {np.median(dist):.0f}, p90 {np.percentile(dist, 90):.0f}")
        ratio, mae = np.median(sar / ais), np.median(err)
        print(f"SAR/AIS length: n {len(pair)}, median ratio {ratio:.2f}, median abs error {mae:.0f} m")
        print(f"VH contrast AUC (matched {len(vh_m)} vs unmatched {len(vh_u)}): {auc(vh_m, vh_u):.3f}")
        print(f"VH medians {np.median(vh_m):.1f} / {np.median(vh_u):.1f} dB")
        print(f"unmatched median length {ulen:.0f} m, confidence {uconf:.2f}")
        print("per Scene unmatched:", {k[17:32]: v for k, v in sorted(per_scene.items()) if v})
        print()


if __name__ == "__main__":
    main()
