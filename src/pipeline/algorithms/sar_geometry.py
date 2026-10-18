"""Where a ship appears in a SAR image.

The azimuth position of a target is fixed by its Doppler frequency, so a ship with a radial velocity is
displaced along the platform track by `(R / V) * v_r`: R slant range, V platform speed, v_r the ship's
velocity along the line of sight. With the ship's ground velocity `u_away` along the look direction (a
right-looking sensor looks 90 degrees clockwise from its heading) and incidence angle theta,
`v_r = u_away * sin(theta)`, and a ship moving away from the sensor is displaced backwards along the track.
Sentinel-1 IW: about 60 s times `u_away`, so 5 m/s away gives roughly 300 m.

A ground-range product is geolocated on the ellipsoid, while the sea surface is `target_height_m` above it
(about 42 m in Denmark). Such a target is placed `target_height_m / tan(theta)` closer to the sensor than its
true position (about 54 m at 38 degrees). On 58 matched ships this was the largest residual, a median of
-57 m along the look direction regardless of speed, length or Scene.
"""

from __future__ import annotations

import math

from contracts.protocols import SarGeometry


def image_displacement_m(geometry: SarGeometry, east_m_s: float, north_m_s: float) -> tuple[float, float]:
    """(east, north) metres by which a ship with this ground velocity appears displaced in the image."""
    de, dn = doppler_displacement_m(geometry, east_m_s, north_m_s)
    look = math.radians(geometry.heading_deg) + math.pi / 2
    toward = geometry.target_height_m / math.tan(math.radians(geometry.incidence_deg))
    return de - toward * math.sin(look), dn - toward * math.cos(look)


def doppler_displacement_m(geometry: SarGeometry, east_m_s: float, north_m_s: float) -> tuple[float, float]:
    """(east, north) metres by which a ship with this ground velocity appears displaced in the image."""
    heading = math.radians(geometry.heading_deg)
    look = heading + math.pi / 2
    away = east_m_s * math.sin(look) + north_m_s * math.cos(look)
    shift = (
        -(geometry.slant_range_m / geometry.speed_m_s) * math.sin(math.radians(geometry.incidence_deg)) * away
    )
    return shift * math.sin(heading), shift * math.cos(heading)
