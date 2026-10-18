"""SceneFetcher for Sentinel-1 IW GRDH products on the CDSE S3 endpoint (docs/adr/0004).

Fast path: the VV measurement TIFF is uncompressed, one row per strip, with contiguous strips, so the rows
covering the Region are fetched as a few large ranged GETs and the columns are cropped in memory.
Fallback: any other layout (compressed, tiled, non-contiguous) downloads the whole VV band.
"""

from __future__ import annotations

import logging
import math
import re
import struct
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

import numpy as np
import tifffile
from botocore.exceptions import ClientError
from numpy.typing import NDArray
from shapely.geometry import Polygon

from contracts.protocols import PermanentError, Region, SarGeometry, SceneRaster, SceneRef, TransientError

from ._retry import RetryPolicy
from ._s3 import S3RangeReader, is_retryable, raise_if_permanent, ranged_get
from .s1_calibration import CalibrationLut, NoiseLut
from .s1_geo import GcpGrid, ring_to_polygon

log = logging.getLogger(__name__)

ENDPOINT_URL = "https://eodata.dataspace.copernicus.eu"
BUCKET = "eodata"
_PRODUCT_ROOT = "Sentinel-1/SAR/IW_GRDH_1S"
_DATE_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})T\d{6}_")
_TIEPOINT_TAG = 33922  # ModelTiepointTag
_AFFINE_WARN_PX = 2.0


@dataclass(frozen=True)
class FetchDiagnostics:
    mode: Literal["row-range", "full-band"]
    bytes_downloaded: int
    requests: int
    window: tuple[int, int, int, int]  # row0, row1, col0, col1 in the full image
    affine_max_error_px: float
    affine_mean_error_px: float
    affine_max_error_m: float
    seconds: float


class _BandMissing(Exception):
    """The product has no object for this band."""


class _Tally:
    """Bytes and requests of ONE fetch call. Per call, because the API runs several fetches on one fetcher."""

    def __init__(self) -> None:
        self.bytes = 0
        self.requests = 0
        self._lock = threading.Lock()

    def add(self, nbytes: int, requests: int = 1) -> None:
        with self._lock:
            self.bytes += nbytes
            self.requests += requests


@dataclass(frozen=True)
class _Layout:
    height: int
    width: int
    dtype: np.dtype[Any]
    offsets: Sequence[int]
    counts: Sequence[int]
    fast: bool  # uncompressed, row-striped and contiguous


def create_s3_client(access_key: str, secret_key: str, endpoint_url: str = ENDPOINT_URL) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="default",
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},  # retries are ours, so they are counted and bounded
            connect_timeout=15,
            read_timeout=60,
        ),
    )


