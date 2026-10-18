"""DN -> linear sigma0 for Sentinel-1 GRD, from the product's calibration annotation XML.

    sigma0 = (|DN|^2 - noise(line, pixel)) / sigmaNought(line, pixel)^2

The sigmaNought LUT is given on a sparse (line, pixel) grid; it is interpolated linearly along pixels, then
along lines. The thermal noise power (annotation/calibration/noise-*.xml, in DN^2) is the product of a
range LUT on a (line, pixel) grid and, for IPF 2.9 and later, a per-swath azimuth LUT. Without the noise the
dark sea is biased high (NESZ about -25 dB, close to the VV sea level and above the VH sea level).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from contracts.protocols import PermanentError

_ROW_BLOCK = 1000
NOISE_FLOOR = 0.1  # pixels at or below the noise level get this share of the noise power


@dataclass(frozen=True)
class CalibrationLut:
    lines: NDArray[np.float64]  # (n,) increasing
    pixels: list[NDArray[np.float64]]  # per vector
    sigma_nought: list[NDArray[np.float64]]  # per vector, same length as the matching pixels

    @classmethod
    def from_xml(cls, xml: bytes) -> CalibrationLut:
        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
            raise PermanentError("calibration XML contains a DTD, refusing to parse")
        try:
            root = ET.fromstring(xml)
            lines: list[float] = []
            pixels: list[NDArray[np.float64]] = []
            sigma: list[NDArray[np.float64]] = []
            for vec in root.iter("calibrationVector"):
                line = vec.findtext("line")
                pix = vec.findtext("pixel")
                sig = vec.findtext("sigmaNought")
                if line is None or pix is None or sig is None:
                    raise ValueError("calibrationVector missing line, pixel or sigmaNought")
                p = np.array(pix.split(), dtype=np.float64)
                s = np.array(sig.split(), dtype=np.float64)
                if p.shape != s.shape or p.size < 2:
                    raise ValueError("pixel and sigmaNought lengths differ")
                lines.append(float(line))
                pixels.append(p)
                sigma.append(s)
        except (ET.ParseError, ValueError) as exc:
            raise PermanentError(f"unparseable calibration XML: {exc}") from exc
        if len(lines) < 2:
            raise PermanentError("calibration XML has fewer than two vectors")
        order = np.argsort(lines)
        return cls(
            np.array(lines)[order],
            [pixels[i] for i in order],
            [sigma[i] for i in order],
        )

    def sigma_nought_window(self, row0: int, row1: int, col0: int, col1: int) -> NDArray[np.float32]:
        """The sigmaNought LUT sampled on every pixel of the window, shape (row1-row0, col1-col0)."""
        cols = np.arange(col0, col1, dtype=np.float64)
        rows = np.arange(row0, row1, dtype=np.float64)
        per_vector = np.stack(
            [np.interp(cols, p, s) for p, s in zip(self.pixels, self.sigma_nought, strict=True)]
        )
        idx = np.clip(np.searchsorted(self.lines, rows, side="right") - 1, 0, len(self.lines) - 2)
        span = self.lines[idx + 1] - self.lines[idx]
        w = np.clip((rows - self.lines[idx]) / span, 0.0, 1.0)[:, None]
        lut = per_vector[idx] * (1.0 - w) + per_vector[idx + 1] * w
        return lut.astype(np.float32)

    def sigma0(
        self, dn: NDArray[np.uint16], row0: int, col0: int, noise: NoiseLut | None = None
    ) -> NDArray[np.float32]:
        """Linear sigma0 of a DN window with top-left pixel (row0, col0); DN == 0 (no data) becomes NaN.

        With `noise` the thermal noise power is subtracted; pixels below the noise level get `NOISE_FLOOR`
        times the noise instead of a negative or zero value.
        """
        out = np.empty(dn.shape, dtype=np.float32)
        for start in range(0, dn.shape[0], _ROW_BLOCK):
            stop = min(start + _ROW_BLOCK, dn.shape[0])
            block = dn[start:stop]
            lut = self.sigma_nought_window(row0 + start, row0 + stop, col0, col0 + dn.shape[1])
            amp = block.astype(np.float32)
            power = amp * amp
            if noise is not None:
                n = noise.window(row0 + start, row0 + stop, col0, col0 + dn.shape[1])
                power = np.maximum(power - n, NOISE_FLOOR * n)
            out[start:stop] = np.where(block == 0, np.nan, power / (lut * lut))
        return out


def _parse_vectors(
    root: ET.Element, tag: str, lut_tag: str
) -> tuple[NDArray[np.float64], list[NDArray[np.float64]], list[NDArray[np.float64]]]:
    lines: list[float] = []
    pixels: list[NDArray[np.float64]] = []
    luts: list[NDArray[np.float64]] = []
    for vec in root.iter(tag):
        line, pix, lut = vec.findtext("line"), vec.findtext("pixel"), vec.findtext(lut_tag)
        if line is None or pix is None or lut is None:
            raise ValueError(f"{tag} missing line, pixel or {lut_tag}")
        p, v = np.array(pix.split(), dtype=np.float64), np.array(lut.split(), dtype=np.float64)
        if p.shape != v.shape or p.size < 2:
            raise ValueError("pixel and noise LUT lengths differ")
        lines.append(float(line))
        pixels.append(p)
        luts.append(v)
    order = np.argsort(lines)
    return np.array(lines)[order], [pixels[i] for i in order], [luts[i] for i in order]


@dataclass(frozen=True)
class _AzimuthSwath:
    first_line: int
    last_line: int
    first_pixel: int
    last_pixel: int
    lines: NDArray[np.float64]
    lut: NDArray[np.float64]


@dataclass(frozen=True)
class NoiseLut:
    """Thermal noise power in DN^2: a range LUT times (IPF 2.9 and later) a per-swath azimuth LUT."""

    lines: NDArray[np.float64]
    pixels: list[NDArray[np.float64]]
    range_lut: list[NDArray[np.float64]]
    azimuth: list[_AzimuthSwath]

    @classmethod
    def from_xml(cls, xml: bytes) -> NoiseLut | None:
        """None when the file holds no noise vectors; PermanentError when it cannot be parsed."""
        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
            raise PermanentError("noise XML contains a DTD, refusing to parse")
        try:
            root = ET.fromstring(xml)
            if root.find(".//noiseRangeVector") is not None:
                lines, pixels, luts = _parse_vectors(root, "noiseRangeVector", "noiseRangeLut")
            else:  # IPF before 2.9: one noise LUT, no azimuth part
                lines, pixels, luts = _parse_vectors(root, "noiseVector", "noiseLut")
            swaths = []
            for vec in root.iter("noiseAzimuthVector"):
                text = vec.findtext
                swaths.append(
                    _AzimuthSwath(
                        int(text("firstAzimuthLine") or 0),
                        int(text("lastAzimuthLine") or 0),
                        int(text("firstRangeSample") or 0),
                        int(text("lastRangeSample") or 0),
                        np.array((text("line") or "").split(), dtype=np.float64),
                        np.array((text("noiseAzimuthLut") or "").split(), dtype=np.float64),
                    )
                )
        except (ET.ParseError, ValueError) as exc:
            raise PermanentError(f"unparseable noise XML: {exc}") from exc
        if len(lines) < 2:
            return None
        return cls(lines, pixels, luts, [s for s in swaths if s.lines.size and s.lines.size == s.lut.size])

    def window(self, row0: int, row1: int, col0: int, col1: int) -> NDArray[np.float32]:
        """Noise power (DN^2) on every pixel of the window."""
        cols = np.arange(col0, col1, dtype=np.float64)
        rows = np.arange(row0, row1, dtype=np.float64)
        per_vector = np.stack(
            [np.interp(cols, p, v) for p, v in zip(self.pixels, self.range_lut, strict=True)]
        )
        idx = np.clip(np.searchsorted(self.lines, rows, side="right") - 1, 0, len(self.lines) - 2)
        w = np.clip((rows - self.lines[idx]) / (self.lines[idx + 1] - self.lines[idx]), 0.0, 1.0)[:, None]
        noise = per_vector[idx] * (1.0 - w) + per_vector[idx + 1] * w
        for sw in self.azimuth:
            r_in = (rows >= sw.first_line) & (rows <= sw.last_line)
            c_in = (cols >= sw.first_pixel) & (cols <= sw.last_pixel)
            if r_in.any() and c_in.any():
                factor = np.interp(rows[r_in], sw.lines, sw.lut)[:, None]
                noise[np.ix_(r_in, c_in)] *= factor
        return noise.astype(np.float32)
