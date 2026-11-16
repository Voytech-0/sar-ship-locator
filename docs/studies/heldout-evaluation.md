# Held-out evaluation of detector v6 (2026-11-12)

Task C of the work plan. Parameters were frozen: nothing was tuned, and nothing in this file changes them.

**Groups.** "Fit" = the 9 Scenes the parameters were fitted on (12 to 17 Oct; the plan says 12 to 15 Oct, but the
original stack's 9th Scene is 17 Oct 17:08, which has no open-water Detections). "Held-out" = the 36 later Scenes
that are `validated` after the clean ingest of [clean-ingest.md](clean-ingest.md) (18 Oct to 8 Nov). The 4 Scenes of
10 to 12 Nov wait for AIS and are in neither group. Detector v6, open water, one AIS day set for both.
Numbers come from `scripts/heldout_eval.py` (DB) and `scripts/recall_vs_ais.py` on a site export (`make site`
equivalent, all 45 Scenes), with the vessel filter of `AGENTS.md` (image position on valid SAR data, 400 m or
more from mapped land or structures).

## Headline table

| | fit (9 Scenes) | held-out (36 Scenes) |
|---|---|---|
| matched / unmatched / ambiguous | 51 / 6 / 1 | 203 / 15 / 6 |
| unmatched share of Detections | 10 % | 7 % |
| unmatched per Scene, median / max | 0 / 6 (12 Oct evening) | 0 / 5 (5 Nov evening) |
| match distance, median / p90 | 35 m / 68 m | 42 m / 83 m |
| SAR / AIS length, median ratio | 1.01 (n 51) | 0.98 (n 201) |
| SAR / AIS length, median abs error | 21 m | 20 m |
| VH contrast AUC, matched vs unmatched (see note) | 0.65 (51 vs 6) | 0.82 (203 vs 15) |
| `dark_ship_candidates` | 3 | 2 |

Recall against AIS by AIS length class (matched open-water Detection / vessels on valid data, 400 m+ from land):

| AIS length | fit | held-out |
|---|---|---|
| under 30 m | 1 of 10 | 9 of 47 (19 %) |
| 30-60 m | 5 of 9 (56 %) | 21 of 35 (60 %) |
| 60-120 m | 15 of 16 (94 %) | 51 of 62 (82 %) |
| 120 m and longer | 29 of 29 | 122 of 124 (98 %) |
| 30 m and longer | 49 of 54 (91 %) | 194 of 221 (88 %) |

Note on the VH AUC: v6 already drops clusters under 24 dB VH contrast and those are not stored, so this AUC is
measured on the survivors only and is not comparable to the 0.967 of the 59-ship spike (all clusters).

## Reading

Held-out is not worse on false alarms: the unmatched share is 7 % against 10 %, no Scene has more than 5
unmatched, and the length calibration holds (ratio 0.98, 20 m error). What is somewhat worse is geometry and
recall. Match distances are 7 m (median) and 15 m (p90) larger; ambiguous is 6 of 224 against 1 of 58 (2.7 %
against 1.7 %, too few for a conclusion). Recall of ships of 30 m and longer is 88 % held-out against 91 % in the fit
group, driven by the 60-120 m class (82 % against 94 %, 11 missed of 62 against 1 of 16) and by the 30-60 m class
in both groups. These are small-number differences, but the 60-120 m one is the largest. The claim in earlier notes
that every ship of 30 m and longer was detected (8/8, 16/16, 29/29) belongs to detector v3: with the v4 to v6 floors
the fit group itself is at 5/9 and 15/16 in the two smaller classes. That is the price of the stacked floors, and the
held-out set shows it is paid in about the same amount outside the fit.

Which parameter is responsible for the misses cannot be read from the DB: clusters dropped by the 30 m and 0.7 floors
and the 24 dB VH floor are not stored. The 24 dB VH floor and the 30 m length floor are the candidates, because
their classes (30-120 m) are where recall falls.

The held-out matched count is 203, above the 100 the plan asks for, so the comparison of the held-out group is
usable; the fit group has only 51 matched, 6 unmatched and 9 Scenes of which 3 carry nearly everything.

## Dark-ship candidates

| Group | Scene (start) | lon, lat | SAR length | Nearest AIS (image plane) |
|---|---|---|---|---|
| fit | 12 Oct 17:00:44 | 11.0454, 55.1146 | 211 m | 6.1 km |
| fit | 12 Oct 17:00:44 | 10.8752, 55.5154 | 149 m | 1.1 km (150 m ship) |
| fit | 12 Oct 17:00:44 | 10.8839, 55.5447 | 114 m | 2.8 km |
| held-out | 31 Oct 05:40:18 | 10.7975, 55.5999 | 192 m | **112 m** (MSC ARIANE, 366 m) |
| held-out | 5 Nov 17:00:44 | 10.9317, 55.4808 | 130 m | 3.3 km |

The 31 Oct candidate is not dark: the 366 m container ship MSC ARIANE was split into two Detections, an 81 m
piece (matched, 98 m from the hull centre) and this 192 m unmatched piece at the ship's centre, because a track is
claimed by one Detection only. Large ships split into several clusters is a finding for the detector, not a dark ship.
The others have no AIS vessel of similar size nearby; they stay candidates, not proof.

## Scenes with many unmatched Detections

Median unmatched per Scene is 0 in both groups. The Scenes at 4 or more unmatched, with the VV sea median (median
sigma0 over non-land pixels, from the stored rasters):

| Scene (start) | group | unmatched | VV sea median | VH sea median |
|---|---|---|---|---|
| 12 Oct 17:00:44 | fit | 6 | -23.0 dB | -38.2 dB |
| 1 Nov 05:31:51 | held-out | 4 | -22.9 dB | -38.7 dB |
| 5 Nov 17:00:44 | held-out | 5 | -22.1 dB | -38.2 dB |

Not explained here. For context the VV sea median over all 45 validated Scenes runs from -10.3 to -32.8 dB (most
between -12 and -23), and Scenes with comparable values (for example 30 Oct 17:00:15 at -23.4 / -38.8 dB, 7 Nov
05:32:04 at -20.7 / -38.3 dB) have 0 or 1 unmatched.

## Changes proposed for your decision (none applied)

1. Store the clusters that the floors drop, with the reason (length, confidence, VH), so the missed 60-120 m ships
   can be attributed to a parameter. Needs a migration; do this before any retuning.
2. Look at the 11 missed held-out ships of 60-120 m with that data, then decide whether the VH floor (24 dB) or
   `min_confidence` should relax.
3. Merge split clusters of very long ships (the MSC ARIANE case) before validation, or let validation assign two
   Detections to one long track; until then the `dark_ship_candidates` rule should exclude Detections inside an
   AIS vessel's footprint.
4. Re-fit nothing on the held-out set: if parameters change, hold out a fresh later period (from 10 Nov on) for the
   next evaluation.
