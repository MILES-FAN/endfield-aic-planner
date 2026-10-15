"use strict";
/* Endfield AIC Factory Planner — static, data baked by scripts/build_web_data.py.
 * Two solvers, both ported from scripts/balance.py:
 *   - Direct  (planLine): acyclic main-product expansion, exact A·x = b solve.
 *   - Closed-loop (planLP): general LP over all recipes via a two-phase simplex
 *     (browser stand-in for scipy linprog) — auto-recycles byproduct/seed cycles,
 *     honors facility caps / open-loop items / self-power, optimizes an objective.
 * Rendered as a left→right production DAG with icons mirrored from wiki.gg. */

const SVGNS = "http://www.w3.org/2000/svg";

let DATA = null;
let producers = {};   // item -> [recipe idx] where it is the main product
let picks = {};       // item -> recipe idx override (from the Recipe-choices panel)
let plannable = [];    // item names that have a continuous recipe (searchable targets)
let current = { target: null, rate: 0 };            // last plan, for re-solving on option change
const lp = { on: false, objective: "min", selfpower: "", caps: {}, open: new Set(), raw: new Set() };
// i18n (locale / I18N / t / curName) lives in i18n.js, loaded before this file.

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);

function fnum(x) {
  const f = +x;
  if (!isFinite(f)) return "–";
  if (Math.abs(f - Math.round(f)) < 1e-9) return String(Math.round(f));
  return (+f.toFixed(3)).toString();
}
function itemIcon(name) {
  const it = DATA.items[name];
  return it ? "icons/items/" + encodeURIComponent(it.icon) : "";
}
function facIcon(name) {
  const f = DATA.facilities[name];
  return f ? "icons/facilities/" + encodeURIComponent(f.icon) : "";
}
function cnOf(name) { return (DATA.items[name] || {}).cn || ""; }
function jaOf(name) { return (DATA.items[name] || {}).ja || ""; }
function tierOf(name) { return (DATA.items[name] || {}).tier || 1; }
// localized display names: the data carries `cn` (中文) and `ja` (日本語) per item /
// facility; EN uses the key itself. Falls back to the EN key when a name is absent.
// These read DATA so they stay here; the string/currency layer is in i18n.js.
const NAME_FIELD = { zh: "cn", ja: "ja" };
function itemName(id) { const f = NAME_FIELD[locale]; return f ? ((DATA.items[id] || {})[f] || id) : id; }
function facName(id) { const f = NAME_FIELD[locale]; return f ? ((DATA.facilities[id] || {})[f] || id) : id; }
function facSize(f) { return (DATA.facilities[f] || {}).size || ""; }   // "3x3" grid footprint
function facArea(f) { const m = facSize(f).match(/(\d+)\D+(\d+)/); return m ? +m[1] * +m[2] : 0; }

function netPerRun(r, item) {
  let v = 0;
  for (const p of r.products) if (p.item === item) v += p.count;
  for (const g of r.ingredients) if (g.item === item) v -= g.count;
  return v;
}

// -------------------------------------------------------------- linear solve
// Gaussian elimination with partial pivoting (floats; results are rounded for
// display). Returns null if singular.
function solveLinear(A, b) {
  const n = A.length;
  const M = A.map((row, i) => row.concat([b[i]]));
  for (let col = 0; col < n; col++) {
    let piv = col, best = Math.abs(M[col][col]);
    for (let r = col + 1; r < n; r++) {
      const v = Math.abs(M[r][col]);
      if (v > best) { best = v; piv = r; }
    }
    if (best < 1e-12) return null;
    [M[col], M[piv]] = [M[piv], M[col]];
    const pv = M[col][col];
    for (let j = col; j <= n; j++) M[col][j] /= pv;
    for (let r = 0; r < n; r++) {
      if (r === col) continue;
      const f = M[r][col];
      if (f === 0) continue;
      for (let j = col; j <= n; j++) M[r][j] -= f * M[col][j];
    }
  }
  return M.map((row) => row[n]);
}

// ------------------------------------------------------------ LP (simplex)
// Minimize cᵀx subject to A_ub·x ≤ b_ub, x ≥ 0, via a two-phase tableau simplex.
// This is the browser stand-in for scipy.optimize.linprog(method="highs") used by
// balance.py's --lp mode. Bland's rule kicks in if a run gets long, to guarantee
// termination under degeneracy (common in these recipe LPs). Returns {success,x}.
function lpPivot(T, r, c, m, ncols) {
  const rr = T[r], inv = 1 / rr[c];
  for (let j = 0; j <= ncols; j++) rr[j] *= inv;
  for (let i = 0; i <= m; i++) {
    if (i === r) continue;
    const ti = T[i], f = ti[c];
    if (Math.abs(f) > 1e-15) for (let j = 0; j <= ncols; j++) ti[j] -= f * rr[j];
  }
}
function lpSetObjective(T, basis, m, ncols, cost) {
  const row = T[m];
  for (let j = 0; j < ncols; j++) row[j] = cost[j] || 0;
  row[ncols] = 0;
  for (let i = 0; i < m; i++) {
    const cb = cost[basis[i]] || 0;
    if (cb !== 0) { const ti = T[i]; for (let j = 0; j <= ncols; j++) row[j] -= cb * ti[j]; }
  }
}
function lpOptimize(T, basis, m, ncols, forbidden) {
  const N = m + ncols, LIMIT = 30000;
  for (let iter = 0; iter < LIMIT; iter++) {
    const bland = iter > 4 * N;
    let piv = -1, best = -1e-9;
    for (let j = 0; j < ncols; j++) {
      if (forbidden && forbidden[j]) continue;
      const d = T[m][j];
      if (d < -1e-9) { if (bland) { piv = j; break; } if (d < best) { best = d; piv = j; } }
    }
    if (piv < 0) return true;                       // optimal
    let leave = -1, minRatio = Infinity, minBasis = Infinity;
    for (let i = 0; i < m; i++) {
      const a = T[i][piv];
      if (a > 1e-9) {
        const ratio = T[i][ncols] / a;
        if (ratio < minRatio - 1e-9 ||
            (bland && Math.abs(ratio - minRatio) <= 1e-9 && basis[i] < minBasis)) {
          minRatio = ratio; leave = i; minBasis = basis[i];
        }
      }
    }
    if (leave < 0) return false;                    // unbounded
    lpPivot(T, leave, piv, m, ncols);
    basis[leave] = piv;
  }
  return true;
}
function linprog(c, A, b) {
  const m = A.length, n = c.length;
  if (m === 0) return { success: true, x: new Array(n).fill(0) };
  const artRow = [];
  for (let i = 0; i < m; i++) if (b[i] < 0) artRow.push(i);
  const nArt = artRow.length, ncols = n + m + nArt;
  const T = Array.from({ length: m + 1 }, () => new Float64Array(ncols + 1));
  const basis = new Array(m);
  const artColOf = {};
  artRow.forEach((ri, k) => (artColOf[ri] = n + m + k));
  for (let i = 0; i < m; i++) {
    const flip = b[i] < 0, s = flip ? -1 : 1, Ti = T[i];
    for (let j = 0; j < n; j++) Ti[j] = s * A[i][j];
    Ti[n + i] = s;                                  // slack column
    Ti[ncols] = s * b[i];                           // rhs (now ≥ 0)
    if (flip) { Ti[artColOf[i]] = 1; basis[i] = artColOf[i]; } else basis[i] = n + i;
  }
  if (nArt > 0) {                                   // Phase I: min Σ artificials
    const cI = new Array(ncols).fill(0);
    for (const ri of artRow) cI[artColOf[ri]] = 1;
    lpSetObjective(T, basis, m, ncols, cI);
    lpOptimize(T, basis, m, ncols, null);
    if (-T[m][ncols] > 1e-6) return { success: false, infeasible: true };
    for (let i = 0; i < m; i++) {                   // drive artificials out of basis
      if (basis[i] >= n + m) {
        let piv = -1;
        for (let j = 0; j < n + m; j++) if (Math.abs(T[i][j]) > 1e-9) { piv = j; break; }
        if (piv >= 0) { lpPivot(T, i, piv, m, ncols); basis[i] = piv; }
      }
    }
  }
  const cII = new Array(ncols).fill(0);             // Phase II: real objective
  for (let j = 0; j < n; j++) cII[j] = c[j];
  lpSetObjective(T, basis, m, ncols, cII);
  const forbidden = new Array(ncols).fill(false);
  for (const ri of artRow) forbidden[artColOf[ri]] = true;
  if (!lpOptimize(T, basis, m, ncols, forbidden)) return { success: false, unbounded: true };
  const x = new Array(n).fill(0);
  for (let i = 0; i < m; i++) if (basis[i] < n) x[basis[i]] = T[i][ncols];
  return { success: true, x };
}

// LP defaults + battery burn values, mirrored from scripts/balance.py.
const DEFAULT_RAW_LP = ["Clean Water", "Power"];
const DEFAULT_BAN_ITEMS = ["Wood"];
// Gas (1.4+), mirrored from balance.py: gathered gases are external leaves; reversible
// fluid⇄gas / solid⇄gas transmutes are tried last and never loop back into a chain.
const GATHERED = ["Xiragen", "Inergen"];
const TRANSMUTERS = new Set(["Fluid-Gas Transmuting Unit", "Solid-Gas Transmuting Unit"]);

