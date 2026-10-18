from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
import tifffile

from contracts.protocols import PermanentError, Region, SceneRef, TransientError
from pipeline.adapters._retry import RetryPolicy
from pipeline.adapters.s1_fetcher import Sentinel1S3Fetcher, parse_geometry

from . import synth
from .conftest import FlakyClient, client_error

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

ACQUIRED = datetime(2026, 10, 14, 5, 32, 4, tzinfo=UTC)
SHIP = (650, 250)


def scene(mapping: synth.Mapping = synth.linear_mapping) -> SceneRef:
    return SceneRef(synth.SCENE_ID, ACQUIRED, synth.footprint(mapping))


def region(mapping: synth.Mapping = synth.linear_mapping, **kw: Any) -> Region:
    return Region("test", "Test", synth.region_ring(mapping, **kw))


def fetcher(client: Any, retry: RetryPolicy, **kw: Any) -> Sentinel1S3Fetcher:
    return Sentinel1S3Fetcher(client, retry=retry, header_block=4096, **kw)


@pytest.fixture
def product(s3: Any) -> bytes:
    tiff = synth.make_tiff(synth.make_dn(SHIP))
    synth.upload_product(s3, tiff)
    return tiff


def test_row_range_fetch_is_calibrated_cropped_and_masked(
    s3: Any, product: bytes, fast_retry: RetryPolicy
) -> None:
    f = fetcher(s3, fast_retry)
    raster = f.fetch(scene(), region())
    diag = f.last_diagnostics
    assert diag is not None and diag.mode == "row-range"
    r0, r1, c0, c1 = diag.window
    assert raster.sigma0_vv.shape == (r1 - r0, c1 - c0)
    assert raster.sigma0_vv.dtype == np.float32
    assert raster.scene_id == synth.SCENE_ID and raster.acquired_at == ACQUIRED

    # sigma0 = DN^2 / sigmaNought^2 = 100^2/100^2 for sea, 1000^2/100^2 for the ship
    assert raster.sigma0_vv[500 - r0, 200 - c0] == pytest.approx(1.0)
    assert raster.sigma0_vv[SHIP[0] - r0, SHIP[1] - c0] == pytest.approx(100.0)
    # inside the window but outside the Region polygon (cols 100..400, rows 400..800)
    assert np.isnan(raster.sigma0_vv[420 - r0, 95 - c0])
    assert np.isnan(raster.sigma0_vv[700 - r0, 410 - c0])
    assert np.isfinite(raster.sigma0_vv[420 - r0, 120 - c0])

    # the affine maps window-relative (col, row) of the ship back to its lon/lat
    a, b, c, d, e, g = raster.transform
    lon, lat = synth.linear_mapping(float(SHIP[1]), float(SHIP[0]))
    col, row = SHIP[1] - c0, SHIP[0] - r0
    assert a * col + b * row + c == pytest.approx(lon, abs=1e-9)
    assert d * col + e * row + g == pytest.approx(lat, abs=1e-9)
    assert diag.affine_max_error_px < 0.01


def test_raster_carries_gcp_geolocation_and_bytes_downloaded(
    s3: Any, product: bytes, fast_retry: RetryPolicy
) -> None:
    f = fetcher(s3, fast_retry)
    raster = f.fetch(scene(), region())
    diag = f.last_diagnostics
    assert diag is not None
    r0, _, c0, _ = diag.window
    assert raster.geolocate is not None
    lon, lat = raster.geolocate(
        np.array([SHIP[0] - r0], dtype=np.float64), np.array([SHIP[1] - c0], dtype=np.float64)
    )
    want_lon, want_lat = synth.linear_mapping(float(SHIP[1]), float(SHIP[0]))
    assert lon[0] == pytest.approx(want_lon, abs=1e-9) and lat[0] == pytest.approx(want_lat, abs=1e-9)
    assert raster.bytes_downloaded == diag.bytes_downloaded > 0


def test_concurrent_fetches_report_their_own_byte_counts(
    s3: Any, product: bytes, fast_retry: RetryPolicy
) -> None:
    """The API runs several fetches on one fetcher; a shared counter would be reset by each new call."""
    from concurrent.futures import ThreadPoolExecutor

    f = fetcher(s3, fast_retry)
    alone = f.fetch(scene(), region()).bytes_downloaded
    with ThreadPoolExecutor(8) as pool:
        counts = list(pool.map(lambda _: f.fetch(scene(), region()).bytes_downloaded, range(16)))
    assert counts == [alone] * 16


