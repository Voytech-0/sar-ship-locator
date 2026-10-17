from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Polygon

from contracts.protocols import PermanentError
from pipeline.adapters.s1_calibration import CalibrationLut
from pipeline.adapters.s1_geo import GcpGrid

from . import synth


def lut_xml(vectors: list[tuple[int, list[int], list[float]]]) -> bytes:
    body = "".join(
        f'<calibrationVector><line>{line}</line><pixel count="{len(p)}">{" ".join(map(str, p))}</pixel>'
        f'<sigmaNought count="{len(s)}">{" ".join(map(str, s))}</sigmaNought></calibrationVector>'
        for line, p, s in vectors
    )
    return f"<calibration><calibrationVectorList>{body}</calibrationVectorList></calibration>".encode()


def test_lut_interpolates_along_pixels_then_lines() -> None:
    lut = CalibrationLut.from_xml(lut_xml([(0, [0, 100], [10.0, 20.0]), (10, [0, 100], [30.0, 40.0])]))
    got = lut.sigma_nought_window(0, 11, 0, 101)
    assert got[0, 0] == pytest.approx(10.0) and got[0, 100] == pytest.approx(20.0)
    assert got[10, 0] == pytest.approx(30.0) and got[10, 100] == pytest.approx(40.0)
    assert got[5, 50] == pytest.approx(25.0)  # centre of the cell


def test_lut_holds_edge_values_outside_the_vectors() -> None:
    lut = CalibrationLut.from_xml(lut_xml([(10, [0, 100], [10.0, 20.0]), (20, [0, 100], [10.0, 20.0])]))
    got = lut.sigma_nought_window(0, 40, 0, 101)
    assert got[0, 0] == pytest.approx(10.0) and got[39, 100] == pytest.approx(20.0)


def test_sigma0_is_dn_squared_over_lut_squared_with_nodata_nan() -> None:
    lut = CalibrationLut.from_xml(lut_xml([(0, [0, 9], [50.0, 50.0]), (9, [0, 9], [50.0, 50.0])]))
    dn = np.full((10, 10), 100, dtype=np.uint16)
    dn[2, 3] = 0
    out = lut.sigma0(dn, 0, 0)
    assert out[0, 0] == pytest.approx(4.0)
    assert np.isnan(out[2, 3])
    assert np.isfinite(out).sum() == 99


def test_sigma0_window_offsets_pick_the_right_lut_cells() -> None:
    lut = CalibrationLut.from_xml(lut_xml([(0, [0, 100], [10.0, 110.0]), (100, [0, 100], [10.0, 110.0])]))
    dn = np.full((2, 2), 10, dtype=np.uint16)
    out = lut.sigma0(dn, 50, 40)  # columns 40,41 -> lut 50,51
    assert out[0, 0] == pytest.approx(100 / 50**2)
    assert out[0, 1] == pytest.approx(100 / 51**2)


def test_unsorted_vectors_are_sorted() -> None:
    lut = CalibrationLut.from_xml(lut_xml([(10, [0, 5], [2.0, 2.0]), (0, [0, 5], [1.0, 1.0])]))
    assert lut.lines.tolist() == [0.0, 10.0]
    assert lut.sigma_nought_window(0, 1, 0, 1)[0, 0] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "xml",
    [
        b"<calibration/>",
        b"not xml",
        lut_xml([(0, [0, 1], [1.0, 1.0])]),  # one vector only
        b"<!DOCTYPE x [<!ENTITY a 'b'>]><calibration/>",
        lut_xml([(0, [0, 1], [1.0]), (1, [0, 1], [1.0, 1.0])]),  # length mismatch
    ],
)
def test_bad_calibration_xml_is_permanent(xml: bytes) -> None:
    with pytest.raises(PermanentError):
        CalibrationLut.from_xml(xml)


def grid(mapping: synth.Mapping = synth.linear_mapping) -> GcpGrid:
    return GcpGrid(np.array(synth.tiepoints(mapping)).reshape(-1, 6))


def test_gcp_interpolation_reproduces_a_bilinear_mapping() -> None:
    g = grid(synth.curved_mapping)
    lon, lat = g.lonlat(np.array([123.4]), np.array([321.0]))
    exp_lon, exp_lat = synth.curved_mapping(321.0, 123.4)
    assert lon[0] == pytest.approx(exp_lon, abs=1e-9) and lat[0] == pytest.approx(exp_lat, abs=1e-9)


def test_irregular_gcp_set_is_rejected() -> None:
    pts = np.array(synth.tiepoints()).reshape(-1, 6)[:-1]
    with pytest.raises(PermanentError, match="regular grid"):
        GcpGrid(pts)


def test_region_window_covers_the_polygon_and_is_clipped() -> None:
    g = grid()
    poly = Polygon(synth.region_ring())
    r0, r1, c0, c1 = g.region_window(poly, synth.HEIGHT, synth.WIDTH) or (0, 0, 0, 0)
    assert r0 <= 400 and r1 >= 800 and c0 <= 100 and c1 >= 400
    assert r0 >= 0 and c1 <= synth.WIDTH
    whole = Polygon(synth.footprint())
    full = g.region_window(whole, synth.HEIGHT, synth.WIDTH)
    assert (
        full is not None
        and full[0] == 0
        and full[2] == 0
        and full[1] == synth.HEIGHT
        and full[3] == synth.WIDTH
    )


def test_region_mask_matches_the_polygon() -> None:
    g = grid()
    poly = Polygon(synth.region_ring())
    window = (350, 850, 50, 450)
    mask = g.region_mask(poly, window)
    assert mask.shape == (500, 400)
    assert mask[450 - 350, 200 - 50] and not mask[10, 10]
    inside = mask[(400 - 350) + 8 : (800 - 350) - 8, (100 - 50) + 8 : (400 - 50) - 8]
    assert inside.all()  # away from the edge, within the 4 px mask step


def test_fit_affine_is_exact_for_linear_and_reports_error_for_curved() -> None:
    window = (0, synth.HEIGHT, 0, synth.WIDTH)
    assert grid().fit_affine(window).max_error_px < 0.01
    fit = grid(synth.curved_mapping).fit_affine(window)
    assert 2 < fit.max_error_px < 50
    assert fit.mean_error_px < fit.max_error_px