class Sentinel1S3Fetcher:
    """Downloads the Region's part of a Scene's VV band and returns calibrated linear sigma0.

    After each call `last_diagnostics` holds bytes downloaded, request count and geolocation error.
    """

    def __init__(
        self,
        client: Any,
        bucket: str = BUCKET,
        retry: RetryPolicy | None = None,
        chunk_bytes: int = 64 << 20,
        max_workers: int = 2,
        header_block: int = 1 << 20,
        subtract_noise: bool = True,
    ) -> None:
        self._subtract_noise = subtract_noise
        self._client = client
        self._bucket = bucket
        self._retry = retry or RetryPolicy()
        self._chunk_bytes = chunk_bytes
        self._header_block = header_block
        self._max_workers = max_workers
        self.last_diagnostics: FetchDiagnostics | None = None

    # ------------------------------------------------------------------ public

    def fetch(self, scene: SceneRef, region: Region) -> SceneRaster:
        t0 = time.monotonic()
        tally = _Tally()
        region_poly = ring_to_polygon(region.polygon)
        if not Polygon(scene.footprint).intersects(region_poly):
            raise PermanentError(f"{scene.scene_id} does not intersect Region {region.region_id}")

        prefix = self._product_prefix(scene)
        sigma0, grid, window, mode = self._read_band(
            prefix, "vv", region_poly, scene, region.region_id, tally
        )
        r0, _, c0, _ = window

        sigma0_vh = None
        try:
            sigma0_vh = self._read_band(prefix, "vh", region_poly, scene, region.region_id, tally, window)[0]
        except _BandMissing as exc:
            log.warning("%s: no VH band, detection uses VV only (%s)", scene.scene_id, exc)

        fit = grid.fit_affine(window)
        if fit.max_error_px > _AFFINE_WARN_PX:
            log.warning(
                "%s: single-affine geolocation error up to %.1f px (%.0f m), mean %.1f px",
                scene.scene_id,
                fit.max_error_px,
                fit.max_error_m,
                fit.mean_error_px,
            )
        self.last_diagnostics = FetchDiagnostics(
            mode=mode,
            bytes_downloaded=tally.bytes,
            requests=tally.requests,
            window=window,
            affine_max_error_px=fit.max_error_px,
            affine_mean_error_px=fit.mean_error_px,
            affine_max_error_m=fit.max_error_m,
            seconds=time.monotonic() - t0,
        )

        def geolocate(
            rows: NDArray[np.float64], cols: NDArray[np.float64]
        ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
            # window-relative pixel-centre indices -> file indices -> bilinear GCP interpolation
            return grid.lonlat(
                np.asarray(rows, dtype=np.float64) + r0, np.asarray(cols, dtype=np.float64) + c0
            )

        acquired_at, geometry = self._annotation(scene, prefix, window, tally)
        return SceneRaster(
            scene_id=scene.scene_id,
            acquired_at=acquired_at,
            sigma0_vv=sigma0,
            sigma0_vh=sigma0_vh,
            transform=fit.transform,
            geolocate=geolocate,
            bytes_downloaded=tally.bytes,
            geometry=geometry,
        )

    # ----------------------------------------------------------------- helpers

    def _read_band(
        self,
        prefix: str,
        pol: Literal["vv", "vh"],
        region_poly: Polygon,
        scene: SceneRef,
        region_id: str,
        tally: _Tally,
        window: tuple[int, int, int, int] | None = None,
    ) -> tuple[NDArray[np.float32], GcpGrid, tuple[int, int, int, int], Literal["row-range", "full-band"]]:
        """Calibrated sigma0 of one polarisation inside the Region window, NaN outside the Region polygon.

        VV picks the window; VH is read over the same window (both bands share the pixel grid). A missing VH
        band raises `_BandMissing`, a missing VV band is permanent.
        """
        name = pol.upper()
        try:
            tiff_key = self._find_key(
                prefix + "measurement/", lambda n: f"-{pol}-" in n and n.endswith(".tiff"), f"{name} band"
            )
            cal_key = self._find_key(
                prefix + "annotation/calibration/",
                lambda n: n.startswith("calibration-") and f"-{pol}-" in n and n.endswith(".xml"),
                f"{name} calibration",
            )
        except PermanentError as exc:
            if window is not None:
                raise _BandMissing(str(exc)) from exc
            raise

        reader = S3RangeReader(self._client, self._bucket, tiff_key, self._retry, self._header_block)
        try:
            layout, grid = self._read_header(reader)
        except (tifffile.TiffFileError, ValueError, struct.error, EOFError, KeyError, IndexError) as exc:
            raise PermanentError(f"{tiff_key}: unreadable TIFF header: {exc!r}") from exc
        tally.add(reader.bytes_read, reader.requests)

        if window is None:
            window = grid.region_window(region_poly, layout.height, layout.width)
            if window is None:
                raise PermanentError(
                    f"{scene.scene_id}: no pixel of the scene lies inside Region {region_id}"
                )
        r0, r1, c0, c1 = window
        if layout.fast:
            dn = self._read_rows(tiff_key, layout, r0, r1, c0, c1, tally)
            mode: Literal["row-range", "full-band"] = "row-range"
        else:
            log.warning(
                "%s: layout is not row-striped/uncompressed, downloading the full %s band", tiff_key, name
            )
            dn = self._read_full_band(tiff_key, reader.size, r0, r1, c0, c1, tally)
            mode = "full-band"

        cal = CalibrationLut.from_xml(self._get_object(cal_key, tally))
        noise = self._read_noise(prefix, pol, scene, tally) if self._subtract_noise else None
        sigma0 = cal.sigma0(dn, r0, c0, noise)
        sigma0[~grid.region_mask(region_poly, window)] = np.nan
        return sigma0, grid, window, mode

    def _read_noise(self, prefix: str, pol: str, scene: SceneRef, tally: _Tally) -> NoiseLut | None:
        """The thermal noise LUT of one band; None (with a warning) when the product has none."""
        try:
            key = self._find_key(
                prefix + "annotation/calibration/",
                lambda n: n.startswith("noise-") and f"-{pol}-" in n and n.endswith(".xml"),
                f"{pol.upper()} noise",
            )
        except PermanentError as exc:
            log.warning("%s: %s, thermal noise is not subtracted", scene.scene_id, exc)
            return None
        noise = NoiseLut.from_xml(self._get_object(key, tally))
        if noise is None:
            log.warning("%s: %s has no noise vectors, thermal noise is not subtracted", scene.scene_id, key)
        return noise

    def _annotation(
        self, scene: SceneRef, prefix: str, window: tuple[int, int, int, int], tally: _Tally
    ) -> tuple[Any, SarGeometry | None]:
        """Acquisition time of the window's centre row and the imaging geometry, from the VV annotation.

        The catalogue time is the start of a ~25 s acquisition, so using it directly would misplace ships
        by up to 25 s of motion when matching against AIS. Falls back to the catalogue time (and no
        geometry) if the annotation is missing or lacks the interval.
        """
        r0, r1, c0, c1 = window
        try:
            key = self._find_key(
                prefix + "annotation/",
                lambda n: n.startswith("s1") and "-vv-" in n and n.endswith(".xml"),
                "VV annotation",
            )
            root = ET.fromstring(self._get_object(key, tally))
            node = root.find(".//azimuthTimeInterval")
            if node is None or not node.text:
                raise ValueError("no azimuthTimeInterval")
            acquired_at = scene.acquired_at + timedelta(seconds=(r0 + r1) / 2 * float(node.text))
        except (PermanentError, ValueError, ET.ParseError) as exc:
            log.warning("%s: using the catalogue acquisition time (%s)", scene.scene_id, exc)
            return scene.acquired_at, None
        try:
            return acquired_at, parse_geometry(root, (r0 + r1) / 2, (c0 + c1) / 2)
        except (ValueError, TypeError) as exc:
            log.warning(
                "%s: no imaging geometry, AIS matching skips the Doppler shift (%s)", scene.scene_id, exc
            )
            return acquired_at, None

    @staticmethod
    def _product_prefix(scene: SceneRef) -> str:
        m = _DATE_RE.search(scene.scene_id)
        y, mo, d = (
            m.groups()
            if m
            else (f"{scene.acquired_at:%Y}", f"{scene.acquired_at:%m}", f"{scene.acquired_at:%d}")
        )
        return f"{_PRODUCT_ROOT}/{y}/{mo}/{d}/{scene.scene_id}.SAFE/"

    def _find_key(self, prefix: str, wanted: Any, what: str) -> str:
        def once() -> list[str]:
            keys: list[str] = []
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                try:
                    page = self._client.list_objects_v2(**kwargs)
                except ClientError as exc:
                    raise_if_permanent(exc)
                    raise
                keys += [o["Key"] for o in page.get("Contents", [])]
                if not page.get("IsTruncated"):
                    return keys
                token = page.get("NextContinuationToken")

        keys = self._retry.call(once, is_retryable, f"S3 LIST {prefix}")
        if not keys:
            # The catalogue can list a product a little before its files are visible on S3.
            raise TransientError(f"no objects under {prefix} (product not on S3 yet?)")
        hits = [k for k in keys if wanted(k.rsplit("/", 1)[-1])]
        if not hits:
            raise PermanentError(f"{prefix}: no {what} found (unsupported polarisation mode?)")
        if len(hits) > 1:
            raise PermanentError(f"{prefix}: {len(hits)} candidates for {what}: {hits}")
        return hits[0]

    def _get_object(self, key: str, tally: _Tally) -> bytes:
        def once() -> bytes:
            try:
                data: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            except ClientError as exc:
                raise_if_permanent(exc)
                raise
            return data

        data = self._retry.call(once, is_retryable, f"S3 GET {key}")
        tally.add(len(data))
        return data

    def _read_header(self, reader: S3RangeReader) -> tuple[_Layout, GcpGrid]:
        with tifffile.TiffFile(cast(BinaryIO, reader)) as tif:
            page = tif.pages[0]
            if not isinstance(page, tifffile.TiffPage) or page.dtype is None:
                raise PermanentError("first TIFF page is not a plain image")
            height, width = page.shape[:2]
            offsets = page.dataoffsets
            counts = page.databytecounts
            tiepoints = page.tags[_TIEPOINT_TAG].value
            itemsize = page.dtype.itemsize
            fast = (
                not page.is_tiled
                and page.compression == tifffile.COMPRESSION.NONE
                and page.planarconfig == tifffile.PLANARCONFIG.CONTIG
                and page.samplesperpixel == 1
                and page.rowsperstrip == 1
                and page.dtype.kind == "u"
                and page.dtype.itemsize == 2
                and len(offsets) == height
                and all(c == width * itemsize for c in counts)
                and all(offsets[i + 1] == offsets[i] + counts[i] for i in range(len(offsets) - 1))
            )
            # tifffile reports a native-order dtype; the raw strip bytes follow the file's byte order
            raw_dtype = np.dtype(f"{tif.byteorder}u2") if itemsize == 2 else page.dtype
            layout = _Layout(height, width, raw_dtype, offsets, counts, fast)
        return layout, GcpGrid(np.asarray(tiepoints, dtype=np.float64))

    def _read_rows(
        self, key: str, lay: _Layout, r0: int, r1: int, c0: int, c1: int, tally: _Tally
    ) -> NDArray[np.uint16]:
        row_bytes = lay.width * lay.dtype.itemsize
        step = max(1, self._chunk_bytes // row_bytes)
        spans = [(a, min(a + step, r1)) for a in range(r0, r1, step)]

        def read(span: tuple[int, int]) -> NDArray[np.uint16]:
            a, b = span
            start = lay.offsets[a]
            end = lay.offsets[b - 1] + lay.counts[b - 1] - 1
            data = ranged_get(self._client, self._bucket, key, start, end, self._retry)
            tally.add(len(data))
            rows = np.frombuffer(data, dtype=lay.dtype).reshape(b - a, lay.width)
            return np.array(rows[:, c0:c1], dtype=np.uint16)

        with ThreadPoolExecutor(self._max_workers) as pool:
            parts = list(pool.map(read, spans))
        return np.vstack(parts)

    def _read_full_band(
        self, key: str, size: int, r0: int, r1: int, c0: int, c1: int, tally: _Tally
    ) -> NDArray[np.uint16]:
        with tempfile.TemporaryDirectory(prefix="s1band-") as tmp:
            path = Path(tmp) / "vv.tiff"
            with path.open("wb") as fh:
                for start in range(0, size, self._chunk_bytes):
                    end = min(start + self._chunk_bytes, size) - 1
                    data = ranged_get(self._client, self._bucket, key, start, end, self._retry)
                    tally.add(len(data))
                    fh.write(data)
            try:
                with tifffile.TiffFile(path) as tif:
                    full = tif.pages[0].asarray()
            except (tifffile.TiffFileError, ValueError, struct.error, EOFError, NotImplementedError) as exc:
                raise PermanentError(f"{key}: cannot decode TIFF: {exc!r}") from exc
        if full.ndim != 2:
            raise PermanentError(f"{key}: expected a single-band image, got shape {full.shape}")
        return np.array(full[r0:r1, c0:c1], dtype=np.uint16)


_LIGHT_M_S = 299_792_458.0
_GRID_POINTS = 4  # grid points nearest to the window centre that are averaged


def parse_geometry(root: ET.Element, centre_row: float, centre_col: float) -> SarGeometry:
    """Imaging geometry near a window centre from a Sentinel-1 annotation (raises ValueError if absent).

    Heading from `platformHeading`, speed from the orbit state vectors, slant range (two-way time times c / 2)
    and incidence from the geolocation grid points nearest to the centre.
    """

    def number(node: ET.Element | None) -> float:
        if node is None or not node.text:
            raise ValueError("annotation field missing")
        return float(node.text)

    heading = number(root.find(".//platformHeading"))
    speeds = [
        math.sqrt(sum(number(v.find(axis)) ** 2 for axis in "xyz")) for v in root.findall(".//orbit/velocity")
    ]
    if not speeds:
        raise ValueError("no orbit state vectors")
    points = [
        (
            number(p.find("line")),
            number(p.find("pixel")),
            number(p.find("slantRangeTime")),
            number(p.find("incidenceAngle")),
        )
        for p in root.findall(".//geolocationGridPoint")
    ]
    if not points:
        raise ValueError("no geolocation grid")
    points.sort(key=lambda p: (p[0] - centre_row) ** 2 + (p[1] - centre_col) ** 2)
    near = points[:_GRID_POINTS]
    return SarGeometry(
        heading_deg=heading,
        speed_m_s=sum(speeds) / len(speeds),
        slant_range_m=sum(p[2] for p in near) / len(near) * _LIGHT_M_S / 2,
        incidence_deg=sum(p[3] for p in near) / len(near),
    )
