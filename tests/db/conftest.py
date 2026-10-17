"""Database gate fixtures: one PostGIS container per session, one cloned database per test.

Builds deploy/postgis/Dockerfile, so the gate exercises the same image the compose stack will run.
Skips when Docker is unavailable.
"""

import itertools
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "sar-postgis:test"
PASSWORD = "gate"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if _docker_available():
        return
    skip = pytest.mark.skip(reason="Docker is not available")
    for item in items:
        if "tests/db" in item.nodeid:
            item.add_marker(skip)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


class Server:
    def __init__(self, port: int) -> None:
        self.port = port
        self._counter = itertools.count()

    def url(self, dbname: str = "postgres") -> str:
        return f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.1:{self.port}/{dbname}"

    def dsn(self, dbname: str = "postgres") -> str:
        return f"postgresql://postgres:{PASSWORD}@127.0.0.1:{self.port}/{dbname}"

    def create_db(self, template: str | None = None) -> str:
        name = f"t{next(self._counter)}_{int(time.time() * 1000) % 100000}"
        with psycopg.connect(self.dsn(), autocommit=True) as conn:
            tpl = f' TEMPLATE "{template}"' if template else ""
            conn.execute(f'CREATE DATABASE "{name}"{tpl}')
        return name


@pytest.fixture(scope="session")
def server() -> Iterator[Server]:
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, str(ROOT / "deploy" / "postgis")], check=True)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-e",
            f"POSTGRES_PASSWORD={PASSWORD}",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container = run.stdout.strip()
    try:
        port = int(
            subprocess.run(
                ["docker", "port", container, "5432/tcp"], capture_output=True, text=True, check=True
            )
            .stdout.splitlines()[0]
            .rsplit(":", 1)[1]
        )
        srv = Server(port)
        # The image's init phase listens on a unix socket only, so a TCP connection means it is ready.
        deadline = time.time() + 90
        while True:
            try:
                psycopg.connect(srv.dsn(), connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        yield srv
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


@pytest.fixture(scope="session")
def migrated_template(server: Server) -> str:
    """A database migrated to head once, used as a template so each test clones it in milliseconds."""
    name = server.create_db()
    command.upgrade(alembic_config(server.url(name)), "head")
    return name


@pytest.fixture
def empty_db(server: Server) -> str:
    return server.create_db()


@pytest.fixture
def db(server: Server, migrated_template: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    name = server.create_db(template=migrated_template)
    with psycopg.connect(server.dsn(name), autocommit=True) as conn:
        yield conn
