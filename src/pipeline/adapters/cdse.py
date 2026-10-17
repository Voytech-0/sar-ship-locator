"""CatalogueClient backed by the public CDSE OData catalogue (no authentication needed)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from contracts.protocols import LonLat, PermanentError, Region, SceneRef, TransientError

from ._retry import RetryPolicy

ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
PAGE_SIZE = 100
MAX_PAGES = 50  # safety net against a catalogue that keeps returning nextLink


class _RetryableHttp(Exception):
    pass


def _wkt_polygon(ring: Sequence[LonLat]) -> str:
    return "POLYGON((" + ",".join(f"{lon:.6f} {lat:.6f}" for lon, lat in ring) + "))"


def _parse_time(value: str) -> datetime:
    # CDSE returns fractional seconds of 5-6 digits and a trailing Z.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _footprint(product: dict[str, Any]) -> list[LonLat]:
    geo = product["GeoFootprint"]
    if geo["type"] == "Polygon":
        ring = geo["coordinates"][0]
    elif geo["type"] == "MultiPolygon":
        ring = geo["coordinates"][0][0]
    else:
        raise ValueError(f"unsupported footprint type {geo['type']}")
    return [(float(p[0]), float(p[1])) for p in ring]


class CdseCatalogue:
    """Lists Sentinel-1 IW GRDH products intersecting a Region.

    `SceneRef.acquired_at` is the catalogue's `ContentDate.Start`, the start of the ~25 s acquisition.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        retry: RetryPolicy | None = None,
        url: str = ODATA_URL,
    ) -> None:
        self._client = client or httpx.Client(timeout=60.0)
        self._retry = retry or RetryPolicy()
        self._url = url

    def search(self, region: Region, since: datetime) -> Sequence[SceneRef]:
        since_utc = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        flt = (
            "Collection/Name eq 'SENTINEL-1'"
            " and Attributes/OData.CSC.StringAttribute/any("
            "a:a/Name eq 'productType' and a/Value eq 'IW_GRDH_1S')"
            f" and OData.CSC.Intersects(area=geography'SRID=4326;{_wkt_polygon(region.polygon)}')"
            f" and ContentDate/Start gt {since_utc}"
        )
        params: dict[str, str] | None = {
            "$filter": flt,
            "$orderby": "ContentDate/Start desc",
            "$top": str(PAGE_SIZE),
        }
        next_url: str | None = self._url
        scenes: dict[str, SceneRef] = {}
        pages = 0
        while next_url is not None:
            pages += 1
            if pages > MAX_PAGES:
                raise TransientError(f"catalogue paging did not end after {MAX_PAGES} pages")
            page = self._get_json(next_url, params)
            for product in page.get("value", []):
                ref = self._to_scene_ref(product)
                scenes.setdefault(ref.scene_id, ref)
            next_url = page.get("@odata.nextLink")
            params = None  # nextLink already carries the query
        return sorted(scenes.values(), key=lambda s: s.acquired_at, reverse=True)

    def _get_json(self, url: str, params: dict[str, str] | None) -> dict[str, Any]:
        def once() -> dict[str, Any]:
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                raise _RetryableHttp(str(exc)) from exc
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                raise _RetryableHttp(f"HTTP {resp.status_code}")
            if resp.status_code >= 400:
                raise PermanentError(
                    f"catalogue rejected the request: HTTP {resp.status_code}: {resp.text[:200]}"
                )
            try:
                body = resp.json()
            except ValueError as exc:  # gateway HTML pages etc.
                raise _RetryableHttp("response was not JSON") from exc
            if not isinstance(body, dict):
                raise PermanentError("unexpected catalogue response shape")
            return body

        return self._retry.call(once, lambda e: isinstance(e, _RetryableHttp), "catalogue request")

    @staticmethod
    def _to_scene_ref(product: dict[str, Any]) -> SceneRef:
        try:
            name = str(product["Name"])
            scene_id = name.removesuffix(".SAFE")
            return SceneRef(
                scene_id=scene_id,
                acquired_at=_parse_time(product["ContentDate"]["Start"]),
                footprint=_footprint(product),
            )
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise PermanentError(f"unparseable catalogue product: {exc!r}") from exc
