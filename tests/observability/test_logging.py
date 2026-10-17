import io
import json
import logging
from collections.abc import Iterator

import pytest

from pipeline.logging import bind, configure_logging


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", buf)
    yield buf
    logging.getLogger().handlers.clear()


def _lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_line_is_single_line_json_with_base_fields(stream: io.StringIO) -> None:
    logging.getLogger("pipeline.test").info("hello %s", "world")
    [rec] = _lines(stream)
    assert rec["message"] == "hello world"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "pipeline.test"
    assert str(rec["timestamp"]).endswith("+00:00")


def test_bound_context_is_attached_and_released(stream: io.StringIO) -> None:
    log = logging.getLogger("pipeline.test")
    with bind(job_id="j1", stage="fetch"):
        with bind(scene_id="S1A_X"):
            log.info("inner")
        log.info("outer")
    log.info("after")
    inner, outer, after = _lines(stream)
    assert (inner["job_id"], inner["stage"], inner["scene_id"]) == ("j1", "fetch", "S1A_X")
    assert "scene_id" not in outer and outer["job_id"] == "j1"
    assert "job_id" not in after


def test_extra_fields_and_exceptions(stream: io.StringIO) -> None:
    log = logging.getLogger("pipeline.test")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("failed", extra={"bytes": 5})
    [rec] = _lines(stream)
    assert rec["bytes"] == 5
    assert "RuntimeError: boom" in str(rec["exception"])
    assert rec["level"] == "ERROR"


def test_configure_twice_does_not_duplicate_lines(stream: io.StringIO) -> None:
    configure_logging("INFO", stream)
    logging.getLogger("pipeline.test").info("once")
    assert len(_lines(stream)) == 1


def test_level_filters(stream: io.StringIO) -> None:
    logging.getLogger("pipeline.test").debug("hidden")
    assert stream.getvalue() == ""
