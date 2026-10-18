# Confirm VV detections with the VH band

Status: accepted (2026-10-18; amends ADR-0004, which fetched VV only)

The false alarms that remain after land masking and detector v4 are short, weak clusters, mostly sharp edges between calm and rough water and speckle. Sea-state edges are bright in VV and almost invisible in VH, where the sea is 6 to 7 dB darker and a ship keeps its radar cross-section.

A spike on all nine real Scenes (VH fetched with the same row-range reader, 20 s and about 440 MB per Scene) compared the VV CFAR clusters of detector v3 with the AIS match label (59 matched, 136 unmatched open-water clusters):

| Feature | AUC, matched vs unmatched |
|---|---|
| VH contrast (peak VH in the box over the median of a 20-50 px sea ring, dB) | 0.97 |
| VV contrast, same definition | 0.92 |
| CFAR confidence | 0.92 |
| SAR length | 0.89 |

A VH contrast floor of 12 dB alone keeps 58 of 59 matched and 23 of 136 unmatched (v4's length and confidence floors: 53 and 23). On top of v4 it leaves 52 matched and 7 unmatched.

## Decision

`fetch` reads VH over the same window as VV (`SceneRaster.sigma0_vh`, stored as `sigma0_vh.npy`). CFAR still runs on VV only. Each cluster gets `vh_contrast_db` (stored on the Detection) and clusters under `min_vh_contrast_db` are dropped (detector v5, 12 dB). A product without a VH band gives VV-only Detections with a null contrast; an unreadable VH band fails `fetch` permanently.

## Considered Options

- **CFAR on VH too, then OR or AND of the hits**: more recall in theory; the recall check against AIS is already 100 percent for ships of 30 m and longer, and VH is close to the noise floor without thermal noise subtraction, so no gain was measurable. Revisit if recall becomes the problem.
- **Sum VV and VH intensities before CFAR**: changes the speckle statistics (ENL) the threshold depends on and mixes the two backgrounds.

## Consequences

- Fetch downloads about twice the bytes (VV and VH rows) and stores a second raster.
- Contract changes, all additive: `SceneRaster.sigma0_vh`, `DetectionCandidate.vh_contrast_db`, `detections.vh_contrast_db` (migration 0008), the OpenAPI `Detection.vh_contrast_db`, one storage key, and the CFAR parameter `min_vh_contrast_db`.
- The 12 dB floor and the ring geometry were fitted on 59 matched ships; treat them as a starting point. A matched 37 m ship had 8 dB.
- The gate can hide a real ship with a weak cross-polar return: the dark-ship candidate at 55.3231 N 11.0001 E (105 m, strong in VV, 7 dB in VH) is a v4 Detection and not a v5 one. Compare versions before reading anything into a missing candidate.
- VH is not noise-subtracted, so the contrast of very weak targets is optimistic or pessimistic depending on the noise floor of the sub-swath.
- Scenes fetched before this change have no VH raster; they get VH when fetched again (allowed once the raster is gone).