def test_only_needed_rows_are_downloaded(s3: Any, product: bytes, fast_retry: RetryPolicy) -> None:
    client = FlakyClient(s3)
    f = fetcher(client, fast_retry, chunk_bytes=60_000)
    f.fetch(scene(), region())
    diag = f.last_diagnostics
    assert diag is not None
    r0, r1, _, _ = diag.window
    data_bytes = (r1 - r0) * synth.WIDTH * 2
    assert (
        data_bytes <= diag.bytes_downloaded < data_bytes + 40_000
    )  # rows plus header blocks and calibration XML
    assert diag.bytes_downloaded < 0.5 * len(product)
    ranges = [c["Range"] for c in client.get_calls if "Range" in c]
    spans = [int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1 for r in ranges]
    row_reads = [
        n for n in spans if n % (synth.WIDTH * 2) == 0
    ]  # header blocks are 4096 bytes, not whole rows
    assert len(row_reads) == -(-(r1 - r0) // 60)  # 60 rows (60 kB) per ranged GET


def test_big_endian_product_gives_the_same_sigma0(s3: Any, fast_retry: RetryPolicy) -> None:
    """Real Sentinel-1 files are little-endian, but the fast path must follow the file's byte order."""
    results = []
    orders: list[Literal["<", ">"]] = ["<", ">"]
    for order in orders:
        synth.upload_product(s3, synth.make_tiff(synth.make_dn(), byteorder=order))
        f = fetcher(s3, fast_retry)
        results.append(f.fetch(scene(), region()).sigma0_vv)
        assert f.last_diagnostics is not None and f.last_diagnostics.mode == "row-range"
    assert np.array_equal(results[0], results[1], equal_nan=True)


def test_truncated_read_is_retried(s3: Any, product: bytes, fast_retry: RetryPolicy) -> None:
    seen = {"hit": 0}

    def truncate_second_data_read(call_no: int, kwargs: dict[str, Any]) -> int | None:
        start, end = (
            (int(x) for x in kwargs["Range"].split("=")[1].split("-")) if "Range" in kwargs else (0, 0)
        )
        if end - start > 100_000 and seen["hit"] == 0:  # first big row-range read
            seen["hit"] += 1
            return 1141  # same shape as the failure seen against CDSE
        return None

    client = FlakyClient(s3, truncate_second_data_read)
    expected = fetcher(s3, fast_retry).fetch(scene(), region()).sigma0_vv
    got = fetcher(client, fast_retry, chunk_bytes=200_000).fetch(scene(), region()).sigma0_vv
    assert seen["hit"] == 1
    np.testing.assert_array_equal(got, expected)


def test_persistent_truncation_becomes_transient_error(
    s3: Any, product: bytes, fast_retry: RetryPolicy
) -> None:
    def always(call_no: int, kwargs: dict[str, Any]) -> int | None:
        start, end = (
            (int(x) for x in kwargs["Range"].split("=")[1].split("-")) if "Range" in kwargs else (0, 0)
        )
        return 10 if end - start > 100_000 else None  # header reads fine, every row-range read is short

    with pytest.raises(TransientError, match="failed after 3 attempts"):
        fetcher(FlakyClient(s3, always), fast_retry).fetch(scene(), region())


def test_server_errors_are_retried_but_auth_errors_are_not(
    s3: Any, product: bytes, fast_retry: RetryPolicy
) -> None:
    calls = {"n": 0}

    def flaky(call_no: int, kwargs: dict[str, Any]) -> int | None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise client_error(503, "SlowDown")
        return None

    fetcher(FlakyClient(s3, flaky), fast_retry).fetch(scene(), region())
    assert calls["n"] > 2

    def denied(call_no: int, kwargs: dict[str, Any]) -> int | None:
        raise client_error(403, "AccessDenied")

    with pytest.raises(PermanentError, match="403"):
        fetcher(FlakyClient(s3, denied), fast_retry).fetch(scene(), region())


@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [("zlib", {"compression": "zlib"}), ("tiled", {"tile": (16, 16)})],
)
def test_other_layouts_fall_back_to_full_band(
    s3: Any, fast_retry: RetryPolicy, kind: str, kwargs: dict[str, Any]
) -> None:
    dn = synth.make_dn(SHIP)
    synth.upload_product(s3, synth.make_tiff(dn))
    expected = fetcher(s3, fast_retry).fetch(scene(), region())

    other = synth.make_tiff(dn, **kwargs)
    s3.put_object(Bucket="eodata", Key=synth.VV_KEY, Body=other)
    f = fetcher(s3, fast_retry, chunk_bytes=100_000)
    got = f.fetch(scene(), region())
    assert f.last_diagnostics is not None and f.last_diagnostics.mode == "full-band"
    assert f.last_diagnostics.bytes_downloaded >= len(other)
    np.testing.assert_array_equal(got.sigma0_vv, expected.sigma0_vv)
    assert got.transform == pytest.approx(expected.transform)


