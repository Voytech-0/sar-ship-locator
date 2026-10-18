"""Validation of Detections against AIS Positions at the Scene's acquisition time.

Each AIS track (one per MMSI) is brought to the acquisition time:

1. Samples on both sides of the acquisition time that are at most `max_gap_s` apart: linear interpolation.
2. Otherwise the nearest sample, if it is within `max_extrapolation_s`, moved by dead reckoning from its
   SOG/COG (left in place when those are missing). A longer gap means the track is not trusted and is dropped,
   so a vessel last heard half an hour before acquisition never matches.

Matching:
- A Detection with more than one distinct track inside the gate is AMBIGUOUS and claims no track.
- A Detection with exactly one track inside the gate is a candidate for it. If several Detections want the
  same track, the nearest one gets it (ties broken by detection id) and the others stay UNMATCHED: one track
  explains at most one Detection.
- A Detection with no track inside the gate is UNMATCHED.

Distances use a local equirectangular projection around the Detections, accurate to a couple of percent.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import cast

import numpy as np
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from contracts.protocols import AisPosition, DetectionRef, MatchLabel, SarGeometry, ValidationResult
from pipeline.algorithms.geo import meters_per_degree
from pipeline.algorithms.sar_geometry import image_displacement_m

_KNOT_MS = 0.514444

# (dt seconds relative to acquisition, lon, lat, sog_knots, cog_deg, the report itself)
_Sample = tuple[float, float, float, float | None, float | None, AisPosition]


class NearestNeighbourValidator:
    """`Validator` implementation: interpolate tracks, nearest-neighbour one-to-one within a gate."""

    def __init__(self, max_gap_s: float = 900.0, max_extrapolation_s: float = 300.0) -> None:
        self.max_gap_s = max_gap_s
        self.max_extrapolation_s = max_extrapolation_s

    def validate(
        self,
        detections: Sequence[DetectionRef],
        positions: Iterable[AisPosition],
        acquired_at: datetime,
        gate_m: float,
        geometry: SarGeometry | None = None,
    ) -> Sequence[ValidationResult]:
        if not detections:
            return []
        tracks = self.tracks_in_image(positions, acquired_at, geometry)
        mmsis = list(tracks)
        det_ll = np.array([d.position for d in detections], dtype=np.float64)
        lon0, lat0 = float(det_ll[:, 0].mean()), float(det_ll[:, 1].mean())
        m_lon, m_lat = meters_per_degree(lat0)

        def project(ll: NDArray[np.float64]) -> NDArray[np.float64]:
            return np.column_stack([(ll[:, 0] - lon0) * m_lon, (ll[:, 1] - lat0) * m_lat])

        det_xy = project(det_ll)
        candidates: list[list[tuple[float, int]]] = [[] for _ in detections]
        if mmsis:
            trk_xy = project(np.array([tracks[m] for m in mmsis], dtype=np.float64))
            tree = cKDTree(trk_xy)
            for i, near in enumerate(cast(list[list[int]], tree.query_ball_point(det_xy, r=gate_m))):
                for j in near:
                    dist = float(np.hypot(*(trk_xy[j] - det_xy[i])))
                    candidates[i].append((dist, int(j)))

        labels: list[MatchLabel] = [MatchLabel.UNMATCHED] * len(detections)
        matched: dict[int, tuple[int, float]] = {}
        wanted: list[tuple[float, str, int, int]] = []
        for i, cands in enumerate(candidates):
            if len(cands) > 1:
                labels[i] = MatchLabel.AMBIGUOUS
            elif len(cands) == 1:
                dist, j = cands[0]
                wanted.append((dist, detections[i].detection_id, i, j))
        claimed: set[int] = set()
        for dist, _, i, j in sorted(wanted):
            if j in claimed:
                continue
            claimed.add(j)
            matched[i] = (j, dist)
            labels[i] = MatchLabel.MATCHED

        results: list[ValidationResult] = []
        for i, det in enumerate(detections):
            if i in matched:
                j, dist = matched[i]
                results.append(ValidationResult(det.detection_id, labels[i], mmsis[j], dist))
            else:
                results.append(ValidationResult(det.detection_id, labels[i], None, None))
        return results

    def tracks_at(
        self, positions: Iterable[AisPosition], acquired_at: datetime
    ) -> dict[int, tuple[float, float]]:
        """Position of every trustworthy track at `acquired_at`, keyed by MMSI."""
        return {m: (t[0], t[1]) for m, t in self._tracks(positions, acquired_at).items()}

    def tracks_in_image(
        self, positions: Iterable[AisPosition], acquired_at: datetime, geometry: SarGeometry | None
    ) -> dict[int, tuple[float, float]]:
        """Where every trustworthy track appears in the SAR image (Doppler and elevation displacements)."""
        out: dict[int, tuple[float, float]] = {}
        for mmsi, (lon, lat, ve, vn, centre) in self._tracks(positions, acquired_at).items():
            m_lon, m_lat = meters_per_degree(lat)
            lon, lat = lon + centre[0] / m_lon, lat + centre[1] / m_lat  # the antenna is not the hull centre
            if geometry is not None:
                de, dn = image_displacement_m(geometry, ve, vn)
                lon, lat = lon + de / m_lon, lat + dn / m_lat
            out[mmsi] = (lon, lat)
        return out

    def _tracks(
        self, positions: Iterable[AisPosition], acquired_at: datetime
    ) -> dict[int, tuple[float, float, float, float, tuple[float, float]]]:
        """(lon, lat, east m/s, north m/s, (east, north) metres from the antenna to the hull centre) of every
        trustworthy track at `acquired_at`. No SOG/COG means 0 m/s; no heading or dimensions means the
        antenna is taken as the hull centre."""
        t0 = acquired_at.timestamp()
        samples: dict[int, list[_Sample]] = defaultdict(list)
        for p in positions:
            dt = p.ts.timestamp() - t0
            if abs(dt) <= self.max_gap_s:  # memory stays bounded when given a whole day of AIS
                samples[p.mmsi].append((dt, p.position[0], p.position[1], p.sog_knots, p.cog_deg, p))

        out: dict[int, tuple[float, float, float, float, tuple[float, float]]] = {}
        for mmsi, rows in samples.items():
            rows.sort(key=lambda s: s[0])
            before = [s for s in rows if s[0] <= 0]
            after = [s for s in rows if s[0] >= 0]
            lo = before[-1] if before else None
            hi = after[0] if after else None
            nearest = min((s for s in (lo, hi) if s is not None), key=lambda s: abs(s[0]), default=None)
            if lo is not None and hi is not None and (hi[0] - lo[0]) <= self.max_gap_s:
                if hi[0] == lo[0]:
                    lon, lat = lo[1], lo[2]
                else:
                    w = -lo[0] / (hi[0] - lo[0])
                    lon, lat = lo[1] + w * (hi[1] - lo[1]), lo[2] + w * (hi[2] - lo[2])
            elif nearest is not None and abs(nearest[0]) <= self.max_extrapolation_s:
                lon, lat = _dead_reckon(nearest)
            else:
                continue
            assert nearest is not None
            out[mmsi] = (lon, lat, *_velocity(nearest), _centre_offset(nearest))
        return out


def _velocity(sample: _Sample) -> tuple[float, float]:
    """(east, north) m/s from a sample's SOG/COG; (0, 0) when either is missing."""
    _, _, _, sog, cog, _ = sample
    if sog is None or cog is None:
        return 0.0, 0.0
    bearing = math.radians(cog)
    return sog * _KNOT_MS * math.sin(bearing), sog * _KNOT_MS * math.cos(bearing)


