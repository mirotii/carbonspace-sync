"""Revit type/material -> CarbonSpace component resolution.

Priority for the component id:
1. The CS_ComponentId parameter filled in Revit (exported as a schedule column).
2. carbonspace_component_id in material_map.csv, looked up by Revit name.
The map also holds per-type overrides: calculation group, quantity basis, factor.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .takeoff import Takeoff, TakeoffRow
from .units import BASES, basis_from_unit

MAP_FIELDS = [
    "revit_name", "carbonspace_component_id", "carbonspace_name", "unit",
    "quantity_basis", "factor", "group", "note",
]


@dataclass
class MapEntry:
    revit_name: str
    carbonspace_component_id: int | None = None
    carbonspace_name: str = ""
    unit: str = ""
    quantity_basis: str = ""
    factor: float = 1.0
    group: str = ""
    note: str = ""


@dataclass
class ResolvedLine:
    group: str
    component_id: int
    component_name: str
    unit: str
    basis: str
    quantity: float
    revit_names: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    lines: list[ResolvedLine]
    unmapped: list[TakeoffRow]
    errors: list[str]
    warnings: list[str]


def _key(name: str) -> str:
    return " ".join(name.strip().lower().split())


def load_map(path: str | Path) -> dict[str, MapEntry]:
    path = Path(path)
    if not path.exists():
        return {}
    entries: dict[str, MapEntry] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            name = (row.get("revit_name") or "").strip()
            if not name:
                continue
            cid = (row.get("carbonspace_component_id") or "").strip()
            factor = (row.get("factor") or "").strip()
            try:
                entries[_key(name)] = MapEntry(
                    revit_name=name,
                    carbonspace_component_id=int(cid) if cid else None,
                    carbonspace_name=(row.get("carbonspace_name") or "").strip(),
                    unit=(row.get("unit") or "").strip(),
                    quantity_basis=(row.get("quantity_basis") or "").strip().lower(),
                    factor=float(factor) if factor else 1.0,
                    group=(row.get("group") or "").strip(),
                    note=(row.get("note") or "").strip(),
                )
            except ValueError as e:
                raise ValueError(f"{path.name} line {i}: {e}") from e
    return entries


def save_map(path: str | Path, entries: dict[str, MapEntry]) -> None:
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MAP_FIELDS)
        w.writeheader()
        for e in sorted(entries.values(), key=lambda e: e.revit_name.lower()):
            row = {**e.__dict__}
            row["carbonspace_component_id"] = e.carbonspace_component_id or ""
            row["factor"] = "" if e.factor == 1.0 else e.factor
            w.writerow(row)


def default_group(takeoff: Takeoff, config: dict) -> str:
    title = takeoff.title.lower()
    for keyword, group in (config.get("group_by_schedule") or {}).items():
        if keyword.lower() in title:
            return group
    return config.get("default_group") or "Revit import"


def resolve(
    takeoffs: list[tuple[Takeoff, list[TakeoffRow]]],
    entries: dict[str, MapEntry],
    lookup: Callable[[int], dict | None],
    config: dict,
    group_override: str | None = None,
) -> Resolution:
    """Turn aggregated takeoff rows into calculation lines keyed by (group, component)."""
    lines: dict[tuple[str, int], ResolvedLine] = {}
    unmapped: list[TakeoffRow] = []
    errors: list[str] = []
    warnings: list[str] = []

    for takeoff, rows in takeoffs:
        fallback_group = group_override or default_group(takeoff, config)
        for row in rows:
            entry = entries.get(_key(row.name))
            cid = row.component_id or (entry.carbonspace_component_id if entry else None)
            if not cid:
                unmapped.append(row)
                continue
            if row.component_id and entry and entry.carbonspace_component_id \
                    and entry.carbonspace_component_id != row.component_id:
                warnings.append(
                    f"'{row.name}': Revit CS_ComponentId {row.component_id} overrides "
                    f"map id {entry.carbonspace_component_id}."
                )
            comp = lookup(cid)
            if comp is None:
                errors.append(f"'{row.name}': CarbonSpace component {cid} not found or not accessible.")
                continue
            unit = comp.get("ComponentUnit") or ""
            basis = (entry.quantity_basis if entry and entry.quantity_basis else None) \
                or basis_from_unit(unit)
            factor = entry.factor if entry else 1.0
            if basis not in BASES:
                errors.append(
                    f"'{row.name}' -> {cid} '{comp.get('Name')}': unit '{unit}' is not understood. "
                    f"Set quantity_basis (area/volume/count/length) for it in the map."
                )
                continue
            if basis == "weight":
                # Takeoffs carry no weight; derive it from volume x density (factor).
                if not entry or entry.factor == 1.0:
                    errors.append(
                        f"'{row.name}' -> {cid} '{comp.get('Name')}' is measured in {unit}. "
                        f"Set quantity_basis=volume and factor=<density kg/m3> in the map."
                    )
                    continue
                basis = "volume"
            quantity = row.quantity(basis) * factor
            if quantity == 0:
                warnings.append(f"'{row.name}': {basis} is 0 in the export.")
            group = (entry.group if entry and entry.group else None) or fallback_group
            key = (group, cid)
            line = lines.get(key)
            if line is None:
                lines[key] = ResolvedLine(group, cid, comp.get("Name") or "", unit, basis,
                                          quantity, [row.name])
            else:
                if line.basis != basis:
                    warnings.append(f"Component {cid} is fed by both {line.basis} and {basis}.")
                line.quantity += quantity
                line.revit_names.append(row.name)
    return Resolution(list(lines.values()), unmapped, errors, warnings)


_CODE_RE = re.compile(r"[A-Za-z]{1,4}[.\-_]?\d+[a-z]?")


def type_code(revit_name: str) -> str | None:
    """'Basic Wall: WT.01a Bench' -> 'WT.01a'; 'Basic Roof: Generic - 40mm' -> None."""
    short = revit_name.split(":", 1)[-1].strip()
    m = _CODE_RE.match(short)
    return m.group(0) if m else None


def library_matches(revit_name: str, library: list[dict]) -> tuple[list[dict], list[dict]]:
    """Components whose name carries the same type code as the Revit type.

    Returns (exact, partial): 'WT.01a' exactly matches 'Project A WT.01a';
    'WT.03' only partially matches 'Project A WT.03a' and 'Project A WT.03b'.
    """
    code = type_code(revit_name)
    if not code:
        return [], []
    esc = re.escape(code)
    exact_re = re.compile(rf"(?<![A-Za-z0-9]){esc}(?![0-9A-Za-z])", re.I)
    partial_re = re.compile(rf"(?<![A-Za-z0-9]){esc}[a-z](?![0-9A-Za-z])", re.I)
    exact = [c for c in library if exact_re.search(c.get("Name") or "")]
    partial = [c for c in library if partial_re.search(c.get("Name") or "")] if not exact else []
    return exact, partial


def groups_in_calculation(calc_set: dict) -> dict[int, str]:
    """component id -> name of the group it sits in (first occurrence)."""
    names = {g["Id"]: (g.get("Name") or "").strip() for g in calc_set.get("CalculationGroups", [])}
    found: dict[int, str] = {}
    for ln in calc_set.get("CalculationGroupComponents", []):
        found.setdefault(ln["ComponentId"], names.get(ln["CalculationGroupId"], ""))
    return found


def unmapped_share(unmapped: list[TakeoffRow], all_rows: list[TakeoffRow]) -> list[tuple[TakeoffRow, float, float]]:
    """(row, share of total area, share of total volume)."""
    tot_a = sum(r.area for r in all_rows) or 1.0
    tot_v = sum(r.volume for r in all_rows) or 1.0
    return [(r, r.area / tot_a, r.volume / tot_v) for r in unmapped]
