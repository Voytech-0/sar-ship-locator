"""GET /detections/{id}/chip: the Chip PNG served from the object store."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import boto3
import numpy as np
import pytest
from moto import mock_aws

from pipeline import db
from pipeline.adapters import S3ObjectStore
from pipeline.stages.chips import chip_png
from tests.api.conftest import Api

RING = [(10.7, 55.0), (11.3, 55.0), (11.3, 55.7), (10.7, 55.7), (10.7, 55.0)]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def store() -> Iterator[S3ObjectStore]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
        client.create_bucket(Bucket="sar")
        yield S3ObjectStore(client, bucket="sar")


@pytest.fixture
def api_with_chips(
    make_api: Callable[..., Api], store: S3ObjectStore
) -> tuple[Api, S3ObjectStore, dict[str, str]]:
    api = make_api(chip_store=store)
    api.discover()
    row = {"lon": 11.0, "lat": 55.3, "confidence": 0.9, "length_m": 100.0, "shore": "open_water"}
    with api.db.connection() as conn:
        db.insert_scene(conn, "S1_A", "great-belt", datetime(2026, 10, 14, 5, 32, tzinfo=UTC), RING)
        with_chip, without_chip, missing_object = db.replace_detections(
            conn, "S1_A", "ca-cfar", "2", [row, row, row]
        )
        db.set_chip_keys(
            conn,
            {with_chip: f"chips/S1_A/{with_chip}.png", missing_object: f"chips/S1_A/{missing_object}.png"},
        )
    store.put(f"chips/S1_A/{with_chip}.png", chip_png(np.full((64, 64), 0.5, dtype=np.float32)), "image/png")
    return api, store, {"ok": with_chip, "none": without_chip, "gone": missing_object}


def test_the_chip_is_served_as_a_cacheable_png(
    api_with_chips: tuple[Api, S3ObjectStore, dict[str, str]],
) -> None:
    api, store, ids = api_with_chips
    resp = api.client.get(f"/v1/detections/{ids['ok']}/chip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers["cache-control"]
    assert resp.content == store.get(f"chips/S1_A/{ids['ok']}.png") and resp.content.startswith(PNG_MAGIC)


@pytest.mark.parametrize("which", ["none", "gone"])
def test_a_detection_without_a_usable_chip_is_404(
    api_with_chips: tuple[Api, S3ObjectStore, dict[str, str]], which: str
) -> None:
    api, _, ids = api_with_chips
    resp = api.client.get(f"/v1/detections/{ids[which]}/chip")
    assert resp.status_code == 404 and resp.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("bad", ["00000000-0000-0000-0000-000000000000", "not-a-uuid", "1"])
def test_unknown_or_malformed_ids_are_404(
    api_with_chips: tuple[Api, S3ObjectStore, dict[str, str]], bad: str
) -> None:
    api, _, _ = api_with_chips
    assert api.client.get(f"/v1/detections/{bad}/chip").status_code == 404


def test_without_an_object_store_every_chip_is_404(make_api: Callable[..., Api]) -> None:
    api = make_api()
    assert api.client.get("/v1/detections/00000000-0000-0000-0000-000000000000/chip").status_code == 404