// ENV zones: a player-built machine, the Gas Dispersing Unit (3×3), fed ≥6/min of a gas
// makes a 13×13 environment; machines fully inside may use that ENV's recipes (some are
// mandatory, e.g. Pyrrolite Gas in Acrid; some are cheaper variants, e.g. Xiranite from
// plain Carbon in Stable). The planner treats every zone as one real machine whose gas
// feed is solved like any other demand. Gas→ENV per endfield.wiki.gg/wiki/Environment.
const DISPERSER = "Gas Dispersing Unit";
const ENV_GAS = { "Stable ENV": "Inergen", "Acrid ENV": "Acridgen" };
const ENV_CLASS = { "Stable ENV": "stable", "Acrid ENV": "acrid" };
const ENV_FEED = 6;
// Zone packing (~approx): 13×13 around the 3×3 unit leaves 4 corner slots for ≤5×5
// machines and 4 edge slots for ≤3×3 (belts/pipes not counted).
const ZONE_BIG = 4, ZONE_SLOTS = 8;
let envMode = "opt";     // "opt" = 最适化: ENV variants only where the zone pays off; "min" = 最小化
let plainMade = new Set(), plainMadeEv = new Set();  // items with a non-ENV, non-transmute recipe
let optionalEnvs = [];   // ENVs that only offer *alternative* recipes (candidates for 最适化)

function isTransmute(r) { return TRANSMUTERS.has(r.facility); }
// may this recipe be used? events only when allowed; an ENV variant only if that ENV is in
// `allowed` or it's the item's sole source (then the zone is mandatory)
function usable(r, eventOK, allowed) {
  if (r.event && !eventOK) return false;
  if (r.environment && !allowed.has(r.environment) && r.products.length
      && (eventOK ? plainMadeEv : plainMade).has(r.products[0].item)) return false;
  return true;
}
// event recipes join in only when the target can't be made any other way
function eventOnly(target) {
  return !DATA.recipes.some((r) => !r.event && r.time && r.products.some((p) => p.item === target));
}
// [[recipe, machines]] -> {env: zones}; whole machines are what has to fit in a zone
function zonesNeeded(use) {
  const big = {}, small = {};
  for (const [r, m] of use) {
    if (!r.environment || m <= 1e-9) continue;
    const n = Math.ceil(m - 1e-9), e = r.environment;
    if (facArea(r.facility) > 9) big[e] = (big[e] || 0) + n; else small[e] = (small[e] || 0) + n;
  }
  const z = {};
  for (const e of new Set([...Object.keys(big), ...Object.keys(small)])) {
    const b = big[e] || 0, s = small[e] || 0;
    z[e] = Math.max(1, Math.ceil(b / ZONE_BIG), Math.ceil((b + s) / ZONE_SLOTS));
  }
  return z;
}
// Solve with zones: zone count depends on the plan, the plan on the zones' gas feed, so
// iterate (monotone, converges in a step or two) and then add the dispersers to the plan.
function planWithZones(solve, allowed) {
  let z = {}, plan = null;
  for (let i = 0; i < 6; i++) {
    const extra = {};
    for (const [e, n] of Object.entries(z)) extra[ENV_GAS[e]] = (extra[ENV_GAS[e]] || 0) + ENV_FEED * n;
    plan = solve(allowed, extra);
    if (plan.error) return plan;
    const need = zonesNeeded(plan.envUse);
    let changed = false;
    for (const [e, n] of Object.entries(need)) if ((z[e] || 0) < n) { z[e] = n; changed = true; }
    if (!changed) break;
  }
  attachZones(plan, z);
  return plan;
}
// add each zone's Gas Dispersing Unit as a machine node: gas -> [ENV] -> its machines
function attachZones(plan, z) {
  plan.env = [];
  for (const env of Object.keys(z).sort()) {
    const gas = ENV_GAS[env], zones = z[env], runs = ENV_FEED * zones;
    const where = plan.envUse.filter(([r, m]) => r.environment === env && m > 1e-9).map(([r, m]) => [r.facility, m]);
    plan.env.push({ env, gas, zones, where });
    plan.recipeOf[env] = { facility: DISPERSER, time: 60 / ENV_FEED, _disperser: true,
      ingredients: [{ item: gas, count: 1 }], products: [{ item: env, count: 1 }] };
    plan.runs[env] = runs;
    plan.internal.push(env);
    plan.machines.push({ item: env, facility: DISPERSER, runs, machines: zones });
    plan.totalMachines += zones;
    plan.power += (DATA.facilities[DISPERSER]?.power || 0) * zones;
    plan.edges.push({ from: gas, to: env, rate: runs });
    for (const it of plan.internal) {
      if (plan.recipeOf[it].environment === env) plan.edges.push({ from: env, to: it, rate: 0, env });
    }
  }
  plan.machines.sort((a, b) => a.facility.localeCompare(b.facility) || a.item.localeCompare(b.item));
}
// how good is a plan (lower = better) under the active objective
function planScore(plan) {
  if (plan.mode === "lp" && plan.objective === "max") return -plan.rate + 1e-6 * plan.totalMachines;
  if (plan.mode === "lp" && plan.objective === "revenue") return -(plan.revenue ? plan.revenue.total : 0) + 1e-6 * plan.totalMachines;
  return plan.totalMachines;
}
// 最适化: try every subset of the optional ENVs (each with its zones + gas chain costed in)
// and keep the best; 最小化: only mandatory zones. Ties go to fewer zones.
function planBest(solve) {
  const subsets = [[]];
  if (envMode === "opt") for (const e of optionalEnvs) for (const s of subsets.slice()) subsets.push([...s, e]);
  subsets.sort((a, b) => a.length - b.length);
  let best = null, base = null, firstErr = null;
  for (const s of subsets) {
    const p = planWithZones(solve, new Set(s));
    if (p.error) { firstErr = firstErr || p; continue; }
    if (!s.length) base = p;
    if (!best || planScore(p) < planScore(best) - 1e-9) best = p;
  }
  if (!best) return firstErr;
  best.envChoice = { mode: envMode, base: base && base !== best ? base : null };
  return best;
}
const BATTERY_POWER = {
  "SC Wuling Battery": 3200, "LC Wuling Battery": 1600, "HC Valley Battery": 1100,
  "SC Valley Battery": 420, "LC Valley Battery": 220,
};

// ------------------------------------------------------------------- indexing
function buildIndices() {
  producers = {};
  DATA.recipes.forEach((r, i) => {
    if (r.products.length) {
      const main = r.products[0].item;
      (producers[main] = producers[main] || []).push(i);
    }
  });
  plannable = Object.keys(producers).sort((a, b) => a.localeCompare(b));
  plainMade = new Set(); plainMadeEv = new Set();
  for (const r of DATA.recipes) {
    if (!r.products.length || r.environment || isTransmute(r)) continue;
    plainMadeEv.add(r.products[0].item);
    if (!r.event) plainMade.add(r.products[0].item);
  }
  optionalEnvs = [...new Set(DATA.recipes.filter((r) => r.environment && r.products.length
    && plainMadeEv.has(r.products[0].item)).map((r) => r.environment))].sort();
}

// Real *net* sources of an external input, given what this plan already makes (port of
// balance.py recycle_sources). Emptying a filled bottle/canister, a reverse transmute, or
// 4 Cuprium Solution -> 1 Precipitation Acid don't count: per ingredient I, if I is made
// from `item` in one step the round trip must gain (>1, e.g. seed -> 2 plants); otherwise
// I must already be in the plan (internal / surplus) or be pumped/gathered.
function recycleSources(item, available) {
  const cnt = (side, it) => side.reduce((s, x) => s + (x.item === it ? x.count : 0), 0);
  const free = new Set([...available, ...GATHERED, "Clean Water"]);
  const out = [];
  for (const r of DATA.recipes) {
    if (!r.time || TRANSMUTERS.has(r.facility) || r.facility === "Thermal Bank") continue;
    const made = cnt(r.products, item);
    if (made <= 0 || cnt(r.ingredients, item) > 0) continue;
    const ings = r.ingredients.filter((g) => !g.activator && g.item !== "Power").map((g) => g.item);
    const ok = ings.every((ing) => {
      const gains = DATA.recipes.filter((d) => d.time && cnt(d.ingredients, item) > 0 && cnt(d.products, ing) > 0)
        .map((d) => made / cnt(r.ingredients, ing) * cnt(d.products, ing) / cnt(d.ingredients, item));
      return gains.length ? Math.max(...gains) > 1 + 1e-9 : free.has(ing);
    });
    if (ok) out.push({ facility: r.facility, ings });
  }
  return out;
}
function recycleMap(inputs, available) {
  const m = {};
  for (const x of inputs) { const s = recycleSources(x.item, available); if (s.length) m[x.item] = s; }
  return m;
}
function recycleText(srcs) {
  return srcs.map((s) => `${facName(s.facility)} (${s.ings.map(itemName).join(" + ")})`).join("; ");
}

