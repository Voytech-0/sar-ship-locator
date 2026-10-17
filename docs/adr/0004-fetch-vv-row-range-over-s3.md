# Fetch only the VV rows covering the Region, over the CDSE S3 endpoint

Status: accepted

A spike (2026-10-17, one S1D IW GRDH scene over the Great Belt) found that the VV measurement TIFF is its own S3 object (883 MB of a 1.8 GB product), uncompressed, one row per strip, uint16, with no overviews and only GCP geolocation. So rows are contiguous bytes and the Region can be fetched with ranged GETs. `SceneFetcher` reads the contiguous row range covering the Region in a few large ranged requests (about 400 MB for the Great Belt, ~20-30 s at the ~20 MB/s measured) and crops columns in memory. It never downloads the whole product, and needs only S3 keys, not the account login.

## Considered Options

- **GDAL `/vsis3/` windowed read of the exact Region crop** (77 MB): smallest transfer, but GDAL issues one request per row. Sequential it ran at 8 rows/s (about 47 min for the Region); with 16 threads it reached 300 rows/s once, then failed with a truncated-strip read error.
- **Download the whole VV band** (883 MB, ~45 s): simplest and robust, kept as the fallback if the layout assumption breaks.
- **openEO server-side crop**: rejected, selects by time and area rather than product id and returns processed data.

## Consequences

- Relies on the uncompressed, row-striped layout of Sentinel-1 GRD; the fetcher must check it from the TIFF header and fall back to the full band when it doesn't hold. Strip offsets come from the header.
- Bandwidth, not request count, is the limit (8 parallel streams were no faster than one), so concurrency stays low.
- The Region's pixel window is found by interpolating GCPs. A global 2nd-order polynomial fit was off by up to 14 px (140 m): fine for choosing a window with margin, not for final Detection geolocation, which needs per-row GCP interpolation.
- A Scene can cover only part of the Region (the test scene ended 15% short): crop to the intersection.
- Calibration (DN to sigma0) from `annotation/calibration/*.xml` is still unverified.
