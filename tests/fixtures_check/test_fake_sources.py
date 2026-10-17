"""Stream B gate: fake-sources serves the documented response shapes, standalone and in Docker."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

from tests.fixtures_check.helpers import FIXTURES, load_meta

APP = Path(__file__).resolve().parents[2] / "deploy" / "fake-sources" / "app.py"


def _load_app() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_sources_app", APP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    app = _load_app()
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_catalogue_has_the_real_cdse_shape(base_url: str) -> None:
    flt = quote("Collection/Name eq 'SENTINEL-1' and ContentDate/Start gt 2026-10-01T00:00:00.000Z")
    status, body = _get(f"{base_url}/odata/v1/Products?$filter={flt}&$top=5")
    assert status == 200
    doc = json.loads(body)
    assert set(doc) == {"@odata.context", "value"}
    (product,) = doc["value"]
    real = json.loads((FIXTURES / "scene" / "meta.json").read_text())["odata_real_record"]
    assert set(real) <= set(product)  # every field we recorded from the real record is present
    assert product["Name"] == f"{load_meta()['scene_id']}.SAFE"
    assert product["S3Path"].startswith("/eodata/Sentinel-1/SAR/IW_GRDH_1S/")
    assert product["GeoFootprint"]["type"] == "Polygon"
    assert product["Footprint"].startswith("geography'SRID=4326;POLYGON ((")
    assert product["ContentDate"]["Start"].endswith("Z")


def test_catalogue_honours_since_filter(base_url: str) -> None:
    flt = quote("ContentDate/Start gt 2026-10-15T00:00:00.000Z")
    _, body = _get(f"{base_url}/odata/v1/Products?$filter={flt}")
    assert json.loads(body)["value"] == []


def test_ais_archive_for_the_fixture_day(base_url: str) -> None:
    status, body = _get(f"{base_url}/aisdk-2026-10-14.zip")
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        (name,) = z.namelist()
        assert name == "aisdk-2026-10-14.csv"
        header = z.read(name).splitlines()[0].decode()
    assert header.startswith("# Timestamp,Type of mobile,MMSI,Latitude,Longitude")


def test_ais_archive_for_other_days_is_not_published(base_url: str) -> None:
    status, _ = _get(f"{base_url}/aisdk-2026-10-16.zip")
    assert status == 404


def test_healthz(base_url: str) -> None:
    status, body = _get(f"{base_url}/healthz")
    assert status == 200 and json.loads(body) == {"status": "ok"}


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="Docker is not available")
def test_container_builds_and_serves() -> None:
    root = Path(__file__).resolve().parents[2]
    tag = "sar-fake-sources-test"
    subprocess.run(
        ["docker", "build", "-q", "-f", "deploy/fake-sources/Dockerfile", "-t", tag, "."],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=600,
    )
    cid = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "127.0.0.1::8080", tag],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        port = (
            subprocess.run(["docker", "port", cid, "8080/tcp"], check=True, capture_output=True, text=True)
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        last: Exception | None = None
        for _ in range(30):
            try:
                status, body = _get(f"http://127.0.0.1:{port}/healthz")
                assert status == 200 and json.loads(body) == {"status": "ok"}
                return
            except (urllib.error.URLError, ConnectionError) as e:
                last = e
                subprocess.run(["sleep", "1"])
        raise AssertionError(f"container never became healthy: {last}")
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