def test_missing_vv_band_is_permanent(s3: Any, product: bytes, fast_retry: RetryPolicy) -> None:
    s3.delete_object(Bucket="eodata", Key=synth.VV_KEY)
    with pytest.raises(PermanentError, match="no VV band"):
        fetcher(s3, fast_retry).fetch(scene(), region())


def test_product_not_yet_on_s3_is_transient(s3: Any, fast_retry: RetryPolicy) -> None:
    with pytest.raises(TransientError, match="not on S3 yet"):
        fetcher(s3, fast_retry).fetch(scene(), region())


def test_corrupt_tiff_is_permanent(s3: Any, fast_retry: RetryPolicy) -> None:
    synth.upload_product(s3, b"this is not a tiff" * 100)
    with pytest.raises(PermanentError, match="unreadable TIFF"):
        fetcher(s3, fast_retry).fetch(scene(), region())


def test_tiff_without_gcps_is_permanent(s3: Any, fast_retry: RetryPolicy) -> None:
    import io

    buf = io.BytesIO()
    tifffile.imwrite(buf, synth.make_dn(SHIP), rowsperstrip=1)
    synth.upload_product(s3, buf.getvalue())
    with pytest.raises(PermanentError, match="unreadable TIFF"):
        fetcher(s3, fast_retry).fetch(scene(), region())


def test_region_not_covered_by_scene_pixels_is_permanent(
    s3: Any, product: bytes, fast_retry: RetryPolicy
) -> None:
    far = Region("far", "Far", [(20.0, 40.0), (20.1, 40.0), (20.1, 40.1), (20.0, 40.1), (20.0, 40.0)])
    with pytest.raises(PermanentError, match="does not intersect"):
        fetcher(s3, fast_retry).fetch(scene(), far)

    # footprint claims overlap but no pixel of the scene lies inside the Region
    wide = SceneRef(
        synth.SCENE_ID, ACQUIRED, [(9.0, 54.0), (12.0, 54.0), (12.0, 57.0), (9.0, 57.0), (9.0, 54.0)]
    )
    with pytest.raises(PermanentError, match="no pixel"):
        fetcher(s3, fast_retry).fetch(
            wide,
            far.__class__(
                "near", "Near", [(11.5, 56.5), (11.6, 56.5), (11.6, 56.6), (11.5, 56.6), (11.5, 56.5)]
            ),
        )


def test_geolocation_error_of_single_affine_is_reported(s3: Any, fast_retry: RetryPolicy) -> None:
    synth.upload_product(s3, synth.make_tiff(synth.make_dn(SHIP), synth.curved_mapping))
    f = fetcher(s3, fast_retry)
    f.fetch(scene(synth.curved_mapping), region(synth.curved_mapping))
    assert f.last_diagnostics is not None
    assert f.last_diagnostics.affine_max_error_px > 1.0
    assert f.last_diagnostics.affine_max_error_m == pytest.approx(
        f.last_diagnostics.affine_max_error_px * 10, rel=1e-6
    )


def test_geometry_is_read_from_a_real_annotation() -> None:
    xml = next((FIXTURES / "s3").rglob("annotation/s1d-iw-grd-vv-*.xml"))
    geometry = parse_geometry(ET.parse(xml).getroot(), 800, 775)
    assert geometry.heading_deg == pytest.approx(-163.6, abs=0.1)
    assert geometry.speed_m_s == pytest.approx(7588, abs=20)
    assert 800_000 < geometry.slant_range_m < 900_000
    assert 30 < geometry.incidence_deg < 40


def test_geometry_needs_orbit_vectors_and_a_grid() -> None:
    with pytest.raises(ValueError, match="orbit"):
        parse_geometry(ET.fromstring("<product><platformHeading>1</platformHeading></product>"), 0, 0)