// -------------------------------------------------------------------- planner
function planLine(target, rateInput) {
  return planBest((allowed, extra) => solveLine(target, rateInput, allowed, extra));
}
// extra = {gas: /min} consumed outside the recipe graph (the ENV zones' dispersers)
function solveLine(target, rateInput, allowed, extra) {
  // choose a recipe per item: DFS over the main-product graph (port of balance.py).
  // A transmute whose input is already on the path would undo itself (A⇄B, singular)
  // -> skipped; if every route of an item loops back it returns false so the caller
  // tries its next recipe or becomes a leaf. Productive loops (plant⇄seed) are fine.
  const eventOK = eventOnly(target);
  let chosen = {}; let raw = new Set(["Clean Water", ...GATHERED]);
  if (picks["Clean Water"] != null) raw.delete("Clean Water");
  for (const g of GATHERED) if (picks[g] != null) raw.delete(g);
  const expand = (it, path) => {
    if (it in chosen || raw.has(it)) return true;
    let cands = (producers[it] || []).filter((ri) => usable(DATA.recipes[ri], eventOK, allowed));
    if (picks[it] != null && (producers[it] || []).includes(picks[it])) cands = [picks[it]];
    else cands.sort((a, b) => {   // real lines first, transmutes last; prefer surviving ENV variants
      const ra = DATA.recipes[a], rb = DATA.recipes[b];
      return (isTransmute(ra) - isTransmute(rb)) || (!ra.environment - !rb.environment) || a - b;
    });
    if (!cands.length) { raw.add(it); return true; }
    let tried = false;
    for (const ri of cands) {
      const r = DATA.recipes[ri];
      const ings = r.ingredients.filter((g) => g.item !== "Power" && g.item !== it);
      if (isTransmute(r) && ings.some((g) => !g.activator && path.has(g.item))) continue;
      tried = true;
      const snapC = { ...chosen }, snapR = new Set(raw);
      chosen[it] = ri;
      const next = new Set(path).add(it);
      if (ings.every((g) => expand(g.item, next) || g.activator)) return true;
      chosen = snapC; raw = snapR;
    }
    if (tried) { raw.add(it); return true; }
    return false;
  };
  if (!expand(target, new Set())) raw.add(target);
  if (!(target in chosen)) return { error: t("errNoRecipe", { x: itemName(target) }) };
  for (const g of Object.keys(extra)) if (!expand(g, new Set())) raw.add(g);

  const internal = Object.keys(chosen);
  const tr = DATA.recipes[chosen[target]];
  const outPerRun = tr.products.filter((p) => p.item === target).reduce((s, p) => s + p.count, 0);
  const fullSpeed = outPerRun * 60 / tr.time;
  const rate = rateInput > 0 ? rateInput : fullSpeed;

  // A·x = b over internal items (row = item balance, col = that item's recipe runs/min)
  const idx = {}; internal.forEach((it, k) => (idx[it] = k));
  const n = internal.length;
  const A = Array.from({ length: n }, () => new Array(n).fill(0));
  const b = new Array(n).fill(0);
  for (let ci = 0; ci < n; ci++) {
    const it = internal[ci];
    for (let ri = 0; ri < n; ri++) A[ci][ri] = netPerRun(DATA.recipes[chosen[internal[ri]]], it);
    b[ci] = (it === target ? rate : 0) + (extra[it] || 0);
  }
  const sol = solveLinear(A, b);
  if (!sol) return { error: t("errDegenerate") };
  const runs = {}; internal.forEach((it) => (runs[it] = sol[idx[it]]));

  // net flow per item + machines + power
  const net = {}; const machines = []; let totalMachines = 0, power = 0;
  const add = (k, v) => (net[k] = (net[k] || 0) + v);
  for (const it of internal) {
    const r = DATA.recipes[chosen[it]]; const x = runs[it];
    for (const p of r.products) add(p.item, p.count * x);
    for (const g of r.ingredients) if (g.item !== "Power") add(g.item, -g.count * x);
    const m = x * r.time / 60;
    totalMachines += m;
    power += (DATA.facilities[r.facility]?.power || 0) * m;
    machines.push({ item: it, facility: r.facility, runs: x, machines: m });
  }
  delete net["Power"];  // Power is generated/drawn, not a piped material (see balance.py)
  for (const [g, v] of Object.entries(extra)) add(g, -v);   // fed to the dispersers
  machines.sort((a, b) => a.facility.localeCompare(b.facility) || a.item.localeCompare(b.item));

  const inputs = internal.length
    ? Object.entries(net).filter(([it, v]) => !(it in chosen) && v < -1e-9)
        .map(([it, v]) => ({ item: it, amount: -v })).sort((a, b) => b.amount - a.amount)
    : [];
  const byproducts = Object.entries(net).filter(([it, v]) => it !== target && v > 1e-9)
    .map(([it, v]) => ({ item: it, amount: v })).sort((a, b) => b.amount - a.amount);

  // edges: for each internal item, one edge per (ingredient -> item) with its /min flow
  const edges = [];
  for (const it of internal) {
    const r = DATA.recipes[chosen[it]]; const x = runs[it];
    for (const g of r.ingredients) {
      if (g.item === "Power") continue;
      edges.push({ from: g.item, to: it, rate: g.count * x });
    }
  }

  const recipeOf = {};
  for (const it of internal) recipeOf[it] = DATA.recipes[chosen[it]];

  return {
    mode: "direct", target, rate, fullSpeed, chosen, raw, internal, runs, recipeOf,
    machines, totalMachines, power, inputs, byproducts, edges, net,
    envUse: internal.map((it) => [recipeOf[it], runs[it] * recipeOf[it].time / 60]),
    recycle: recycleMap(inputs, [...internal, ...byproducts.map((x) => x.item)]),
  };
}

