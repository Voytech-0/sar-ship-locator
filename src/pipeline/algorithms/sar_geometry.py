"""Where a moving ship appears in a SAR image.

The azimuth position of a target is fixed by its Doppler frequency, so a ship with a radial velocity is
displaced along the platform track by `(R / V) * v_r`: R slant range, V platform speed, v_r the ship's
velocity along the line of sight. With the ship's ground velocity `u_away` along the look direction (a
right-looking sensor looks 90 degrees clockwise from its heading) and incidence angle theta,
`v_r = u_away * sin(theta)`, and a ship moving away from the sensor is displaced backwards along the track.
Sentinel-1 IW: about 60 s times `u_away`, so 5 m/s away gives roughly 300 m.
"""

from __future__ import annotations

import math

from contracts.protocols import SarGeometry


def doppler_displacement_m(geometry: SarGeometry, east_m_s: float, north_m_s: float) -> tuple[float, float]:
    """(east, north) metres by which a ship with this ground velocity appears displaced in the image."""
    heading = math.radians(geometry.heading_deg)
    look = heading + math.pi / 2
    away = east_m_s * math.sin(look) + north_m_s * math.cos(look)
    shift = (
        -(geometry.slant_range_m / geometry.speed_m_s) * math.sin(math.radians(geometry.incidence_deg)) * away
    )
    return shift * math.sin(heading), shift * math.cos(heading)
