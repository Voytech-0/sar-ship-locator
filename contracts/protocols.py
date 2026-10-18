"""Interfaces between pipeline components. Implementations live in `pipeline/`; fakes in `tests/`.

Pure value types plus Protocols, no I/O and no third-party imports at runtime, so every stream can
type-check and test against this file alone. Coordinates are WGS 84 (lon, lat). Times are timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

LonLat = tuple[float, float]


class Geolocator(Protocol):
    """Maps pixel indices (row, col; pixel centres, 0-based) to (lon, lat) arrays of the same shape."""

    def __call__(
        self, rows: NDArray[np.float64], cols: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]: ...


class ShoreClass(StrEnum):
    OPEN_WATER = "open_water"
    NEAR_SHORE = "near_shore"
    ON_LAND = "on_land"  # never stored; Detections here are dropped


class MatchLabel(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Region:
    region_id: str
    name: str
    polygon: Sequence[LonLat]  # exterior ring, closed


@dataclass(frozen=True)
class SceneRef:
    """A Scene as listed by the catalogue, before any download."""

    scene_id: str
    acquired_at: datetime
    footprint: Sequence[LonLat]  # exterior ring, closed


@dataclass(frozen=True)
class SarGeometry:
    """Imaging geometry of a Scene's Region window, from the product annotation (right-looking sensor).

    A ship moving in range is displaced along the platform track in the image by about
    `slant_range_m / speed_m_s * sin(incidence)` times its ground speed along the look direction, so a
    moving ship appears hundreds of metres from where AIS puts it, and any ship appears about
    `target_height_m / tan(incidence)` closer to the sensor than it is. The Validator corrects for both.
    """

    heading_deg: float  # platform heading, degrees clockwise from north
    speed_m_s: float  # platform speed
    slant_range_m: float
    incidence_deg: float
    # Height of the targets above the ellipsoid the product is geolocated on (sea level: the geoid height).
    # Set from the Region config when the Scene is fetched.
    target_height_m: float = 0.0


@dataclass(frozen=True)
class SceneRaster:
    """A Scene cropped to its Region, ready for detection."""

    scene_id: str
    acquired_at: datetime
    sigma0_vv: NDArray[np.float32]  # linear sigma0, shape (rows, cols); NaN outside the Region
    transform: tuple[float, float, float, float, float, float]  # affine (a, b, c, d, e, f), pixel -> lon/lat
    water_mask: NDArray[np.bool_] | None = None  # True = Detector may use pixel (not land); set by stage
    # Accurate pixel -> lon/lat mapping (e.g. GCP interpolation). `transform` is only a coarse approximation
    # for a Sentinel-1 window (up to ~200 m off); Detectors must report positions via `geolocate` when set.
    # None means `transform` is exact (synthetic rasters, tests).
    geolocate: Geolocator | None = None
    bytes_downloaded: int = 0  # set by the fetcher; feeds FetchResult.bytes_downloaded
    geometry: SarGeometry | None = None  # set by the fetcher when the annotation has it


@dataclass(frozen=True)
class DetectionCandidate:
    position: LonLat
    confidence: float  # 0..1
    length_m: float | None
    pixel_box: tuple[int, int, int, int]  # (row0, col0, row1, col1), half-open (end excluded); cuts the Chip


@dataclass(frozen=True)
class AisPosition:
    mmsi: int
    ts: datetime
    position: LonLat
    sog_knots: float | None = None
    cog_deg: float | None = None
    heading: int | None = None
    # Static data, filled when the archive row has it.
    name: str | None = None
    ship_type: str | None = None
    length_m: float | None = None
    width_m: float | None = None
    # Antenna position on the hull: metres to the bow, stern, port and starboard side (AIS A, B, C, D).
    to_bow_m: float | None = None
    to_stern_m: float | None = None
    to_port_m: float | None = None
    to_starboard_m: float | None = None


@dataclass(frozen=True)
class DetectionRef:
    detection_id: str
    position: LonLat
    length_m: float | None


@dataclass(frozen=True)
class ValidationResult:
    detection_id: str
    label: MatchLabel
    matched_mmsi: int | None  # set iff label == MATCHED
    match_distance_m: float | None  # set iff label == MATCHED


class CatalogueClient(Protocol):
    """Lists Sentinel-1 GRD products. Real: CDSE OData/STAC. Fake: `fake-sources`."""

    def search(self, region: Region, since: datetime) -> Sequence[SceneRef]: ...


class SceneFetcher(Protocol):
    """Downloads a product and returns it cropped to the Region. Raises `PermanentError` on corrupt data."""

    def fetch(self, scene: SceneRef, region: Region) -> SceneRaster: ...


class AisSource(Protocol):
    """Source of historical AIS. Real: DMA daily archives. Possible fallback: a live recorder."""

    def latest_available_day(self) -> date | None: ...

    def positions(self, day: date, region: Region) -> Iterable[AisPosition]:
        """All AIS Positions of one UTC day inside the Region. Raises `NotAvailableError` if not published."""
        ...


class LandMask(Protocol):
    def not_land_mask(self, raster: SceneRaster) -> NDArray[np.bool_]:
        """Mask aligned to `raster` (True = not land). Keeps the near-shore band so those ships are flagged,
        not dropped. This is what the `detect` stage passes to the Detector as `raster.water_mask`."""
        ...

    def water_mask(self, raster: SceneRaster) -> NDArray[np.bool_]:
        """Stricter mask (True = open water): excludes land and the near-shore band."""
        ...

    def classify(self, position: LonLat) -> ShoreClass: ...


class Detector(Protocol):
    name: str
    version: str  # bumps whenever behaviour or parameters change

    def detect(self, raster: SceneRaster) -> Sequence[DetectionCandidate]:
        """Pure function of the raster and the Detector's own parameters. Uses `raster.water_mask` if set and
        reports positions through `raster.geolocate` when set, else `raster.transform`."""
        ...


class Validator(Protocol):
    def validate(
        self,
        detections: Sequence[DetectionRef],
        positions: Iterable[AisPosition],
        acquired_at: datetime,
        gate_m: float,
        geometry: SarGeometry | None = None,
    ) -> Sequence[ValidationResult]:
        """Interpolate AIS tracks to `acquired_at`, match nearest-neighbour one-to-one within `gate_m`.
        With `geometry`, each track is first moved to where a moving ship appears in the SAR image.
        A Detection with more than one track in the gate is AMBIGUOUS, one with none is UNMATCHED."""
        ...


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes:
        """Raises FileNotFoundError for a missing key."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class TransientError(Exception):
    """Retryable failure (network, rate limit). Job fails with `error_retryable = true`."""


class PermanentError(Exception):
    """Not retryable (corrupt product, unsupported format). Job fails with `error_retryable = false`."""


class NotAvailableError(TransientError):
    """The requested data is not published yet (e.g. AIS day within the archive lag)."""