// -------------------------------------------- closed-loop LP planner (--lp)
// Mirrors balance.py's plan_lp: choose runs/min over *all* recipes to meet the
// target(s) while auto-closing byproduct/seed cycles, honoring facility caps,
// open-loop items, self-power and an objective (min machines / max output /
// max revenue). Summary tables are exact; the flow graph uses each item's
// main-product recipe (byproduct loops are reflected in the tables, per balance.py).
function planLP(opts) {
  return planBest((allowed, extra) => solveLP(opts, allowed, extra));
}
function solveLP(opts, allowed, extra) {
  const recipes = DATA.recipes.slice();
  let selfp = null;
  if (opts.selfpower) {
    const cap = BATTERY_POWER[opts.selfpower];
    if (!cap) return { error: t("errNoBattery", { x: itemName(opts.selfpower) }) };
    recipes.push({ facility: "Thermal Bank", time: 40, mode: "Power", _selfpower: true,
      ingredients: [{ item: opts.selfpower, count: 1 }], products: [{ item: "Power", count: cap }] });
    selfp = { item: opts.selfpower, cap };
  }
  const banItems = new Set(DEFAULT_BAN_ITEMS);
  (opts.banItems || []).forEach((x) => banItems.add(x));
  (opts.allowItems || []).forEach((x) => banItems.delete(x));
  const banFacs = new Set(opts.banFacs || []);
  const eventOK = eventOnly(opts.targets[0].item);

  const R = [];
  recipes.forEach((r, i) => {
    if (!r.time || banFacs.has(r.facility)) return;
    if (!r._selfpower && !usable(r, eventOK, allowed)) return;
    if (r.facility === "Thermal Bank" && !r._selfpower) return;
    if (r.ingredients.some((g) => banItems.has(g.item))) return;
    R.push(i);
  });

  const itemSet = new Set(), produced = new Set();
  for (const i of R) {
    for (const g of recipes[i].ingredients) if (g.item !== "Power") itemSet.add(g.item);
    for (const p of recipes[i].products) if (p.item !== "Power") { itemSet.add(p.item); produced.add(p.item); }
  }
  const target = opts.targets[0].item;
  if (!produced.has(target)) return { error: t("errNoRecipe", { x: itemName(target) }) };

  const raws = new Set([...DEFAULT_RAW_LP, ...GATHERED]);
  (opts.extraRaws || []).forEach((x) => raws.add(x));
  (opts.openItems || []).forEach((x) => raws.add(x));
  for (const it of itemSet) if (!produced.has(it)) raws.add(it);
  const constrained = [...itemSet].filter((it) => !raws.has(it));

  const targets = {};
  for (const t of opts.targets) targets[t.item] = t.rate || 0;
  for (const [g, v] of Object.entries(extra)) targets[g] = (targets[g] || 0) + v;  // disperser feed

  const A = [], b = [];
  for (const it of constrained) { A.push(R.map((i) => -netPerRun(recipes[i], it))); b.push(-(targets[it] || 0)); }
  for (const [fac, cap] of Object.entries(opts.caps || {})) {
    A.push(R.map((i) => recipes[i].facility === fac ? recipes[i].time / 60 : 0)); b.push(cap);
  }
  for (const [it, cap] of Object.entries(opts.supply || {})) {
    A.push(R.map((i) => -netPerRun(recipes[i], it))); b.push(cap);
  }
  if (selfp) {
    A.push(R.map((i) => {
      const r = recipes[i], gen = r._selfpower ? selfp.cap : 0;
      return ((DATA.facilities[r.facility]?.power || 0) - gen) * r.time / 60;
    })); b.push(0);
  }

  const maximize = opts.objective === "max" ? target : null;
  let c;
  if (opts.objective === "revenue") {
    const rev = {};
    for (const t of opts.targets) { const info = DATA.items[t.item]; if (info && info.price != null) rev[t.item] = info.price; }
    if (!Object.keys(rev).length) return { error: t("errNoPrice") };
    c = R.map((i) => -Object.entries(rev).reduce((s, [it, p]) => s + p * netPerRun(recipes[i], it), 0) + 1e-4 * recipes[i].time / 60);
  } else if (maximize) {
    c = R.map((i) => -1e6 * netPerRun(recipes[i], maximize) + recipes[i].time / 60);
  } else {
    c = R.map((i) => recipes[i].time / 60);
  }

  const res = linprog(c, A, b);
  if (!res.success) return { error: res.unbounded ? t("errUnbounded") : t("errInfeasible") };

  const x = {};
  R.forEach((ri, k) => { if (res.x[k] > 1e-6) x[ri] = res.x[k]; });

  const net = {}, grossIn = {}, grossOut = {};
  const addTo = (o, k, v) => (o[k] = (o[k] || 0) + v);
  const machines = []; let totalMachines = 0, power = 0, gen = 0, burned = 0;
  for (const ri in x) {
    const r = recipes[ri], runs = x[ri], mm = runs * r.time / 60;
    for (const p of r.products) { addTo(net, p.item, p.count * runs); addTo(grossOut, p.item, p.count * runs); }
    for (const g of r.ingredients) { addTo(net, g.item, -g.count * runs); addTo(grossIn, g.item, g.count * runs); }
    power += (DATA.facilities[r.facility]?.power || 0) * mm;
    totalMachines += mm;
    if (r._selfpower) { gen += selfp.cap * mm; burned = runs; }
    machines.push({ item: r.products[0] ? r.products[0].item : "?", facility: r.facility, runs, machines: mm });
  }
  delete net["Power"];
  for (const [g, v] of Object.entries(extra)) addTo(net, g, -v);   // fed to the dispersers
  machines.sort((a, b) => a.facility.localeCompare(b.facility) || a.item.localeCompare(b.item));

  const openSet = new Set(opts.openItems || []);
  const sale = new Set([target, ...(maximize ? [maximize] : [])]);
  const inputs = Object.entries(net).filter(([it, v]) => v < -1e-6 && !openSet.has(it))
    .map(([it, v]) => ({ item: it, amount: -v })).sort((a, b) => b.amount - a.amount);
  const byproducts = Object.entries(net).filter(([it, v]) => v > 1e-6 && !sale.has(it) && !openSet.has(it))
    .map(([it, v]) => ({ item: it, amount: v })).sort((a, b) => b.amount - a.amount);
  const openLoop = [...openSet].map((it) => ({ item: it, supply: grossIn[it] || 0, dump: grossOut[it] || 0 }))
    .filter((o) => o.supply || o.dump).sort((a, b) => b.supply - a.supply);

  // producer map for the graph: each item's main-product recipe with the largest run
  const recipeOf = {}, runs = {};
  for (const [ri, rn] of Object.entries(x).map(([k, v]) => [+k, v]).sort((a, b) => b[1] - a[1])) {
    const r = recipes[ri], main = r.products[0] ? r.products[0].item : null;
    if (main && main !== "Power" && !raws.has(main) && !(main in recipeOf)) { recipeOf[main] = r; runs[main] = rn; }
  }
  const internal = Object.keys(recipeOf);
  const edges = [];
  for (const it of internal) {
    const r = recipeOf[it], rn = runs[it];
    for (const g of r.ingredients) if (g.item !== "Power") edges.push({ from: g.item, to: it, rate: g.count * rn });
  }

  let revenue = null;                                // sale revenue if the target is priced
  let revTotal = 0, revCur = "", anyPriced = false;
  for (const it of sale) {
    const inf = DATA.items[it];
    if (inf && inf.price != null) { revTotal += (net[it] || 0) * inf.price; revCur = inf.currency || ""; anyPriced = true; }
  }
  if (anyPriced) revenue = { total: revTotal, currency: revCur };

  return {
    mode: "lp", target, rate: net[target] || 0, recipeOf, runs, internal, edges, net,
    machines, totalMachines, power, inputs, byproducts, openLoop,
    selfpower: selfp ? { ...selfp, gen, burned, net: gen - power } : null,
    objective: opts.objective, maximize, sale: [...sale], revenue,
    envUse: Object.entries(x).map(([ri, rn]) => [recipes[ri], rn * recipes[ri].time / 60]),
    recycle: recycleMap(inputs, [...internal, ...byproducts.map((x) => x.item)]),
  };
}

// --------------------------------------------------------------- DAG layout
const NODE_W = 216, NODE_H = 74, COL_W = 268, PAD = 40, V_GAP = 26, DUMMY_H = 12;

