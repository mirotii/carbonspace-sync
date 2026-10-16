"""cs_sync - push Revit schedule quantities into CarbonSpace calculations.

Commands: setup, login, check, map, push, report, compare. Run `cs_sync <command> -h`.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import re
import sys

from datetime import datetime, timezone
from pathlib import Path

from carbonspace import auth
from carbonspace.client import CarbonSpaceClient, CarbonSpaceError
from carbonspace.mapping import (MapEntry, _key, default_group, groups_in_calculation, library_matches,
                                 load_map, resolve, save_map, unmapped_share)
from carbonspace.push import apply_plan, build_plan, verify
from carbonspace.report import compare as compare_boms
from carbonspace.report import gfa, summarise
from carbonspace.takeoff import Takeoff, TakeoffRow, aggregate, parse_takeoff
from carbonspace.units import basis_from_unit, format_expression

DEFAULT_MAP = "material_map.csv"


# ---------------------------------------------------------------- helpers
def table(rows: list[list], headers: list[str], widths: list[int] | None = None) -> None:
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = widths or [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    widths = [min(w, 60) for w in widths]

    def fmt(r):
        return "  ".join((v if len(v) <= w else v[: w - 1] + "…").ljust(w) for v, w in zip(r, widths))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print(fmt(r))


def num(v: float, digits: int = 0) -> str:
    return f"{v:,.{digits}f}"


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Not an interactive terminal; add --yes to proceed.")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def make_client(need_write: bool = False) -> CarbonSpaceClient:
    key = auth.get_api_key()
    token = auth.get_token()
    left = auth.token_seconds_left(token)
    if token and left <= 60:
        token = None
    if need_write and not token:
        raise CarbonSpaceError("Writing needs a fresh login token (they last 24 h). Run:  cs_sync login")
    if not key and not token:
        raise CarbonSpaceError("No API key stored. Run:  cs_sync setup")
    # The client reads with the API key and only falls back to the token when needed.
    return CarbonSpaceClient(api_key=key, token=token)


def load_takeoffs(paths: list[str], cfg: dict) -> list[tuple[Takeoff, list[TakeoffRow]]]:
    result = []
    for p in paths:
        t = parse_takeoff(p, cfg.get("columns") or None)
        rows, warns = aggregate(t.rows)
        t.warnings += warns
        print(f"{t.source}: '{t.title}', {len(t.rows)} rows -> {len(rows)} types "
              f"(columns: {', '.join(f'{k}={v}' for k, v in t.columns.items())})")
        for w in t.warnings:
            print(f"  ! {w}")
        result.append((t, rows))
    return result


def strip_family(name: str) -> str:
    """'Basic Wall: WT.01a Bench' -> 'WT.01a Bench'."""
    return name.split(":", 1)[1].strip() if ":" in name else name.strip()


def log_push(entry: dict) -> Path:
    p = auth.config_dir() / "push-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return p


# ---------------------------------------------------------------- commands
def cmd_setup(args) -> int:
    print("Paste your CarbonSpace API key (format: you@email.com|xxxx). It will not be shown.")
    key = (auth.read_clipboard() if args.from_clipboard else getpass.getpass("API key: ")).strip().strip('"')
    if "|" not in key:
        print("That does not look like a CarbonSpace API key (expected email|key).")
        return 1
    me = CarbonSpaceClient(api_key=key).get_me()
    where = auth.store_secret("api_key", key)
    print(f"OK - key for {me.get('Email')} stored in {where}.")
    return 0


def cmd_login(args) -> int:
    print("1. Open https://public.carbonspace.earth and log in.")
    print("2. Press F12 -> Console, paste this line and press Enter:\n")
    print("   " + auth.TOKEN_SNIPPET + "\n")
    print("3. Come back here (do not copy anything else).")
    if sys.stdin.isatty() and not args.no_wait:
        input("Press Enter when done... ")
    token = auth.read_clipboard().strip().strip('"')
    if not token.startswith("eyJ") or token.count(".") != 2:
        print("The clipboard does not contain a login token. Repeat step 2 and try again.")
        return 1
    hours = auth.token_seconds_left(token) / 3600
    if hours <= 0:
        print("That token has already expired. Repeat step 2.")
        return 1
    me = CarbonSpaceClient(token=token).get_me()
    where = auth.store_secret("token", token)
    print(f"OK - logged in as {me.get('Email')}; token valid for {hours:.1f} h, stored in {where}.")
    return 0


def cmd_check(args) -> int:
    client = make_client()
    me = client.get_me()
    print(f"API key OK: {me.get('Name')} <{me.get('Email')}> (user {me.get('Id')})")
    left = auth.token_seconds_left(auth.get_token())
    print(f"Login token: {'valid for %.1f h' % (left / 3600) if left > 0 else 'missing/expired - run cs_sync login before push'}")
    writable = set(me.get("WriteProjectIds") or [])
    rows = [[p["Id"], p["Name"], "library" if p.get("IsLibrary") else "", "write" if p["Id"] in writable else "read"]
            for p in client.projects()]
    print()
    table(rows, ["Id", "Project", "Type", "Access"])
    return 0


def _candidates(client: CarbonSpaceClient, row: TakeoffRow, cfg: dict, library: list[dict]) -> list[dict]:
    """Up to 5 likely components: company-library components carrying the same
    type code first (e.g. 'WT.03' -> WT.03a, WT.03b), then a text search."""
    exact, partial = library_matches(row.name, library)
    found: list[dict] = exact + partial
    short = strip_family(row.name)
    words = re.sub(r"[^A-Za-z ]+", " ", short).split()
    for q in [short] + ([" ".join(words[:2])] if words else []):
        if len(found) >= 5:
            break
        try:
            found += client.search_components(q, cfg.get("search_project_ids"))
        except CarbonSpaceError:
            pass
    unique, ids = [], set()
    for c in found:
        if c["Id"] not in ids:
            ids.add(c["Id"])
            unique.append(c)
    return unique[:5]


def cmd_map(args) -> int:
    cfg = auth.load_config()
    client = make_client()
    takeoffs = load_takeoffs(args.csv, cfg)
    entries = load_map(args.map)
    interactive = sys.stdin.isatty() and not args.no_interactive
    projects = {p["Id"]: p["Name"] for p in client.projects()}

    # Components already built in the company library, matched by type code.
    try:
        library = client.components_by_project(cfg["library_project_id"])
    except CarbonSpaceError as e:
        library = []
        print(f"  ! Could not list the company library ({e}); automatic matching is off.")
    # Groups used in an existing calculation (e.g. one entered by hand) seed the map.
    ref_groups: dict[int, str] = {}
    if args.from_calc:
        ref_set = client.calculation_set(args.from_calc)
        ref_groups = groups_in_calculation(ref_set)
        print(f"Using groups from calculation {args.from_calc} '{ref_set['Calculation']['Name']}' "
              f"({len(ref_groups)} components)")

    rows_out, unmapped, all_rows = [], [], []
    for t, rows in takeoffs:
        group = default_group(t, cfg)
        for r in rows:
            all_rows.append(r)
            e = entries.get(_key(r.name))
            is_new = e is None
            if is_new:
                e = entries[_key(r.name)] = MapEntry(revit_name=r.name, group=group)
            cid = r.component_id or e.carbonspace_component_id
            source = "Revit" if r.component_id else ("map" if cid else "")
            if not cid:
                exact, _ = library_matches(r.name, library)
                if len(exact) == 1:
                    cid, source = exact[0]["Id"], "auto (name)"
            if not cid and interactive:
                cands = _candidates(client, r, cfg, library)
                print(f"\n'{r.name}'  (area {num(r.area, 1)} m², volume {num(r.volume, 2)} m³, count {num(r.count)})")
                for i, c in enumerate(cands, 1):
                    comp = client.component(c["Id"]) or {}
                    used = f"  (in calc {args.from_calc}: {ref_groups[c['Id']]})" if c["Id"] in ref_groups else ""
                    print(f"  {i}. {c['Id']}  {c.get('Name')}  [{comp.get('ComponentUnit') or '?'}]"
                          f"  - {projects.get(c.get('ProjectId'), c.get('ProjectId'))}{used}")
                ans = input("  Choose 1-5, type a component id, Enter = skip, q = stop asking: ").strip().lower()
                if ans == "q":
                    interactive = False
                elif ans.isdigit():
                    pick = int(ans)
                    cid = cands[pick - 1]["Id"] if 1 <= pick <= len(cands) else pick
                    source = "chosen"
            if not cid:
                unmapped.append(r)
                rows_out.append([r.name, "", "UNMAPPED", "", "", e.group])
                continue
            comp = client.component(cid)
            if comp is None:
                rows_out.append([r.name, cid, "NOT FOUND in CarbonSpace", "", source, e.group])
                continue
            if not r.component_id:
                e.carbonspace_component_id = cid
            if cid in ref_groups and (is_new or args.overwrite_groups):
                e.group = ref_groups[cid]
            e.carbonspace_name = comp.get("Name") or ""
            e.unit = comp.get("ComponentUnit") or ""
            basis = e.quantity_basis or basis_from_unit(e.unit) or "?"
            qty = r.quantity(basis) * e.factor if basis in ("area", "volume", "count", "length") else None
            rows_out.append([r.name, cid, f"{e.carbonspace_name} [{e.unit}]",
                             f"{basis} = {format_expression(qty) if qty is not None else '?'}", source, e.group])

    save_map(args.map, entries)
    print()
    table(rows_out, ["Revit type", "Component", "CarbonSpace name [unit]", "Quantity", "Id from", "Group"])
    print(f"\nMap saved: {Path(args.map).resolve()}  (edit the 'group' column to choose calculation groups)")
    auto = [row for row in rows_out if row[4] == "auto (name)"]
    if auto:
        print(f"{len(auto)} type(s) were matched automatically by name - check them, or put the ids into "
              f"CS_ComponentId in Revit to make them permanent.")
    if unmapped:
        print(f"\n{len(unmapped)} type(s) still UNMAPPED - fill CS_ComponentId in Revit or carbonspace_component_id in the map:")
        for r, sa, sv in unmapped_share(unmapped, all_rows):
            print(f"  - {r.name}: {sa:.1%} of area, {sv:.1%} of volume")
        return 2
    return 0


def cmd_push(args) -> int:
    cfg = auth.load_config()
    if not args.calc and not (args.name and args.project):
        print("Give --calc <id> to update a calculation, or --name \"Option A\" --project <id> to create one.")
        return 1
    client = make_client()
    takeoffs = load_takeoffs(args.csv, cfg)
    entries = load_map(args.map)
    res = resolve(takeoffs, entries, client.component, cfg, args.group)
    for w in res.warnings:
        print(f"  ! {w}")
    if res.unmapped or res.errors:
        all_rows = [r for _, rows in takeoffs for r in rows]
        for r, sa, sv in unmapped_share(res.unmapped, all_rows):
            print(f"  x UNMAPPED '{r.name}': {sa:.1%} of area, {sv:.1%} of volume")
        for e in res.errors:
            print(f"  x {e}")
        print("\nNothing was written. Fix the items above (run: cs_sync map --csv ...) and try again.")
        return 2

    calc_set = None
    if args.calc:
        calc_set = client.calculation_set(args.calc)
        calc = calc_set["Calculation"]
        if args.project and calc["ProjectId"] != args.project:
            print(f"Calculation {args.calc} belongs to project {calc['ProjectId']}, not {args.project}.")
            return 1
        args.project = calc["ProjectId"]
        print(f"\nTarget: calculation {args.calc} '{calc['Name']}' in project {args.project}")
    else:
        tpl = cfg.get("template_calculation_id")
        if tpl:
            calc_set = client.calculation_set(tpl)
        print(f"\nTarget: NEW calculation '{args.name}' in project {args.project}"
              + (f" (copy of template {tpl})" if tpl else " (CarbonSpace default groups)"))

    plan = build_plan(calc_set, res.lines, prune=args.prune)
    for a in plan.actions:
        if not a.component_name:
            comp = client.component(a.component_id) or {}
            a.component_name, a.unit = comp.get("Name") or "", a.unit or comp.get("ComponentUnit") or ""
    rows = [[a.kind.upper(), a.group, f"{a.component_id} {a.component_name}",
             "; ".join(strip_family(n) for names in a.revit_names for n in names.split("; ")),
             a.old or "", a.new or "", a.unit] for a in plan.actions]
    table(rows, ["Action", "Group", "Component", "Revit types", "Old", "New", "Unit"])
    if plan.new_groups and calc_set is not None:
        print(f"\nNew groups to create: {', '.join(plan.new_groups)}")
    print(f"\n{plan.count('create')} new, {plan.count('update')} changed, {plan.count('keep')} unchanged, "
          f"{plan.count('delete')} to delete, {plan.count('manual')} hand-entered lines left alone.")
    if plan.count("stale"):
        print(f"{plan.count('stale')} line(s) written by an earlier push are not in this export (STALE). "
              f"Add --prune to delete them.")

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0
    if not plan.has_writes and args.calc:
        print("\nNothing to change.")
        return 0
    if not confirm("\nWrite these changes to CarbonSpace?", args.yes):
        print("Cancelled.")
        return 1

    writer = make_client(need_write=True)
    if args.calc:
        calc_id = args.calc
    else:
        tpl = cfg.get("template_calculation_id")
        new = writer.copy_calculation(tpl, args.project, args.name) if tpl else writer.create_calculation(args.project, args.name)
        calc_id = new["Id"]
        print(f"Created calculation {calc_id}")
        plan = build_plan(writer.calculation_set(calc_id), res.lines, prune=args.prune)
    done = apply_plan(writer, calc_id, args.project, plan)
    problems = verify(writer, calc_id, res.lines)
    path = log_push({
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": args.project, "calculation": calc_id, "files": args.csv, "done": done,
        "lines": [{"group": l.group, "component": l.component_id, "quantity": format_expression(l.quantity),
                   "basis": l.basis, "revit": l.revit_names} for l in res.lines],
        "problems": problems,
    })
    print(f"\nDone: {done}. Calculation {calc_id}: https://public.carbonspace.earth/projects/{args.project}")
    if problems:
        print("Check these lines in CarbonSpace:")
        for p in problems:
            print(f"  ! {p}")
    print(f"Logged to {path}")
    return 3 if problems else 0


def cmd_library(args) -> int:
    """List a library's components with their ids, for filling CS_ComponentId in Revit."""
    cfg = auth.load_config()
    client = make_client()
    project = args.project or cfg["library_project_id"]
    rows = []
    for c in sorted(client.components_by_project(project), key=lambda c: c.get("Name") or ""):
        stage = {k: (c.get(f"{k}RangeValue") or {}).get("Min", 0) for k in "ABC"}
        rows.append({"CS_ComponentId": c["Id"], "Name": (c.get("Name") or "").strip(),
                     "Unit": c.get("ComponentUnit") or "",
                     # The web app shows stage A (upfront) per unit; A-C adds use and end of life.
                     "kgCO2e_A_per_unit": round(stage["A"], 1),
                     "kgCO2e_A_to_C_per_unit": round(sum(stage.values()), 1),
                     "Type": ["Simple", "Nested", "Range"][c.get("Type", 0)] if c.get("Type") in (0, 1, 2) else ""})
    table([[r["CS_ComponentId"], r["Name"], r["Unit"], r["kgCO2e_A_per_unit"], r["kgCO2e_A_to_C_per_unit"]] for r in rows],
          ["CS_ComponentId", "Name", "Unit", "kgCO2e/unit A (as web)", "A-C"])
    if args.out:
        _write_csv(args.out, rows)
        print(f"\nSaved {args.out}")
    return 0


