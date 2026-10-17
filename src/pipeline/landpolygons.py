"""Build the land polygon file that `PolygonLandMask` reads (`PIPELINE_LAND_POLYGONS`).

Source: OpenStreetMap land polygons, pre-processed from the coastline by osmdata.openstreetmap.de
(https://osmdata.openstreetmap.de/data/land-polygons.html, split 4326 variant, ~0.9 GB zip, updated daily).
The data is (c) OpenStreetMap contributors, ODbL: keep the attribution that `write_geojson` adds.

Structures that stand in the water (bridges, wind turbines, offshore platforms) are fetched from OSM through
the Overpass API and added as buffered polygons, so the mask treats them like land. On the Great Belt scenes
the bridge alone caused about a third of the unmatched Detections (see AGENTS.md).

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
from typing import Any

import httpx
import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.geometry.base import BaseGeometry

from contracts.protocols import PermanentError, TransientError
from pipeline.adapters._retry import RetryPolicy
from pipeline.api.config import load_region_config

log = logging.getLogger(__name__)

URL = "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"
ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)"
DEFAULT_CACHE = Path("data/cache/land-polygons")
DEFAULT_MARGIN_DEG = 0.1
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass answers 406 to anonymous clients: identify ourselves.
USER_AGENT = "sar-ship-locator/0.1 (portfolio project; https://github.com/Voytech-0/sar-ship-locator)"
# Radar returns of a structure extend past its footprint (pylons, blades, sidelobes): buffer generously.
BUFFER_M = {"bridge": 80.0, "turbine": 75.0, "platform": 100.0}


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


def _overpass_query(bounds: tuple[float, float, float, float]) -> str:
    w, s, e, n = bounds
    bbox = f"({s},{w},{n},{e})"
    return (
        "[out:json][timeout:120];("
        f'way["bridge"~"yes|viaduct|movable"]["highway"]{bbox};'
        f'way["bridge"~"yes|viaduct|movable"]["railway"]{bbox};'
        f'way["man_made"="bridge"]{bbox};'
        f'node["power"="generator"]["generator:source"="wind"]{bbox};'
        f'way["man_made"="offshore_platform"]{bbox};'
        f'node["man_made"="offshore_platform"]{bbox};'
        ");out geom;"
    )


def _kind(tags: dict[str, str]) -> str:
    if tags.get("man_made") == "offshore_platform":
        return "platform"
    if tags.get("power") == "generator":
        return "turbine"
    return "bridge"


def fetch_structures(
    region_ring: Sequence[Sequence[float]],
    margin_deg: float,
    client: httpx.Client | None = None,
    retry: RetryPolicy | None = None,
) -> list[BaseGeometry]:
    """Bridges, wind turbines and offshore platforms near the Region as buffered polygons (WGS 84)."""
    clip = Polygon(list(region_ring)).buffer(margin_deg)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(180.0), headers={"User-Agent": USER_AGENT})
    retry = retry or RetryPolicy(attempts=6, base_delay=10.0, max_delay=120.0)

    def once() -> dict[str, Any]:
        resp = client.post(OVERPASS_URL, data={"data": _overpass_query(clip.bounds)})
        if resp.status_code in (429, 500, 502, 503, 504):
            raise TransientError(f"Overpass busy: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"Overpass rejected the query: HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if not isinstance(body, dict) or "elements" not in body:
            raise TransientError("Overpass returned an unexpected body")
        return body

    try:
        body = retry.call(
            once, lambda e: isinstance(e, (TransientError, httpx.TransportError)), "Overpass query"
        )
    finally:
        if own:
            client.close()

    kinds: dict[str, list[BaseGeometry]] = {k: [] for k in BUFFER_M}
    for el in body["elements"]:
        kind = _kind(el.get("tags", {}))
        if el["type"] == "node":
            kinds[kind].append(Point(el["lon"], el["lat"]))
        elif len(el.get("geometry", [])) >= 2:
            kinds[kind].append(LineString([(p["lon"], p["lat"]) for p in el["geometry"]]))
    return [g for kind, geoms in kinds.items() for g in _buffer_m(geoms, BUFFER_M[kind], clip)]


def _project(geom: BaseGeometry, fn: Any) -> BaseGeometry:
    """Apply a pyproj (x, y) -> (x, y) transform to every coordinate of `geom`."""
    out: BaseGeometry = shapely.transform(geom, lambda c: np.column_stack(fn(c[:, 0], c[:, 1])))
    return out


def _buffer_m(geoms: list[BaseGeometry], metres: float, clip: Polygon) -> list[BaseGeometry]:
    """Buffer WGS 84 geometries by `metres` (done in the local UTM zone), clipped to `clip`."""
    if not geoms:
        return []
    lon0 = clip.centroid.x
    utm = f"EPSG:{32600 + int((lon0 + 180) // 6) + 1}"
    to_m = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform
    to_deg = Transformer.from_crs(utm, "EPSG:4326", always_xy=True).transform
    merged = shapely.union_all([_project(g, to_m).buffer(metres) for g in geoms])
    back = _project(merged, to_deg).intersection(clip)
    return [g for g in shapely.get_parts(back) if g.geom_type == "Polygon" and not g.is_empty]


def write_geojson(geoms: Iterable[BaseGeometry], path: Path) -> int:
    features = [{"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in geoms]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "attribution": ATTRIBUTION, "features": features})
    )
    return len(features)


def build(
    region_configs: Sequence[Path],
    out: Path,
    zip_path: Path,
    margin_deg: float,
    structures: bool = True,
    client: httpx.Client | None = None,
) -> int:
    """Clip land (and structures in the water) for all Regions into one GeoJSON."""
    geoms: list[BaseGeometry] = []
    for cfg_path in region_configs:
        cfg = load_region_config(cfg_path)
        part = clip_land(zip_path, cfg.polygon, margin_deg)
        log.info("%s: %d land polygons", cfg.region_id, len(part))
        geoms.extend(part)
        if structures:
            built = fetch_structures(cfg.polygon, margin_deg, client=client)
            log.info("%s: %d structure polygons (bridges, turbines, platforms)", cfg.region_id, len(built))
            geoms.extend(built)
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
    ap.add_argument("--no-structures", action="store_true", help="skip bridges, turbines and platforms")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    zip_path = args.zip or download(cache_dir=args.cache_dir)
    n = build(args.region, args.out, zip_path, args.margin_deg, structures=not args.no_structures)
    print(f"wrote {n} land polygons to {args.out}")


if __name__ == "__main__":
    main()
