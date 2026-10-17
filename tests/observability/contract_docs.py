"""Parsers for contract files, shared by the observability tests."""

import re
from dataclasses import dataclass
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    type: str
    labels: tuple[str, ...]


def metric_specs() -> list[MetricSpec]:
    """Rows of the table in contracts/metrics.md."""
    specs = []
    for line in (CONTRACTS / "metrics.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].startswith("`sar_"):
            # "`stage`, `status` (`succeeded`/`failed`)": drop the parenthesised enum values
            labels = tuple(re.findall(r"`(\w+)`", re.sub(r"\([^)]*\)", "", cells[2])))
            specs.append(MetricSpec(cells[0].strip("`"), cells[1], labels))
    return specs


def schema_tables() -> dict[str, set[str]]:
    """Table name -> column names, from contracts/schema.sql."""
    sql = (CONTRACTS / "schema.sql").read_text()
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S):
        columns = set()
        for raw in match.group(2).splitlines():
            line = raw.strip()
            if not line or line.startswith(("--", "CONSTRAINT", "PRIMARY KEY")):
                continue
            columns.add(line.split()[0])
        tables[match.group(1)] = columns
    return tables