function layout(plan) {
  const realIds = new Set(plan.internal);
  for (const e of plan.edges) { realIds.add(e.from); realIds.add(e.to); }

  // longest-path level from raw sources (raw = 0); higher = more processed = further right
  const level = {}; const memo = {};
  const lv = (it, stack) => {
    if (memo[it] != null) return memo[it];
    if (!(it in plan.recipeOf)) return (memo[it] = 0);         // raw leaf
    if (stack.has(it)) return 0;                               // cycle guard
    stack.add(it);
    let mx = 0;
    for (const g of plan.recipeOf[it].ingredients) {
      if (g.item === "Power") continue;
      mx = Math.max(mx, 1 + lv(g.item, stack));
    }
    const env = plan.recipeOf[it].environment;          // its ENV zone sits left of it too
    if (env && env in plan.recipeOf) mx = Math.max(mx, 1 + lv(env, stack));
    stack.delete(it);
    return (memo[it] = mx);
  };
  for (const id of realIds) level[id] = lv(id, new Set());
  const maxLevel = Math.max(0, ...Object.values(level));

  // Dummy waypoints: break every multi-column edge into a chain of thin nodes, one
  // per intermediate column, so a long flow (a raw input reused far downstream, or a
  // recycle loop feeding a byproduct back) gets its OWN vertical channel from the
  // crossing-minimization pass instead of a straight line hidden behind the cards it
  // crosses. Feedback edges (consumer at/left of producer) are the recycle loops.
  const meta = {};
  for (const id of realIds) meta[id] = { dummy: false, level: level[id] };
  const columns = Array.from({ length: maxLevel + 1 }, () => []);
  for (const id of realIds) columns[level[id]].push(id);

  const adj = {};
  const link = (a, b) => { (adj[a] = adj[a] || []).push(b); (adj[b] = adj[b] || []).push(a); };
  const routes = []; let dseq = 0;
  for (const e of plan.edges) {
    const lu = level[e.from], lw = level[e.to];
    const feedback = lw <= lu;
    const mids = [];
    if (lw > lu) for (let L = lu + 1; L < lw; L++) mids.push(L);
    else for (let L = lu - 1; L > lw; L--) mids.push(L);
    const chain = [e.from];
    for (const L of mids) {
      const did = `__d${dseq++}`;
      meta[did] = { dummy: true, level: L };
      columns[L].push(did);
      chain.push(did);
    }
    chain.push(e.to);
    for (let i = 0; i < chain.length - 1; i++) link(chain[i], chain[i + 1]);
    routes.push({ chain, rate: e.rate, feedback, env: e.env || null });
  }

  // crossing-minimization: barycenter sweeps over real + dummy nodes together
  const pos = {};
  columns.forEach((col) => col.forEach((id, i) => (pos[id] = i)));
  for (let iter = 0; iter < 8; iter++) {
    const seq = iter % 2 === 0 ? [...columns.keys()] : [...columns.keys()].reverse();
    for (const c of seq) {
      columns[c].sort((a, b) => bary(a, adj, pos) - bary(b, adj, pos));
      columns[c].forEach((id, i) => (pos[id] = i));
    }
  }

  // vertical placement: mixed slot heights (thin dummies, tall cards), columns centered
  const hOf = (id) => (meta[id].dummy ? DUMMY_H : NODE_H);
  const colH = (col) => col.reduce((s, id) => s + hOf(id), 0) + V_GAP * Math.max(0, col.length - 1);
  const maxH = Math.max(NODE_H, ...columns.map(colH));
  const centerY = PAD + maxH / 2;
  const nodes = {};
  columns.forEach((col, c) => {
    const x = PAD + c * COL_W;
    let y = centerY - colH(col) / 2;
    for (const id of col) {
      const dummy = meta[id].dummy, h = hOf(id);
      nodes[id] = {
        id, x, y, w: dummy ? 0 : NODE_W, h, dummy,
        raw: !dummy && !(id in plan.recipeOf),
        target: id === plan.target,
        cx: x + NODE_W / 2, cy: y + h / 2,
      };
      y += h + V_GAP;
    }
  });

  // resolve each edge to a routed polyline. A waypoint contributes TWO points — the
  // column's entry and exit edge at its reserved channel y — so the line runs FLAT
  // across each column it passes (that y is guaranteed clear of cards) and only slopes
  // inside the empty inter-column gaps. That's what keeps a long flow from ever
  // crossing behind a card it isn't connected to.
  const edges = routes.map((r) => {
    const pts = [];
    r.chain.forEach((id, i) => {
      const nd = nodes[id];
      if (nd.dummy) {
        const l = { x: nd.x, y: nd.cy }, rt = { x: nd.x + NODE_W, y: nd.cy };
        if (r.feedback) { pts.push(rt, l); } else { pts.push(l, rt); }
      } else {
        const first = i === 0;
        const rightSide = r.feedback ? !first : first;
        pts.push({ x: rightSide ? nd.x + NODE_W : nd.x, y: nd.cy });
      }
    });
    return { points: pts, rate: r.rate, feedback: r.feedback, env: r.env };
  });

  const width = PAD * 2 + maxLevel * COL_W + NODE_W;
  const height = maxH + PAD * 2;
  const tnode = nodes[plan.target];
  const focusX = tnode ? tnode.x + NODE_W / 2 : width / 2;
  return { nodes, edges, width, height, focusX };
}
function bary(id, adj, pos) {
  const ns = adj[id] || [];
  if (!ns.length) return pos[id];
  let s = 0, n = 0;
  for (const m of ns) if (pos[m] != null) { s += pos[m]; n++; }
  return n ? s / n : pos[id];
}
function edgePath(pts) {
  let d = `M${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const p0 = pts[i - 1], p1 = pts[i], dx = (p1.x - p0.x) * 0.5;
    d += ` C${(p0.x + dx).toFixed(1)},${p0.y.toFixed(1)} ` +
         `${(p1.x - dx).toFixed(1)},${p1.y.toFixed(1)} ${p1.x.toFixed(1)},${p1.y.toFixed(1)}`;
  }
  return d;
}

// ------------------------------------------------------------------ rendering
let view = { tx: 0, ty: 0, scale: 1 };
let lastLayout = null;

function renderGraph(plan) {
  const svg = $("graph");
  const nodesLayer = $("nodes");
  const L = layout(plan);
  lastLayout = L;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  while (nodesLayer.firstChild) nodesLayer.removeChild(nodesLayer.firstChild);

  const vp = document.createElementNS(SVGNS, "g");
  vp.setAttribute("id", "viewport");
  const gEdges = document.createElementNS(SVGNS, "g");
  vp.appendChild(gEdges);
  svg.appendChild(vp);

  // edges (routed polylines through the layout's waypoints)
  const maxRate = Math.max(...L.edges.map((e) => e.rate), 1);
  for (const e of L.edges) {
    const pts = e.points;
    if (pts.length < 2) continue;
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", edgePath(pts));
    if (e.env) {                     // ENV zone coverage, not a material flow: dashed, colored, unlabeled
      path.setAttribute("class", `edge env-link env-${ENV_CLASS[e.env] || "other"}`);
      gEdges.appendChild(path);
      continue;
    }
    path.setAttribute("class", "edge" + (e.feedback ? " feedback" : ""));
    path.setAttribute("stroke-width", (1.2 + 2.6 * (e.rate / maxRate)).toFixed(2));
    gEdges.appendChild(path);

    // label on the segment nearest the middle of the route
    const k = Math.max(1, Math.floor(pts.length / 2));
    const a = pts[k - 1], b = pts[k];
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2 - 2;
    const txt = document.createElementNS(SVGNS, "text");
    txt.setAttribute("class", "edge-label");
    txt.setAttribute("x", mx); txt.setAttribute("y", my);
    txt.setAttribute("text-anchor", "middle");
    txt.textContent = fnum(e.rate) + t("perMin");
    const bg = document.createElementNS(SVGNS, "rect");
    const w = txt.textContent.length * 6 + 6;
    bg.setAttribute("class", "edge-label-bg");
    bg.setAttribute("x", mx - w / 2); bg.setAttribute("y", my - 10);
    bg.setAttribute("width", w); bg.setAttribute("height", 13); bg.setAttribute("rx", 3);
    gEdges.appendChild(bg); gEdges.appendChild(txt);
  }

  // nodes: plain HTML cards absolutely positioned in an overlay div, NOT SVG
  // foreignObject — Chromium/WebKit have a longstanding bug where <img> content
  // inside a foreignObject doesn't repaint reliably when an ancestor SVG group's
  // transform attribute changes fast (e.g. every pointermove while dragging), so
  // icons visually "stick" mid-drag. A plain HTML layer with a parallel CSS
  // transform (see applyView) doesn't have that problem.
  for (const id in L.nodes) {
    const nd = L.nodes[id];
    if (nd.dummy) continue;                  // waypoints are routing-only, not cards
    const div = document.createElement("div");
    // machines inside an ENV zone (and the zone's disperser) get that ENV's colored frame
    const env = plan.recipeOf[id] && (plan.recipeOf[id].environment || (plan.recipeOf[id]._disperser && id));
    div.className = `gnode t${tierOf(id)} ${nd.raw ? "raw" : "prod"}${nd.target ? " target" : ""}`
      + (env ? ` in-env env-${ENV_CLASS[env] || "other"}` : "")
      + (plan.recipeOf[id] && plan.recipeOf[id]._disperser ? " env-src" : "");
    div.style.left = nd.x + "px";
    div.style.top = nd.y + "px";
    div.style.width = nd.w + "px";
    div.style.height = nd.h + "px";
    div.innerHTML = nodeHTML(id, nd, plan);
    div.addEventListener("mousemove", (ev) => showTip(ev, id, nd, plan));
    div.addEventListener("mouseleave", hideTip);
    nodesLayer.appendChild(div);
  }

  svg.removeAttribute("hidden");
  nodesLayer.removeAttribute("hidden");
  $("placeholder").setAttribute("hidden", "");
  fitView();
}

function nodeHTML(id, nd, plan) {
  const nameHTML = `<span class="en">${esc(itemName(id))}</span>`;
  if (nd.raw) {
    const open = plan.openLoop && plan.openLoop.find((o) => o.item === id);
    const rec = (plan.recycle || {})[id] ? `<span class="recyc">${t("recyclable")}</span>` : "";
    const amt = plan.inputs.find((x) => x.item === id);
    const label = open ? `<span class="recyc">${t("openLoopLabel")}</span>` : `${t("rawInput")} ${rec}`;
    const val = open ? fnum(open.supply) : (amt ? fnum(amt.amount) : "");
    return `<span class="bar"></span>
      <img class="ic" src="${itemIcon(id)}" alt="" loading="lazy">
      <div class="meta"><div class="nm">${nameHTML}</div>
        <div class="mc">${label}</div></div>
      <div class="rt"><span class="out">${val}</span></div>`;
  }
  const r = plan.recipeOf[id];
  const m = plan.machines.find((x) => x.item === id);
  if (r._disperser) {                          // ENV zone = its Gas Dispersing Unit(s)
    return `<span class="bar"></span>
      <img class="ic" src="${itemIcon(id)}" alt="" loading="lazy">
      <div class="meta">
        <div class="nm">${nameHTML}</div>
        <div class="mc"><img class="fic" src="${facIcon(r.facility)}" alt="">
          <span class="fnm">${esc(facName(r.facility))}</span></div>
      </div>
      <div class="rt">
        <span class="out">${t("zonesN", { n: fnum(m.machines) })}</span>
        <span class="mct">×${fnum(m.machines)}</span>
      </div>`;
  }
  const out = netPerRun(r, id) * plan.runs[id];
  const envTag = r.environment
    ? `<span class="envtag">${esc(t({ "Stable ENV": "envStable", "Acrid ENV": "envAcrid" }[r.environment] || r.environment))}</span>` : "";
  // machine count is its own fixed element (never truncated by the name), the machine
  // name sits beside a larger facility icon (localized so it stays short).
  return `<span class="bar"></span>${envTag}
    <img class="ic" src="${itemIcon(id)}" alt="" loading="lazy">
    <div class="meta">
      <div class="nm">${nameHTML}</div>
      <div class="mc"><img class="fic" src="${facIcon(r.facility)}" alt="">
        <span class="fnm">${esc(facName(r.facility))}</span></div>
    </div>
    <div class="rt">
      <span class="out">${fnum(out)}<small>${t("perMin")}</small></span>
      <span class="mct">×${fnum(m.machines)}</span>
    </div>`;
}

// ------------------------------------------------------------------- panel
function renderPanel(plan) {
  $("panel").removeAttribute("hidden");
  $("graph-toolbar").removeAttribute("hidden");

  const tgt = plan.target;
  const isLp = plan.mode === "lp";
  const pm = t("perMin");
  const objText = !isLp ? `@ ${fnum(plan.rate)}${pm}`
    : plan.objective === "max" ? `${t("maximizeTo")} ${fnum(plan.rate)}${pm}`
    : plan.objective === "revenue" ? `${t("maxRevenueTo")} ${fnum(plan.revenue.total)} ${esc(plan.revenue.currency)}${pm}`
    : `${t("minMachinesAt")} ${fnum(plan.rate)}${pm}`;
  $("plan-title").innerHTML =
    `<b>${esc(itemName(tgt))}</b> · ${objText}` +
    (isLp ? ` <span class="tag">${t("closedLoopTag")}</span>` : "");

  // summary
  const priced = DATA.items[tgt] || {};
  const unit = `<small> ${t("units")}</small>`;
  // base footprint counts whole (placeable) machines per step: Σ ⌈machines⌉ × tiles
  const footprint = plan.machines.reduce((s, m) => s + Math.ceil(m.machines - 1e-9) * facArea(m.facility), 0);
  const stats = [
    [t("machines"), fnum(plan.totalMachines), ""],
    [t("powerDraw"), fnum(plan.power), unit],
    [t("footprint"), fnum(footprint), `<small> ${t("tiles")}</small>`],
  ];
  if (plan.selfpower) {
    stats.push([t("powerNet"), fnum(plan.selfpower.net), unit]);
    stats.push([t("burns"), fnum(plan.selfpower.burned),
      `<small> ${esc(itemName(plan.selfpower.item))}${pm}</small>`]);
  } else {
    stats.push([t("output"), fnum(plan.rate), `<small>${pm}</small>`]);
    stats.push(isLp ? [t("objectiveShort"), esc({ min: t("fewest"), max: t("maxOut"), revenue: t("maxRev") }[plan.objective]), ""]
                    : [t("fullLine"), fnum(plan.fullSpeed), `<small>${pm}</small>`]);
  }
  let html = stats.map(([k, v, u]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${v}${u}</div></div>`).join("");
  const revenue = isLp ? (plan.revenue ? plan.revenue.total : null)
                       : (priced.price != null ? plan.rate * priced.price : null);
  const revCur = isLp ? (plan.revenue ? plan.revenue.currency : "") : (priced.currency || "");
  if (revenue != null) {
    html += `<div class="stat wide"><div class="k">${t("revenue")} (${esc(curName(revCur))})</div>
      <div class="v rev">${fnum(revenue)}<small> ${pm}</small></div></div>`;
  }
  $("summary").innerHTML = html;

  // machines
  $("mach-total").textContent = t("machTotal", { n: fnum(plan.totalMachines) });
  $("machines").innerHTML = plan.machines.map((m) => `
    <div class="row">
      <img src="${facIcon(m.facility)}" alt="">
      <div class="name">${esc(facName(m.facility))}${facSize(m.facility)
        ? ` <span class="szt">${esc(facSize(m.facility).replace("x", "×"))}</span>` : ""}
        <span class="cn">→ ${esc(itemName(m.item))}</span></div>
      <div class="val">×${fnum(m.machines)}</div>
    </div>`).join("");

  // raw inputs
  $("inputs").innerHTML = plan.inputs.length ? plan.inputs.map((x) => {
    const src = (plan.recycle || {})[x.item];
    const rec = src ? `<span class="recyc">↺ ${esc(recycleText(src))}</span>` : "";
    return `<div class="row">
      <img src="${itemIcon(x.item)}" alt="">
      <div class="name">${esc(itemName(x.item))}<div class="sub2">${rec}</div></div>
      <div class="val">${fnum(x.amount)}</div>
    </div>`;
  }).join("") : `<div class="empty">${t("noInputs")}</div>`;

  // byproducts
  const bp = $("byproducts");
  $("byproducts-block").style.display = plan.byproducts.length ? "" : "none";
  bp.innerHTML = plan.byproducts.map((x) => `
    <div class="row">
      <img src="${itemIcon(x.item)}" alt="">
      <div class="name">${esc(itemName(x.item))}</div>
      <div class="val dim">+${fnum(x.amount)}</div>
    </div>`).join("");

  // open-loop fluids (LP only): supply externally + dump the production
  const olBlock = $("openloop-block");
  const openLoop = plan.openLoop || [];
  olBlock.style.display = openLoop.length ? "" : "none";
  $("openloop").innerHTML = openLoop.map((o) => {
    const netV = o.supply - o.dump;
    const ns = (netV >= 0 ? "+" : "") + fnum(netV);
    return `<div class="row">
      <img src="${itemIcon(o.item)}" alt="">
      <div class="name">${esc(itemName(o.item))}
        <div class="sub2">${t("supply")} ${fnum(o.supply)} · ${t("dump")} ${fnum(o.dump)}</div></div>
      <div class="val ${netV >= 0 ? "" : "dim"}">${ns}</div>
    </div>`;
  }).join("");

  // ENV zones (gas 1.4+): each zone = one Gas Dispersing Unit (already counted in Machines)
  const env = plan.env || [];
  const ch = plan.envChoice || {};
  const optNote = ch.mode !== "opt" ? t("envMinNote")
    : ch.base ? t("envOptSaved", { n: fnum(planScore(ch.base) - planScore(plan)) })
    : optionalEnvs.length ? t("envOptSkip") : "";
  $("env-block").style.display = env.length || optNote ? "" : "none";
  $("env-note").textContent = optNote;
  $("envs").innerHTML = env.map((e) => `
    <div class="row env-row env-${ENV_CLASS[e.env] || "other"}">
      <img src="${itemIcon(e.env)}" alt="">
      <div class="name">${esc(itemName(e.env))}
        <div class="sub2">${esc(t("envFeed", { n: fnum(ENV_FEED * e.zones), x: itemName(e.gas) }))}</div>
        <div class="sub2">${esc(e.where.map(([f, m]) => `${facName(f)} ×${fnum(m)}`).join(", "))}</div></div>
      <div class="val">${t("zonesN", { n: e.zones })}</div>
    </div>`).join("");

  renderAlternatives(plan);
}

