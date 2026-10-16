"""Plan and apply a push of resolved lines into a CarbonSpace calculation.

Lines are written one by one (create / update / delete) instead of through
CreateOrUpdateCalculation, because that endpoint deletes every line that is not
in the request - including lines people entered by hand.

Lines written by this tool carry a Comment starting with TAG. Only tagged lines
are ever deleted (and only with --prune); hand-entered lines are never touched
unless the same component sits in the same group, in which case it is updated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .client import CarbonSpaceClient
from .mapping import ResolvedLine
from .units import format_expression

TAG = "cs_sync"


@dataclass
class Action:
    kind: str  # create | update | keep | delete | stale | manual
    group: str
    component_id: int
    component_name: str = ""
    unit: str = ""
    old: str | None = None
    new: str | None = None
    revit_names: list[str] = field(default_factory=list)
    line: dict | None = None


@dataclass
class Plan:
    actions: list[Action]
    new_groups: list[str]

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)

    @property
    def has_writes(self) -> bool:
        return bool(self.new_groups) or any(a.kind in ("create", "update", "delete") for a in self.actions)


def _gkey(name: str | None) -> str:
    return " ".join((name or "").strip().lower().split())


def make_comment(revit_names: list[str]) -> str:
    text = f"{TAG}: " + "; ".join(revit_names)
    return text if len(text) <= 250 else text[:247] + "..."


def _same_number(a: str | None, b: str | None) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return (a or "") == (b or "")


def build_plan(calc_set: dict | None, lines: list[ResolvedLine], prune: bool = False) -> Plan:
    groups = {g["Id"]: g for g in (calc_set or {}).get("CalculationGroups", [])}
    group_names = {_gkey(g["Name"]) for g in groups.values()}
    existing: dict[tuple[str, int], list[dict]] = {}
    for ln in (calc_set or {}).get("CalculationGroupComponents", []):
        gname = groups.get(ln["CalculationGroupId"], {}).get("Name", "")
        existing.setdefault((_gkey(gname), ln["ComponentId"]), []).append(ln)
    # Tagged lines first so re-pushes update the tool's own lines.
    for lst in existing.values():
        lst.sort(key=lambda ln: not (ln.get("Comment") or "").startswith(TAG))

    actions: list[Action] = []
    new_groups: list[str] = []
    used: set[int] = set()
    for rl in lines:
        new = format_expression(rl.quantity)
        a = Action("create", rl.group, rl.component_id, rl.component_name, rl.unit,
                   new=new, revit_names=rl.revit_names)
        candidates = [ln for ln in existing.get((_gkey(rl.group), rl.component_id), []) if ln["Id"] not in used]
        if candidates:
            ln = candidates[0]
            used.add(ln["Id"])
            a.line, a.old = ln, ln.get("Expression")
            # Same number as now -> nothing to write, whoever entered it.
            a.kind = "keep" if _same_number(a.old, new) else "update"
        elif _gkey(rl.group) not in group_names and rl.group not in new_groups:
            new_groups.append(rl.group)
        actions.append(a)

    for (gk, cid), lst in existing.items():
        for ln in lst:
            if ln["Id"] in used:
                continue
            gname = groups.get(ln["CalculationGroupId"], {}).get("Name", "")
            tagged = (ln.get("Comment") or "").startswith(TAG)
            kind = ("delete" if prune else "stale") if tagged else "manual"
            name = ((ln.get("Component") or {}).get("Name")) or ln.get("OverrideName") or ""
            actions.append(Action(kind, gname, cid, name, old=ln.get("Expression"), line=ln,
                                  revit_names=[(ln.get("Comment") or "").removeprefix(f"{TAG}: ")] if tagged else []))
    return Plan(actions, new_groups)


def apply_plan(client: CarbonSpaceClient, calc_id: int, project_id: int, plan: Plan,
               log=print) -> dict:
    """Execute the plan, then force CarbonSpace to recompute. Returns a summary."""
    calc_set = client.calculation_set(calc_id)
    group_ids = {_gkey(g["Name"]): g["Id"] for g in calc_set.get("CalculationGroups", [])}
    for name in plan.new_groups:
        g = client.create_group(calc_id, project_id, name)
        group_ids[_gkey(name)] = g["Id"]
        log(f"  + group '{name}'")

    done = {"create": 0, "update": 0, "delete": 0}
    for a in plan.actions:
        if a.kind == "create":
            client.create_line(calc_id, project_id, group_ids[_gkey(a.group)], a.component_id,
                               a.new, make_comment(a.revit_names))
        elif a.kind == "update":
            client.update_line(a.line, Expression=a.new, Comment=make_comment(a.revit_names))
        elif a.kind == "delete":
            client.delete_line(a.line["Id"])
        else:
            continue
        done[a.kind] += 1
        log(f"  {a.kind:<6} {a.group} / {a.component_id} {a.component_name}: {a.old or ''} -> {a.new or ''}")

    # New lines keep Quantity=0 until the server re-evaluates expressions;
    # rewriting an inventory value triggers that for the whole calculation.
    bom = client.bom(calc_id)
    gfa = next((p.get("Value") for p in bom.get("CalculationProperties") or []
                if p.get("FullKey") == "building.gfa"), 0)
    client.update_inventory(calc_id, {"building.gfa": format_expression(float(gfa or 0))})
    return done


def verify(client: CarbonSpaceClient, calc_id: int, lines: list[ResolvedLine]) -> list[str]:
    """Compare quantities CarbonSpace computed against what was pushed."""
    calc_set = client.calculation_set(calc_id)
    gname = {g["Id"]: _gkey(g["Name"]) for g in calc_set.get("CalculationGroups", [])}
    computed: dict[tuple[str, int], float] = {}
    seen: set[tuple] = set()
    for bl in client.bom(calc_id).get("CalculationLines") or []:
        # BOM rows are expanded per EPD; count each calculation line once.
        k = (bl["CalculationGroupId"], bl["TopComponentId"], bl.get("OverrideName"), bl["TopQuantity"])
        if k in seen:
            continue
        seen.add(k)
        key = (gname.get(bl["CalculationGroupId"], ""), bl["TopComponentId"])
        computed[key] = computed.get(key, 0) + (bl["TopQuantity"] or 0)
    problems = []
    for rl in lines:
        got = computed.get((_gkey(rl.group), rl.component_id))
        if got is None or abs(got - float(format_expression(rl.quantity))) > 1e-3:
            problems.append(f"{rl.group} / {rl.component_id} {rl.component_name}: "
                            f"pushed {format_expression(rl.quantity)}, CarbonSpace shows {got}")
    return problems
