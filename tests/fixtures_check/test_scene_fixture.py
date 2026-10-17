"""Stream B gate: the Scene fixture is a faithful, usable miniature of a Sentinel-1 GRD product."""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET

import numpy as np
import tifffile

from tests.fixtures_check.helpers import (
    Geolocator,
    find_bright_blobs,
    load_dn,
    load_expected,
    load_meta,
    scene_dir,
    tiff_path,
)


def test_tiff_mimics_real_grd_layout() -> None:
    with tifffile.TiffFile(tiff_path()) as tf:
        page = tf.pages[0]
        assert isinstance(page, tifffile.TiffPage)
        assert page.dtype == np.uint16
        assert int(page.compression) == 1  # no compression
        assert page.rowsperstrip == 1
        assert page.shape == (load_meta()["height"], load_meta()["width"])
        assert 33922 in page.tags  # GCP tiepoints
        assert 33550 not in page.tags  # no pixel scale: geolocation is by GCPs only, like the real file
    data = tiff_path().read_bytes()
    assert data[:2] == b"MM"  # big-endian, as the real product
    (ifd_offset,) = struct.unpack(">I", data[4:8])
    assert ifd_offset > len(data) - 4096, "IFD must sit at the end of the file, as in the real product"


def test_rows_are_contiguous_so_row_ranges_can_be_fetched() -> None:
    with tifffile.TiffFile(tiff_path()) as tf:
        page = tf.pages[0]
        assert isinstance(page, tifffile.TiffPage)
        offsets, counts = page.dataoffsets, page.databytecounts
    assert all(o + c == n for o, c, n in zip(offsets, counts, offsets[1:], strict=False))
    assert set(counts) == {load_meta()["width"] * 2}


def test_meta_is_consistent_with_tiff_and_annotation() -> None:
    meta = load_meta()
    dn = load_dn()
    assert dn.shape == (meta["height"], meta["width"])
    ann = ET.parse(scene_dir() / "annotation" / (tiff_path().stem + ".xml")).getroot()
    assert ann.findtext("imageAnnotation/imageInformation/numberOfSamples") == str(meta["width"])
    assert ann.findtext("imageAnnotation/imageInformation/numberOfLines") == str(meta["height"])
    points = ann.findall("geolocationGrid/geolocationGridPointList/geolocationGridPoint")
    assert len(points) == len(meta["gcps"])


def test_calibration_covers_the_whole_raster() -> None:
    meta = load_meta()
    cal = ET.parse(next((scene_dir() / "annotation" / "calibration").glob("calibration-*.xml"))).getroot()
    assert float(cal.findtext("calibrationInformation/absoluteCalibrationConstant") or "nan") > 0
    vectors = cal.findall("calibrationVectorList/calibrationVector")
    lines = [int(v.findtext("line") or "-1") for v in vectors]
    assert lines[0] == 0 and lines[-1] == meta["height"] - 1
    for v in vectors:
        pixels = [int(x) for x in (v.findtext("pixel") or "").split()]
        assert pixels[0] == 0 and pixels[-1] == meta["width"] - 1
        for lut in ("sigmaNought", "betaNought", "gamma", "dn"):
            values = [float(x) for x in (v.findtext(lut) or "").split()]
            assert len(values) == len(pixels) and min(values) > 0


def test_footprint_intersects_the_great_belt_region() -> None:
    meta = load_meta()
    lons = [p[0] for p in meta["footprint"]]
    lats = [p[1] for p in meta["footprint"]]
    assert min(lons) >= 10.70 and max(lons) <= 11.30 and min(lats) >= 55.00 and max(lats) <= 55.70


def test_geolocation_is_inside_footprint() -> None:
    meta = load_meta()
    geo = Geolocator(meta)
    lon, lat = geo(meta["height"] / 2, meta["width"] / 2)
    lons = [p[0] for p in meta["footprint"]]
    lats = [p[1] for p in meta["footprint"]]
    assert min(lons) < lon < max(lons) and min(lats) < lat < max(lats)


def test_image_has_water_land_and_ships() -> None:
    dn = load_dn()
    assert not (dn == 0).any(), "no NoData fill in the crop"
    assert 100 < float(np.median(dn)) < 300  # dark open water dominates
    assert float(dn.max()) > 3000  # bright targets exist


def test_simple_threshold_finds_the_expected_ships_in_open_water() -> None:
    blobs = find_bright_blobs(load_dn())
    expected = load_expected()["ships"]
    assert len(blobs) >= 3
    for ship in expected:
        near = [b for b in blobs if abs(b.row - ship["row"]) < 10 and abs(b.col - ship["col"]) < 10]
        assert near, f"no bright blob near expected ship at {ship['row']},{ship['col']}"
    # The island's bright spot is a deliberate false alarm: a plain threshold finds it (a land mask is needed)
    for fa in load_expected()["known_false_alarms"]:
        assert [b for b in blobs if abs(b.row - fa["row"]) < 10 and abs(b.col - fa["col"]) < 10]