function renderAlternatives(plan) {
  const block = $("alts-block"), host = $("alts");
  if (plan.mode === "lp") { block.setAttribute("hidden", ""); host.innerHTML = ""; return; }
  const multi = plan.internal.filter((it) => (producers[it] || []).length > 1);
  if (!multi.length) { block.setAttribute("hidden", ""); host.innerHTML = ""; return; }
  block.removeAttribute("hidden");
  host.innerHTML = multi.map((it) => {
    const opts = producers[it].map((ri) => {
      const r = DATA.recipes[ri];
      const ings = r.ingredients.filter((g) => g.item !== "Power")
        .map((g) => `${itemName(g.item)}×${fnum(g.count)}`).join(" + ") || "—";
      const sel = ri === plan.chosen[it] ? "selected" : "";
      return `<option value="${ri}" ${sel}>${esc(facName(r.facility))}: ${esc(ings)}</option>`;
    }).join("");
    return `<div class="alt-item">
      <div class="alt-label"><img src="${itemIcon(it)}" alt="">${esc(itemName(it))}</div>
      <select data-item="${esc(it)}">${opts}</select>
    </div>`;
  }).join("");
  host.querySelectorAll("select").forEach((sel) => {
    sel.addEventListener("change", () => {
      picks[sel.dataset.item] = +sel.value;
      runPlan(plan.target, plan.rate);
    });
  });
}

// ------------------------------------------------------------------ search
let sugItems = [], sugActive = -1;

// JEC-style pinyin match on a 中文 name via pinyin-pro (vendor/, MIT): full pinyin,
// initials or a mix per syllable ("zhuotlj" / "ztlj" → 灼铜零件), polyphones (重 =
// zhong/chong), hanzi+pinyin mixed ("灼tong"), v for ü. Characters must be contiguous.
// Returns matched char indices, or null. No-ops if the library failed to load.
const PY_OPTS = { precision: "start", continuous: true, v: true, space: "ignore" };
function pinyinHit(cn, q) {
  if (!cn || !q || !window.pinyinPro || !/[a-z]/i.test(q)) return null;
  try { return window.pinyinPro.match(cn, q.toLowerCase(), PY_OPTS) || null; } catch { return null; }
}

function updateSuggest() {
  const q = $("target").value.trim().toLowerCase();
  const ul = $("suggest");
  if (!q) { ul.setAttribute("hidden", ""); return; }
  const scored = [];
  for (const name of plannable) {
    const en = name.toLowerCase(), cn = cnOf(name), ja = jaOf(name);
    let s = -1;
    if (en.startsWith(q)) s = 0;
    else if (en.includes(q)) s = 1;
    else if ((cn && cn.includes(q)) || (ja && ja.includes(q))) s = 2;   // match any language
    else {                                    // pinyin on the 中文 name: start of name ranks first
      const hit = pinyinHit(cn, q);
      if (hit) s = hit[0] === 0 ? 2.5 : 3;
    }
    if (s >= 0) scored.push([s, name]);
  }
  scored.sort((a, b) => a[0] - b[0]   // pinyin tier: the closest (shortest) 中文 name first
    || (a[0] >= 2.5 ? cnOf(a[1]).length - cnOf(b[1]).length : 0) || a[1].localeCompare(b[1]));
  sugItems = scored.slice(0, 40).map((x) => x[1]);
  sugActive = sugItems.length ? 0 : -1;
  if (!sugItems.length) { ul.setAttribute("hidden", ""); return; }
  // primary = localized name; secondary = the English key (a cross-language search aid)
  ul.innerHTML = sugItems.map((name, i) => {
    const alt = locale === "en" ? cnOf(name) : name;
    return `
    <li data-i="${i}" class="${i === sugActive ? "active" : ""}">
      <img src="${itemIcon(name)}" alt="" loading="lazy">
      <span class="en">${esc(itemName(name))}</span>
      <span class="cn">${esc(alt)}</span>
    </li>`;
  }).join("");
  ul.removeAttribute("hidden");
  ul.querySelectorAll("li").forEach((li) => {
    li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(sugItems[+li.dataset.i]); });
  });
}
function choose(name) {
  $("target").value = itemName(name);      // show localized; plan keeps the canonical id
  $("suggest").setAttribute("hidden", "");
  picks = {};
  runPlan(name, parseFloat($("rate").value) || 0);
}

