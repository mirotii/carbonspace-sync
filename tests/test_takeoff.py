from pathlib import Path

import pytest

from carbonspace.takeoff import aggregate, parse_takeoff, read_text, sniff_delimiter

SAMPLES = Path(__file__).parent / "samples"


@pytest.mark.parametrize("name", ["Wall Type Test.txt", "Wall Type Test.csv"])
def test_wall_schedule_txt_and_csv_agree(name):
    t = parse_takeoff(SAMPLES / name)
    assert t.title == "Wall Schedule 3"
    names = [r.name for r in t.rows]
    # Totals row (no type name) is dropped; 12 types remain.
    assert len(t.rows) == 12
    # Unquoted comma inside the type name is repaired in the CSV variant.
    assert "Basic Wall: WT.03_Plywood Partition, Non-Structural Bathroom" in names
    r = next(r for r in t.rows if r.name == "Basic Wall: WT.01a")
    assert (r.area, r.volume, r.count) == (148, 61.55, 9)
    assert r.component_id is None
    assert round(sum(r.area for r in t.rows), 6) == 459
    # Revit's total (99.73) sums unrounded values; the exported rows are rounded.
    assert sum(r.volume for r in t.rows) == pytest.approx(130.67, abs=0.02)


def test_roof_schedule():
    t = parse_takeoff(SAMPLES / "Roof Type Test.csv")
    assert [r.name for r in t.rows] == [
        "Basic Roof: Roof Model",
        "Basic Roof: Generic - 20mm Corrugated",
        "Basic Roof: Generic - 40mm",
    ]
    assert t.rows[0].volume == 179.45


def write(tmp_path, text, encoding, name="x.txt"):
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return p


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "utf-8", "cp1252"])
def test_encodings(tmp_path, encoding):
    text = '"Schedule"\n"CS_ComponentId"\t"Family and Type"\t"Volume"\n"80460"\t"Basic Wall: Façade"\t"1,200.50 m³"\n'
    p = write(tmp_path, text.replace("³", "3") if encoding == "cp1252" else text, encoding)
    t = parse_takeoff(p)
    assert t.rows[0].name == "Basic Wall: Façade"
    assert t.rows[0].component_id == 80460
    assert t.rows[0].volume == 1200.5


def test_component_id_and_bad_id(tmp_path):
    text = ("Wall Schedule\nCS_ComponentId,Family and Type,Area\n"
            "80460,Basic Wall: A,10 m²\nWT.01,Basic Wall: B,5 m²\n,,15 m²\n")
    t = parse_takeoff(write(tmp_path, text, "utf-8", "x.csv"))
    assert [r.component_id for r in t.rows] == [80460, None]
    assert t.rows[1].bad_id == "WT.01"
    assert any("not a number" in w for w in t.warnings)


def test_grouping_headers_and_subtotals_skipped(tmp_path):
    text = ("Wall Schedule\nFamily and Type,Area\n\nLevel 1,\nBasic Wall: A,10 m²\nBasic Wall: A,5 m²\n,15 m²\n"
            "\nGrand total: 3,\n")
    # 'Level 1' sits in the name column; Revit grouping headers have no quantities,
    # but they still look like a type, so they are kept with zero quantities.
    t = parse_takeoff(write(tmp_path, text, "utf-8", "x.csv"))
    rows, _ = aggregate(t.rows)
    a = next(r for r in rows if r.name == "Basic Wall: A")
    assert a.area == 15


def test_aggregate_conflicting_ids():
    from carbonspace.takeoff import TakeoffRow
    rows, warnings = aggregate([TakeoffRow("A", 1, area=1), TakeoffRow("a ", 2, area=2)])
    assert len(rows) == 1 and rows[0].area == 3 and rows[0].component_id == 1
    assert warnings


def test_sniff_delimiter():
    assert sniff_delimiter('"a"\t"b"\n') == "\t"
    assert sniff_delimiter("a,b\n") == ","
    assert sniff_delimiter("a;b;c\n") == ";"


def test_read_text_bom(tmp_path):
    assert read_text(write(tmp_path, "m³", "utf-8-sig")) == "m³"


def test_trailing_empty_column_and_unquoted_comma(tmp_path):
    # Edited in Excel: trailing empty column in the header, comma inside unquoted names.
    text = "\r\n".join([
        "Wall Schedule 3,,,,,", "CS_ComponentId,Family and Type,Area,Volume,Count,", ",,,,,",
        "80460,Basic Wall: WT.01a,112 m²,48.84 m³,9,",
        "80478,Basic Wall: WT.03_Plywood Partition, Non-Structural,21 m²,2.93 m³,4",
        ",,334 m²,99.73 m³,,", ""])
    t = parse_takeoff(write(tmp_path, text, "utf-8-sig", "x.csv"))
    assert [(r.component_id, r.name, r.area, r.count) for r in t.rows] == [
        (80460, "Basic Wall: WT.01a", 112, 9),
        (80478, "Basic Wall: WT.03_Plywood Partition, Non-Structural", 21, 4)]
