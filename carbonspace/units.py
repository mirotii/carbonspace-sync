"""Number and unit parsing for Revit schedule values."""
from __future__ import annotations

import re

# Revit exports quantities with a unit suffix, e.g. "24.53 m³", "1,200.00 m²", "264 SF".
_NUMBER_RE = re.compile(r"[-+]?\d[\d.,   ]*")

# Conversion factors to SI (m, m², m³) for imperial units Revit may emit.
_TO_SI = {
    "ft": 0.3048,
    "lf": 0.3048,
    "ft2": 0.09290304,
    "sf": 0.09290304,
    "ft3": 0.028316846592,
    "cf": 0.028316846592,
    "mm": 0.001,
    "cm": 0.01,
}

# Normalised component unit text -> which takeoff column provides the quantity.
_BASIS_BY_UNIT = {
    "m3": "volume",
    "m2": "area",
    "m": "length",
    "pcs": "count",
    "pc": "count",
    "piece": "count",
    "pieces": "count",
    "st": "count",
    "stk": "count",
    "nr": "count",
    "no": "count",
    "kg": "weight",
    "t": "weight",
    "ton": "weight",
}

BASES = ("area", "volume", "count", "length", "weight")


def normalise_unit(text: str | None) -> str:
    """Lowercase a unit and replace superscripts: 'm³' -> 'm3', 'ft²' -> 'ft2'."""
    if not text:
        return ""
    return (
        text.strip().lower()
        .replace("²", "2").replace("³", "3")
        .replace("^2", "2").replace("^3", "3")
        .replace(" ", "")
    )


def _normalise_separators(s: str) -> str:
    """Turn a number with thousands/decimal separators into a float-parsable string."""
    s = re.sub(r"[   ]", "", s)
    if "," in s and "." in s:
        # Whichever separator comes last is the decimal separator.
        if s.rfind(",") > s.rfind("."):
            return s.replace(".", "").replace(",", ".")
        return s.replace(",", "")
    if "," in s:
        if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", s):
            return s.replace(",", "")  # 1,200 -> thousands separator
        return s.replace(",", ".")  # 24,53 -> decimal comma
    if s.count(".") > 1:
        return s.replace(".", "")  # 1.200.000 -> thousands separator
    return s


def parse_quantity(text: str | None) -> float | None:
    """Parse '1,200.50 m²' -> 1200.5. Imperial units are converted to SI.

    Returns None when the cell holds no number.
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    raw = m.group(0).strip().rstrip(".,")
    try:
        value = float(_normalise_separators(raw))
    except ValueError:
        return None
    unit = normalise_unit(text[m.end():])
    factor = _TO_SI.get(unit)
    if factor is None and unit in ("sqft", "ft²"):
        factor = _TO_SI["ft2"]
    return value * factor if factor else value


def basis_from_unit(component_unit: str | None) -> str | None:
    """Map a CarbonSpace ComponentUnit ('m2', 'm3', 'pcs', 'kg') to a takeoff basis."""
    return _BASIS_BY_UNIT.get(normalise_unit(component_unit))


def format_expression(value: float) -> str:
    """Format a quantity for a CarbonSpace Expression field: '48.84', '112'."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"
