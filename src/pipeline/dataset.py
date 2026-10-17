"""Export the labelled Chips as a versioned, immutable training dataset (contracts/storage.md).

Labels come from AIS validation and are WEAK:
  matched    a ship: an AIS vessel was at its position (label 1)
  unmatched  unknown: clutter, small boats and ships without AIS all land here (label 0)
  ambiguous  left out by default: two AIS tracks were within the gate

So a model trained on this learns "looks like an AIS-broadcasting ship vs everything else", which is a start
for a ship-vs-clutter classifier, not verified ground truth. The manifest records this next to the data.

Examples are split by SCENE (a scene never straddles train and test): Chips cut from one scene share the same
sea state and speckle, so a random split would leak. See `assign_splits`.

    docker compose run --rm pipeline-api python -m pipeline.dataset v1
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

from contracts.protocols import ObjectStore
from pipeline.db import Database

LABELS = {"matched": 1, "unmatched": 0, "ambiguous": -1}
NOTES = (
    "Weak labels from AIS validation: 'matched' = an AIS vessel was within the gate at the Detection; "
    "'unmatched' = unknown (clutter, small boats and ships without AIS). Not verified ground truth. "
    "Examples are split by scene; chips are 64x64 float32 linear sigma0 (VV)."
)


class DatasetExistsError(Exception):
    """A dataset version is immutable once written."""


def assign_splits(scene_ids: Sequence[str]) -> dict[str, str]:
    """Split WHOLE scenes into train / validation / test, roughly 70/15/15.

    Scenes are ordered by a hash of their id (stable, not by date) and cut into runs, so every split gets
    scenes as soon as there are three of them; hashing each scene independently would leave validation and
    test empty for a handful of scenes.
    """
    scenes = sorted(set(scene_ids), key=lambda sid: hashlib.sha256(sid.encode()).hexdigest())
    n = len(scenes)
    n_validation = max(1, int(n * 0.15)) if n >= 3 else 0  # floor: small sets keep more training data
    n_test = max(1, int(n * 0.15)) if n >= 2 else 0
    n_train = n - n_validation - n_test
    names = ["train"] * n_train + ["validation"] * n_validation + ["test"] * n_test
    return dict(zip(scenes, names, strict=True))


def _manifest_key(version: str) -> str:
    return f"datasets/{version}/manifest.json"


def export(
    db: Database,
    store: ObjectStore,
    version: str,
    *,
    detector_name: str | None = None,
    detector_version: str | None = None,
    include_ambiguous: bool = False,
    bundle: bool = True,
) -> dict[str, Any]:
    """Write `datasets/{version}/manifest.json` (and `chips.npz`) and return the manifest."""
    if not version or "/" in version:
        raise ValueError(f"bad dataset version {version!r}")
    if store.exists(_manifest_key(version)):
        raise DatasetExistsError(
            f"dataset {version} already exists; exports are immutable, use a new version"
        )

    labels = ["matched", "unmatched"] + (["ambiguous"] if include_ambiguous else [])
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT d.id::text AS detection_id, d.scene_id, s.acquired_at,
                   ST_X(d.geom) AS lon, ST_Y(d.geom) AS lat,
                   d.confidence, d.length_m, d.shore::text AS shore, d.detector_name, d.detector_version,
                   d.chip_key, v.label::text AS label, v.matched_mmsi, v.match_distance_m, v.gate_m
            FROM detections d
            JOIN validations v ON v.detection_id = d.id
            JOIN scenes s ON s.scene_id = d.scene_id
            WHERE d.chip_key IS NOT NULL
              AND v.label::text = ANY(%s)
              AND (%s::text IS NULL OR d.detector_name = %s)
              AND (%s::text IS NULL OR d.detector_version = %s)
            ORDER BY s.acquired_at, d.id
            """,
            (labels, detector_name, detector_name, detector_version, detector_version),
        ).fetchall()

    split_by_scene = assign_splits([r["scene_id"] for r in rows])
    examples = []
    for r in rows:
        chip_npy = r["chip_key"].removesuffix(".png") + ".npy"
        examples.append(
            {
                "detection_id": r["detection_id"],
                "scene_id": r["scene_id"],
                "acquired_at": r["acquired_at"].astimezone(UTC).isoformat(),
                "lon": r["lon"],
                "lat": r["lat"],
                "confidence": r["confidence"],
                "length_m": r["length_m"],
                "shore": r["shore"],
                "label": r["label"],
                "target": LABELS[r["label"]],
                "split": split_by_scene[r["scene_id"]],
                "matched_mmsi": r["matched_mmsi"],
                "match_distance_m": r["match_distance_m"],
                "gate_m": r["gate_m"],
                "detector": f"{r['detector_name']}@{r['detector_version']}",
                "chip_npy": chip_npy,
                "chip_png": r["chip_key"],
            }
        )

    counts: dict[str, dict[str, int]] = {}
    for e in examples:
        by_label = counts.setdefault(e["split"], {})
        by_label[e["label"]] = by_label.get(e["label"], 0) + 1
    manifest: dict[str, Any] = {
        "version": version,
        "created_at": datetime.now(UTC).isoformat(),
        "notes": NOTES,
        "filters": {
            "detector_name": detector_name,
            "detector_version": detector_version,
            "labels": labels,
        },
        "scenes": sorted({e["scene_id"] for e in examples}),
        "counts": {"total": len(examples), "by_split": counts},
        "bundle": f"datasets/{version}/chips.npz" if bundle and examples else None,
        "examples": examples,
    }

    if manifest["bundle"]:
        chips = np.stack(
            [np.load(io.BytesIO(store.get(e["chip_npy"])), allow_pickle=False) for e in examples]
        )
        buf = io.BytesIO()
        np.savez_compressed(
            buf,
            chips=chips.astype(np.float32),
            target=np.array([e["target"] for e in examples], dtype=np.int8),
            split=np.array([e["split"] for e in examples]),
            detection_id=np.array([e["detection_id"] for e in examples]),
        )
        store.put(manifest["bundle"], buf.getvalue(), "application/octet-stream")
    # the manifest goes last: its presence marks a complete dataset
    store.put(_manifest_key(version), json.dumps(manifest, indent=1).encode(), "application/json")
    return manifest


def main(argv: Sequence[str] | None = None) -> None:
    from pipeline.adapters import S3ObjectStore, create_store_client

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", help="new dataset version, e.g. v1 (immutable once written)")
    ap.add_argument("--detector", help="only Detections of this detector name")
    ap.add_argument("--detector-version", help="only Detections of this detector version")
    ap.add_argument("--include-ambiguous", action="store_true")
    ap.add_argument("--no-bundle", action="store_true", help="manifest only, no chips.npz")
    args = ap.parse_args(argv)

    env = os.environ
    store = S3ObjectStore(
        create_store_client(env["STORE_ENDPOINT_URL"], env["STORE_ACCESS_KEY"], env["STORE_SECRET_KEY"]),
        bucket=env.get("STORE_BUCKET", "sar"),
    )
    manifest = export(
        Database(env["PIPELINE_DATABASE_URL"]),
        store,
        args.version,
        detector_name=args.detector,
        detector_version=args.detector_version,
        include_ambiguous=args.include_ambiguous,
        bundle=not args.no_bundle,
    )
    total, scenes = manifest["counts"]["total"], len(manifest["scenes"])
    print(f"dataset {args.version}: {total} examples from {scenes} scenes")
    for split, by_label in sorted(manifest["counts"]["by_split"].items()):
        print(f"  {split:10s} {by_label}")


if __name__ == "__main__":
    main()
