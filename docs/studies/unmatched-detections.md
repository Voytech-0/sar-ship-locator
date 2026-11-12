# Unmatched open-water Detections and dark-ship candidates

Moved from AGENTS.md (2026-11-11), text unchanged.

### Study of the unmatched open-water Detections (2026-10-17)

First real run, 14 Oct scene (CA-CFAR v1, pfa 1e-6, min 3 px): 474 Detections, 94 open water, 12 AIS-matched, 77 unmatched. Looking at the chips of the unmatched ones:

- Most of the strong unmatched Detections were the Great Belt Bridge (long diagonal deck streaks of 150-290 m and pylon dots at lat 55.34, lon 10.94-11.08): 63 of 181 remaining unmatched on the three studied scenes were on the bridge corridor.
- Most weak ones (confidence under 0.5, median length 36 m) were sharp diagonal sea-state edges (the border between wind-sheltered calm water and rougher water) and speckle; they were also clustered (half had 2+ neighbours within 300 m).
- What remains are small bright dots in groups (36-60 m: turbines, buoys or small boats) and a handful of large ship-shaped targets without AIS: the dark-ship candidates.

Offline experiments (3 scenes, 12/13/14 Oct, real land): the edge-contrast idea did not separate matched from unmatched (median 1.34 vs 1.30) and was dropped. What worked: (a) a structures mask: OSM bridges, wind turbines and offshore platforms added to the land file as buffered polygons (80/75/100 m), unmatched 461 to 338 with no matched ship lost; (b) stricter detector settings: v2 = pfa 1e-10 and min 6 px keeps 47 of 50 matched ships and gives unmatched 109 with the structures mask (a 76 percent cut vs 461). Grid in the table of the experiment: pfa 1e-8..1e-12 x min 4..10 px; 1e-10/6 is the chosen knee (3 scenes only, so treat it as a starting point, not a tuned result).

Re-run on a fresh stack with v2 and the structures mask: 14 Oct scene 474 to 157 Detections, 77 to 7 unmatched open-water; the `dark_ship_candidates` view (unmatched, open water, length 100 m or more, confidence 0.9 or more) lists 6 targets, all from the 12 Oct scene, matching the ship-like chips found by eye. A candidate is not proof (small vessels, AIS gaps, clutter remain possible).

Detection recall against AIS (`scripts/recall_vs_ais.py` on the exported site, detector v3, 63 AIS vessels whose image position lies on valid SAR data (alpha of `preview.png`) and 400 m+ from mapped land or structures): every ship of 30 m and longer was detected and matched (30-60 m 8 of 8, 60-120 m 16 of 16, 120 m and longer 29 of 29); under 30 m 5 of 10. Counting ships inside the preview's bounding box or the catalogue footprint had made it look like 8 large ships were missed; those corners and Region edges have no data. Few ships and one week, so this is not a recall estimate for the whole year, and the AIS-less ships are by definition not in it.

Dark-ship candidates after all corrections (detector v3, 9 Scenes): 4, all in the 12 Oct evening Scene (105, 121, 207 and 109 m SAR length). None has an AIS track within 1.1 km in the image plane (nearest 1.1, 3.8, 6.1 and 2.8 km), so no matching tweak explains them; the one at 55.3231 N 11.0001 E has no AIS report at all within 2 km, the others lie in busy lanes. Still candidates, not proof (small vessels, AIS gaps, clutter).
