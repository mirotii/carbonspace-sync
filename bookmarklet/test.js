// Tests for the bookmarklet core. `node test.js` runs offline parsing tests;
// `node test.js --live` also pushes into a temporary copy in test project 1646.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const cs = require('./cs-sync.js');

const samples = path.join(__dirname, '..', 'tests', 'samples');
const load = f => cs.parseSchedule(cs.decode(new Uint8Array(fs.readFileSync(path.resolve(samples, f)))), path.basename(f));

// Parsing: TXT and CSV of the same schedule agree, comma-in-name is repaired, totals row dropped.
for (const f of ['Wall Type Test.txt', 'Wall Type Test.csv']) {
  const s = load(f);
  assert.strictEqual(s.title, 'Wall Schedule 3');
  assert.strictEqual(s.types.length, 12, f);
  assert.ok(s.types.some(t => t.name === 'Basic Wall: WT.03_Plywood Partition, Non-Structural Bathroom'), f);
  const a = s.types.find(t => t.name === 'Basic Wall: WT.01a');
  assert.deepStrictEqual([a.area, a.volume, a.count], [148, 61.55, 9]);
  assert.strictEqual(s.types.reduce((x, t) => x + t.area, 0), 459);
}
assert.strictEqual(load('Roof Type Test.csv').types.length, 3);
// Edited in Excel: trailing empty column in the header, comma inside unquoted names.
const xl = cs.parseSchedule([
  'Wall Schedule 3,,,,,', 'CS_ComponentId,Family and Type,Area,Volume,Count,', ',,,,,',
  '80460,Basic Wall: WT.01a,112 m²,48.84 m³,9,',
  '80478,Basic Wall: WT.03_Plywood Partition, Non-Structural,21 m²,2.93 m³,4',
  ',,334 m²,99.73 m³,,', ''].join(String.fromCharCode(13, 10)), 'xl.csv');
assert.deepStrictEqual(xl.types.map(t => [t.id, t.name, t.area, t.count]), [[80460, 'Basic Wall: WT.01a', 112, 9], [80478, 'Basic Wall: WT.03_Plywood Partition, Non-Structural', 21, 4]]);
for (const [t, v] of [['24.53 m³', 24.53], ['1,200.00 m²', 1200], ['1.200,50 m²', 1200.5], ['24,53', 24.53], ['', null], ['100 SF', 9.290304]]) {
  const got = cs.parseQuantity(t);
  assert.ok(v === null ? got === null : Math.abs(got - v) < 1e-9, `${t} -> ${got}`);
}
// UTF-16 export with ids
const u16 = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from('"S"\n"CS_ComponentId"\t"Type"\t"Area"\n"80460"\t"Basic Wall: X"\t"5 m²"\n', 'utf16le')]);
const s16 = cs.parseSchedule(cs.decode(new Uint8Array(u16)), 'u16.txt');
assert.deepStrictEqual([s16.types[0].id, s16.types[0].area], [80460, 5]);

// Planning: existing component updated in its own group; new component goes to schedule group.
const comps = { 80460: { Name: 'Project A WT.01a', ComponentUnit: 'm2' }, 77303: { Name: 'Timber frame inner wall', ComponentUnit: 'm2' }, 80470: { Name: 'RT.01', ComponentUnit: 'm2' } };
const calcSet = {
  Calculation: { Id: 1, ProjectId: 1, Name: 'x' },
  CalculationGroups: [{ Id: 10, Name: 'Walls Buildup' }, { Id: 11, Name: 'Roof ' }],
  CalculationGroupComponents: [
    { Id: 100, ComponentId: 80460, CalculationGroupId: 10, Expression: '145', Comment: null },
    { Id: 101, ComponentId: 80470, CalculationGroupId: 11, Expression: '98', Comment: 'cs_sync: Basic Roof: Roof Model' },
    { Id: 102, ComponentId: 80481, CalculationGroupId: 10, Expression: '22', Comment: 'cs_sync: Basic Wall: WT.05' },
  ],
};
const sched = { title: 'Wall Schedule 3', file: 'w.txt', warnings: [], types: [
  { name: 'Basic Wall: WT.01a', id: 80460, area: 112 }, { name: 'Basic Wall: WT.01a Bench', id: 80460, area: 11 },
  { name: 'Basic Wall: WT.06', id: 77303, area: 6 }, { name: 'Basic Wall: WT.99', id: null, area: 2 }] };
