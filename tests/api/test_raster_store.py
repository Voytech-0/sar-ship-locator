from datetime import UTC, datetime

import numpy as np

from contracts.protocols import SceneRaster
from pipeline.stages.raster_store import delete_raster, load_raster, save_raster


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise FileNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def raster(vh: np.ndarray | None) -> SceneRaster:
    return SceneRaster(
        scene_id="S1X_TEST",
        acquired_at=datetime(2026, 10, 14, 5, 32, tzinfo=UTC),
        sigma0_vv=np.full((40, 30), 0.5, dtype=np.float32),
        transform=(1e-4, 0.0, 10.0, 0.0, -1e-4, 55.0),
        sigma0_vh=vh,
    )


def test_vh_band_round_trips_and_is_deleted_with_the_raster() -> None:
    store = MemoryStore()
    vh = np.full((40, 30), 0.01, dtype=np.float32)
    save_raster(store, raster(vh))
    loaded = load_raster(store, "S1X_TEST")
    assert loaded.sigma0_vh is not None
    np.testing.assert_array_equal(loaded.sigma0_vh, vh)
    delete_raster(store, "S1X_TEST")
    assert store.objects == {}


def test_raster_stored_without_vh_loads_as_vv_only() -> None:
    store = MemoryStore()
    save_raster(store, raster(None))
    assert load_raster(store, "S1X_TEST").sigma0_vh is None
