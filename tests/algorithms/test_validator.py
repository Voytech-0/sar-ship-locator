import math
from datetime import timedelta

import pytest

from contracts.protocols import AisPosition, DetectionRef, MatchLabel, SarGeometry, ValidationResult
from pipeline.algorithms import NearestNeighbourValidator
from pipeline.algorithms.sar_geometry import doppler_displacement_m
from tests.algorithms.synthetic import ACQUIRED

LAT, LON = 55.35, 10.97
M_LAT = 111_200.0
M_LON = 111_320.0 * 0.5675  # cos(55.35 deg)


def det(i: str, east_m: float = 0.0, north_m: float = 0.0) -> DetectionRef:
    return DetectionRef(i, (LON + east_m / M_LON, LAT + north_m / M_LAT), None)


def ais(
    mmsi: int,
    dt_s: float,
    east_m: float = 0.0,
    north_m: float = 0.0,
    sog_knots: float | None = None,
    cog_deg: float | None = None,
) -> AisPosition:
    return AisPosition(
        mmsi,
        ACQUIRED + timedelta(seconds=dt_s),
        (LON + east_m / M_LON, LAT + north_m / M_LAT),
        sog_knots=sog_knots,
        cog_deg=cog_deg,
    )


def run(dets: list[DetectionRef], pos: list[AisPosition], gate: float = 500.0) -> dict[str, ValidationResult]:
    return {r.detection_id: r for r in NearestNeighbourValidator().validate(dets, pos, ACQUIRED, gate)}


def test_matched_within_gate() -> None:
    r = run([det("a")], [ais(219000001, -5, 100, 0), ais(219000001, 5, 100, 0)])["a"]
    assert r.label == MatchLabel.MATCHED
    assert r.matched_mmsi == 219000001
    assert r.match_distance_m == pytest.approx(100, abs=3)


def test_unmatched_when_nothing_in_gate() -> None:
    r = run([det("a")], [ais(219000001, 0, 2000, 0)])["a"]
    assert (r.label, r.matched_mmsi, r.match_distance_m) == (MatchLabel.UNMATCHED, None, None)


def test_unmatched_when_no_ais_at_all() -> None:
    assert run([det("a")], [])["a"].label == MatchLabel.UNMATCHED


def test_no_detections() -> None:
    assert NearestNeighbourValidator().validate([], [ais(219000001, 0)], ACQUIRED, 500) == []


def test_ambiguous_when_two_tracks_in_gate() -> None:
    pos = [ais(219000001, 0, 100, 0), ais(219000002, 0, -150, 50)]
    r = run([det("a")], pos)["a"]
    assert (r.label, r.matched_mmsi, r.match_distance_m) == (MatchLabel.AMBIGUOUS, None, None)


def test_same_track_with_several_samples_is_not_ambiguous() -> None:
    pos = [ais(219000001, t, 100, 0) for t in (-30, -10, 10, 30)]
    assert run([det("a")], pos)["a"].label == MatchLabel.MATCHED


def test_two_detections_competing_for_one_track_nearest_wins() -> None:
    pos = [ais(219000001, 0, 0, 0)]
    out = run([det("far", 300, 0), det("near", 50, 0)], pos)
    assert out["near"].label == MatchLabel.MATCHED and out["near"].matched_mmsi == 219000001
    assert out["far"].label == MatchLabel.UNMATCHED


def test_tie_between_competing_detections_is_deterministic() -> None:
    pos = [ais(219000001, 0, 0, 0)]
    out = run([det("b", 100, 0), det("a", -100, 0)], pos)
    assert out["a"].label == MatchLabel.MATCHED  # equal distance: lower detection id wins
    assert out["b"].label == MatchLabel.UNMATCHED


def test_one_to_one_with_separate_tracks() -> None:
    pos = [ais(219000001, 0, 0, 0), ais(219000002, 0, 3000, 0)]
    out = run([det("a", 20), det("b", 3010)], pos)
    assert out["a"].matched_mmsi == 219000001
    assert out["b"].matched_mmsi == 219000002


def test_moving_vessel_is_interpolated_to_acquisition_time() -> None:
    # 10 kn east is ~5.1 m/s: samples 60 s either side sit 308 m west and 308 m east of the detection.
    pos = [ais(219000001, -60, -308, 0), ais(219000001, 60, 308, 0)]
    r = run([det("a")], pos, gate=50)["a"]
    assert r.label == MatchLabel.MATCHED
    assert r.match_distance_m == pytest.approx(0, abs=5)
    # Taking only the nearer sample would be 308 m off and miss a 50 m gate.
    assert run([det("a")], pos[:1], gate=50)["a"].label == MatchLabel.UNMATCHED


def test_stale_last_sample_is_not_used() -> None:
    # Last heard 30 minutes before acquisition, parked on the detection: not trustworthy.
    pos = [ais(219000001, -1800, 0, 0, sog_knots=0.0, cog_deg=0.0)]
    assert run([det("a")], pos)["a"].label == MatchLabel.UNMATCHED


