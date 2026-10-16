"""Summaries of a calculation's results (from /api/Bom/GetByCalculation)."""
from __future__ import annotations

STAGES = ("CO2A", "CO2B", "CO2C", "CO2D")


def gfa(bom: dict) -> float:
    return float(next((p.get("Value") or 0 for p in bom.get("CalculationProperties") or []
                       if p.get("FullKey") == "building.gfa"), 0) or 0)


def _row(label: dict, lines: list[dict]) -> dict:
    r = dict(label)
    for s in STAGES:
        r[s] = sum(ln.get(s) or 0 for ln in lines)
    r["CO2_A_to_C"] = r["CO2A"] + r["CO2B"] + r["CO2C"]
    r["CO2Biogenic"] = sum(ln.get("CO2Biogenic") or 0 for ln in lines)
    r["Weight_kg"] = sum(ln.get("Weight") or 0 for ln in lines)
    return r


def summarise(bom: dict, by: str) -> list[dict]:
    """by: 'total' | 'layer' | 'component' | 'material'. Values in kgCO2e."""
    lines = bom.get("CalculationLines") or []
    keyfuncs = {
        "total": lambda ln: {},
        "layer": lambda ln: {"MainShearingLayer": (ln.get("MainShearingLayer") or "").strip(),
                             "SubShearingLayer": (ln.get("SubShearingLayer") or "").strip()},
        "component": lambda ln: {"Component": ln.get("OverrideName") or ln.get("TopComponentName") or "",
                                 "ComponentId": ln.get("TopComponentId")},
        "material": lambda ln: {"MaterialCategory": ln.get("MaterialCategory") or "",
                                "EPD": ln.get("EpdComponentName") or ""},
    }
    keyf = keyfuncs[by]
    buckets: dict[tuple, tuple[dict, list]] = {}
    for ln in lines:
        label = keyf(ln)
        k = tuple(label.values())
        buckets.setdefault(k, (label, []))[1].append(ln)
    rows = [_row(label, lns) for label, lns in buckets.values()] or ([_row({}, [])] if by == "total" else [])
    rows.sort(key=lambda r: -r["CO2_A_to_C"])
    return rows


def compare(bom_a: dict, bom_b: dict, by: str) -> list[dict]:
    """Side-by-side A-C totals for two calculations."""
    a = {tuple(v for k, v in r.items() if k not in _VALUE_KEYS): r for r in summarise(bom_a, by)}
    b = {tuple(v for k, v in r.items() if k not in _VALUE_KEYS): r for r in summarise(bom_b, by)}
    rows = []
    for key in list(a) + [k for k in b if k not in a]:
        label = {k: v for k, v in (a.get(key) or b.get(key)).items() if k not in _VALUE_KEYS}
        va = (a.get(key) or {}).get("CO2_A_to_C", 0.0)
        vb = (b.get(key) or {}).get("CO2_A_to_C", 0.0)
        rows.append({**label, "A_to_C_first": va, "A_to_C_second": vb, "difference": vb - va,
                     "change_pct": (vb - va) / va * 100 if va else None})
    rows.sort(key=lambda r: -abs(r["difference"]))
    return rows


_VALUE_KEYS = set(STAGES) | {"CO2_A_to_C", "CO2Biogenic", "Weight_kg"}
