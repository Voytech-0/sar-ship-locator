# SAR Ship Locator

Monitors ship traffic in a chosen sea corridor by detecting vessels in satellite radar imagery and checking them against broadcast position reports.

## Language

**Region**:
A configured geographic polygon being monitored.
_Avoid_: AOI, area, zone

**Scene**:
One satellite radar acquisition covering a Region, cropped to that Region.
_Avoid_: Product, image, tile

**Detection**:
A candidate ship found in a Scene, with a geographic position and attributes.
_Avoid_: Target, hit, vessel, ship

**VH contrast**:
Peak VH backscatter in a Detection's pixel box over the median VH of the sea ring around it, in dB. A ship stands out in VH; sea-state edges and speckle do not (ADR-0007).
_Avoid_: VH score

**Chip**:
A small image cutout around a Detection, kept for review and later model training.
_Avoid_: Patch, thumbnail, crop

**AIS Position**:
One recorded position report broadcast by a vessel.
_Avoid_: Ping, AIS message

**Validation**:
Comparing a Detection with AIS Positions interpolated to the Scene's acquisition time.
_Avoid_: Verification, correlation

**Matched**:
A Detection that has an AIS counterpart, so is a known transmitting vessel.
_Avoid_: Legal ship, identified

**Unmatched**:
A Detection with no AIS counterpart. A dark-ship candidate only; not proof of one.
_Avoid_: Dark ship, suspicious

**Image position**:
Where a ship appears in the SAR image, not where it is. A moving ship is displaced along the satellite track by its Doppler shift, and every ship at sea is displaced toward the sensor by its height above the ellipsoid. Validation compares a Detection with the AIS position moved to its image position.
_Avoid_: Corrected position, true position (the AIS position is the true one)

**Imaging geometry**:
Platform heading, speed, slant range and incidence angle of a Scene's Region window, read from the product annotation (`SarGeometry`). It is what turns an AIS position into an image position.
_Avoid_: Orbit data, metadata