let plan = cs.buildPlan([sched], comps, calcSet);
const kinds = plan.actions.map(a => `${a.kind}:${a.want.id}:${a.group}:${a.newExpr || ''}`);
// Roof line from an earlier push is not touched by a wall-only push; the old wall line is stale.
assert.deepStrictEqual(kinds, ['update:80460:Walls Buildup:123', 'create:77303:Facade opaque:6', 'stale:80481:Walls Buildup:']);
assert.strictEqual(plan.missing.length, 1);
plan = cs.buildPlan([sched], comps, calcSet, { prune: true });
assert.ok(plan.actions.some(a => a.kind === 'delete' && a.line.Id === 102));
assert.ok(!plan.actions.some(a => a.line && a.line.Id === 101), 'roof line must be left alone');
// Matching Revit type names to components by type code.
assert.deepStrictEqual(cs.typeCodes('uw01 300mm').map(c => c.full), ['uw1']);
assert.deepStrictEqual(cs.typeCodes('Project A WT.01a').map(c => c.full), ['wt1a']);
assert.strictEqual(cs.matchScore('Basic Wall: uw01 300mm', 'UW.01'), 100);
assert.strictEqual(cs.matchScore('Basic Wall: UW-01 ext', 'UW.01 External wall'), 100);
assert.strictEqual(cs.matchScore('Basic Wall: WT.01a Bench', 'Project A WT.01a'), 100);
assert.strictEqual(cs.matchScore('Basic Wall: WT.03_Plywood Partition', 'Project A WT.03a'), 60);
assert.strictEqual(cs.matchScore('Basic Wall: WT.01a', 'Project A WT.01b'), 60);
assert.strictEqual(cs.matchScore('Basic Wall: UW.02', 'UW.01'), 0);
assert.strictEqual(cs.matchScore('Basic Roof: Green roof', 'Project A Green Roof'), 40);
const LIB = [{ Id: 80460, Name: 'Project A WT.01a' }, { Id: 80462, Name: 'Project A WT.01b' }, { Id: 80478, Name: 'Project A WT.03a' }, { Id: 80479, Name: 'Project A WT.03b' }, { Id: 1, Name: 'UW.01' }];
assert.deepStrictEqual(cs.suggest('Basic Wall: WT.03_Plywood', LIB).map(s => s.comp.Id), [80478, 80479]);

// Where ids come from: chosen > Revit > remembered (calc comments) > saved > unique name match.
const wall = load('Wall Type Test.txt');
const remembered = cs.rememberedFromCalc({ CalculationGroupComponents: [{ ComponentId: 80462, Comment: 'cs_sync: Basic Wall: WT.01b; Basic Wall: X' }] });
assert.strictEqual(remembered['basic wall: wt.01b'], 80462);
const [res] = cs.resolveIds([wall], { library: LIB, remembered, saved: { 'basic wall: wt.02': 1 }, chosen: { 'basic wall: wt.05 - 60min compartment line': 0 } });
const src = n => res.types.find(t => t.name === n);
assert.deepStrictEqual([src('Basic Wall: WT.01a').id, src('Basic Wall: WT.01a').src], [80460, 'auto']);
assert.deepStrictEqual([src('Basic Wall: WT.01b').id, src('Basic Wall: WT.01b').src], [80462, 'remembered']);
assert.deepStrictEqual([src('Basic Wall: WT.02').id, src('Basic Wall: WT.02').src], [1, 'saved']);
assert.deepStrictEqual([src('Basic Wall: WT.03_Plywood Partition, Non-Structural').id, src('Basic Wall: WT.03_Plywood Partition, Non-Structural').src], [null, null]);
assert.deepStrictEqual([src('Basic Wall: WT.05 - 60min compartment line').id, src('Basic Wall: WT.05 - 60min compartment line').src], [null, 'skipped']);

