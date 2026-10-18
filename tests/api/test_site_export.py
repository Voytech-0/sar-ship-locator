"""The review-website export: Detections, AIS vessels at acquisition time, preview, chips."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from pipeline import db
from pipeline.adapters import S3ObjectStore
from pipeline.site_export import export_scene
from pipeline.stages.preview import Preview, save_preview
from tests.api.conftest import Api
from tests.api.test_dataset import add_scene, store  # noqa: F401  (fixture)

WHEN = datetime(2026, 10, 14, 5, 32, tzinfo=UTC)


def test_scene_export(api: Api, store: S3ObjectStore, tmp_path: Path) -> None:  # noqa: F811
    add_scene(api, store, "S1_A", [("matched", True), ("unmatched", False)])
    save_preview(store, "S1_A", Preview(b"\x89PNG", (55.0, 10.9, 55.5, 11.2)))
    rows = [
        {
            "mmsi": 219000001,
            "ts": WHEN.replace(minute=m),
            "lon": 11.0 + m * 1e-4,
            "lat": 55.3,
            "sog_knots": 10.0,
            "cog_deg": 90.0,
            "heading": 90,
        }
        for m in (30, 34)
    ] + [  # far outside the preview
        {"mmsi": 111, "ts": WHEN, "lon": 5.0, "lat": 50.0, "sog_knots": 1.0, "cog_deg": 0.0, "heading": 0}
    ]
    with api.db.connection() as conn:
        db.insert_ais_positions(conn, rows)
        db.upsert_ais_vessels(conn, [{"mmsi": 219000001, "name": "TAURUS", "length_m": 183.0}])
        db.upsert_ais_vessels(conn, [{"mmsi": 219000001, "ship_type": "Tanker"}])

    doc = export_scene(api.db, store, "S1_A", tmp_path)

    assert doc["stats"]["open_water"] == {"matched": 1, "unmatched": 1}
    assert doc["stats"]["match_rate"] == 0.5
    with_chip = [d for d in doc["detections"] if d["chip"]]
    assert len(with_chip) == 1 and (tmp_path / "S1_A" / with_chip[0]["chip"]).exists()
    assert [v["mmsi"] for v in doc["ais"]] == [219000001]
    assert len(doc["ais"][0]["track"]) == 2
    vessel = doc["ais"][0]
    assert (vessel["name"], vessel["ship_type"], vessel["length_m"], vessel["width_m"]) == (
        "TAURUS",
        "Tanker",
        183.0,
        None,
    )
    assert doc["preview"]["bounds"] == [[55.0, 10.9], [55.5, 11.2]]
    assert (tmp_path / "S1_A" / "preview.png").exists()
    assert json.loads((tmp_path / "S1_A" / "scene.json").read_text())["scene_id"] == "S1_A"
    assert np.isfinite(doc["ais"][0]["lon"])
