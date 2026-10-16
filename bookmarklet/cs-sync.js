// CarbonSpace Sync bookmarklet - developed by Unknown Works.
// Runs on public.carbonspace.earth and reuses the page's own login, so no key or
// token handling is needed. Reads Revit schedule exports (.txt / .csv) with a
// CS_ComponentId column and writes the quantities into the open calculation.
// The same file is loaded by Node for tests (module.exports = core).
(function () {
  'use strict';
  var API = 'https://api.public.carbonspace.earth';
  var TAG = 'cs_sync';

  // ---------------------------------------------------------------- parsing
  var COLUMNS = {
    id: ['cs_componentid', 'cs_component', 'carbonspace id'],
    name: ['family and type', 'type', 'material: name', 'material', 'family', 'type name'],
    area: ['area', 'material: area'],
    volume: ['volume', 'material: volume'],
    count: ['count'],
    length: ['length']
  };
  var TO_SI = { ft: 0.3048, lf: 0.3048, ft2: 0.09290304, sf: 0.09290304, ft3: 0.028316846592, cf: 0.028316846592, mm: 0.001, cm: 0.01 };
  var BASIS_BY_UNIT = { m3: 'volume', m2: 'area', m: 'length', pcs: 'count', pc: 'count', piece: 'count', pieces: 'count', st: 'count', stk: 'count', nr: 'count', no: 'count', kg: 'weight', t: 'weight', ton: 'weight' };
  var GROUP_BY_SCHEDULE = [['wall', 'Facade opaque'], ['curtain', 'Facade transparent'], ['window', 'Facade transparent'], ['roof', 'Roofs'], ['floor', 'Floors'], ['ceiling', 'Ceilings and raised floors'], ['stair', 'Stairs and ramps'], ['column', 'Columns'], ['framing', 'Beams'], ['beam', 'Beams'], ['foundation', 'Foundation']];

  function decode(bytes) {
    // Revit exports UTF-16 or UTF-8 (with BOM); older files may be cp1252.
    if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder('utf-16le').decode(bytes.subarray(2));
    if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder('utf-16be').decode(bytes.subarray(2));
    if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) return new TextDecoder('utf-8').decode(bytes.subarray(3));
    var nul = 0;
    for (var i = 1; i < Math.min(bytes.length, 40); i += 2) if (bytes[i] === 0) nul++;
    if (nul > 5) return new TextDecoder('utf-16le').decode(bytes);
    try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch (e) { return new TextDecoder('windows-1252').decode(bytes); }
  }

  function sniffDelimiter(text) {
    var lines = text.split(/\r?\n/).filter(function (l) { return l.trim(); }).slice(0, 10).join('\n');
    if (lines.indexOf('\t') >= 0) return '\t';
    return (lines.split(';').length > lines.split(',').length) ? ';' : ',';
  }

  function parseRows(text, delim) {
    var rows = [], row = [], cell = '', q = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (q) {
        if (ch === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += ch;
      } else if (ch === '"') q = true;
      else if (ch === delim) { row.push(cell); cell = ''; }
      else if (ch === '\n') { row.push(cell.replace(/\r$/, '')); rows.push(row); row = []; cell = ''; }
      else cell += ch;
    }
    if (cell || row.length) { row.push(cell.replace(/\r$/, '')); rows.push(row); }
    return rows;
  }

  function normUnit(u) {
    return (u || '').trim().toLowerCase().replace(/²/g, '2').replace(/³/g, '3').replace(/\^2/g, '2').replace(/\^3/g, '3').replace(/\s/g, '');
  }

  function parseQuantity(text) {
    if (text == null) return null;
    text = String(text).trim();
    var m = /[-+]?\d[\d.,   ]*/.exec(text);
    if (!m) return null;
    var s = m[0].trim().replace(/[.,]$/, '').replace(/[   ]/g, '');
    if (s.indexOf(',') >= 0 && s.indexOf('.') >= 0) {
      s = s.lastIndexOf(',') > s.lastIndexOf('.') ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
    } else if (s.indexOf(',') >= 0) {
      s = /^[-+]?\d{1,3}(,\d{3})+$/.test(s) ? s.replace(/,/g, '') : s.replace(',', '.');
    } else if ((s.match(/\./g) || []).length > 1) {
      s = s.replace(/\./g, '');
    }
    var v = parseFloat(s);
    if (isNaN(v)) return null;
    var f = TO_SI[normUnit(text.slice(m.index + m[0].length))];
    return f ? v * f : v;
  }

  function findCol(header, cands) {
    var low = header.map(function (h) { return h.trim().toLowerCase(); });
    for (var i = 0; i < cands.length; i++) { var k = low.indexOf(cands[i]); if (k >= 0) return k; }
    return -1;
  }

  // Parse one schedule export. Returns {title, file, types:[{name,id,badId,area,volume,count,length}], warnings}.
  function parseSchedule(text, file) {
    var delim = sniffDelimiter(text);
    var rows = parseRows(text, delim);
    var hi = -1;
    for (var i = 0; i < Math.min(rows.length, 20); i++) if (findCol(rows[i], COLUMNS.name) >= 0) { hi = i; break; }
    if (hi < 0) throw new Error(file + ': no "Family and Type" (or "Type") column found.');
    // Ignore empty trailing columns (Excel / Revit sometimes add a trailing comma).
    var header = rows[hi].slice();
    while (header.length && !header[header.length - 1].trim()) header.pop();
    var idx = {};
    Object.keys(COLUMNS).forEach(function (k) { idx[k] = findCol(header, COLUMNS[k]); });
    if (idx.area < 0 && idx.volume < 0 && idx.count < 0 && idx.length < 0) throw new Error(file + ': no Area / Volume / Count column found.');
    var title = '';
    for (var t = 0; t < hi && !title; t++) title = rows[t].filter(function (c) { return c.trim(); })[0] || '';
    // rows/delim/idx are kept so the file can be written back with ids filled in.
    var res = { title: title.trim() || file, file: file, types: [], warnings: [], rows: rows, delim: delim, idx: idx, headerRow: hi };
    var byName = {};
    rows.slice(hi + 1).forEach(function (raw, n) {
      var row = raw.slice();
      while (row.length > header.length && !row[row.length - 1].trim()) row.pop();
      raw = row;
      // Revit's CSV export does not quote names with commas; re-join the split name.
      var extra = raw.length - header.length;
      if (delim === ',' && extra > 0 && idx.name >= 0) {
        row = raw.slice(0, idx.name).concat([raw.slice(idx.name, idx.name + extra + 1).join(',')], raw.slice(idx.name + extra + 1));
      }
      rows[hi + 1 + n] = row;
      var cell = function (k) { return idx[k] >= 0 && idx[k] < row.length ? row[idx[k]].trim() : ''; };
      var name = cell('name');
      if (!name) return; // blank, totals, grouping headers
      var idText = cell('id').replace(/\.0$/, ''), id = null, badId = null;
      if (idText) {
        if (/^\d+$/.test(idText)) id = parseInt(idText, 10);
        else { badId = idText; res.warnings.push('"' + name + '": CS_ComponentId "' + idText + '" is not a number.'); }
      }
      var key = name.toLowerCase().replace(/\s+/g, ' ');
      var ty = byName[key];
      if (!ty) { ty = byName[key] = { name: name, id: id, badId: badId, area: 0, volume: 0, count: 0, length: 0, rowIdx: [] }; res.types.push(ty); }
      else if (id && ty.id && id !== ty.id) res.warnings.push('"' + name + '" has two different CS_ComponentId values.');
      ty.id = ty.id || id;
      ty.rowIdx.push(hi + 1 + n);
      ['area', 'volume', 'count', 'length'].forEach(function (q) { ty[q] += parseQuantity(cell(q)) || 0; });
    });
    return res;
  }

  // ---------------------------------------------------------------- matching
  // Type codes like "UW.01", "uw01 300mm", "WT.01a Bench" -> {base:'uw1', full:'uw1'} / {base:'wt1', full:'wt1a'}.
  function typeCodes(text) {
    var re = /(?:^|[^a-z0-9])([a-z]{1,4})[\s._-]*(\d{1,3})([a-z]?)(?![a-z0-9])/gi, out = [], m;
    while ((m = re.exec(text))) {
      var base = m[1].toLowerCase() + parseInt(m[2], 10);
      out.push({ base: base, full: base + m[3].toLowerCase() });
    }
    return out;
  }
  function shortName(n) { var i = n.indexOf(':'); return (i > 0 ? n.slice(i + 1) : n).trim(); }
  function normName(n) { return String(n || '').trim().toLowerCase().replace(/\s+/g, ' '); }

  // How well a CarbonSpace component name fits a Revit type name (0-100).
  function matchScore(typeName, compName) {
    var tc = typeCodes(shortName(typeName))[0];
    if (tc) {
      var best = 0;
      typeCodes(compName || '').forEach(function (cc) {
        if (cc.full === tc.full) best = Math.max(best, 100);
        else if (cc.base === tc.base) best = Math.max(best, 60);
      });
      if (best) return best;
    }
    var a = shortName(typeName).toLowerCase().replace(/[^a-z0-9]/g, ''), b = String(compName || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    return a && b && (a.indexOf(b) >= 0 || b.indexOf(a) >= 0) ? 40 : 0;
  }
  function suggest(typeName, library) {
    return library.map(function (c) { return { comp: c, score: matchScore(typeName, c.Name) }; })
      .filter(function (s) { return s.score > 0; })
      .sort(function (x, y) { return y.score - x.score || String(x.comp.Name).localeCompare(y.comp.Name); });
  }

  // Folders as a flat, indented list: [{id, path, depth, projectId}], parents before children.
  function folderTree(folders, projectNames) {
    var kids = {}, out = [];
    folders.forEach(function (f) { var p = f.ParentId || ('p' + f.ProjectId); (kids[p] = kids[p] || []).push(f); });
    var walk = function (key, path, depth) {
      (kids[key] || []).sort(function (a, b) { return String(a.Name).localeCompare(b.Name); }).forEach(function (f) {
        var p = path.concat([f.Name]);
        out.push({ id: f.Id, path: p.join(' / '), depth: depth, projectId: f.ProjectId, name: f.Name });
        walk(f.Id, p, depth + 1);
      });
    };
    Object.keys(projectNames || {}).forEach(function (pid) { walk('p' + pid, [projectNames[pid]], 0); });
    return out;
  }
  // Components inside a folder or any of its subfolders (folderId null = all).
  function inFolder(library, folders, folderId) {
    if (!folderId) return library;
    var parent = {}; folders.forEach(function (f) { parent[f.Id] = f.ParentId; });
    var under = function (fid) { for (var i = 0; fid && i < 50; i++, fid = parent[fid]) if (fid === folderId) return true; return false; };
    return library.filter(function (c) { return under(c.FolderId); });
  }

  // Revit type name -> component id, from comments written by earlier pushes.
  function rememberedFromCalc(calcSet) {
    var out = {};
    (calcSet && calcSet.CalculationGroupComponents || []).forEach(function (l) {
      var c = l.Comment || '';
      if (c.indexOf(TAG + ': ') !== 0) return;
      c.slice(TAG.length + 2).split('; ').forEach(function (n) { if (n && n.slice(-3) !== '...') out[normName(n)] = l.ComponentId; });
    });
    return out;
  }

  // Give every type an id and say where it came from:
  // chosen (in the panel) > Revit (CS_ComponentId) > remembered (earlier push) > saved (this browser) > auto (unique name match).
  function resolveIds(schedules, src) {
    src = src || {};
    var lib = src.library || [];
    return schedules.map(function (s) {
      var copy = Object.assign({}, s);
      copy.types = s.types.map(function (t) {
        var k = normName(t.name), r = Object.assign({}, t, { src: null });
        var ch = (src.chosen || {})[k];
        if (ch !== undefined) { r.id = ch || null; r.src = ch ? 'chosen' : 'skipped'; return r; }
        if (t.id) { r.src = 'Revit'; return r; }
        var rem = (src.remembered || {})[k];
        if (rem) { r.id = rem; r.src = 'remembered'; return r; }
        var saved = (src.saved || {})[k];
        if (saved) { r.id = saved; r.src = 'saved'; return r; }
        var hits = suggest(t.name, lib).filter(function (x) { return x.score === 100; });
        if (hits.length === 1) { r.id = hits[0].comp.Id; r.src = 'auto'; }
        return r;
      });
      return copy;
    });
  }

  // The original export with the CS_ComponentId column filled in (added if missing).
  function exportWithIds(schedule, resolvedTypes) {
    var rows = schedule.rows.map(function (r) { return r.slice(); });
    var col = schedule.idx.id;
    if (col < 0) {
      col = 0;
      rows = rows.map(function (r, i) { return [i === schedule.headerRow ? 'CS_ComponentId' : ''].concat(r); });
    }
    resolvedTypes.forEach(function (t) {
      t.rowIdx.forEach(function (i) { while (rows[i].length <= col) rows[i].push(''); rows[i][col] = t.id ? String(t.id) : ''; });
    });
    var d = schedule.delim;
    var cell = function (v) { v = String(v == null ? '' : v); return d === '\t' || /[",\n;]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    return rows.map(function (r) { return r.map(cell).join(d); }).join('\r\n') + '\r\n';
  }

  function defaultGroup(title) {
    var t = (title || '').toLowerCase();
    for (var i = 0; i < GROUP_BY_SCHEDULE.length; i++) if (t.indexOf(GROUP_BY_SCHEDULE[i][0]) >= 0) return GROUP_BY_SCHEDULE[i][1];
    return 'Revit import';
  }

  function fmt(v) {
    var s = (Math.round(v * 10000) / 10000).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
    return s === '-0' || s === '' ? '0' : s;
  }

  // ---------------------------------------------------------------- planning
  // schedules: parsed schedules; components: {id: component}; calcSet: GetCalculationSetById.
  // A component already in the calculation is updated where it is (whatever its group);
  // a new one goes into a group chosen from the schedule title.
  function buildPlan(schedules, components, calcSet, opts) {
    opts = opts || {};
    var groups = calcSet.CalculationGroups || [];
    var lines = calcSet.CalculationGroupComponents || [];
    var gname = {}; groups.forEach(function (g) { gname[g.Id] = (g.Name || '').trim(); });
    var isTagged = function (l) { return (l.Comment || '').indexOf(TAG) === 0; };
    var wanted = {}, order = [], missing = [], errors = [], warnings = [];

    schedules.forEach(function (s) {
      s.warnings.forEach(function (w) { warnings.push(w); });
      s.types.forEach(function (ty) {
        if (!ty.id) { missing.push({ name: ty.name, area: ty.area, volume: ty.volume, file: s.file }); return; }
        var comp = components[ty.id];
        if (!comp) { errors.push('"' + ty.name + '": component ' + ty.id + ' does not exist or you have no access to it.'); return; }
        var basis = BASIS_BY_UNIT[normUnit(comp.ComponentUnit)];
        if (!basis || basis === 'weight') { errors.push('"' + ty.name + '" -> ' + comp.Name + ' is measured in "' + (comp.ComponentUnit || '?') + '", which a schedule cannot provide.'); return; }
        var w = wanted[ty.id];
        if (!w) { w = wanted[ty.id] = { id: ty.id, name: comp.Name, unit: comp.ComponentUnit, basis: basis, qty: 0, types: [], group: defaultGroup(s.title) }; order.push(w); }
        w.qty += ty[basis];
        w.types.push(ty.name);
        if (!ty[basis]) warnings.push('"' + ty.name + '": ' + basis + ' is 0 in the export.');
      });
    });

    var used = {}, actions = [];
    order.forEach(function (w) {
      var cands = lines.filter(function (l) { return l.ComponentId === w.id && !used[l.Id]; });
      cands.sort(function (a, b) { return isTagged(b) - isTagged(a); });
      var a = { kind: 'create', want: w, newExpr: fmt(w.qty), group: w.group };
      if (cands.length) {
        var l = cands[0]; used[l.Id] = true;
        a.line = l; a.oldExpr = l.Expression; a.group = gname[l.CalculationGroupId];
        // Same number as now -> nothing to write, whoever entered it.
        a.kind = Math.abs(parseFloat(l.Expression) - parseFloat(a.newExpr)) < 1e-6 ? 'keep' : 'update';
        if (cands.length > 1) warnings.push(w.name + ': ' + (cands.length - 1) + ' other line(s) with this component are left as they are.');
      }
      actions.push(a);
    });
    // Only lines from the same kind of schedule (e.g. "Basic Wall:") count as stale,
    // so pushing just the wall schedule never touches lines that came from roofs.
    var family = function (n) { var i = n.indexOf(':'); return i > 0 ? n.slice(0, i).trim().toLowerCase() : ''; };
    var families = {};
    schedules.forEach(function (s) { s.types.forEach(function (t) { families[family(t.name)] = true; }); });
    var sameKind = function (l) {
      return (l.Comment || '').slice(TAG.length + 1).split(';').some(function (n) { return families[family(n.trim())]; });
    };
    lines.forEach(function (l) {
      if (!used[l.Id] && isTagged(l) && !wanted[l.ComponentId] && sameKind(l)) {
        actions.push({ kind: opts.prune ? 'delete' : 'stale', line: l, oldExpr: l.Expression, group: gname[l.CalculationGroupId], want: { id: l.ComponentId, name: (components[l.ComponentId] || {}).Name || ('component ' + l.ComponentId), types: [(l.Comment || '').replace(TAG + ': ', '')], unit: (components[l.ComponentId] || {}).ComponentUnit } });
      }
    });
    return { actions: actions, missing: missing, errors: errors, warnings: warnings };
  }

  function comment(types) {
    var c = TAG + ': ' + types.join('; ');
    return c.length > 250 ? c.slice(0, 247) + '...' : c;
  }

  // ---------------------------------------------------------------- API
  function makeApi(getToken) {
    return function api(method, path, body, params) {
      var url = API + path + (params ? '?' + params.map(function (p) { return encodeURIComponent(p[0]) + '=' + encodeURIComponent(p[1]); }).join('&') : '');
      return Promise.resolve(getToken()).then(function (tok) {
        var h = { Authorization: 'Bearer ' + tok, accept: 'application/json' };
        if (body !== undefined) h['content-type'] = 'application/json';
        return fetch(url, { method: method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined });
      }).then(function (r) {
        return r.text().then(function (t) {
          if (!r.ok) {
            var m = t; try { m = JSON.parse(t).Message || t; } catch (e) { }
            var hint = r.status === 401 ? ' (log in to CarbonSpace again)' : r.status === 403 ? ' (no write access to this project)' : '';
            throw new Error(r.status + hint + ': ' + String(m).slice(0, 200));
          }
          try { return JSON.parse(t); } catch (e) { return t; }
        });
      });
    };
  }

  function loadComponents(api, ids) {
    var out = {};
    return Promise.all(ids.map(function (id) {
      return api('GET', '/api/Component/GetById/' + id).then(function (c) { if (c && c.Id) out[id] = c; }, function () { });
    })).then(function () { return out; });
  }

  // Execute a plan, then make CarbonSpace re-evaluate quantities.
  function applyPlan(api, calcSet, plan, log) {
    var calc = calcSet.Calculation, pid = calc.ProjectId, cid = calc.Id;
    var gid = {}; (calcSet.CalculationGroups || []).forEach(function (g) { gid[(g.Name || '').trim().toLowerCase()] = g.Id; });
    var steps = Promise.resolve(), done = { create: 0, update: 0, delete: 0 };
    plan.actions.forEach(function (a) {
      steps = steps.then(function () {
        if (a.kind === 'update') {
          var body = Object.assign({}, a.line, { Expression: a.newExpr, Comment: comment(a.want.types) });
          delete body.Component; delete body.Calculation; delete body.CalculationGroup;
          return api('PATCH', '/api/CalculationGroupComponent/Update', body, [['fields', 'Expression'], ['fields', 'Comment']]).then(function () { done.update++; log('Updated ' + a.want.name + ': ' + a.oldExpr + ' → ' + a.newExpr); });
        }
        if (a.kind === 'delete') {
          return api('DELETE', '/api/CalculationGroupComponent/Delete/' + a.line.Id).then(function () { done.delete++; log('Removed ' + a.want.name); });
        }
        if (a.kind !== 'create') return;
        var key = a.group.trim().toLowerCase();
        var g = gid[key] ? Promise.resolve(gid[key]) : api('POST', '/api/CalculationGroup/Create', {
          Id: 0, ObjectId: '00000000-0000-0000-0000-000000000000', CalculationId: cid, Name: a.group,
          ParentId: null, ReplacementFactor: 1, IsComparison: false, Ordinal: 99, ProjectId: pid
        }).then(function (ng) { gid[key] = ng.Id; log('Added group ' + a.group); return ng.Id; });
        return g.then(function (groupId) {
          return api('POST', '/api/CalculationGroupComponent/Create', {
            Id: 0, ObjectId: '00000000-0000-0000-0000-000000000000', Quantity: 0, Expression: a.newExpr,
            Comment: comment(a.want.types), CalculationGroupId: groupId, ComponentId: a.want.id,
            CalculationId: cid, Ordinal: 0, Suppressed: false, ProjectId: pid
          });
        }).then(function () { done.create++; log('Added ' + a.want.name + ': ' + a.newExpr + ' ' + (a.want.unit || '')); });
      });
    });
    // New lines stay at 0 until expressions are re-evaluated; re-saving GFA does that.
    return steps.then(function () { return api('GET', '/api/Bom/GetByCalculation/' + cid); }).then(function (bom) {
      var gfa = ((bom.CalculationProperties || []).filter(function (p) { return p.FullKey === 'building.gfa'; })[0] || {}).Value || 0;
      return api('PATCH', '/api/Calculation/UpdateInventoryDictionary/UpdateInventoryDictionary/' + cid, { 'building.gfa': fmt(gfa) });
    }).then(function () { return done; });
  }

  function totals(bom) {
    var t = { A: 0, B: 0, C: 0, D: 0 };
    (bom.CalculationLines || []).forEach(function (l) { t.A += l.CO2A || 0; t.B += l.CO2B || 0; t.C += l.CO2C || 0; t.D += l.CO2D || 0; });
    t.AC = t.A + t.B + t.C;
    t.gfa = ((bom.CalculationProperties || []).filter(function (p) { return p.FullKey === 'building.gfa'; })[0] || {}).Value || 0;
    return t;
  }

  var core = { parseQuantity: parseQuantity, parseSchedule: parseSchedule, decode: decode, buildPlan: buildPlan, applyPlan: applyPlan, makeApi: makeApi, loadComponents: loadComponents, totals: totals, fmt: fmt,
    typeCodes: typeCodes, matchScore: matchScore, suggest: suggest, rememberedFromCalc: rememberedFromCalc, resolveIds: resolveIds, exportWithIds: exportWithIds,
    folderTree: folderTree, inFolder: inFolder };
  if (typeof module !== 'undefined' && module.exports) { module.exports = core; return; }

  // ---------------------------------------------------------------- UI
  var old = document.getElementById('cs-sync-panel');
  if (old) { old.remove(); return; }
  var host = [].slice.call(document.querySelectorAll('*')).filter(function (e) { return e.__vue_app__; })[0];
  if (location.hostname !== 'public.carbonspace.earth' || !host) {
    alert('CarbonSpace Sync: open https://public.carbonspace.earth, log in, open a calculation, then click this bookmark again.');
    return;
  }
  var auth0 = host.__vue_app__.config.globalProperties.$auth0;
  var api = makeApi(function () { return auth0.getAccessTokenSilently(); });

  var css = '#cs-sync-panel{position:fixed;top:12px;right:12px;bottom:12px;width:min(780px,calc(100vw - 24px));z-index:2147483647;background:#fff;color:#1d2433;' +
    'border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.25);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;display:flex;flex-direction:column;overflow:hidden}' +
    '#cs-sync-panel *{box-sizing:border-box}#cs-sync-panel header{background:#0b1f33;color:#fff;padding:14px 18px;display:flex;align-items:center;gap:12px}' +
    '#cs-sync-panel header b{font-size:16px;flex:1}#cs-sync-panel header .by{display:block;font-size:11.5px;font-weight:500;color:#9fb3c8;letter-spacing:.03em;margin-top:2px}#cs-sync-panel .credit{font-size:11px;color:#8a94a6;text-align:right;padding:0 18px 8px}#cs-sync-panel header button{background:none;border:0;color:#fff;font-size:22px;cursor:pointer;line-height:1}' +
    '#cs-sync-panel .body{padding:16px 18px;overflow:auto;flex:1}#cs-sync-panel h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#5b6577;margin:18px 0 8px}' +
    '#cs-sync-panel h3:first-child{margin-top:0}#cs-sync-panel .drop{border:2px dashed #9fb3c8;border-radius:10px;padding:22px;text-align:center;color:#5b6577;cursor:pointer}' +
    '#cs-sync-panel .drop.over{background:#eaf4ff;border-color:#2b7bd6}#cs-sync-panel table{width:100%;border-collapse:collapse;font-size:12.5px}' +
    '#cs-sync-panel th,#cs-sync-panel td{text-align:left;padding:5px 6px;border-bottom:1px solid #e6e9ef;vertical-align:top}#cs-sync-panel th{color:#5b6577;font-weight:600}' +
    '#cs-sync-panel td.n{text-align:right;white-space:nowrap}#cs-sync-panel .tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600}' +
    '.cs-create{background:#e3f6e8;color:#13723a}.cs-update{background:#fff3d6;color:#8a5a00}.cs-keep{background:#eef0f4;color:#5b6577}.cs-delete{background:#fde4e4;color:#a11b1b}.cs-stale{background:#f3e8ff;color:#6b2fb3}' +
    '#cs-sync-panel .warn{background:#fff6e5;border-left:4px solid #e0a000;padding:8px 10px;margin:8px 0;border-radius:4px}' +
    '#cs-sync-panel .err{background:#fdecec;border-left:4px solid #d12c2c;padding:8px 10px;margin:8px 0;border-radius:4px}' +
    '#cs-sync-panel .ok{background:#e9f7ee;border-left:4px solid #1f9d55;padding:8px 10px;margin:8px 0;border-radius:4px}' +
    '#cs-sync-panel footer{padding:12px 18px;border-top:1px solid #e6e9ef;display:flex;gap:10px;align-items:center}' +
    '#cs-sync-panel .go{background:#1f9d55;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-weight:600;font-size:14px;cursor:pointer}' +
    '#cs-sync-panel .go:disabled{background:#b8c2cc;cursor:default}#cs-sync-panel input[type=number]{width:110px;padding:5px 8px;border:1px solid #c6ceda;border-radius:6px}' +
    '#cs-sync-panel .muted{color:#5b6577;font-size:12.5px}#cs-sync-panel ul{margin:4px 0 0 18px;padding:0}' +
    '#cs-sync-panel .summary{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 4px}#cs-sync-panel .summary .tag{font-size:12px;padding:3px 10px}' +
    '#cs-sync-panel .cs-miss{background:#ffe9d1;color:#a14a00}#cs-sync-panel .src{background:#f0f1f4;color:#6b7383;font-weight:500}' +
    '#cs-sync-panel td.n{font-variant-numeric:tabular-nums}#cs-sync-panel s.old{color:#8a94a6}#cs-sync-panel .u{color:#8a94a6;font-size:11.5px}' +
    '#cs-sync-panel .delta{display:block;font-size:11px;color:#5b6577}#cs-sync-panel tr.dim td{color:#8a94a6}#cs-sync-panel tr.dim b{font-weight:500}' +
    '#cs-sync-panel tr.off td.n b,#cs-sync-panel tr.off td.n .delta{text-decoration:line-through}#cs-sync-panel tr.off .tag{opacity:.5}#cs-sync-panel input[data-act]{vertical-align:middle;margin:0 2px 0 0;cursor:pointer}'+
    '#cs-sync-panel tr.need td{background:#fff8ee}#cs-sync-panel tr.div td{padding:10px 6px 4px;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:#5b6577;border-bottom:1px solid #c6ceda}#cs-sync-panel td{vertical-align:middle}' +
    '#cs-sync-panel select{width:100%;max-width:300px;padding:4px 6px;border:1px solid #c6ceda;border-radius:6px;background:#fff;font:inherit;font-size:12.5px}' +
    '#cs-sync-panel .file{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;margin:10px 0 4px}#cs-sync-panel .file .links{margin-left:auto;font-size:12.5px}' +
    '#cs-sync-panel a{color:#0b1f33;font-weight:500;text-decoration:underline;text-underline-offset:2px}#cs-sync-panel input[type=checkbox]{accent-color:#0b1f33}#cs-sync-panel .drop.over{border-color:#0b1f33;background:#eef2f6}';
  var panel = document.createElement('div');
  panel.id = 'cs-sync-panel';
  panel.innerHTML = '<style>' + css + '</style><header><b>CarbonSpace Sync · Revit schedule → calculation<span class="by">by Unknown Works</span></b><button title="Close">×</button></header>' +
    '<div class="body"><h3>1 · Calculation</h3><div id="cs-calc"></div>' +
    '<h3>2 · Revit schedules</h3><div class="drop" id="cs-drop">Drop Revit schedule exports here (.txt or .csv) <span class="muted">or click</span></div>' +
    '<input type="file" id="cs-file" multiple accept=".txt,.csv" style="display:none"><div id="cs-preview"></div></div>' +
    '<footer><label class="muted"><input type="checkbox" id="cs-prune"> Remove STALE lines</label><span style="flex:1"></span><button class="go" id="cs-go" disabled>Push to CarbonSpace</button></footer><div class="credit">Developed by Unknown Works</div>';
  document.body.appendChild(panel);
  var $ = function (id) { return panel.querySelector('#' + id); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var num = function (v, d) { return Number(v || 0).toLocaleString('en-GB', { maximumFractionDigits: d || 0 }); };
  panel.querySelector('header button').onclick = function () { panel.remove(); };

  var LIBRARY_PROJECTS = [1302]; // UW Library; the calculation's own project is added too
  var STORE = 'cs-sync-map-v1';
  var state = { calcSet: null, schedules: [], components: {}, plan: null, library: [], chosen: {}, resolved: [], off: {} };
  var WRITES = { create: 1, update: 1, delete: 1 };
  var actKey = function (a) { return a.kind + ':' + a.want.id + ':' + (a.line ? a.line.Id : 'new'); };
  // Choices saved in this browser are kept per CarbonSpace project, so one project's
  // "WT.01a" never leaks into another project.
  var storeKey = function () { return STORE + '-' + (state.calcSet ? state.calcSet.Calculation.ProjectId : 0); };
  var loadSaved = function () { try { return JSON.parse(localStorage.getItem(storeKey()) || '{}'); } catch (e) { return {}; } };
  var save = function (pairs) {
    try { var m = loadSaved(); Object.keys(pairs).forEach(function (k) { if (pairs[k]) m[k] = pairs[k]; else delete m[k]; }); localStorage.setItem(storeKey(), JSON.stringify(m)); } catch (e) { }
  };

  // Components the type dropdowns offer: the company library plus the calculation's project,
  // narrowed to one folder (e.g. "UW Components / Project A") so projects don't get mixed up.
  function loadLibrary(projectId) {
    var pids = LIBRARY_PROJECTS.concat([projectId]).filter(function (v, i, a) { return a.indexOf(v) === i; });
    var names = {};
    return Promise.all([
      Promise.all(pids.map(function (pid) {
        return Promise.all([
          api('GET', '/api/Component/GetByProject/' + pid).then(function (list) { return list || []; }, function () { return []; }),
          api('GET', '/api/Project/GetById/' + pid).then(function (p) { names[pid] = p.Name; }, function () { names[pid] = 'Project ' + pid; })
        ]).then(function (r) { return r[0]; });
      })),
      api('GET', '/api/Folder/GetAll').then(function (f) { return f || []; }, function () { return []; })
    ]).then(function (r) {
      var seen = {};
      state.allLibrary = [].concat.apply([], r[0]).filter(function (c) { if (seen[c.Id]) return false; seen[c.Id] = true; state.components[c.Id] = state.components[c.Id] || c; return true; })
        .sort(function (a, b) { return String(a.Name).localeCompare(b.Name); });
      state.folders = r[1].filter(function (f) { return pids.indexOf(f.ProjectId) >= 0; });
      state.tree = folderTree(state.folders, names);
      // Default: the folder saved for this project, else one named like the project, else everything.
      var saved = null; try { saved = localStorage.getItem('cs-sync-folder-' + projectId); } catch (e) { }
      var pname = String(names[projectId] || '').trim().toLowerCase();
      var byName = state.tree.filter(function (f) { return f.projectId !== projectId && f.name.trim().toLowerCase() === pname; })[0];
      state.folderId = saved === 'all' ? null : saved && state.tree.some(function (f) { return String(f.id) === saved; }) ? +saved : byName ? byName.id : null;
      applyFolder();
    });
  }
  function applyFolder() {
    state.library = inFolder(state.allLibrary || [], state.folders || [], state.folderId);
  }
  function folderPicker() {
    var count = function (id) { return inFolder(state.allLibrary || [], state.folders || [], id).length; };
    return '<p>Folder: <select id="cs-folder" style="max-width:420px"><option value="all">All (' + (state.allLibrary || []).length + ' components)</option>' +
      (state.tree || []).map(function (f) {
        return '<option value="' + f.id + '"' + (f.id === state.folderId ? ' selected' : '') + '>' + new Array(f.depth + 1).join('   ') + esc(f.path) + ' (' + count(f.id) + ')</option>';
      }).join('') + '</select></p>';
  }

  function setCalc(id) {
    $('cs-calc').innerHTML = '<span class="muted">Loading calculation ' + esc(id) + '…</span>';
    return api('GET', '/api/Calculation/GetCalculationSetById/' + id).then(function (s) {
      state.calcSet = s;
      return Promise.all([api('GET', '/api/Bom/GetByCalculation/' + id), loadLibrary(s.Calculation.ProjectId)]).then(function (res) {
        var bom = res[0];
        var t = totals(bom);
        $('cs-calc').innerHTML = '<b>' + esc(s.Calculation.Name) + '</b> <span class="muted">(calculation ' + s.Calculation.Id + ', project ' + s.Calculation.ProjectId + ', ' +
          (s.CalculationGroupComponents || []).length + ' lines)</span><div class="muted">Now: A–C ' + num(t.AC) + ' kgCO₂e · A ' + num(t.A) + ' kgCO₂e' + (t.gfa ? ' · ' + num(t.AC / t.gfa, 1) + ' kgCO₂e/m² GFA' : '') + '</div>';
        refresh();
      });
    }).catch(function (e) { $('cs-calc').innerHTML = '<div class="err">' + esc(e.message) + '</div>' + calcPicker(); bindPicker(); });
  }
  function calcPicker() { return '<div>Calculation id: <input type="number" id="cs-calcid"> <button id="cs-load">Load</button></div>'; }
  function loadTyped() { var v = ($('cs-calcid') || {}).value; if (v) { shown = String(v).trim(); setCalc(shown); return true; } return false; }
  function bindPicker() {
    var b = $('cs-load'), i = $('cs-calcid');
    if (b) b.onclick = loadTyped;
    if (i) i.onkeydown = function (e) { if (e.key === 'Enter') loadTyped(); };
  }

  function addFiles(files) {
    var reads = [].slice.call(files).map(function (f) {
      return f.arrayBuffer().then(function (buf) { return parseSchedule(decode(new Uint8Array(buf)), f.name); });
    });
    $('cs-preview').innerHTML = '<p class="muted">Reading files…</p>';
    Promise.all(reads).then(function (list) {
      list.forEach(function (s) { state.schedules = state.schedules.filter(function (x) { return x.file !== s.file; }).concat([s]); });
      var ids = []; state.schedules.forEach(function (s) { s.types.forEach(function (t) { if (t.id && !state.components[t.id] && ids.indexOf(t.id) < 0) ids.push(t.id); }); });
      return loadComponents(api, ids).then(function (c) { Object.assign(state.components, c); refresh(); });
    }).catch(function (e) { $('cs-preview').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; });
  }

  function refresh() {
    if (!state.schedules.length) { $('cs-go').disabled = true; return; }
    if (!state.calcSet) {
      $('cs-go').disabled = true;
      // A typed but not yet loaded id is loaded now; otherwise ask for one.
      if (!loadTyped()) $('cs-preview').innerHTML = '<div class="warn">' + state.schedules.length + ' file(s) read. Choose a calculation first.</div>';
      return;
    }
    var ids = (state.calcSet.CalculationGroupComponents || []).map(function (l) { return l.ComponentId; });
    state.schedules.forEach(function (s) { s.types.forEach(function (t) { if (t.id) ids.push(t.id); }); });
    Object.keys(state.chosen).forEach(function (k) { if (state.chosen[k]) ids.push(state.chosen[k]); });
    var need = ids.filter(function (id, i) { return !state.components[id] && ids.indexOf(id) === i; });
    loadComponents(api, need).then(function (c) {
      Object.assign(state.components, c);
      var resolved = state.resolved = resolveIds(state.schedules, {
        chosen: state.chosen, remembered: rememberedFromCalc(state.calcSet), saved: loadSaved(), library: state.library
      });
      var plan = state.plan = buildPlan(resolved, state.components, state.calcSet, { prune: $('cs-prune').checked });
      plan.actions.forEach(function (a) { a.off = !!(WRITES[a.kind] && state.off[actKey(a)]); });
      var count = function (k) { return plan.actions.filter(function (a) { return a.kind === k && !a.off; }).length; };
      var chip = function (cls, n, label) { return n ? '<span class="tag ' + cls + '">' + n + ' ' + label + '</span>' : ''; };
      var h = '<div class="summary">' + [chip('cs-update', count('update'), 'to update'), chip('cs-create', count('create'), 'new'),
        chip('cs-delete', count('delete'), 'to remove'), chip('cs-stale', count('stale'), 'stale'), chip('cs-keep', count('keep'), 'unchanged'),
        chip('cs-miss', plan.missing.length, 'not matched')].join('') + '</div>';
      if (plan.errors.length) h += '<div class="err"><b>Fix these first:</b><ul>' + plan.errors.map(function (e) { return '<li>' + esc(e) + '</li>'; }).join('') + '</ul></div>';
      plan.warnings.forEach(function (w) { h += '<div class="warn">' + esc(w) + '</div>'; });
      h += '<h3>3 · What will change</h3>' + changesTable(plan);
      h += '<h3>4 · Revit types → CarbonSpace components</h3>' + folderPicker() + resolved.map(matchTable).join('');
      $('cs-preview').innerHTML = h;
      bindMatchTable();
      var n = count('create') + count('update') + count('delete');
      $('cs-go').disabled = !n || plan.errors.length > 0;
      $('cs-go').textContent = n ? 'Push ' + n + ' change' + (n > 1 ? 's' : '') + ' to CarbonSpace' : 'Nothing to change';
    });
  }

  var ORDER = { update: 0, create: 1, delete: 2, stale: 3, keep: 4 };
  function changesTable(plan) {
    var acts = plan.actions.slice().sort(function (a, b) { return ORDER[a.kind] - ORDER[b.kind]; });
    var keep = acts.filter(function (a) { return a.kind === 'keep'; }).length;
    var shown = state.showKeep ? acts : acts.filter(function (a) { return a.kind !== 'keep'; });
    var unit = function (a) { return ' <span class="u">' + esc(a.want.unit || '') + '</span>'; };
    var delta = function (a) {
      var d = parseFloat(a.newExpr) - parseFloat(a.oldExpr);
      return isNaN(d) ? '' : '<span class="delta">' + (d > 0 ? '+' : '') + num(d, 2) + '</span>';
    };
    var change = function (a) {
      if (a.kind === 'update') return '<s class="old">' + esc(a.oldExpr) + '</s> → <b>' + esc(a.newExpr) + '</b>' + unit(a) + delta(a);
      if (a.kind === 'create') return '<b>' + esc(a.newExpr) + '</b>' + unit(a) + '<span class="delta">new</span>';
      if (a.kind === 'delete') return '<s class="old">' + esc(a.oldExpr) + '</s>' + unit(a) + '<span class="delta">removed</span>';
      if (a.kind === 'stale') return esc(a.oldExpr) + unit(a) + '<span class="delta">not in Revit</span>';
      return esc(a.newExpr) + unit(a);
    };
    var rows = shown.map(function (a) {
      var box = WRITES[a.kind] ? '<input type="checkbox" data-act="' + esc(actKey(a)) + '"' + (a.off ? '' : ' checked') + ' title="Include this change"> ' : '';
      return '<tr class="' + (a.kind === 'keep' || a.off ? 'dim' : '') + (a.off ? ' off' : '') + '"><td style="white-space:nowrap">' + box + '<span class="tag cs-' + a.kind + '">' + a.kind.toUpperCase() + '</span></td>' +
        '<td><b>' + esc(a.want.name) + '</b> <span class="muted">#' + a.want.id + '</span><div class="muted">' +
        esc(a.want.types.map(function (t) { return shortName(t); }).join(', ')) + '</div></td>' +
        '<td class="muted">' + esc(a.group) + '</td><td class="n">' + change(a) + '</td></tr>';
    }).join('');
    if (!shown.length) rows = '<tr><td colspan="4" class="muted">No changes.</td></tr>';
    var more = keep ? '<p class="muted"><a href="#" data-toggle="keep">' + (state.showKeep ? 'Hide' : 'Show') + ' ' + keep + ' unchanged</a>' : '<p class="muted">';
    if (acts.some(function (a) { return a.kind === 'stale'; })) more += (keep ? ' · ' : '') + 'STALE = no longer in Revit.';
    return '<table><tr><th style="width:104px"></th><th>CarbonSpace component · Revit types</th><th>Group</th><th class="n">Now → New</th></tr>' + rows + '</table>' + more + '</p>';
  }

  // How each type got its component; types that need a look are shown even when the list is collapsed.
  var SRC = {
    Revit: ['src', 'from Revit'], remembered: ['src', 'last push'], chosen: ['src', 'chosen'], saved: ['src', 'saved'],
    auto: ['cs-update', 'name match'], skipped: ['cs-keep', 'skipped'], none: ['cs-miss', 'choose']
  };
  var needsLook = function (t) { return !t.id || t.src === 'auto'; };
  // One table per schedule: each Revit type with a dropdown of components, best name matches first.
  function matchTable(s, si) {
    var all = s.types.length, attention = s.types.filter(needsLook).length;
    var check = s.types.filter(needsLook), done = s.types.filter(function (t) { return !needsLook(t); });
    var row = function (t) {
      var sug = suggest(t.name, state.library).slice(0, 6);
      var top = sug.map(function (x) { return x.comp.Id; });
      var opt = function (c, star) {
        return '<option value="' + c.Id + '"' + (c.Id === t.id ? ' selected' : '') + '>' + (star ? '• ' : '') + esc(c.Name) + ' (' + esc(c.ComponentUnit || '?') + ') #' + c.Id + '</option>';
      };
      var current = t.id && !state.library.some(function (c) { return c.Id === t.id; }) && state.components[t.id];
      var sel = '<select data-type="' + esc(t.name) + '"><option value="">— none (skip) —</option>' +
        (current ? opt(current) : '') +
        (sug.length ? '<optgroup label="Best matches">' + sug.map(function (x) { return opt(x.comp, true); }).join('') + '</optgroup>' : '') +
        '<optgroup label="All components">' + state.library.filter(function (c) { return top.indexOf(c.Id) < 0; }).map(function (c) { return opt(c); }).join('') + '</optgroup>' +
        '<option value="other">Other component number…</option></select>';
      var basis = t.id && state.components[t.id] ? BASIS_BY_UNIT[normUnit(state.components[t.id].ComponentUnit)] : null;
      var q = basis && basis !== 'weight' ? num(t[basis], 2) + ' <span class="u">' + esc(state.components[t.id].ComponentUnit) + '</span>' : num(t.area, 1) + ' <span class="u">m²</span>';
      var src = SRC[t.src || 'none'] || SRC.none;
      return '<tr' + (t.id ? '' : ' class="need"') + '><td>' + esc(shortName(t.name)) + '</td><td class="n">' + q + '</td><td>' + sel + '</td><td><span class="tag ' + src[0] + '">' + src[1] + '</span></td></tr>';
    };
    var divider = function (label) { return '<tr class="div"><td colspan="4">' + label + '</td></tr>'; };
    var rows = (check.length ? divider('To check (' + check.length + ') · skipped if left empty') + check.map(row).join('') : '') +
      (done.length ? divider('Matched (' + done.length + ')') + done.map(row).join('') : '');
    var head = '<div class="file"><b>' + esc(s.file) + '</b> <span class="muted">— ' + esc(s.title) + ' · ' + all + ' types' +
      (attention ? ', <b>' + attention + ' to check</b>' : ', all matched') + '</span>' +
      '<span class="links"><a href="#" data-export="' + si + '">Download with CS_ComponentId</a></span></div>';
    return head + '<table><tr><th>Revit type</th><th class="n">Quantity</th><th>CarbonSpace component</th><th style="width:110px"></th></tr>' + rows + '</table>';
  }

  function bindMatchTable() {
    [].forEach.call(panel.querySelectorAll('input[data-act]'), function (cb) {
      cb.onchange = function () { var k = cb.getAttribute('data-act'); if (cb.checked) delete state.off[k]; else state.off[k] = true; refresh(); };
    });
    [].forEach.call(panel.querySelectorAll('a[data-toggle]'), function (a) {
      a.onclick = function (e) {
        e.preventDefault();
        if (a.getAttribute('data-toggle') === 'keep') state.showKeep = !state.showKeep; else state.showAllTypes = !state.showAllTypes;
        refresh();
      };
    });
    var fsel = $('cs-folder');
    if (fsel) fsel.onchange = function () {
      state.folderId = fsel.value === 'all' ? null : +fsel.value;
      try { localStorage.setItem('cs-sync-folder-' + state.calcSet.Calculation.ProjectId, fsel.value); } catch (e) { }
      applyFolder();
      refresh();
    };
    [].forEach.call(panel.querySelectorAll('select[data-type]'), function (sel) {
      sel.onchange = function () {
        var k = normName(sel.getAttribute('data-type')), v = sel.value;
        if (v === 'other') {
          v = prompt('Component number (the number at the end of the component\'s web address, e.g. 80460):');
          if (!v || !/^\d+$/.test(v.trim())) { refresh(); return; }
          v = v.trim();
        }
        state.chosen[k] = v ? parseInt(v, 10) : 0;
        var p = {}; p[k] = state.chosen[k]; save(p);
        refresh();
      };
    });
    [].forEach.call(panel.querySelectorAll('a[data-export]'), function (a) {
      a.onclick = function (e) {
        e.preventDefault();
        var i = +a.getAttribute('data-export'), s = state.schedules[i];
        var text = exportWithIds(s, state.resolved[i].types);
        var dot = s.file.lastIndexOf('.');
        var name = (dot > 0 ? s.file.slice(0, dot) : s.file) + ' (with ids)' + (dot > 0 ? s.file.slice(dot) : '.txt');
        download(name, '﻿' + text, 'text/plain');
      };
    });
  }

  function download(name, data, type) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([typeof data === 'string' ? data : JSON.stringify(data, null, 1)], { type: type || 'application/json' }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
  }

  $('cs-go').onclick = function () {
    var s = state.calcSet, plan = state.plan;
    if (!confirm('Write ' + $('cs-go').textContent.replace('Push ', '').replace(' to CarbonSpace', '') + ' into "' + s.Calculation.Name + '"?\n\nA backup of the calculation is downloaded first.')) return;
    $('cs-go').disabled = true; $('cs-go').textContent = 'Pushing…';
    var stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
    download('carbonspace-calc-' + s.Calculation.Id + '-before-' + stamp + '.json', s);
    var log = [], before = null;
    api('GET', '/api/Bom/GetByCalculation/' + s.Calculation.Id).then(function (b) { before = totals(b); })
      .then(function () { return applyPlan(api, s, { actions: plan.actions.filter(function (a) { return !a.off; }) }, function (m) { log.push(m); }); })
      .then(function () { return api('GET', '/api/Bom/GetByCalculation/' + s.Calculation.Id); })
      .then(function (bom) {
        var t = totals(bom), d = t.AC - before.AC;
        $('cs-preview').innerHTML = '<div class="ok"><b>Done.</b> A–C ' + num(before.AC) + ' → <b>' + num(t.AC) + ' kgCO₂e</b> (' + (d >= 0 ? '+' : '') + num(d) + (before.AC ? ', ' + (d >= 0 ? '+' : '') + (d / before.AC * 100).toFixed(1) + '%' : '') + ')' +
          (t.gfa ? '<br>' + num(t.AC / t.gfa, 1) + ' kgCO₂e/m² GFA' : '') + '</div><ul class="muted">' + log.map(function (m) { return '<li>' + esc(m) + '</li>'; }).join('') + '</ul>' +
          '<p><button class="go" id="cs-reload">Reload page to see the result</button></p>';
        $('cs-reload').onclick = function () { location.reload(); };
        $('cs-go').textContent = 'Done';
        // Remember every type's component in this browser too (the calculation remembers it via comments).
        var pairs = {}; state.resolved.forEach(function (s) { s.types.forEach(function (t) { if (t.id) pairs[normName(t.name)] = t.id; }); });
        save(pairs);
        state.schedules = [];
      })
      .catch(function (e) { $('cs-preview').insertAdjacentHTML('afterbegin', '<div class="err"><b>Push stopped:</b> ' + esc(e.message) + '<br>Steps already done are listed below. The backup file has the state before the push.</div><ul class="muted">' + log.map(function (m) { return '<li>' + esc(m) + '</li>'; }).join('') + '</ul>'); $('cs-go').textContent = 'Failed'; });
  };

  var drop = $('cs-drop');
  drop.onclick = function () { $('cs-file').click(); };
  $('cs-file').onchange = function (e) { addFiles(e.target.files); e.target.value = ''; };
  drop.ondragover = function (e) { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = function () { drop.classList.remove('over'); };
  drop.ondrop = function (e) { e.preventDefault(); drop.classList.remove('over'); addFiles(e.dataTransfer.files); };
  $('cs-prune').onchange = refresh;

  // The web app is a single-page app: follow navigation while the panel is open.
  var shown = null;
  function detect() {
    var m = /calculation\/(\d+)/.exec(location.pathname + location.hash);
    var id = m ? m[1] : null;
    if (id && id !== shown) { shown = id; setCalc(id); }
    else if (!id && shown === null) { shown = ''; $('cs-calc').innerHTML = calcPicker(); bindPicker(); }
  }
  detect();
  var timer = setInterval(function () { if (!document.body.contains(panel)) clearInterval(timer); else detect(); }, 800);
})();