// -------------------------------------------------------------- run + errors
function defaultRateOf(it) {
  const cands = producers[it] || [];
  if (!cands.length) return 6;
  const r = DATA.recipes[cands[0]];
  const out = r.products.filter((p) => p.item === it).reduce((s, p) => s + p.count, 0);
  return out * 60 / r.time;
}
function buildLPOpts(target, rate) {
  const floor = lp.objective === "min" ? (rate || defaultRateOf(target)) : (rate || 0);
  return {
    targets: [{ item: target, rate: floor }],
    objective: lp.objective,
    caps: { ...lp.caps },
    selfpower: lp.selfpower || null,
    openItems: [...lp.open],
    extraRaws: [...lp.raw],
  };
}
function runPlan(target, rate) {
  current = { target, rate };
  const plan = lp.on ? planLP(buildLPOpts(target, rate)) : planLine(target, rate);
  if (plan.error) {
    $("graph").setAttribute("hidden", "");
    $("panel").setAttribute("hidden", "");
    $("graph-toolbar").setAttribute("hidden", "");
    const ph = $("placeholder");
    ph.removeAttribute("hidden");
    ph.innerHTML = `<p class="big">${t("cantPlan")}</p><p>${esc(plan.error)}</p>`;
    return;
  }
  renderPanel(plan);   // first: it unhides the panel + toolbar, which fitView measures
  renderGraph(plan);
}
function replan() { if (current.target) runPlan(current.target, current.rate); }

