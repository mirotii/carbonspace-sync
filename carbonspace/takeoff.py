"""Parse Revit schedule exports (CSV or tab-separated TXT) and aggregate by type."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from .units import parse_quantity

# Column name candidates; the first match in the header row wins (case-insensitive).
DEFAULT_COLUMNS: dict[str, list[str]] = {
    "component_id": ["CS_ComponentId", "CS_Component", "CarbonSpace Id"],
    "name": ["Family and Type", "Type", "Material: Name", "Material", "Family", "Type Name"],
    "area": ["Area", "Material: Area"],
    "volume": ["Volume", "Material: Volume"],
    "count": ["Count"],
    "length": ["Length"],
}

QUANTITY_FIELDS = ("area", "volume", "count", "length")


@dataclass
class TakeoffRow:
    name: str
    component_id: int | None = None
    area: float = 0.0
    volume: float = 0.0
    count: float = 0.0
    length: float = 0.0
    source: str = ""
    # Raw CS_ComponentId text when it is filled but not a valid integer.
    bad_id: str | None = None

    def quantity(self, basis: str) -> float:
        return float(getattr(self, basis, 0.0) or 0.0)


@dataclass
class Takeoff:
    title: str
    source: str
    rows: list[TakeoffRow]
    columns: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def read_text(path: str | Path) -> str:
    """Read a Revit export, detecting UTF-16 / UTF-8(-BOM) / cp1252."""
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")
    # UTF-16 without BOM: every other byte is NUL for ASCII text.
    if len(data) > 4 and data[1:20:2].count(0) > 5:
        return data.decode("utf-16-le")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def sniff_delimiter(text: str) -> str:
    sample = [ln for ln in text.splitlines()[:10] if ln.strip()]
    tabs = sum(ln.count("\t") for ln in sample)
    commas = sum(ln.count(",") for ln in sample)
    semis = sum(ln.count(";") for ln in sample)
    if tabs:
        return "\t"
    return ";" if semis > commas else ","


def _find_column(header: list[str], candidates: list[str]) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered.index(cand.lower())
    return None


def _repair_row(row: list[str], width: int, name_idx: int | None) -> list[str]:
    """Revit's CSV export does not quote fields, so a comma inside a type name
    ("WT.03_Plywood Partition, Non-Structural") splits it. Re-join the extra
    cells into the name column."""
    extra = len(row) - width
    if extra <= 0 or name_idx is None:
        return row
    joined = ",".join(row[name_idx:name_idx + extra + 1])
    return row[:name_idx] + [joined] + row[name_idx + extra + 1:]


def parse_takeoff(path: str | Path, columns: dict[str, list[str]] | None = None) -> Takeoff:
    columns = {**DEFAULT_COLUMNS, **(columns or {})}
    path = Path(path)
    text = read_text(path)
    delim = sniff_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))

    header_idx = None
    for i, row in enumerate(rows[:20]):
        if _find_column(row, columns["name"]) is not None:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            f"{path.name}: no header row found. Expected one of these columns: "
            + ", ".join(columns["name"])
        )

    header = [h.strip() for h in rows[header_idx]]
    # Ignore empty trailing columns (Excel / Revit sometimes add a trailing comma).
    while header and not header[-1]:
        header.pop()
    idx = {key: _find_column(header, cands) for key, cands in columns.items()}
    title = next((c.strip() for r in rows[:header_idx] for c in r if c.strip()), path.stem)
    found = {k: header[i] for k, i in idx.items() if i is not None}
    result = Takeoff(title=title, source=path.name, rows=[], columns=found)

    if not any(idx[q] is not None for q in QUANTITY_FIELDS):
        raise ValueError(f"{path.name}: no quantity column (Area / Volume / Count / Length) found.")

    def cell(row: list[str], key: str) -> str:
        i = idx.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    for line_no, raw in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        raw = list(raw)
        while len(raw) > len(header) and not raw[-1].strip():
            raw.pop()
        row = _repair_row(raw, len(header), idx["name"]) if delim == "," else raw
        if not any(c.strip() for c in row):
            continue  # blank / grouping spacer row
        name = cell(row, "name")
        if not name:
            # Totals, subtotals and grouping headers have no type name.
            continue
        r = TakeoffRow(name=name, source=f"{path.name}:{line_no}")
        id_text = cell(row, "component_id")
        if id_text:
            cleaned = id_text.strip().removesuffix(".0")
            if cleaned.isdigit():
                r.component_id = int(cleaned)
            else:
                r.bad_id = id_text
                result.warnings.append(
                    f"{r.source}: CS_ComponentId '{id_text}' for '{name}' is not a number; ignored."
                )
        for q in QUANTITY_FIELDS:
            setattr(r, q, parse_quantity(cell(row, q)) or 0.0)
        result.rows.append(r)
    return result


def aggregate(rows: list[TakeoffRow]) -> tuple[list[TakeoffRow], list[str]]:
    """Sum rows of the same Revit type. Returns (rows, warnings)."""
    merged: dict[str, TakeoffRow] = {}
    warnings: list[str] = []
    for r in rows:
        key = r.name.strip().lower()
        m = merged.get(key)
        if m is None:
            merged[key] = TakeoffRow(**{**r.__dict__})
            continue
        if r.component_id and m.component_id and r.component_id != m.component_id:
            warnings.append(
                f"'{r.name}' has different CS_ComponentId values ({m.component_id} and "
                f"{r.component_id}); using {m.component_id}."
            )
        m.component_id = m.component_id or r.component_id
        for q in QUANTITY_FIELDS:
            setattr(m, q, getattr(m, q) + getattr(r, q))
        m.source = f"{m.source}, {r.source}"
    return list(merged.values()), warnings
