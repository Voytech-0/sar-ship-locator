"""Build the land polygon file that `PolygonLandMask` reads (`PIPELINE_LAND_POLYGONS`).

Source: OpenStreetMap land polygons, pre-processed from the coastline by osmdata.openstreetmap.de
(https://osmdata.openstreetmap.de/data/land-polygons.html, split 4326 variant, ~0.9 GB zip, updated daily).
The data is (c) OpenStreetMap contributors, ODbL: keep the attribution that `write_geojson` adds.

The zip is downloaded once and cached. Land is clipped to each Region plus a margin, and the tile seams of the
"split" polygons are dissolved so the near-shore band is not cut by an artificial coastline.

    uv run --extra land python -m pipeline.landpolygons --region contracts/region.greatbelt.yaml \
        --out data/land/great-belt.geojson
"""

from __future__ import annotations

import argparse
import json
import logging
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

import httpx
import shapely
from shapely.geometry import Polygon, mapping
from shapely.geometry.base import BaseGeometry

from pipeline.api.config import load_region_config

log = logging.getLogger(__name__)

URL = "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"
ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)"
DEFAULT_CACHE = Path("data/cache/land-polygons")
DEFAULT_MARGIN_DEG = 0.1


def download(url: str = URL, cache_dir: Path = DEFAULT_CACHE) -> Path:
    """Fetch the zip into `cache_dir` unless a complete copy is already there; returns its path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / url.rsplit("/", 1)[-1]
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)) as client:
        size = int(client.head(url).headers["content-length"])
        if dest.exists() and dest.stat().st_size == size:
            log.info("using cached %s", dest)
            return dest
        part = dest.with_suffix(".part")
        log.info("downloading %s (%.0f MB)", url, size / 1e6)
        with client.stream("GET", url) as resp, part.open("wb") as fh:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
        if part.stat().st_size != size:
            raise OSError(f"incomplete download: {part.stat().st_size} of {size} bytes")
        part.replace(dest)
    return dest


def _shapefile_path(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        shp = [n for n in zf.namelist() if n.endswith("land_polygons.shp")]
    if len(shp) != 1:
        raise ValueError(f"{zip_path}: expected one land_polygons.shp, found {shp}")
    return f"/vsizip/{zip_path}/{shp[0]}"


def clip_land(
    zip_path: Path, region_ring: Sequence[Sequence[float]], margin_deg: float
) -> list[BaseGeometry]:
    """Land polygons inside the Region plus `margin_deg`, as clean polygons with tile seams dissolved."""
    from pyogrio.raw import read  # type: ignore[import-untyped]  # optional extra `land`

    clip = Polygon(list(region_ring)).buffer(margin_deg)
    _, _, wkb, _ = read(_shapefile_path(zip_path), bbox=tuple(clip.bounds))
    land = [g for g in shapely.from_wkb(wkb) if g is not None and g.intersects(clip)]
    if not land:
        return []
    merged = shapely.union_all(land).intersection(clip)
    return [g for g in shapely.get_parts(merged) if g.geom_type == "Polygon" and not g.is_empty]


def write_geojson(geoms: Iterable[BaseGeometry], path: Path) -> int:
    features = [{"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in geoms]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "attribution": ATTRIBUTION, "features": features})
    )
    return len(features)


def build(region_configs: Sequence[Path], out: Path, zip_path: Path, margin_deg: float) -> int:
    """Clip land for all Regions into one GeoJSON (the union covers every Region's window)."""
    geoms: list[BaseGeometry] = []
    for cfg_path in region_configs:
        cfg = load_region_config(cfg_path)
        part = clip_land(zip_path, cfg.polygon, margin_deg)
        log.info("%s: %d land polygons", cfg.region_id, len(part))
        geoms.extend(part)
    return write_geojson(geoms, out)


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--region", type=Path, action="append", required=True, help="region config YAML (repeatable)"
    )
    ap.add_argument("--out", type=Path, required=True, help="GeoJSON to write")
    ap.add_argument("--zip", type=Path, help="use this land-polygons zip instead of downloading")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--margin-deg", type=float, default=DEFAULT_MARGIN_DEG)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    zip_path = args.zip or download(cache_dir=args.cache_dir)
    n = build(args.region, args.out, zip_path, args.margin_deg)
    print(f"wrote {n} land polygons to {args.out}")


if __name__ == "__main__":
    main()
