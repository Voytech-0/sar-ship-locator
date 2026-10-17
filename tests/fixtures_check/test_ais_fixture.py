"""Stream B gate: the AIS slice parses, and real vessels line up with real ships in the Scene fixture."""

from __future__ import annotations

from datetime import datetime

from tests.fixtures_check.helpers import (
    Geolocator,
    ais_csv_path,
    find_bright_blobs,
    load_dn,
    load_expected,
    load_meta,
    nearest_vessel,
    position_at,
    read_ais,
    scene_time,
    tracks_by_mmsi,
)

EXPECTED_HEADER = (
    "# Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,ROT,SOG,COG,Heading,IMO,Callsign,"
    "Name,Ship type,Cargo type,Width,Length,Type of position fixing device,Draught,Destination,ETA,"
    "Data source type,A,B,C,D"
)


def test_header_is_the_original_dma_layout() -> None:
    assert ais_csv_path().read_text().splitlines()[0] == EXPECTED_HEADER


def test_slice_parses_and_is_inside_region_and_window() -> None:
    rows = read_ais(ais_csv_path())
    assert len(rows) > 5000
    assert all(10.70 <= r.lon <= 11.30 and 55.00 <= r.lat <= 55.70 for r in rows)
    assert min(r.ts for r in rows) >= datetime(2026, 10, 14, 5, 17)
    assert max(r.ts for r in rows) <= datetime(2026, 10, 14, 5, 47)
    assert len({r.mmsi for r in rows}) > 20


def test_scene_acquisition_time_is_inside_the_slice() -> None:
    meta = load_meta()
    rows = read_ais(ais_csv_path())
    assert (
        min(r.ts for r in rows)
        < scene_time(meta, 0)
        < scene_time(meta, meta["height"])
        < max(r.ts for r in rows)
    )


def test_tracks_interpolate() -> None:
    tracks = tracks_by_mmsi(read_ais(ais_csv_path()))
    track = max(tracks.values(), key=len)
    mid = track[len(track) // 2]
    pos = position_at(track, mid.ts)
    assert pos is not None and abs(pos[0] - mid.lat) < 1e-9


def test_detected_ships_match_ais_within_a_few_hundred_metres() -> None:
    """Each expected ship, geolocated from its pixel position and row time, lies near its AIS track.

    Observed on this fixture: 120 m, 118 m and 227 m; the third is the weakest radar return and the fastest
    vessel (13.3 kn) across a 12 s AIS reporting gap.
    """
    meta = load_meta()
    geo = Geolocator(meta)
    tracks = tracks_by_mmsi(read_ais(ais_csv_path()))
    blobs = find_bright_blobs(load_dn())
    for ship in load_expected()["ships"]:
        blob = min(blobs, key=lambda b: (b.row - ship["row"]) ** 2 + (b.col - ship["col"]) ** 2)
        lon, lat = geo(blob.row, blob.col)
        found = nearest_vessel(tracks, lon, lat, scene_time(meta, blob.row))
        assert found is not None
        mmsi, distance = found
        assert mmsi == ship["mmsi"]
        assert distance < 300, f"{mmsi}: {distance:.0f} m"