def _write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def cmd_report(args) -> int:
    client = make_client()
    bom = client.bom(args.calc)
    calc = client.calculation_set(args.calc)["Calculation"]
    area = gfa(bom)
    total = summarise(bom, "total")[0]
    print(f"Calculation {args.calc} '{calc['Name']}'  GFA {num(area)} m²")
    print(f"  A1-A5 {num(total['CO2A'])}  B {num(total['CO2B'])}  C {num(total['CO2C'])}  "
          f"=> A-C {num(total['CO2_A_to_C'])} kgCO2e" + (f"  ({num(total['CO2_A_to_C'] / area, 1)} kgCO2e/m²)" if area else ""))
    print(f"  D (beyond life cycle) {num(total['CO2D'])}   biogenic {num(total['CO2Biogenic'])}")
    out_rows = []
    for by in (["layer", "component", "material"] if args.by == "all" else [args.by]):
        rows = summarise(bom, by)
        labels = [k for k in rows[0] if k not in ("CO2A", "CO2B", "CO2C", "CO2D", "CO2_A_to_C", "CO2Biogenic", "Weight_kg")] if rows else []
        print(f"\nBy {by}:")
        table([[r[k] for k in labels] + [num(r["CO2_A_to_C"]), f"{r['CO2_A_to_C'] / total['CO2_A_to_C']:.0%}" if total["CO2_A_to_C"] else ""]
               for r in rows], labels + ["A-C kgCO2e", "share"])
        out_rows += [{"breakdown": by, **r} for r in rows]
    if args.out:
        _write_csv(args.out, out_rows)
        print(f"\nSaved {args.out}")
    return 0


