import pytest

from carbonspace.mapping import MapEntry, load_map, resolve, save_map
from carbonspace.push import TAG, build_plan
from carbonspace.takeoff import Takeoff, TakeoffRow
from carbonspace.units import basis_from_unit, format_expression, parse_quantity


@pytest.mark.parametrize("text,expected", [
    ("24.53 m³", 24.53),
    ("1,200.00 m²", 1200.0),
    ("1.200,50 m²", 1200.5),
    ("24,53", 24.53),
    ("6", 6.0),
    ("", None),
    ("n/a", None),
    ("100 SF", 9.290304),
    ("10 ft³", 0.28316846592),
    ("-3.5 m", -3.5),
])
def test_parse_quantity(text, expected):
    got = parse_quantity(text)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_basis_from_unit():
    assert basis_from_unit("m2") == "area"
    assert basis_from_unit("m³") == "volume"
    assert basis_from_unit("pcs") == "count"
    assert basis_from_unit("kg") == "weight"
    assert basis_from_unit("null") is None


def test_format_expression():
    assert format_expression(112.0) == "112"
    assert format_expression(48.8400001) == "48.84"
    assert format_expression(0) == "0"


COMPONENTS = {
    80460: {"Name": "Project A WT.01a", "ComponentUnit": "m2"},
    80465: {"Name": "Douglas Fir", "ComponentUnit": "m3"},
    77244: {"Name": "Steel profile", "ComponentUnit": "kg"},
}
CFG = {"group_by_schedule": {"wall": "Facade opaque"}, "default_group": "Revit import"}


def wall_takeoff(rows):
    return [(Takeoff(title="Wall Schedule 3", source="t.txt", rows=rows), rows)]


def test_resolve_uses_revit_id_unit_basis_and_sums_same_component():
    rows = [TakeoffRow("Basic Wall: WT.01a", 80460, area=112, volume=48.84),
            TakeoffRow("Basic Wall: WT.01a Bench", 80460, area=11, volume=4.4),
            TakeoffRow("Basic Wall: Timber", 80465, area=5, volume=2)]
    res = resolve(wall_takeoff(rows), {}, COMPONENTS.get, CFG)
    assert not res.errors and not res.unmapped
    by_id = {l.component_id: l for l in res.lines}
    assert by_id[80460].quantity == 123 and by_id[80460].basis == "area"
    assert by_id[80460].group == "Facade opaque"
    assert by_id[80465].quantity == 2 and by_id[80465].basis == "volume"


def test_resolve_map_fallback_group_override_and_unmapped():
    rows = [TakeoffRow("Basic Wall: WT.02", None, area=17), TakeoffRow("Basic Wall: X", None, area=1)]
    entries = {"basic wall: wt.02": MapEntry("Basic Wall: WT.02", 80460, group="Separating walls")}
    res = resolve(wall_takeoff(rows), entries, COMPONENTS.get, CFG)
    assert [(l.component_id, l.group, l.quantity) for l in res.lines] == [(80460, "Separating walls", 17)]
    assert [r.name for r in res.unmapped] == ["Basic Wall: X"]


def test_resolve_weight_needs_density_factor():
    rows = [TakeoffRow("Steel", 77244, volume=2)]
    res = resolve(wall_takeoff(rows), {}, COMPONENTS.get, CFG)
    assert res.errors and not res.lines
    entries = {"steel": MapEntry("Steel", factor=7850)}
    res = resolve(wall_takeoff(rows), entries, COMPONENTS.get, CFG)
    assert res.lines[0].quantity == 15700


def test_resolve_unknown_component():
    res = resolve(wall_takeoff([TakeoffRow("A", 999, area=1)]), {}, COMPONENTS.get, CFG)
    assert "not found" in res.errors[0]


def test_map_roundtrip(tmp_path):
    p = tmp_path / "m.csv"
    save_map(p, {"a": MapEntry("A", 1, "Comp", "m2", "", 1.0, "Roofs", "n")})
    assert load_map(p)["a"].carbonspace_component_id == 1
    assert load_map(p)["a"].group == "Roofs"


def calc_set(lines):
    return {
        "CalculationGroups": [{"Id": 1, "Name": "Facade opaque"}, {"Id": 2, "Name": "Roofs"}],
        "CalculationGroupComponents": lines,
    }


def test_plan_create_update_keep_manual_stale():
    from carbonspace.mapping import ResolvedLine
    existing = [
        {"Id": 10, "CalculationGroupId": 1, "ComponentId": 80460, "Expression": "100", "Comment": f"{TAG}: WT.01a"},
        {"Id": 11, "CalculationGroupId": 2, "ComponentId": 80470, "Expression": "147", "Comment": None},
        {"Id": 12, "CalculationGroupId": 2, "ComponentId": 80475, "Expression": "29", "Comment": f"{TAG}: RT.06"},
        {"Id": 13, "CalculationGroupId": 1, "ComponentId": 80465, "Expression": "2", "Comment": f"{TAG}: x"},
    ]
    lines = [
        ResolvedLine("Facade opaque", 80460, "WT.01a", "m2", "area", 123, ["WT.01a"]),
        ResolvedLine("Facade opaque", 80465, "Fir", "m3", "volume", 2, ["x"]),
        ResolvedLine("Floors", 80466, "FT.01", "m2", "area", 163, ["FT.01"]),
    ]
    plan = build_plan(calc_set(existing), lines)
    kinds = {(a.kind, a.component_id) for a in plan.actions}
    assert ("update", 80460) in kinds
    assert ("keep", 80465) in kinds
    assert ("create", 80466) in kinds
    assert ("manual", 80470) in kinds  # hand-entered: never touched
    assert ("stale", 80475) in kinds   # earlier push, not in export
    assert plan.new_groups == ["Floors"]
    pruned = build_plan(calc_set(existing), lines, prune=True)
    assert ("delete", 80475) in {(a.kind, a.component_id) for a in pruned.actions}
    assert ("manual", 80470) in {(a.kind, a.component_id) for a in pruned.actions}


LIBRARY = [{"Id": 80460, "Name": "Project A WT.01a"}, {"Id": 80462, "Name": "Project A WT.01b"},
           {"Id": 80478, "Name": "Project A WT.03a"}, {"Id": 80479, "Name": "Project A WT.03b"},
           {"Id": 80477, "Name": "Project A WT.02"}]


def test_library_matches():
    from carbonspace.mapping import library_matches, type_code
    assert type_code("Basic Wall: WT.01a Bench") == "WT.01a"
    assert type_code("Basic Roof: Generic - 40mm") is None
    exact, partial = library_matches("Basic Wall: WT.01a Bench", LIBRARY)
    assert [c["Id"] for c in exact] == [80460] and partial == []
    exact, partial = library_matches("Basic Wall: WT.03_Plywood Partition, Non-Structural", LIBRARY)
    assert exact == [] and [c["Id"] for c in partial] == [80478, 80479]
    assert library_matches("Basic Wall: WT.02 - Plywood Partition", LIBRARY)[0][0]["Id"] == 80477
    assert library_matches("Basic Wall: WT.10", LIBRARY) == ([], [])


def test_groups_in_calculation():
    from carbonspace.mapping import groups_in_calculation
    s = {"CalculationGroups": [{"Id": 1, "Name": "Separating walls "}],
         "CalculationGroupComponents": [{"ComponentId": 80477, "CalculationGroupId": 1}]}
    assert groups_in_calculation(s) == {80477: "Separating walls"}
