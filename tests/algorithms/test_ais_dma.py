import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contracts.protocols import AisPosition, Region
from pipeline.algorithms import ParseStats, iter_dma_positions

REGION = Region(
    "great-belt",
    "Great Belt",
    [(10.70, 55.00), (11.30, 55.00), (11.30, 55.70), (10.70, 55.70), (10.70, 55.00)],
)

HEADER = (
    "# Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,ROT,SOG,COG,Heading,IMO,Callsign,"
    "Name,Ship type,Cargo type,Width,Length,Type of position fixing device,Draught,Destination,ETA,"
    "Data source type,A,B,C,D"
)


def row(
    ts: str, kind: str, mmsi: str, lat: str, lon: str, sog: str = "10.5", cog: str = "90.0", hdg: str = "88"
) -> str:
    return (
        f"{ts},{kind},{mmsi},{lat},{lon},Under way using engine,0.0,{sog},{cog},{hdg},"
        "Unknown,Unknown,Unknown,Undefined,,,,GPS,,Unknown,,AIS,,,,"
    )


def csv_text(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def parse(text: str) -> tuple[list[AisPosition], ParseStats]:
    stats = ParseStats()
    return list(iter_dma_positions(io.StringIO(text), REGION, stats)), stats


def test_parses_a_vessel_row() -> None:
    out, stats = parse(csv_text(row("14/10/2026 05:32:20", "Class A", "219123456", "55.3400", "10.9700")))
    (p,) = out
    assert p.mmsi == 219123456
    assert p.ts == datetime(2026, 10, 14, 5, 32, 20, tzinfo=UTC)
    assert p.position == (10.97, 55.34)
    assert (p.sog_knots, p.cog_deg, p.heading) == (10.5, 90.0, 88)
    assert (stats.rows_read, stats.yielded) == (1, 1)


def test_filters_to_region_polygon_not_just_bbox() -> None:
    triangle = Region("t", "T", [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (0.0, 0.0)])
    text = csv_text(
        row("14/10/2026 05:00:00", "Class A", "219000001", "2.0", "2.0"),  # inside
        row("14/10/2026 05:00:00", "Class A", "219000002", "8.0", "8.0"),  # in bbox, outside triangle
        row("14/10/2026 05:00:00", "Class A", "219000003", "20.0", "20.0"),  # outside bbox
    )
    stats = ParseStats()
    out = list(iter_dma_positions(io.StringIO(text), triangle, stats))
    assert [p.mmsi for p in out] == [219000001]
    assert stats.outside_region == 2


def test_skips_non_vessels() -> None:
    text = csv_text(
        row("14/10/2026 05:00:00", "Base Station", "2190001", "55.3", "10.9"),
        row("14/10/2026 05:00:00", "AtoN", "992190001", "55.3", "10.9"),
        row("14/10/2026 05:00:00", "Class B", "219000009", "55.3", "10.9"),
    )
    out, stats = parse(text)
    assert [p.mmsi for p in out] == [219000009]
    assert stats.non_vessel == 2


def test_malformed_rows_are_skipped_and_counted() -> None:
    text = csv_text(
        row("14/10/2026 05:00:00", "Class A", "219000001", "55.3", "10.9"),
        row("14/10/2026 05:00:00", "Class A", "219000002", "abc", "10.9"),  # bad latitude
        row("not a time", "Class A", "219000003", "55.3", "10.9"),  # bad timestamp
        row("14/10/2026 05:00:00", "Class A", "x1", "55.3", "10.9"),  # bad mmsi
        row("14/10/2026 05:00:00", "Class A", "12345", "55.3", "10.9"),  # not a 9-digit mmsi
        row("14/10/2026 05:00:00", "Class A", "219000006", "91.0", "181.0"),  # 'no position' sentinel
        "short,row",
        row("14/10/2026 05:00:01", "Class A", "219000007", "55.31", "10.91"),
    )
    out, stats = parse(text)
    assert [p.mmsi for p in out] == [219000001, 219000007]
    assert stats.malformed == 6
    assert stats.yielded == 2


def test_not_available_sentinels_become_none() -> None:
    text = csv_text(
        row(
            "14/10/2026 05:00:00", "Class A", "219000001", "55.3", "10.9", sog="102.3", cog="360.0", hdg="511"
        )
    )
    (p,) = parse(text)[0]
    assert (p.sog_knots, p.cog_deg, p.heading) == (None, None, None)


def test_blank_optional_fields_become_none() -> None:
    text = csv_text(
        row("14/10/2026 05:00:00", "Class A", "219000001", "55.3", "10.9", sog="", cog="", hdg="")
    )
    (p,) = parse(text)[0]
    assert (p.sog_knots, p.cog_deg, p.heading) == (None, None, None)


def test_header_only_and_empty_inputs() -> None:
    assert parse(HEADER + "\n")[0] == []
    assert parse("")[0] == []


def test_missing_required_column_raises() -> None:
    with pytest.raises(ValueError, match="lacks columns"):
        list(iter_dma_positions(io.StringIO("# Timestamp,MMSI\n1,2\n"), REGION))


def test_header_matching_is_case_and_hash_insensitive() -> None:
    text = csv_text(row("14/10/2026 05:00:00", "Class A", "219000001", "55.3", "10.9"))
    assert len(parse(text.replace("# Timestamp", "timestamp", 1))[0]) == 1


def test_reads_zip_and_csv_paths_and_binary_streams(tmp_path: Path) -> None:
    text = csv_text(row("14/10/2026 05:00:00", "Class A", "219000001", "55.3", "10.9"))
    csv_path = tmp_path / "aisdk-2026-10-14.csv"
    csv_path.write_text(text)
    zip_path = tmp_path / "aisdk-2026-10-14.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "ignore me")
        zf.writestr("aisdk-2026-10-14.csv", text)

    assert len(list(iter_dma_positions(csv_path, REGION))) == 1
    assert len(list(iter_dma_positions(str(zip_path), REGION))) == 1
    assert len(list(iter_dma_positions(io.BytesIO(text.encode()), REGION))) == 1


def test_zip_without_csv_raises(tmp_path: Path) -> None:
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("notes.txt", "nothing")
    with pytest.raises(ValueError, match=r"no \.csv"):
        list(iter_dma_positions(zip_path, REGION))


def test_is_lazy_and_tolerates_invalid_utf8() -> None:
    good = csv_text(row("14/10/2026 05:00:00", "Class A", "219000001", "55.3", "10.9")).encode()
    bad = b"14/10/2026 05:00:00,Class A,219000002,55.3,10.9,\xff\xfe,0,1,2,3\n"
    it = iter_dma_positions(io.BytesIO(good + bad), REGION)
    assert next(it).mmsi == 219000001  # first row is available before the rest is read
    list(it)  # the bad row is skipped without raising


def test_static_vessel_fields_are_parsed() -> None:
    line = (
        "14/10/2026 05:17:01,Class A,636014088,55.4,11.0,Under way using engine,0.0,12.3,328.5,326,"
        "9417206,A8RF2,TAURUS,Tanker,Category X,32,183,GPS,10.9,DKSKA,,AIS,147,36,22,10"
    )
    [p], _ = parse(csv_text(line))
    assert (p.name, p.ship_type, p.length_m, p.width_m) == ("TAURUS", "Tanker", 183.0, 32.0)
    # antenna 147 m from the bow, 36 m from the stern, 22 m from port, 10 m from starboard
    assert (p.to_bow_m, p.to_stern_m, p.to_port_m, p.to_starboard_m) == (147.0, 36.0, 22.0, 10.0)


def test_missing_static_fields_are_none() -> None:
    [p], _ = parse(csv_text(row("14/10/2026 05:18:01", "Class A", "636014089", "55.4", "11.0")))
    assert (p.name, p.ship_type, p.length_m, p.width_m) == (None, None, None, None)