// ------------------------------------------------------------ pan / zoom
function applyView() {
  const vp = document.querySelector("#viewport");
  const nodesLayer = $("nodes");
  if (vp) vp.setAttribute("transform", `translate(${view.tx},${view.ty}) scale(${view.scale})`);
  // same transform, kept in lockstep, on the HTML overlay that holds the node cards
  if (nodesLayer) nodesLayer.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`;
}
const MAX_SCALE = 1.1, READABLE_SCALE = 0.72;
function fitView() {
  if (!lastLayout) return;
  const box = $("graph-host").getBoundingClientRect();
  // the graph toolbar floats over the top of the stage: fit into the area below it
  const tb = $("graph-toolbar");
  const top = tb.hidden ? 0 : Math.max(0, tb.getBoundingClientRect().bottom - box.top);
  const host = { width: box.width, height: box.height - top };
  const m = 0.94;
  const scaleW = host.width / lastLayout.width, scaleH = host.height / lastLayout.height;
  let scale = Math.min(scaleW, scaleH, MAX_SCALE) * m;
  // Fitting a big graph whole shrinks cards to an unreadable size (the machine icon /
  // 中文 name / ×count become illegible). So never go below a readable floor — capped
  // by MAX_SCALE and what the height allows — and let the user pan to the rest. Small
  // graphs still fit fully; only large ones start zoomed-in and pannable.
  scale = Math.max(scale, Math.min(READABLE_SCALE, scaleH * m, MAX_SCALE));
  view.scale = scale;

  const gw = lastLayout.width * scale, gh = lastLayout.height * scale;
  // vertical: center if it fits, else start at the top (read downward)
  view.ty = top + (gh <= host.height ? (host.height - gh) / 2 : PAD * scale);
  if (gw <= host.width) {
    view.tx = (host.width - gw) / 2;                     // fits: center
  } else {                                               // overflows: focus the product, clamped
    view.tx = Math.min(0, Math.max(host.width - gw, host.width / 2 - lastLayout.focusX * scale));
  }
  applyView();
}
function zoomAt(cx, cy, factor) {
  const ns = Math.min(2.5, Math.max(0.15, view.scale * factor));
  const k = ns / view.scale;
  view.tx = cx - (cx - view.tx) * k;
  view.ty = cy - (cy - view.ty) * k;
  view.scale = ns;
  applyView();
}

function initInteractions() {
  // Listeners live on #graph-host (the shared parent of the SVG edge layer and the
  // HTML node-card overlay), not on #graph, so a drag/pinch that starts on top of a
  // node card — which is no longer inside the SVG's DOM subtree — still bubbles up
  // and pans/zooms correctly.
  const host = $("graph-host");
  host.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = host.getBoundingClientRect();
    zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });

  // Pointer Events unify mouse/touch/pen: one active pointer pans (mouse-drag or a
  // single finger); two active pointers pinch-zoom around their midpoint and pan with
  // it, which is the gesture phones expect since there's no scroll wheel to zoom with.
  const pointers = new Map();     // pointerId -> last {x,y} in client coords
  let drag = null;                // single-pointer pan anchor {x,y,tx,ty}
  let pinch = null;                // two-pointer state {dist, mx, my} in host-local coords

  const hostLocal = (x, y) => {
    const r = host.getBoundingClientRect();
    return { x: x - r.left, y: y - r.top };
  };
  const pinchState = () => {
    const [a, b] = [...pointers.values()];
    const am = hostLocal(a.x, a.y), bm = hostLocal(b.x, b.y);
    return { dist: Math.hypot(a.x - b.x, a.y - b.y), mx: (am.x + bm.x) / 2, my: (am.y + bm.y) / 2 };
  };

  host.addEventListener("pointerdown", (e) => {
    try { host.setPointerCapture(e.pointerId); } catch (_) { /* some WebViews are flaky with multi-touch capture */ }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
      host.classList.add("panning");
    } else if (pointers.size === 2) {
      drag = null;
      pinch = pinchState();
    }
  });
  host.addEventListener("pointermove", (e) => {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2 && pinch) {
      const now = pinchState();
      zoomAt(pinch.mx, pinch.my, now.dist / pinch.dist);
      view.tx += now.mx - pinch.mx;
      view.ty += now.my - pinch.my;
      applyView();
      pinch = now;
    } else if (pointers.size === 1 && drag) {
      view.tx = drag.tx + (e.clientX - drag.x);
      view.ty = drag.ty + (e.clientY - drag.y);
      applyView();
    }
  });
  const release = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size === 1) {
      const [p] = [...pointers.values()];
      drag = { x: p.x, y: p.y, tx: view.tx, ty: view.ty };
      pinch = null;
    } else if (pointers.size === 2) {
      pinch = pinchState();
    } else {
      drag = null; pinch = null; host.classList.remove("panning");
    }
  };
  host.addEventListener("pointerup", release);
  host.addEventListener("pointercancel", release);

  $("graph-toolbar").addEventListener("click", (e) => {
    const z = e.target.dataset.zoom;
    if (!z) return;
    const rect = host.getBoundingClientRect();
    if (z === "fit") fitView();
    else zoomAt(rect.width / 2, rect.height / 2, z === "in" ? 1.2 : 1 / 1.2);
  });
  window.addEventListener("resize", () => { if (lastLayout) fitView(); });
}

// ------------------------------------------------------------------ tooltip
function showTip(ev, id, nd, plan) {
  const tip = $("tooltip");
  const it = DATA.items[id] || {};
  let lines = "";
  const envName = (e) => t({ "Stable ENV": "envStable", "Acrid ENV": "envAcrid" }[e] || e);
  if (!nd.raw && plan.recipeOf[id]._disperser) {          // an ENV zone's Gas Dispersing Units
    const z = (plan.env || []).find((e) => e.env === id) || { zones: 0, where: [], gas: "" };
    lines = `<div class="line"><b>${esc(facName(DISPERSER))}</b> ×${z.zones} · 3×3 · ${t("envArea")}</div>
      <div class="line">${esc(t("envFeed", { n: fnum(ENV_FEED * z.zones), x: itemName(z.gas) }))} · ${t("powerByGas")}</div>
      <div class="line">${t("envCovers")}<br>${z.where.map(([f, m]) => `${esc(facName(f))} ×${fnum(m)}`).join("<br>")}</div>`;
  } else if (!nd.raw) {
    const r = plan.recipeOf[id];
    const m = plan.machines.find((x) => x.item === id);
    const ing = r.ingredients.filter((g) => g.item !== "Power")
      .map((g) => `${esc(itemName(g.item))} ×${fnum(g.count * plan.runs[id])}${t("perMin")}`).join("<br>") || "—";
    const sz = facSize(r.facility) ? ` · ${esc(facSize(r.facility).replace("x", "×"))}` : "";
    lines = `<div class="line"><b>${esc(facName(r.facility))}</b> ×${fnum(m.machines)}${sz} · ${fnum(r.time)}${t("perRun")} · ${fnum(plan.runs[id])} ${t("runsPerMin")}</div>
      <div class="line">${t("consumes")}<br>${ing}</div>`;
    if (r.environment) lines += `<div class="line">${t("inEnv", { x: esc(envName(r.environment)) })}</div>`;
  } else {
    lines = `<div class="line">${t("externalRaw")}</div>`;
    const src = (plan.recycle || {})[id];
    if (src) lines += `<div class="line">${t("recyclableVia")} ${esc(recycleText(src))}</div>`;
  }
  const alt = locale === "en" ? (it.cn || "") : id;
  tip.innerHTML = `<h4>${esc(itemName(id))} ${alt ? `<span class="cn">${esc(alt)}</span>` : ""}</h4>
    <div class="line">${it.env ? t("envZones") : `${t("tier")} ${it.tier || 1}`}${it.price != null ? ` · ${t("sells")} ${it.price} ${esc(curName(it.currency || ""))}` : ""}</div>${lines}`;
  tip.removeAttribute("hidden");
  const pad = 14;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  const r = tip.getBoundingClientRect();
  if (x + r.width > innerWidth) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight) y = ev.clientY - r.height - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { $("tooltip").setAttribute("hidden", ""); }

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------------ LP UI
function resolveItem(text) {
  const q = text.trim().toLowerCase(), raw = text.trim();
  if (!q) return null;                                   // exact match (EN / 中文 / 日本語) first
  for (const n in DATA.items) if (n.toLowerCase() === q || cnOf(n) === raw || jaOf(n) === raw) return n;
  for (const n in DATA.items) if (n.toLowerCase().includes(q) || cnOf(n).includes(raw) || jaOf(n).includes(raw)) return n;
  for (const n in DATA.items) if (pinyinHit(cnOf(n), raw)) return n;
  return null;
}
function renderChips(hostId, keys, labelFn, onRemove) {
  $(hostId).innerHTML = keys.map((k) =>
    `<span class="chip" data-k="${esc(String(k))}">${labelFn(k)}<button class="chip-x">×</button></span>`).join("");
  $(hostId).querySelectorAll(".chip-x").forEach((btn) => {
    btn.addEventListener("click", () => { onRemove(btn.parentElement.dataset.k); replan(); });
  });
}
function refreshChips() {
  renderChips("lp-caps", Object.keys(lp.caps),
    (f) => `${esc(facName(f))} ≤ ${fnum(lp.caps[f])}`, (k) => delete lp.caps[k]);
  renderChips("lp-open", [...lp.open],
    (k) => esc(itemName(k)), (k) => lp.open.delete(k));
  renderChips("lp-raw", [...lp.raw],
    (k) => esc(itemName(k)), (k) => lp.raw.delete(k));
}
// (re)fill the selects/datalist with localized labels — called on load and lang switch
function populateLPControls() {
  const selSP = $("lp-selfpower").value, selFac = $("lp-cap-fac").value;
  $("lp-items").innerHTML = Object.keys(DATA.items).sort()
    .map((n) => `<option value="${esc(itemName(n))}"></option>`).join("");
  $("lp-cap-fac").innerHTML = Object.keys(DATA.facilities).sort()
    .map((f) => `<option value="${esc(f)}">${esc(facName(f))}</option>`).join("");
  $("lp-selfpower").innerHTML = `<option value="">${t("none")}</option>` +
    Object.keys(BATTERY_POWER).filter((b) => DATA.items[b])
      .map((b) => `<option value="${esc(b)}">${esc(itemName(b))} (${BATTERY_POWER[b]})</option>`).join("");
  $("lp-selfpower").value = selSP; $("lp-cap-fac").value = selFac;
}
function initLP() {
  populateLPControls();
  $("lp-toggle").addEventListener("click", () => {
    lp.on = !lp.on;
    $("lp-toggle").setAttribute("aria-checked", String(lp.on));
    $("lp-toggle").classList.toggle("on", lp.on);
    $("lp-bar").toggleAttribute("hidden", !lp.on);
    replan();
  });
  $("lp-obj").addEventListener("change", (e) => { lp.objective = e.target.value; replan(); });
  $("lp-selfpower").addEventListener("change", (e) => { lp.selfpower = e.target.value; replan(); });
  $("lp-cap-add").addEventListener("click", () => {
    const fac = $("lp-cap-fac").value, n = parseFloat($("lp-cap-n").value);
    if (fac && n > 0) { lp.caps[fac] = n; $("lp-cap-n").value = ""; refreshChips(); replan(); }
  });
  const addItem = (inputId, set) => {
    const it = resolveItem($(inputId).value);
    if (it) { set.add(it); $(inputId).value = ""; refreshChips(); replan(); }
  };
  $("lp-open-add").addEventListener("click", () => addItem("lp-open-in", lp.open));
  $("lp-raw-add").addEventListener("click", () => addItem("lp-raw-in", lp.raw));
  $("lp-open-in").addEventListener("keydown", (e) => { if (e.key === "Enter") addItem("lp-open-in", lp.open); });
  $("lp-raw-in").addEventListener("keydown", (e) => { if (e.key === "Enter") addItem("lp-raw-in", lp.raw); });
}

// ---------------------------------------------------------------- i18n apply
function applyStaticI18n() {
  document.documentElement.lang = locale;   // "en" | "zh" | "ja"
  document.title = t("appTitle");
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => el.setAttribute("placeholder", t(el.dataset.i18nPh)));
  document.querySelectorAll("[data-i18n-title]").forEach((el) => el.setAttribute("title", t(el.dataset.i18nTitle)));
  $("lang").value = locale;
  applyTheme();                             // re-label the theme button in the new language
}
function setLocale(next) {
  locale = next;
  try { localStorage.setItem("locale", locale); } catch (_) { /* private mode */ }
  applyStaticI18n();
  populateLPControls();
  refreshChips();
  if (current.target) $("target").value = itemName(current.target);
  replan();
}

// ------------------------------------------------------------------ theme
// auto → light → dark. "auto" leaves <html> unforced so CSS follows the OS; the
// stored choice is applied before first paint by the inline script in index.html.
const THEMES = ["auto", "light", "dark"];
const THEME_ICON = { auto: "◐", light: "☀", dark: "☾" };
let theme = "auto";
function applyTheme() {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const btn = $("theme");
  btn.textContent = THEME_ICON[theme];
  const label = t("theme" + theme[0].toUpperCase() + theme.slice(1));
  btn.title = label; btn.setAttribute("aria-label", label);
}
function initTheme() {
  try { const s = localStorage.getItem("theme"); if (THEMES.includes(s)) theme = s; } catch (_) { /* private mode */ }
  applyTheme();
  $("theme").addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem("theme", theme); } catch (_) { /* private mode */ }
    applyTheme();
  });
}

// ------------------------------------------------------------------- boot
async function boot() {
  DATA = await fetch("data/planner.json").then((r) => r.json());
  let stored = null;
  try { stored = localStorage.getItem("locale"); } catch (_) { /* private mode */ }
  const nl = (navigator.language || "").toLowerCase();
  locale = (stored && I18N[stored]) ? stored
    : nl.startsWith("zh") ? "zh" : nl.startsWith("ja") ? "ja" : "en";

  buildIndices();
  initInteractions();
  initLP();
  initTheme();
  applyStaticI18n();
  $("lang").addEventListener("change", (e) => setLocale(e.target.value));
  $("env").addEventListener("change", (e) => {
    envMode = e.target.value === "min" ? "min" : "opt";
    replan();
  });

  const input = $("target");
  input.addEventListener("input", updateSuggest);
  input.addEventListener("focus", updateSuggest);
  input.addEventListener("blur", () => setTimeout(() => $("suggest").setAttribute("hidden", ""), 150));
  input.addEventListener("keydown", (e) => {
    const ul = $("suggest");
    if (ul.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sugActive = (sugActive + (e.key === "ArrowDown" ? 1 : -1) + sugItems.length) % sugItems.length;
      ul.querySelectorAll("li").forEach((li, i) => li.classList.toggle("active", i === sugActive));
      const active = ul.querySelector("li.active"); if (active) active.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (sugActive >= 0) choose(sugItems[sugActive]);
    } else if (e.key === "Escape") ul.setAttribute("hidden", "");
  });
  $("rate").addEventListener("keydown", (e) => { if (e.key === "Enter") $("plan").click(); });
  $("plan").addEventListener("click", () => {
    // input may hold a localized name typed in either language — resolveItem matches both
    const resolved = resolveItem(input.value.trim());
    const target = (resolved && producers[resolved]) ? resolved
      : (sugItems.length ? sugItems[Math.max(0, sugActive)] : null);
    if (target) { input.value = itemName(target); picks = {}; runPlan(target, parseFloat($("rate").value) || 0); }
  });

  // demo plan on load
  const demo = ["Heavy Xiranite", "Steel", plannable[0]].find((n) => n && producers[n]);
  if (demo) { input.value = itemName(demo); runPlan(demo, 0); }
}
boot();