// Export writes ids back into the same file layout; parsing it again gives the same ids.
for (const f of ['Wall Type Test.txt', 'Wall Type Test.csv']) {
  const s = load(f);
  const [r] = cs.resolveIds([s], { library: LIB });
  const again = cs.parseSchedule(cs.exportWithIds(s, r.types), f);
  assert.deepStrictEqual(again.types.map(t => [t.name, t.id, t.area]), r.types.map(t => [t.name, t.id, t.area]), f);
}
// A schedule without an id column gets one added.
const noId = cs.parseSchedule('S\nFamily and Type,Area\nBasic Wall: WT.01b,5 m²\n', 'n.csv');
const [noIdRes] = cs.resolveIds([noId], { library: LIB });
assert.strictEqual(cs.parseSchedule(cs.exportWithIds(noId, noIdRes.types), 'n.csv').types[0].id, 80462);
// Folders: tree paths and components inside a folder (with subfolders).
const folders = [{ Id: 1, ProjectId: 1302, Name: 'UW Components', ParentId: null }, { Id: 2, ProjectId: 1302, Name: 'Project A', ParentId: 1 },
  { Id: 3, ProjectId: 1302, Name: 'Project C', ParentId: 1 }, { Id: 4, ProjectId: 1302, Name: 'UW Materials', ParentId: null }];
const tree = cs.folderTree(folders, { 1302: 'UW Library' });
assert.deepStrictEqual(tree.map(f => [f.path, f.depth]), [['UW Library / UW Components', 0], ['UW Library / UW Components / Project A', 1], ['UW Library / UW Components / Project C', 1], ['UW Library / UW Materials', 0]]);
const flib = [{ Id: 10, Name: 'WT.01a', FolderId: 2 }, { Id: 11, Name: 'WT.01a', FolderId: 3 }, { Id: 12, Name: 'Timber', FolderId: 4 }];
assert.deepStrictEqual(cs.inFolder(flib, folders, 1).map(c => c.Id), [10, 11]);
assert.deepStrictEqual(cs.inFolder(flib, folders, 2).map(c => c.Id), [10]);
assert.strictEqual(cs.inFolder(flib, folders, null).length, 3);
// Same code in two project folders: only the selected folder's component is auto-matched.
const two = { title: 'W', file: 'w', warnings: [], types: [{ name: 'Basic Wall: WT.01a', id: null, area: 1, rowIdx: [] }] };
assert.strictEqual(cs.resolveIds([two], { library: flib })[0].types[0].id, null, 'ambiguous across folders');
assert.strictEqual(cs.resolveIds([two], { library: cs.inFolder(flib, folders, 3) })[0].types[0].id, 11);
console.log('offline tests passed');

// ---------------------------------------------------------------- live
if (process.argv.includes('--live')) (async () => {
  const api = cs.makeApi(() => process.env.CARBONSPACE_TOKEN);
  const copy = await api('POST', '/api/Calculation/Copy/1307', undefined, [['targetProject', 1646], ['name', 'zz bookmarklet live test (temp)']]);
  try {
    const set = await api('GET', '/api/Calculation/GetCalculationSetById/' + copy.Id);
    const wall = load(process.argv[process.argv.indexOf('--live') + 1]);
    const ids = [...new Set([...wall.types.map(t => t.id).filter(Boolean), ...set.CalculationGroupComponents.map(l => l.ComponentId)])];
    const components = await cs.loadComponents(api, ids);
    const p = cs.buildPlan([wall], components, set);
    console.log('plan:', p.actions.filter(a => a.kind !== 'keep').map(a => `${a.kind} ${a.want.name} ${a.oldExpr || ''}->${a.newExpr || ''}`).join(' | '));
    const before = cs.totals(await api('GET', '/api/Bom/GetByCalculation/' + copy.Id));
    const done = await cs.applyPlan(api, set, p, () => {});
    const bom = await api('GET', '/api/Bom/GetByCalculation/' + copy.Id);
    const after = cs.totals(bom);
    const q = {}; for (const l of bom.CalculationLines) q[l.TopComponentId] = l.TopQuantity;
    for (const a of p.actions.filter(a => a.kind === 'create' || a.kind === 'update')) {
      assert.ok(Math.abs(q[a.want.id] - parseFloat(a.newExpr)) < 1e-6 || set.CalculationGroupComponents.filter(l => l.ComponentId === a.want.id).length > 1, `quantity of ${a.want.name}: ${q[a.want.id]} vs ${a.newExpr}`);
    }
    console.log('done', done, `A-C ${before.AC.toFixed(0)} -> ${after.AC.toFixed(0)}`);
    console.log('live test passed');
  } finally {
    await api('DELETE', '/api/Calculation/Delete/' + copy.Id);
    console.log('deleted temp calc', copy.Id);
  }
})().catch(e => { console.error('LIVE FAIL', e); process.exit(1); });