def test_recent_sample_is_dead_reckoned() -> None:
    # Heard 100 s ago doing 10 kn due east: now ~514 m east of that fix, which is where the detection is.
    pos = [ais(219000001, -100, -514, 0, sog_knots=10.0, cog_deg=90.0)]
    r = run([det("a")], pos, gate=60)["a"]
    assert r.label == MatchLabel.MATCHED


def test_recent_sample_without_motion_data_stays_in_place() -> None:
    pos = [ais(219000001, -100, 0, 0)]
    assert run([det("a")], pos, gate=60)["a"].label == MatchLabel.MATCHED


def test_wide_gap_between_bracketing_samples_does_not_interpolate() -> None:
    # Samples 40 minutes apart straddle acquisition; the track is dropped rather than guessed.
    pos = [ais(219000001, -1200, -2000, 0), ais(219000001, 1200, 2000, 0)]
    assert run([det("a")], pos)["a"].label == MatchLabel.UNMATCHED


def test_samples_far_from_acquisition_are_ignored() -> None:
    pos = [ais(219000001, 7200, 0, 0), ais(219000001, -7200, 0, 0)]
    assert run([det("a")], pos)["a"].label == MatchLabel.UNMATCHED


def test_ambiguous_detection_does_not_steal_a_track_from_a_clear_one() -> None:
    # Track 1 sits between "amb" and "clear"; "amb" also sees track 2. "clear" sees only track 1.
    pos = [ais(219000001, 0, 0, 0), ais(219000002, 0, -400, 0)]
    out = run([det("amb", -200, 0), det("clear", 300, 0)], pos, gate=450)
    assert out["amb"].label == MatchLabel.AMBIGUOUS
    assert out["clear"].label == MatchLabel.MATCHED and out["clear"].matched_mmsi == 219000001


def test_one_result_per_detection_in_input_order() -> None:
    dets = [det("c", 5000), det("a"), det("b", 2500)]
    results = NearestNeighbourValidator().validate(dets, [ais(219000001, 0)], ACQUIRED, 500)
    assert [r.detection_id for r in results] == ["c", "a", "b"]


def test_consumes_a_generator_of_positions() -> None:
    gen = (p for p in [ais(219000001, 0, 100, 0)])
    results = NearestNeighbourValidator().validate([det("a")], gen, ACQUIRED, 500)
    assert results[0].label == MatchLabel.MATCHED


# Descending pass: heading 196 degrees (south-south-west), looking 90 degrees clockwise, i.e. west-north-west.
GEOMETRY = SarGeometry(heading_deg=-163.6, speed_m_s=7588.0, slant_range_m=867_000.0, incidence_deg=37.9)


def test_doppler_displacement_of_a_ship_moving_away_from_the_sensor() -> None:
    look = math.radians(GEOMETRY.heading_deg + 90)
    u = 5.0  # m/s along the look direction, away from the sensor
    de, dn = doppler_displacement_m(GEOMETRY, u * math.sin(look), u * math.cos(look))
    expected = 867_000 / 7588 * math.sin(math.radians(37.9)) * u  # about 350 m
    assert math.hypot(de, dn) == pytest.approx(expected, rel=1e-6)
    # backwards along the platform track (heading 196.4 degrees)
    heading = math.radians(GEOMETRY.heading_deg)
    assert de * math.sin(heading) + dn * math.cos(heading) == pytest.approx(-expected, rel=1e-6)


def test_ship_moving_along_the_track_or_standing_still_is_not_displaced() -> None:
    heading = math.radians(GEOMETRY.heading_deg)
    assert doppler_displacement_m(GEOMETRY, 0.0, 0.0) == (0.0, 0.0)
    de, dn = doppler_displacement_m(GEOMETRY, 8 * math.sin(heading), 8 * math.cos(heading))
    assert math.hypot(de, dn) == pytest.approx(0.0, abs=1e-6)


def test_moving_ship_matches_only_once_the_displacement_is_applied() -> None:
    look = math.radians(GEOMETRY.heading_deg + 90)
    knots = 10.0
    cog = math.degrees(look) % 360  # sailing away from the sensor
    track = [ais(219000001, dt, 0, 0, knots, cog) for dt in (-5, 5)]
    de, dn = doppler_displacement_m(
        GEOMETRY, knots * 0.514444 * math.sin(look), knots * 0.514444 * math.cos(look)
    )
    dets = [det("a", de, dn)]  # the SAR image shows the ship displaced

    plain = {r.detection_id: r for r in NearestNeighbourValidator().validate(dets, track, ACQUIRED, 150)}
    shifted = {
        r.detection_id: r for r in NearestNeighbourValidator().validate(dets, track, ACQUIRED, 150, GEOMETRY)
    }

    assert math.hypot(de, dn) > 250
    assert plain["a"].label == MatchLabel.UNMATCHED
    assert shifted["a"].label == MatchLabel.MATCHED
    assert shifted["a"].match_distance_m == pytest.approx(0, abs=5)


def test_a_standing_ship_is_unaffected_by_the_geometry() -> None:
    r = {
        x.detection_id: x
        for x in NearestNeighbourValidator().validate(
            [det("a", 40)], [ais(219000001, 0)], ACQUIRED, 150, GEOMETRY
        )
    }
    assert r["a"].label == MatchLabel.MATCHED
    assert r["a"].match_distance_m == pytest.approx(40, abs=3)