def cmd_compare(args) -> int:
    client = make_client()
    a, b = args.calc
    bom_a, bom_b = client.bom(a), client.bom(b)
    ta, tb = summarise(bom_a, "total")[0]["CO2_A_to_C"], summarise(bom_b, "total")[0]["CO2_A_to_C"]
    print(f"A-C total: {a} = {num(ta)}  vs  {b} = {num(tb)} kgCO2e  ({(tb - ta) / ta:+.1%})" if ta else "")
    ga, gb = gfa(bom_a), gfa(bom_b)
    if ga and gb:
        print(f"Per m² GFA: {num(ta / ga, 1)} vs {num(tb / gb, 1)} kgCO2e/m²")
    rows = compare_boms(bom_a, bom_b, args.by)
    labels = [k for k in rows[0] if k not in ("A_to_C_first", "A_to_C_second", "difference", "change_pct")] if rows else []
    print()
    table([[r[k] for k in labels] + [num(r["A_to_C_first"]), num(r["A_to_C_second"]), num(r["difference"]),
           f"{r['change_pct']:+.0f}%" if r["change_pct"] is not None else "new"] for r in rows],
          labels + [str(a), str(b), "difference", "change"])
    if args.out:
        _write_csv(args.out, rows)
        print(f"\nSaved {args.out}")
    return 0


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    p = argparse.ArgumentParser(prog="cs_sync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="store your CarbonSpace API key (read access)")
    s.add_argument("--from-clipboard", action="store_true", help="read the key from the clipboard")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("login", help="store a login token from the web app (needed to write, lasts 24 h)")
    s.add_argument("--no-wait", action="store_true", help="read the clipboard immediately")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("check", help="verify credentials and list projects")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("map", help="check/complete the mapping of Revit types to CarbonSpace components")
    s.add_argument("--csv", nargs="+", required=True, help="Revit schedule export(s), .csv or .txt")
    s.add_argument("--map", default=DEFAULT_MAP, help=f"mapping table (default {DEFAULT_MAP})")
    s.add_argument("--no-interactive", action="store_true", help="do not ask for unmapped types")
    s.add_argument("--from-calc", type=int, help="take calculation groups from an existing calculation (e.g. one entered by hand)")
    s.add_argument("--overwrite-groups", action="store_true", help="with --from-calc: also replace groups already in the map")
    s.set_defaults(func=cmd_map)

    s = sub.add_parser("library", help="list component ids of a library (to fill CS_ComponentId in Revit)")
    s.add_argument("--project", type=int, help="library project id (default: company library)")
    s.add_argument("--out", help="save the list as CSV")
    s.set_defaults(func=cmd_library)

    s = sub.add_parser("push", help="write quantities into a calculation")
    s.add_argument("--csv", nargs="+", required=True, help="Revit schedule export(s), .csv or .txt")
    s.add_argument("--calc", type=int, help="existing calculation id to update")
    s.add_argument("--project", type=int, help="project id (only needed with --name; taken from --calc otherwise)")
    s.add_argument("--name", help="name for a new calculation (when --calc is not given)")
    s.add_argument("--group", help="put every line in this calculation group")
    s.add_argument("--map", default=DEFAULT_MAP)
    s.add_argument("--prune", action="store_true", help="delete lines from earlier pushes that are no longer in the export")
    s.add_argument("--dry-run", action="store_true", help="preview only, write nothing")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("report", help="carbon results of a calculation")
    s.add_argument("--calc", type=int, required=True)
    s.add_argument("--by", choices=["layer", "component", "material", "all"], default="all")
    s.add_argument("--out", help="save the breakdown as CSV")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("compare", help="compare two calculations (design options)")
    s.add_argument("--calc", type=int, nargs=2, required=True, metavar=("FIRST", "SECOND"))
    s.add_argument("--by", choices=["layer", "component", "material"], default="layer")
    s.add_argument("--out", help="save the comparison as CSV")
    s.set_defaults(func=cmd_compare)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (CarbonSpaceError, ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
