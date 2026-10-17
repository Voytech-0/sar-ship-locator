"""Stand-in for the external data sources, serving the committed fixtures.

Standard library only, so it runs standalone (`python app.py`) and in a tiny container.

Endpoints
  GET /odata/v1/Products   CDSE OData catalogue in the real response shape ($filter, $top, $orderby accepted;
                           only the date and footprint-intersects parts of $filter are honoured)
  GET /aisdk-YYYY-MM-DD.zip  DMA-style daily AIS archive; days other than the fixture day return 404,
                           as the real bucket does inside its publication lag
  GET /healthz

Ranged GETs of the scene TIFF are NOT served here: seed an S3-compatible store (MinIO in compose, moto in unit
tests) from `tests/fixtures/s3/` instead. See README.md.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = (
    Path(os.environ["FIXTURES_DIR"])
    if "FIXTURES_DIR" in os.environ
    else Path(__file__).resolve().parents[2] / "tests" / "fixtures"
)
META = json.loads((FIXTURES / "scene" / "meta.json").read_text())
AIS_CSV = next((FIXTURES / "ais").glob("*.csv"))
AIS_DAY = re.search(r"(\d{4}-\d{2}-\d{2})", AIS_CSV.name).group(1)  # type: ignore[union-attr]


def ring_to_wkt(ring: list[list[float]]) -> str:
    return "POLYGON ((" + ", ".join(f"{lon} {lat}" for lon, lat in ring) + "))"


def product() -> dict[str, object]:
    """One catalogue record in the shape CDSE returns, built from the fixture Scene."""
    real = META["odata_real_record"]
    ring = META["footprint"]
    return {
        "@odata.mediaContentType": "application/octet-stream",
        "Id": META["product_id"],
        "Name": f"{META['scene_id']}.SAFE",
        "ContentType": "application/octet-stream",
        "ContentLength": real["ContentLength"],
        "OriginDate": real["OriginDate"],
        "PublicationDate": real["PublicationDate"],
        "ModificationDate": real["ModificationDate"],
        "Online": True,
        "EvictionDate": "9999-12-31T23:59:59.999999Z",
        "S3Path": real["S3Path"],
        "Checksum": [],
        "ContentDate": {"Start": META["acquired_at"], "End": META["last_line_at"]},
        "Footprint": f"geography'SRID=4326;{ring_to_wkt(ring)}'",
        "GeoFootprint": {"type": "Polygon", "coordinates": [ring]},
    }


def products_response(query: dict[str, list[str]]) -> dict[str, object]:
    flt = query.get("$filter", [""])[0]
    items = [product()]
    m = re.search(r"ContentDate/Start gt (\S+?)(?:\s|$)", flt)
    if m:
        since = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        acquired = datetime.fromisoformat(META["acquired_at"].replace("Z", "+00:00"))
        items = [p for p in items if acquired > since]
    top = int(query.get("$top", ["1000"])[0])
    return {
        "@odata.context": "$metadata#Products(Attributes())",
        "value": items[:top],
    }


def ais_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(AIS_CSV, arcname=f"aisdk-{AIS_DAY}.csv")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/odata/v1/Products":
            self._send(200, json.dumps(products_response(parse_qs(url.query))).encode(), "application/json")
        elif url.path == f"/aisdk-{AIS_DAY}.zip":
            self._send(200, ais_zip(), "application/zip")
        elif url.path == "/healthz":
            self._send(200, b'{"status": "ok"}', "application/json")
        else:
            self._send(404, b"<Error><Code>NoSuchKey</Code></Error>", "application/xml")

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
