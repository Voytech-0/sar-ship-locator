# Match AIS in the image plane, not in the world

Status: accepted

The first real run put matched ships a median 131 m (p90 295 m) from their AIS position, growing with speed, so the 500 m match gate hid a large systematic error. A residual fit over 59 matched open-water ships (3 to 9 Scenes, 12 to 15 Oct 2026) split it into three physical effects, each removed in turn:

1. **Doppler displacement.** A SAR image places a moving ship by its Doppler shift, so it is displaced along the platform track by `(R / V) * v_r`, about 60 s times its speed along the look direction (5 m/s away is over 300 m). The azimuth offset correlated with the ship's range velocity at r = -0.87 (slope -60 s) and the offset along the ship's own course against its speed at r = -0.03, which ruled out a timing error. Median 131 to 74 m.
2. **Sea height.** The GRD product is geolocated on the WGS 84 ellipsoid and the sea is about 42 m above it in Denmark, which places every ship `h / tan(incidence)` (about 54 m) closer to the sensor. The range residual was -57 m for every ship in every Scene; the fitted height was 40 m. Median 74 to 52 m.
3. **Antenna position.** AIS reports the antenna, usually aft, the radar return is centred on the hull. The along-ship residual was +43 m and grew with ship length. Moving the AIS reference to the hull centre with the antenna distances A/B/C/D gave a median of 33 m (p90 67 m).

The Validator therefore compares each Detection with where the AIS vessel would appear in the image: its interpolated position, moved to the hull centre, then by the Doppler and elevation displacements from the Scene's imaging geometry.

## Considered Options

- **Per-Detection acquisition time.** The original guess (a +-5 s spread across the window). At most about 40 m for a 15 kn ship and not visible in the data; dropped.
- **A fitted constant for the Doppler term.** Quick, but tuned on three Scenes; the geometry in the product annotation gives the same effect from first principles and per Scene.
- **Correct the Detection positions instead of the AIS tracks.** Every Detection would carry a second position and the stored geolocation would no longer be the product's. Moving the few hundred AIS tracks per Scene is cheaper and leaves the Detections as measured.
- **Keep the 500 m gate and accept the error.** It made 22 tracks ambiguous; after the corrections the gate is 200 m and open-water ambiguous matches fell from 7 to 1.

## Consequences

- `fetch` reads heading, platform speed (orbit state vectors), slant range and incidence from the VV annotation into `scenes.sar_geometry` (`SarGeometry`); `validation.sea_level_height_m` (42 for the Great Belt, the geoid height) is added at that point. A Region elsewhere needs its own geoid height; a geoid model would replace the constant.
- A Scene fetched before the geometry existed validates without the corrections until it is fetched again.
- Hull-centre correction needs the AIS static data (`ais_vessels`: A/B/C/D) and a heading or course; otherwise the antenna is used.
- The effects were fitted on 59 ships. The constants that remain (42 m, the 200 m gate) are starting points, and the remaining 33 m median is mostly ship motion, AIS position error and the 10 m pixel.
- The same displacement applies to fixed structures that are tall (bridge decks, turbines): the structure mask is in true positions. A look at 141 unmatched Detections found only 3 on the sensor side of a mapped structure, so it is not worth correcting yet.
