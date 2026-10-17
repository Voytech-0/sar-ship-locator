"""Step 0 gate: the contract files are valid and consistent with each other."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text())
    return data


def test_openapi_is_valid(openapi: dict[str, Any]) -> None:
    validate(openapi)


def test_openapi_enums_match_schema_sql(openapi: dict[str, Any]) -> None:
    sql = (CONTRACTS / "schema.sql").read_text()
    schemas = openapi["components"]["schemas"]
    for sql_type, api_schema in [
        ("scene_status", "SceneStatus"),
        ("stage", "Stage"),
        ("job_status", "JobStatus"),
        ("match_label", "MatchLabel"),
    ]:
        for value in schemas[api_schema]["enum"]:
            assert f"'{value}'" in sql.split(f"CREATE TYPE {sql_type} AS ENUM")[1].split(";")[0], (
                sql_type,
                value,
            )


def test_region_example_matches_config_schema() -> None:
    schema = json.loads((CONTRACTS / "config.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(yaml.safe_load((CONTRACTS / "region.greatbelt.yaml").read_text()), schema)


def test_protocols_importable() -> None:
    from contracts import protocols

    assert issubclass(protocols.NotAvailableError, protocols.TransientError)
    assert not issubclass(protocols.PermanentError, protocols.TransientError)
