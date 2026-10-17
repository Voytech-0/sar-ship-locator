"""Validate API responses against `contracts/openapi.yaml` (status documented, media type, body schema)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import yaml
from httpx import Response

SPEC: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml").read_text()
)


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node and node["$ref"].startswith("#/components/responses/"):
        node = SPEC["components"]["responses"][node["$ref"].rsplit("/", 1)[1]]
    return node


def validate_body(body: Any, schema: dict[str, Any]) -> None:
    root = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "components": SPEC["components"],
        **schema,
    }
    jsonschema.Draft202012Validator(
        root, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    ).validate(body)


def assert_conforms(response: Response, method: str, template: str) -> None:
    """The response's status is documented for the operation and its body matches the documented schema."""
    operation = SPEC["paths"][template][method.lower()]
    documented = operation["responses"].get(str(response.status_code))
    assert documented is not None, f"{method} {template}: undocumented status {response.status_code}"
    content = _resolve(documented).get("content", {})
    if not content:
        return
    media = response.headers["content-type"].split(";")[0].strip()
    assert media in content, f"{method} {template}: media type {media!r} not in {list(content)}"
    schema = content[media]["schema"]
    if media == "text/plain":
        return
    validate_body(response.json(), schema)
