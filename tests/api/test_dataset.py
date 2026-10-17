"""The dataset export: weak AIS labels, split by scene, immutable versions."""

from __future__ import annotations

import io
import json
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime

import boto3
import numpy as np
import pytest
from moto import mock_aws

from pipeline import db
from pipeline.adapters import S3ObjectStore
from pipeline.dataset import DatasetExistsError, assign_splits, export
from pipeline.stages.chips import chip_npy, chip_png
from tests.api.conftest import Api

RING = [(10.7, 55.0), (11.3, 55.0), (11.3, 55.7), (10.7, 55.7), (10.7, 55.0)]


@pytest.fixture
def store() -> Iterator[S3ObjectStore]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
        client.create_bucket(Bucket="sar")
        yield S3ObjectStore(client, bucket="sar")


def add_scene(
    api: Api,
    store: S3ObjectStore,
    scene_id: str,
    detections: list[tuple[str | None, bool]],
    version: str = "2",
) -> list[str]:
    """A Scene with one Detection per (label, has_chip) item; label None means never validated."""
    api.discover()  # makes sure the Region row exists
    rows = [
        {"lon": 11.0 + i * 0.001, "lat": 55.3, "confidence": 0.95, "length_m": 120.0, "shore": "open_water"}
        for i in range(len(detections))
    ]
    with api.db.connection() as conn:
        db.insert_scene(conn, scene_id, "great-belt", datetime(2026, 10, 14, 5, 32, tzinfo=UTC), RING)
        ids = db.replace_detections(conn, scene_id, "ca-cfar", version, rows)
        for det_id, (label, _) in zip(ids, detections, strict=True):
            if label is not None:
                mmsi, dist = (219000001, 90.0) if label == "matched" else (None, None)
                db.upsert_validation(conn, det_id, label, mmsi, dist, 500.0)
    chip_keys = {}
    for k, (det_id, (_, has_chip)) in enumerate(zip(ids, detections, strict=True)):
        if has_chip:
            chip = np.full((64, 64), float(k + 1), dtype=np.float32)
            store.put(f"chips/{scene_id}/{det_id}.npy", chip_npy(chip), "application/octet-stream")
            store.put(f"chips/{scene_id}/{det_id}.png", chip_png(chip), "image/png")
            chip_keys[det_id] = f"chips/{scene_id}/{det_id}.png"
    with api.db.connection() as conn:
        db.set_chip_keys(conn, chip_keys)
    return ids


def test_export_has_labels_targets_splits_and_a_bundle(api: Api, store: S3ObjectStore) -> None:
    ids = add_scene(api, store, "S1_A", [("matched", True), ("unmatched", True), ("ambiguous", True)])
    manifest = export(api.db, store, "v1")

    assert {e["label"] for e in manifest["examples"]} == {"matched", "unmatched"}  # ambiguous left out
    by_label = {e["label"]: e for e in manifest["examples"]}
    assert by_label["matched"]["target"] == 1 and by_label["matched"]["matched_mmsi"] == 219000001
    assert by_label["unmatched"]["target"] == 0 and by_label["unmatched"]["matched_mmsi"] is None
    assert by_label["matched"]["detector"] == "ca-cfar@2"
    assert manifest["scenes"] == ["S1_A"] and manifest["counts"]["total"] == 2
    assert "weak" in manifest["notes"].lower()

    stored = json.loads(store.get("datasets/v1/manifest.json"))
    assert stored["examples"] == manifest["examples"]
    bundle = np.load(io.BytesIO(store.get("datasets/v1/chips.npz")))
    assert bundle["chips"].shape == (2, 64, 64) and bundle["chips"].dtype == np.float32
    assert sorted(bundle["target"].tolist()) == [0, 1]
    # the bundle rows line up with the manifest rows
    for row, example in enumerate(manifest["examples"]):
        assert bundle["detection_id"][row] == example["detection_id"]
        assert bundle["chips"][row][0, 0] == (1.0 if example["label"] == "matched" else 2.0)
    assert set(bundle["detection_id"].tolist()) <= set(ids)


