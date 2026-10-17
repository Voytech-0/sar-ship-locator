# Test fixtures

Small, real data cut from one Sentinel-1 scene and one day of Danish AIS. Total size is about 7 MB. See `NOTICE` for attribution.

## Scene: a miniature Sentinel-1 GRD product

Real product `S1D_IW_GRDH_1SDV_20261014T053204_20261014T053229_005007_009759_0CC6`, VV band, a 1600 x 1550 px crop (origin row 13450, col 11700 of the 26475 x 16665 original) south-west of the Great Belt Bridge. It contains open water, Zealand's coast, an island and three ships.

It is a **self-contained mini-scene**: the TIFF, the annotation and the calibration are all in the crop's own pixel coordinates (row 0, col 0 is the crop's corner), so a fetcher or Detector treats it as a whole scene. `scene/meta.json` records where the crop came from.

The tree under `s3/` mirrors the CDSE bucket layout, so seeding an S3 store from it gives realistic keys:

```
s3/eodata/Sentinel-1/SAR/IW_GRDH_1S/2026/10/14/<scene>.SAFE/
    measurement/s1d-iw-grd-vv-...-001.tiff
    annotation/s1d-iw-grd-vv-...-001.xml
    annotation/calibration/calibration-s1d-iw-grd-vv-...-001.xml
```

Faithfulness to the real product, checked by `tests/fixtures_check`:

- uint16 DN, little-endian (`II`, as the real file), uncompressed, **one row per strip, strips contiguous**
- **IFD at the end of the file** (as in the real file; a header-only read does not find it, ADR-0004 fetchers must read the tail)
- no CRS or pixel scale; geolocation is only the 20 GCPs in `ModelTiepointTag` (`I, J, 0, lon, lat, 0`)
- the GCPs were bilinearly resampled from the real 10 x 21 lattice onto the crop, so positions agree with the real product up to the interpolation error
- calibration LUTs (sigma0, beta0, gamma, dn) were linearly resampled from the real vectors onto the crop grid (3 vectors, every 40th pixel)
- annotation is a trimmed copy: only `adsHeader`, `productInformation`, `imageInformation` (slice list removed) and `geolocationGrid` remain. **Noise annotation is not included**, so thermal noise removal is not exercised.

`scene/meta.json`: scene id, `acquired_at` (time of the crop's first line), `azimuth_time_interval_s` (seconds per row, so a pixel's time is `acquired_at + row * interval`), footprint ring, GCPs, S3 keys, and the real OData record the fake catalogue mimics. Catalogue footprint and times describe the **crop**, not the full real product.

`scene/expected_ships.json`: ground truth. Three open-water ships, each matched to a real AIS vessel (120 m, 118 m and 227 m from the blob; labels are weak), plus one known false alarm on the island that a plain threshold picks up and only a land mask removes. `scene/preview.png` is a 4x downsampled preview.

## AIS: `ais/aisdk-2026-10-14.great-belt-0517-0547.csv`

Rows from the Danish Maritime Authority daily file `aisdk-2026-10-14` for lon 10.82..11.30, lat 55.02..55.50, 05:17..05:47 UTC (10,676 rows, 48 vessels), original columns, original order, no rows altered. The scene was acquired at about 05:32:25 UTC.

Real header (other streams parse this; note the leading `# `):

```
# Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,ROT,SOG,COG,Heading,IMO,Callsign,Name,Ship type,Cargo type,Width,Length,Type of position fixing device,Draught,Destination,ETA,Data source type,A,B,C,D
```

Format notes: `Timestamp` is `dd/mm/YYYY HH:MM:SS` in UTC; `Latitude`/`Longitude` are decimal degrees; many fields are empty (class B transponders, base stations, aids to navigation), so parsers must tolerate blank `SOG`, `Length` and others. Rows without a usable position exist in the full file and should be skipped.

## fake-sources and the S3 side

`deploy/fake-sources` serves the OData catalogue and the DMA zip (see its README). Ranged GETs of the TIFF are served by an S3-compatible store seeded from `s3/`:

- unit tests: `moto` (`mock_aws`), `create_bucket("eodata")`, upload every file under `s3/eodata/` with its relative path as the key
- compose: MinIO with a one-shot `mc mirror tests/fixtures/s3/ local/` job creates and fills bucket `eodata`

## Regenerating

The fixtures were cut with throwaway scripts (S3 row-range read per ADR-0004, then resampling). They are not part of the repo: regenerate by repeating that recipe against the same scene and day if ever needed.
