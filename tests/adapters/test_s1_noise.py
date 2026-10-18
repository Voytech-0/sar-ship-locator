from __future__ import annotations

import numpy as np
import pytest

from contracts.protocols import PermanentError
from pipeline.adapters.s1_calibration import NOISE_FLOOR, CalibrationLut, NoiseLut

from . import synth


def lut() -> CalibrationLut:
    return CalibrationLut.from_xml(synth.calibration_xml(100.0))


def noise(power: float = 400.0, azimuth: float = 1.0) -> NoiseLut:
    result = NoiseLut.from_xml(synth.noise_xml(power, azimuth))
    assert result is not None
    return result


def test_noise_power_is_the_range_lut_times_the_swath_azimuth_lut() -> None:
    n = noise(400.0, 1.5).window(100, 104, 50, 54)
    assert n.shape == (4, 4) and n.dtype == np.float32
    np.testing.assert_allclose(n, 600.0)


def test_azimuth_lut_applies_only_inside_its_swath() -> None:
    xml = synth.noise_xml(400.0, 2.0).replace(
        f"<lastRangeSample>{synth.WIDTH - 1}</lastRangeSample>".encode(),
        b"<lastRangeSample>99</lastRangeSample>",
    )
    n = NoiseLut.from_xml(xml)
    assert n is not None
    row = n.window(10, 11, 95, 105)[0]
    np.testing.assert_allclose(row[:5], 800.0)  # columns 95..99 are in the swath
    np.testing.assert_allclose(row[5:], 400.0)


def test_azimuth_lut_is_interpolated_along_the_lines() -> None:
    xml = synth.noise_xml(100.0, 1.0).replace(
        b'<noiseAzimuthLut count="2">1.0 1.0</noiseAzimuthLut>',
        b'<noiseAzimuthLut count="2">1.0 3.0</noiseAzimuthLut>',
    )
    n = NoiseLut.from_xml(xml)
    assert n is not None
    top, bottom = n.window(0, 1, 0, 1)[0, 0], n.window(synth.HEIGHT - 1, synth.HEIGHT, 0, 1)[0, 0]
    assert top == pytest.approx(100.0) and bottom == pytest.approx(300.0)


def test_sigma0_subtracts_the_noise_and_floors_pixels_below_it() -> None:
    dn = np.array([[0, 10, 100, 1000]], dtype=np.uint16)
    plain = lut().sigma0(dn, 5, 5)
    clean = lut().sigma0(dn, 5, 5, noise(400.0))
    assert np.isnan(clean[0, 0])
    assert clean[0, 3] == pytest.approx((1000**2 - 400) / 100**2)
    assert clean[0, 2] == pytest.approx((100**2 - 400) / 100**2)
    # DN 10 is 100 DN^2, below the 400 DN^2 noise: not negative, a tenth of the noise instead
    assert clean[0, 1] == pytest.approx(NOISE_FLOOR * 400 / 100**2)
    assert clean[0, 2] < plain[0, 2]


def test_a_file_without_noise_vectors_gives_none() -> None:
    assert NoiseLut.from_xml(b"<noise/>") is None


def test_ipf_before_2_9_has_a_single_noise_lut() -> None:
    vector = "<noiseVector><line>{}</line><pixel>0 499</pixel><noiseLut>250 250</noiseLut></noiseVector>"
    xml = (
        f"<noise><noiseVectorList>{vector.format(0)}{vector.format(1199)}</noiseVectorList></noise>".encode()
    )
    n = NoiseLut.from_xml(xml)
    assert n is not None and n.azimuth == []
    np.testing.assert_allclose(n.window(100, 102, 10, 12), 250.0)


@pytest.mark.parametrize(
    "xml",
    [
        b"<noise><noiseRangeVectorList><noiseRangeVector><line>0</line></noiseRangeVector></noiseRangeVectorList>",
        b'<!DOCTYPE x [<!ENTITY a "b">]><noise/>',
        b"not xml",
    ],
)
def test_bad_noise_xml_is_permanent(xml: bytes) -> None:
    with pytest.raises(PermanentError):
        NoiseLut.from_xml(xml)
