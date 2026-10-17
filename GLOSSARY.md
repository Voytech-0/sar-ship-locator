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