def _dead_reckon(sample: _Sample) -> tuple[float, float]:
    """Move a sample to acquisition time (dt = sample time - acquisition time) along its COG at its SOG."""
    dt, lon, lat, sog, cog, _ = sample
    if sog is None or cog is None:
        return lon, lat
    dist_m = sog * _KNOT_MS * (-dt)
    m_lon, m_lat = meters_per_degree(lat)
    bearing = math.radians(cog)
    return lon + dist_m * math.sin(bearing) / m_lon, lat + dist_m * math.cos(bearing) / m_lat


def _centre_offset(sample: _Sample) -> tuple[float, float]:
    """(east, north) metres from the reported antenna position to the hull centre.

    The antenna sits aft on most ships (typically near the bridge) while the SAR return is centred on the
    hull, which put Detections of large ships 40-60 m ahead of their AIS position. Needs the heading (or
    the course) and the four antenna distances; otherwise the antenna is taken as the centre.
    """
    p = sample[5]
    bearing = p.heading if p.heading is not None else p.cog_deg
    if bearing is None or None in (p.to_bow_m, p.to_stern_m, p.to_port_m, p.to_starboard_m):
        return 0.0, 0.0
    assert p.to_bow_m is not None and p.to_stern_m is not None
    assert p.to_port_m is not None and p.to_starboard_m is not None
    forward = (p.to_bow_m - p.to_stern_m) / 2
    starboard = (p.to_starboard_m - p.to_port_m) / 2
    b = math.radians(bearing)
    return (
        forward * math.sin(b) + starboard * math.cos(b),
        forward * math.cos(b) - starboard * math.sin(b),
    )
