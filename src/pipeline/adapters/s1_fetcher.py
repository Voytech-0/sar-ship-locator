"""SceneFetcher for Sentinel-1 IW GRDH products on the CDSE S3 endpoint (docs/adr/0004).

Fast path: the VV measurement TIFF is uncompressed, one row per strip, with contiguous strips, so the rows
covering the Region are fetched as a few large ranged GETs and the columns are cropped in memory.
Fallback: any other layout (compressed, tiled, non-contiguous) downloads the whole VV band.
"""

from __future__ import annotations

import logging
import re
import struct
import tempfile
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

import numpy as np
import tifffile
from botocore.exceptions import ClientError
from numpy.typing import NDArray
from shapely.geometry import Polygon

from contracts.protocols import PermanentError, Region, SceneRaster, SceneRef, TransientError

from ._retry import RetryPolicy
from ._s3 import S3RangeReader, is_retryable, raise_if_permanent, ranged_get
from .s1_calibration import CalibrationLut
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
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._retry = retry or RetryPolicy()
        self._chunk_bytes = chunk_bytes
        self._header_block = header_block
        self._max_workers = max_workers
        self.last_diagnostics: FetchDiagnostics | None = None
        self._lock = threading.Lock()
        self._bytes = 0
        self._requests = 0

    # ------------------------------------------------------------------ public

    def fetch(self, scene: SceneRef, region: Region) -> SceneRaster:
        t0 = time.monotonic()
        self._bytes = self._requests = 0
        region_poly = ring_to_polygon(region.polygon)
        if not Polygon(scene.footprint).intersects(region_poly):
            raise PermanentError(f"{scene.scene_id} does not intersect Region {region.region_id}")

        prefix = self._product_prefix(scene)
        tiff_key = self._find_key(
            prefix + "measurement/", lambda n: "-vv-" in n and n.endswith(".tiff"), "VV band"
        )
        cal_key = self._find_key(
            prefix + "annotation/calibration/",
            lambda n: n.startswith("calibration-") and "-vv-" in n and n.endswith(".xml"),
            "VV calibration",
        )

        reader = S3RangeReader(self._client, self._bucket, tiff_key, self._retry, self._header_block)
        try:
            layout, grid = self._read_header(reader)
        except (tifffile.TiffFileError, ValueError, struct.error, EOFError, KeyError, IndexError) as exc:
            raise PermanentError(f"{tiff_key}: unreadable TIFF header: {exc!r}") from exc
        self._count(reader.bytes_read, reader.requests)

        window = grid.region_window(region_poly, layout.height, layout.width)
        if window is None:
            raise PermanentError(
                f"{scene.scene_id}: no pixel of the scene lies inside Region {region.region_id}"
            )
        r0, r1, c0, c1 = window

        if layout.fast:
            dn = self._read_rows(tiff_key, layout, r0, r1, c0, c1)
            mode: Literal["row-range", "full-band"] = "row-range"
        else:
            log.warning("%s: layout is not row-striped/uncompressed, downloading the full VV band", tiff_key)
            dn = self._read_full_band(tiff_key, reader.size, r0, r1, c0, c1)
            mode = "full-band"

        cal = CalibrationLut.from_xml(self._get_object(cal_key))
        sigma0 = cal.sigma0(dn, r0, c0)
        del dn
        sigma0[~grid.region_mask(region_poly, window)] = np.nan

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
            bytes_downloaded=self._bytes,
            requests=self._requests,
            window=window,
            affine_max_error_px=fit.max_error_px,
            affine_mean_error_px=fit.mean_error_px,
            affine_max_error_m=fit.max_error_m,
            seconds=time.monotonic() - t0,
        )
        return SceneRaster(
            scene_id=scene.scene_id,
            acquired_at=scene.acquired_at,
            sigma0_vv=sigma0,
            transform=fit.transform,
        )

    # ----------------------------------------------------------------- helpers

    def _count(self, nbytes: int, requests: int) -> None:
        with self._lock:
            self._bytes += nbytes
            self._requests += requests

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

    def _get_object(self, key: str) -> bytes:
        def once() -> bytes:
            try:
                data: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            except ClientError as exc:
                raise_if_permanent(exc)
                raise
            return data

        data = self._retry.call(once, is_retryable, f"S3 GET {key}")
        self._count(len(data), 1)
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
                and page.dtype == np.dtype("<u2")
                and len(offsets) == height
                and all(c == width * itemsize for c in counts)
                and all(offsets[i + 1] == offsets[i] + counts[i] for i in range(len(offsets) - 1))
            )
            layout = _Layout(height, width, page.dtype, offsets, counts, fast)
        return layout, GcpGrid(np.asarray(tiepoints, dtype=np.float64))

    def _read_rows(self, key: str, lay: _Layout, r0: int, r1: int, c0: int, c1: int) -> NDArray[np.uint16]:
        row_bytes = lay.width * lay.dtype.itemsize
        step = max(1, self._chunk_bytes // row_bytes)
        spans = [(a, min(a + step, r1)) for a in range(r0, r1, step)]

        def read(span: tuple[int, int]) -> NDArray[np.uint16]:
            a, b = span
            start = lay.offsets[a]
            end = lay.offsets[b - 1] + lay.counts[b - 1] - 1
            data = ranged_get(self._client, self._bucket, key, start, end, self._retry)
            self._count(len(data), 1)
            rows = np.frombuffer(data, dtype=lay.dtype).reshape(b - a, lay.width)
            return np.array(rows[:, c0:c1], dtype=np.uint16)

        with ThreadPoolExecutor(self._max_workers) as pool:
            parts = list(pool.map(read, spans))
        return np.vstack(parts)

    def _read_full_band(self, key: str, size: int, r0: int, r1: int, c0: int, c1: int) -> NDArray[np.uint16]:
        with tempfile.TemporaryDirectory(prefix="s1band-") as tmp:
            path = Path(tmp) / "vv.tiff"
            with path.open("wb") as fh:
                for start in range(0, size, self._chunk_bytes):
                    end = min(start + self._chunk_bytes, size) - 1
                    data = ranged_get(self._client, self._bucket, key, start, end, self._retry)
                    self._count(len(data), 1)
                    fh.write(data)
            try:
                with tifffile.TiffFile(path) as tif:
                    full = tif.pages[0].asarray()
            except (tifffile.TiffFileError, ValueError, struct.error, EOFError, NotImplementedError) as exc:
                raise PermanentError(f"{key}: cannot decode TIFF: {exc!r}") from exc
        if full.ndim != 2:
            raise PermanentError(f"{key}: expected a single-band image, got shape {full.shape}")
        return np.array(full[r0:r1, c0:c1], dtype=np.uint16)
