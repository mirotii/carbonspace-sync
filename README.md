# cs_sync — Revit schedules → CarbonSpace

Send the quantities from a Revit schedule straight into a CarbonSpace calculation, so you don't have to type them in by hand. When the design changes, export the schedule again and push again: only the lines that changed get updated.

```
Revit schedule (.txt)  ──►  cs_sync push  ──►  CarbonSpace calculation  ──►  cs_sync report
```

---

## How it works

1. In Revit, every wall, roof or floor type has a **`CS_ComponentId`** type parameter. It holds the CarbonSpace component number, for example `80460` for *Project A WT.01a*.
2. Your schedule includes that parameter as a column, together with Area, Volume and Count.
3. `cs_sync` reads the schedule and checks each id in CarbonSpace. It uses the column that matches the component's unit (m² → Area, m³ → Volume, pcs → Count) and writes the quantities into the calculation.

The tool **never creates CarbonSpace components**. It only refers to components that already exist in the CarbonSpace libraries or in **UW Library** (project 1302). If a build-up is missing, create it once in UW Library in the web app, then use its id.

---

## One-time setup (≈10 minutes)

### 1. Install Python
Download Python 3.12 from <https://www.python.org/downloads/>. **Tick "Add python.exe to PATH"** during install.
Then open *PowerShell* in this folder and run:
```
python -m pip install -r requirements.txt
```

### 2. Get your API key (for reading)
1. Open <https://api.public.carbonspace.earth/swagger>, click **Authorize**. Under *Auth0*, tick **openid, profile, email**, click Authorize and log in with your CarbonSpace account.
2. Under **User**, open `GET /api/User/GetMe` → *Try it out* → *Execute*. Note the `"Id"` number.
3. Open `GET /api/User/GenerateApiKey`, enter that Id, *Execute*. Copy the result (`you@unknown.works|…`), **without** the quotes.
4. Run:
   ```
   python cs_sync.py setup --from-clipboard
   ```

The key is saved in Windows Credential Manager on your own PC. **Use your own key. Never share it, and never paste it into files or chat.** Anyone who has it can read every project you can.

### 3. Check it works
```
python cs_sync.py check
```
You should see your name and the projects you can access.

---

## Every time you push

### A. Export the schedule from Revit
- Use a schedule with the columns **CS_ComponentId, Family and Type, Area, Volume, Count**. Other column names can be set in `cs_sync.json`; see *Settings*.
- *Export → Reports → Schedule*, and **save as .txt** (tab-separated). The `.csv` route also works, but Revit doesn't quote names containing commas, so `.txt` is safer.
- You can push several schedules together, for example walls + roofs + floors.

### B. Log in for writing (once a day)
CarbonSpace only accepts changes from a web login, and a login lasts 24 hours.
```
python cs_sync.py login
```
Follow the three steps it prints: log in at public.carbonspace.earth, paste one line into the browser console (F12), then press Enter.

### C. Check the mapping
```
python cs_sync.py map --csv "Wall Schedule.txt" "Roof Schedule.txt"
```
This lists every Revit type with the CarbonSpace component and the quantity it will use. Any type without a `CS_ComponentId` gets up to 5 suggestions to choose from. Your choice is saved in `material_map.csv`.

**Reusing what is already in CarbonSpace.** When a type has no `CS_ComponentId`, `map` looks in UW Library for a component with the same type code. For example, `Basic Wall: WT.01a Bench` is matched to *Project A WT.01a*. Add `--from-calc <id>` to copy the calculation groups from a calculation someone already built by hand:
```
python cs_sync.py map --csv "Wall Schedule.txt" --from-calc 1307
```
Automatic matches are marked `auto (name)`. Check them, then copy the ids into `CS_ComponentId` in Revit to make them permanent.

### D. Preview, then push
```
python cs_sync.py push --csv "Wall Schedule.txt" "Roof Schedule.txt" --project 1303 --calc 1307 --dry-run
python cs_sync.py push --csv "Wall Schedule.txt" "Roof Schedule.txt" --project 1303 --calc 1307
```
- To create a new design option instead, use `--name "Option B"` in place of `--calc`.
- The preview shows each line as one of:
  - **CREATE** – a new line will be added
  - **UPDATE** – the quantity will change
  - **KEEP** – nothing changes
  - **STALE** – the line came from an earlier push, but the type is no longer in the schedule
  - **MANUAL** – the line was entered by hand. The tool never touches it.
- To remove STALE lines, add `--prune`.
- Nothing is written until you type `y` (or pass `--yes`).

### E. Results
```
python cs_sync.py report --calc 1307 --out results.csv
python cs_sync.py compare --calc 1307 1308 --by layer
```
`report` gives the A–C total in kgCO2e (and per m² when GFA is set in CarbonSpace), broken down by shearing layer, by component and by material.

---

## The mapping table (`material_map.csv`)

You only need it for types that have **no** `CS_ComponentId` in Revit, or to change *where* a line goes. Open it in Excel, edit it, and save it as CSV.

| column | what it does |
|---|---|
| `revit_name` | exactly as in the schedule, e.g. `Basic Wall: WT.02` |
| `carbonspace_component_id` | used when the Revit parameter is empty (the Revit value always wins) |
| `group` | calculation group, e.g. `Separating walls`. Empty → chosen from the schedule title (Wall → *Facade opaque*, Roof → *Roofs*, Floor → *Floors* …) |
| `quantity_basis` | leave empty (it follows the component unit) unless you need to force `area` / `volume` / `count` / `length` |
| `factor` | multiplier. Use the density (e.g. `7850`) with `quantity_basis=volume` for components measured in kg |

`carbonspace_name` and `unit` are filled in by the tool for reference. See `material_map.example.csv`.

> **Tip:** internal walls should go in *Separating walls*, not *Facade opaque*. Set the `group` column once per type and it is remembered.

---

## Settings (`cs_sync.json`, optional)

Put this file next to your schedules to change the defaults for one project:
```json
{
  "template_calculation_id": 1915,
  "group_by_schedule": {"wall": "Facade opaque", "roof": "Roofs"},
  "columns": {"name": ["Type Mark", "Family and Type"]}
}
```
- `template_calculation_id`: new calculations (`--name`) are copied from this one, with its groups, GFA and any fixed lines.
- `library_project_id` (default 1302) and `search_project_ids` control where `map` looks for suggestions.

---

## Good to know

- **Lines written by the tool** carry a comment starting with `cs_sync:`. That is how the tool recognises its own lines. Hand-entered lines are never deleted.
- **Rounding:** Revit exports rounded values (e.g. `48.84 m³`), so totals can differ from the Revit total row by about 0.01.
- **Log:** every push is recorded in `%APPDATA%\CarbonSpaceSync\push-log.jsonl`: time, calculation and what was written.
- **Errors:**
  - `401` → run `login` again (the token expired).
  - `403` → you have no write access to that project.
  - `not found` → wrong component id.

### Shared "service account"?
Not recommended. One key or login shared by everyone cannot be revoked for a single person, and every change looks as if one person made it. If the practice wants one anyway, IT should own the account, keep the key in a password manager and rotate it (`GenerateApiKey`) whenever someone leaves.

---

## For developers
- `python -m pytest` runs the unit tests (CSV parsing, units, mapping, push planning).
- API findings and quirks are in `docs/api-notes.md`. In short: the API key is read-only; writes need the web access token; new lines are only evaluated after an inventory update.
- Stage B (pyRevit buttons reading quantities directly from the model) will reuse the `carbonspace` package.
