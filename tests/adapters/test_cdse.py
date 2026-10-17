from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from contracts.protocols import PermanentError, Region, TransientError
from pipeline.adapters._retry import RetryPolicy
from pipeline.adapters.cdse import CdseCatalogue

REGION = Region("gb", "Great Belt", [(10.7, 55.0), (11.3, 55.0), (11.3, 55.7), (10.7, 55.7), (10.7, 55.0)])
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
NO_WAIT = RetryPolicy(attempts=3, base_delay=0.0, sleep=lambda _s: None)


def product(name: str, start: str) -> dict[str, Any]:
    ring = [[10.5, 53.2], [14.5, 53.2], [15.0, 54.7], [10.9, 55.2], [10.5, 53.2]]
    return {
        "Id": name,
        "Name": f"{name}.SAFE",
        "ContentDate": {"Start": start, "End": start},
        "GeoFootprint": {"type": "Polygon", "coordinates": [ring]},
    }


A = product(
    "S1C_IW_GRDH_1SDV_20261015T052403_20261015T052428_009891_013B8C_448C", "2026-10-15T05:24:03.574410Z"
)
B = product("S1D_IW_GRDH_1SDV_20261014T053204_20261014T053229_005007_009759_0CC6", "2026-10-14T05:32:04.123Z")


def catalogue(handler: Any) -> CdseCatalogue:
    return CdseCatalogue(httpx.Client(transport=httpx.MockTransport(handler)), NO_WAIT)


def test_query_filters_on_product_type_region_and_time() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"value": []})

    assert catalogue(handler).search(REGION, SINCE) == []
    flt = parse_qs(urlparse(str(seen[0].url)).query)["$filter"][0]
    assert "Collection/Name eq 'SENTINEL-1'" in flt
    assert "IW_GRDH_1S" in flt
    assert "POLYGON((10.700000 55.000000,11.300000 55.000000,11.300000 55.700000" in flt
    assert "ContentDate/Start gt 2026-10-01T00:00:00.000Z" in flt
    assert not seen[0].headers.get("authorization")  # public catalogue


def test_pages_are_followed_and_results_parsed_newest_first() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "page2" in str(req.url):
            return httpx.Response(200, json={"value": [B, A]})  # A repeated across pages: de-duplicated
        return httpx.Response(
            200, json={"value": [A], "@odata.nextLink": "https://example.invalid/odata?page2"}
        )

    scenes = catalogue(handler).search(REGION, SINCE)
    assert [s.scene_id for s in scenes] == [
        "S1C_IW_GRDH_1SDV_20261015T052403_20261015T052428_009891_013B8C_448C",
        "S1D_IW_GRDH_1SDV_20261014T053204_20261014T053229_005007_009759_0CC6",
    ]
    assert scenes[0].acquired_at == datetime(2026, 10, 15, 5, 24, 3, 574410, tzinfo=UTC)
    assert scenes[0].footprint[0] == (10.5, 53.2) and scenes[0].footprint[0] == scenes[0].footprint[-1]


def test_rate_limit_and_server_errors_are_retried() -> None:
    codes = iter([429, 503])

    def handler(req: httpx.Request) -> httpx.Response:
        code = next(codes, 200)
        return httpx.Response(code, json={"value": [A]})

    assert len(catalogue(handler).search(REGION, SINCE)) == 1


def test_network_errors_are_retried_then_become_transient() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    with pytest.raises(TransientError, match="failed after 3 attempts"):
        catalogue(handler).search(REGION, SINCE)
    assert calls["n"] == 3


def test_client_errors_are_permanent_and_not_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad filter")

    with pytest.raises(PermanentError, match="HTTP 400"):
        catalogue(handler).search(REGION, SINCE)
    assert calls["n"] == 1


def test_html_gateway_page_is_retried() -> None:
    pages = iter(
        [httpx.Response(200, text="<html>bad gateway</html>"), httpx.Response(200, json={"value": [A]})]
    )
    assert len(catalogue(lambda req: next(pages)).search(REGION, SINCE)) == 1


def test_unparseable_product_is_permanent() -> None:
    bad = {"Name": "x.SAFE", "ContentDate": {"Start": "2026-10-15T00:00:00Z"}}  # no footprint
    with pytest.raises(PermanentError, match="unparseable"):
        catalogue(lambda req: httpx.Response(200, json={"value": [bad]})).search(REGION, SINCE)


def test_endless_paging_is_cut_off() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://example.invalid/next"})

    with pytest.raises(TransientError, match="paging"):
        catalogue(handler).search(REGION, SINCE)