def test_ambiguous_can_be_included(api: Api, store: S3ObjectStore) -> None:
    add_scene(api, store, "S1_A", [("matched", True), ("ambiguous", True)])
    manifest = export(api.db, store, "v1", include_ambiguous=True)
    assert {e["label"]: e["target"] for e in manifest["examples"]} == {"matched": 1, "ambiguous": -1}


def test_detections_without_chip_or_validation_are_left_out(api: Api, store: S3ObjectStore) -> None:
    add_scene(api, store, "S1_A", [("matched", True), ("matched", False), (None, True)])
    manifest = export(api.db, store, "v1")
    assert manifest["counts"]["total"] == 1


def test_detector_filter(api: Api, store: S3ObjectStore) -> None:
    add_scene(api, store, "S1_A", [("matched", True)], version="1")
    add_scene(api, store, "S1_B", [("matched", True)], version="2")
    assert export(api.db, store, "v-all")["counts"]["total"] == 2
    only_v2 = export(api.db, store, "v-2", detector_version="2")
    assert [e["scene_id"] for e in only_v2["examples"]] == ["S1_B"]


def test_a_version_is_immutable(api: Api, store: S3ObjectStore) -> None:
    add_scene(api, store, "S1_A", [("matched", True)])
    first = export(api.db, store, "v1")
    add_scene(api, store, "S1_B", [("unmatched", True)])
    with pytest.raises(DatasetExistsError):
        export(api.db, store, "v1")
    assert json.loads(store.get("datasets/v1/manifest.json"))["counts"] == first["counts"]
    assert export(api.db, store, "v2")["counts"]["total"] == 2


def test_empty_export_writes_a_manifest_without_a_bundle(api: Api, store: S3ObjectStore) -> None:
    manifest = export(api.db, store, "v0")
    assert manifest["counts"]["total"] == 0 and manifest["bundle"] is None
    assert not store.exists("datasets/v0/chips.npz") and store.exists("datasets/v0/manifest.json")


@pytest.mark.parametrize("version", ["", "a/b"])
def test_bad_version_names_are_rejected(api: Api, store: S3ObjectStore, version: str) -> None:
    with pytest.raises(ValueError, match="bad dataset version"):
        export(api.db, store, version)


def test_splits_are_roughly_70_15_15_and_never_split_a_scene() -> None:
    scenes = [f"S1_{n}" for n in range(2000)]
    shares = Counter(assign_splits(scenes).values())
    assert {k: round(v / 2000, 2) for k, v in shares.items()} == pytest.approx(
        {"train": 0.70, "validation": 0.15, "test": 0.15}, abs=0.01
    )
    assert assign_splits(scenes + scenes) == assign_splits(scenes)  # one entry per scene, order irrelevant
    assert assign_splits(scenes[::-1]) == assign_splits(scenes)


@pytest.mark.parametrize(
    ("n", "expected"),
    [(1, (1, 0, 0)), (2, (1, 0, 1)), (3, (1, 1, 1)), (5, (3, 1, 1)), (10, (8, 1, 1)), (20, (14, 3, 3))],
)
def test_a_few_scenes_still_populate_every_split_they_can(n: int, expected: tuple[int, int, int]) -> None:
    counts = Counter(assign_splits([f"S1_{k}" for k in range(n)]).values())
    assert (counts["train"], counts["validation"], counts["test"]) == expected


def test_all_chips_of_a_scene_share_a_split(api: Api, store: S3ObjectStore) -> None:
    add_scene(api, store, "S1_A", [("matched", True), ("unmatched", True), ("unmatched", True)])
    manifest = export(api.db, store, "v1")
    assert len({e["split"] for e in manifest["examples"]}) == 1
