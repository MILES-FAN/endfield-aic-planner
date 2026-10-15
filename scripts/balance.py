#!/usr/bin/env python3
"""Plan an AIC line: machine counts + raw inputs + surplus for a target output rate.

Reads wiki/recipes/recipes.json (built by scrape_recipes.py). Expands the dependency
graph from the target using each item's *main-product* recipe (products[0]), skipping any
recipe that would close a cycle (the 1.4 gas update added reversible fluid⇄gas / solid⇄gas
transmutes, tried last), so an exact rational linear solve gives runs/min for every step.
Items with no usable continuous main-product recipe (ores, powders, pumped/gathered fluids
and gases like Clean Water / Xiragen / Inergen, and pure byproducts like Sewage / Inert
Xircon Effluent) are treated as external leaves.

Gas (1.4+): Transmuting Units need a steady activator feed (6/min Liquid Xiranite /
Xiragen per machine) — a consumed input: the official wiki (wiki.skport.com, entries
1085/1086) lists it under each formula's "Materials Needed". Disable with --no-activator. Recipes tagged with an ENV (Acrid / Stable) must sit inside a Gas
Dispersing Unit zone; the plan lists which zones you need. Event-only recipes are ignored
unless --event (or the target exists only in an event).

Per-min rate of one full-speed machine of a recipe = 60 / time(s); machines needed for a
recipe = runs/min × time / 60 (fractional = one machine at that % speed, or split lines).

Output:
  - machines: facility · product · runs/min · machine-equivalents
  - external inputs (consumed but not produced internally), per /min
  - surplus byproducts (produced but not consumed), per /min
  - note: which leaves are recyclable — a recipe that's a real net source given this plan
    (productive loop like seed→2 plants, or fed by this plan's own products/surplus)
  - --tree: also draw the plan as a CLI flow diagram — a tree from the target down to
    raw inputs, each node tagged with its [machine ×count] and each edge with the /min
    flowing into its parent (shared steps shown once as `↑ above`, cycles as `↻ loop`).

Usage:
  python3 scripts/balance.py 重息壤                  # default: one full-speed line
  python3 scripts/balance.py "Heavy Xiranite" --rate 12
  python3 scripts/balance.py 赫铜装备原件 --pick "息壤=Forge of the Sky"
  python3 scripts/balance.py 重息壤 --raw 清水,污水    # force these as supplied externally
  python3 scripts/balance.py 重息壤 --tree           # + flow diagram (works with --lp too)
  python3 scripts/balance.py 中容武陵电池=30 --lp --open 污水   # recycle all but 污水 (gross in/out)
  python3 scripts/balance.py 灼铜 --tree             # gas chain: 气体反应炉 + 固气转化机 (Acrid ENV)
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES_JSON = ROOT / "wiki" / "recipes" / "recipes.json"

MANUAL_FACILITIES = {"Crafting", "Construct"}  # instant / not continuous lines
ALWAYS_RAW = {"Power"}
# LP-mode defaults: pumped/utility items that are net-sinks even though a recipe
# "produces" them as a byproduct (making Clean Water via Purification costs more
# water upstream), so they can't be closed — supply them externally. Clean Water is
# pumped *outside* the base (Fluid Pumps), so consuming it is free and the right
# objective is fewest in-base machines (= footprint), which favours the ×2 water-fed
# plant recipes over the water-free ones.
DEFAULT_RAW_LP = {"Clean Water", "Power"}
# Gathered gases (Gas Extractors on Xiragen Reservoirs / Inergen Vents). Xiragen *can* be
# transmuted from Liquid Xiranite / Xiranite, but extracting it is the normal source, so
# both planners treat it as an external leaf (override with --pick in greedy mode).
GATHERED = {"Xiragen", "Inergen"}
# Machines whose recipes are reversible state changes (A⇄B). Greedy expansion tries them
# last so e.g. Cuprium Gas comes from a real line, not from un-gasifying something.
TRANSMUTERS = {"Fluid-Gas Transmuting Unit", "Solid-Gas Transmuting Unit"}
# Gas Dispersing Unit: feed ≥6/min of a gas → 13×13 ENV zone (excess wasted; zones may not
# overlap). Both mappings stated on endfield.wiki.gg/wiki/Environment (also Humid←Aquagen,
# Xiranite←Xiragen — no recipes use those yet).
ENV_GAS = {"Stable ENV": "Inergen", "Acrid ENV": "Acridgen"}
ENV_FEED = 6
# Gathered, NOT farmable at scale — don't let the planner route bulk production through
# them (e.g. Wood→Carbon looks cheap but you can't get Wood in volume). Override per-run
# with `--allow 原木`. Plants (Buckflower/Sandleaf/Jincao…) are scalable via Planting Unit.
DEFAULT_BAN_ITEMS = {"Wood"}

# Per-machine power draw (Power units), from each facility page's |power= on
# endfield.wiki.gg, with 提纯机/扩展反应池 confirmed via wiki.skport.com (item 856/855).
# Thermal Bank/Construct/Crafting draw nothing (generator/manual).
POWER = {
    "Forge of the Sky": 50, "Reactor Crucible": 50, "Expanded Crucible": 100,
    "Refining Unit": 5, "Shredding Unit": 5, "Grinding Unit": 50,
    "Packaging Unit": 20, "Planting Unit": 20, "Seed-Picking Unit": 10,
    "Fitting Unit": 20, "Moulding Unit": 10, "Gearing Unit": 10,
    "Filling Unit": 20, "Separating Unit": 20, "Purification Unit": 50,
    "Fluid-Gas Transmuting Unit": 50, "Solid-Gas Transmuting Unit": 50,
    "Gas Reactor Globe": 50,
    "Thermal Bank": 0, "Construct": 0, "Crafting": 0,
}
POWER_ASSUMED: set[str] = set()  # all confirmed (skport for 提纯机=50 / 扩展反应池=100)

# Battery 发电容量 burned in 热能池/Thermal Bank (Power), 40s each. Each battery's burn value
# lives on its OWN skport page (not the Thermal Bank table): SC Wuling=3200 confirmed via
# wiki.skport.com item 758 ("Production Power 3200, 40s"). NB: HC Wuling Battery does NOT exist
# in the current version (future content) — SC Wuling (3200) is the densest fuel now.
BATTERY_POWER = {
    "SC Wuling Battery": 3200, "LC Wuling Battery": 1600, "HC Valley Battery": 1100,
    "SC Valley Battery": 420, "LC Valley Battery": 220,
}
# Outpost (据点) sell prices as {item: (price, currency)}, scraped from ALL endfield.wiki.gg
# outposts (Category:Outposts, 5). TWO currencies: 武陵券 (Wuling, from == Trade == tables at
# Cardiac Remediation Station / Sky King Flats) and 谷地券 (Valley, from {{Outpost level}}
# prosperity tables at Infra-Station / Reconstruction HQ / Refugee Camp). Prices are consistent
# across outposts. Currencies are NOT interchangeable — --max-revenue sums within one only.
# NB: SC Valley Battery sell price is 30 谷地券 (its 420 is power-gen, unrelated).
TRADE_PRICE = {
    # 武陵券 (Wuling)
    "Xiranite": (1, "武陵券"), "Heavy Xiranite": (27, "武陵券"),
    "LC Wuling Battery": (25, "武陵券"), "SC Wuling Battery": (54, "武陵券"),
    "Hetonite Part": (48, "武陵券"), "Cuprium Part": (1, "武陵券"),
    "Yazhen Syringe (A)": (22, "武陵券"), "Yazhen Syringe (C)": (16, "武陵券"),
    "Jincao Tea": (22, "武陵券"), "Jincao Drink": (16, "武陵券"),
    # 谷地券 (Valley)
    "LC Valley Battery": (16, "谷地券"), "SC Valley Battery": (30, "谷地券"),
    "HC Valley Battery": (70, "谷地券"), "Steel Part": (3, "谷地券"),
    "Ferrium Part": (1, "谷地券"), "Amethyst Part": (1, "谷地券"),
    "Amethyst Bottle": (2, "谷地券"), "Origocrust": (1, "谷地券"),
    "Buck Capsule (A)": (70, "谷地券"), "Buck Capsule (B)": (27, "谷地券"),
    "Buck Capsule (C)": (10, "谷地券"), "Canned Citrome (A)": (70, "谷地券"),
    "Canned Citrome (B)": (27, "谷地券"), "Canned Citrome (C)": (10, "谷地券"),
}
FACILITY_CN = {
    "天有洪炉": "Forge of the Sky", "反应池": "Reactor Crucible",
    "扩展反应池": "Expanded Crucible", "提纯机": "Purification Unit",
    "封装机": "Packaging Unit", "精炼台": "Refining Unit", "热能池": "Thermal Bank",
    "热能库": "Thermal Bank", "研磨机": "Grinding Unit", "粉碎机": "Shredding Unit",
    "液气转化机": "Fluid-Gas Transmuting Unit", "固气转化机": "Solid-Gas Transmuting Unit",
    "气体反应炉": "Gas Reactor Globe",
}  # extended at load time from recipes.json `facilities` (every scraped cnname)
# Common name variants the wiki shortens (源矿 = 源石矿 = Originium Ore, etc.)
ITEM_ALIAS = {"源石矿": "Originium Ore", "源石": "Originium Ore"}


# ----------------------------------------------------------------- helpers
def fnum(x) -> str:
    f = float(x)
    return str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:.3f}".rstrip("0")


def label(name: str, items: dict) -> str:
    cn = (items.get(name) or {}).get("cn", "")
    return f"{cn} {name}".strip()


def resolve(items: dict, token: str) -> str | None:
    token = ITEM_ALIAS.get(token, token)
    tl = token.lower()
    for n in items:
        if n.lower() == tl:
            return n
    for n, info in items.items():
        if (info.get("cn") or "") == token:
            return n
    cands = [n for n, info in items.items()
             if tl in n.lower() or token in (info.get("cn") or "")]
    return cands[0] if len(cands) == 1 else (cands if cands else None)


def prepare_recipes(data: dict, include_event: bool, activator: bool,
                    envs: set[str] = frozenset()) -> list[dict]:
    """Solver view of recipes.json: drop event recipes (unless asked) and append each
    machine's activator feed as a consumed ingredient (rate/min × time/60 per run, i.e.
    exactly `rate` /min per full-speed machine; tagged `_activator`).
    ENV recipes (often a cheaper variant, e.g. Xiranite from plain Carbon in Stable ENV)
    need a Gas Dispersing Unit zone — a fixed cost the LP can't price — so one whose ENV
    isn't in `envs` is kept only when its product has no non-ENV recipe."""
    facs = data.get("facilities", {})
    pool = [r for r in data["recipes"] if include_event or not r.get("event")]
    plain = {r["products"][0]["item"] for r in pool   # (a reverse transmute isn't a source)
             if r["products"] and r.get("time") and not r.get("environment")
             and r["facility"] not in TRANSMUTERS}
    out = []
    for r in pool:
        env = r.get("environment")
        if env and env not in envs and r["products"] and r["products"][0]["item"] in plain:
            continue
        act = (facs.get(r["facility"]) or {}).get("activator")
        if activator and act and r.get("time"):
            r = {**r, "ingredients": r["ingredients"] + [{
                "item": act["item"], "_activator": True,
                "count": float(Fraction(str(act["rate"])) * Fraction(str(r["time"])) / 60)}]}
        out.append(r)
    return out


def env_report(used, items) -> None:
    """used: [(recipe, machines)] -> print which Gas Dispersing Unit zones the plan needs."""
    need: dict[str, list] = defaultdict(list)
    for r, m in used:
        if r.get("environment") and m > 1e-9:
            need[r["environment"]].append(f"{r['facility']} ×{fnum(m)}")
    if not need:
        return
    print("\n## ENV zones (place these machines inside a Gas Dispersing Unit 气体散布机 zone, 13×13)")
    for env, where in sorted(need.items()):
        gas = ENV_GAS.get(env)
        feed = f"feed ≥{ENV_FEED}/min {label(gas, items)} per zone" if gas else "feed ?"
        print(f"  {env:<11} {feed}  ← {', '.join(where)}")


def recycle_sources(item: str, recipes: list[dict], available: set[str]) -> list[tuple]:
    """Recipes that are a real *net* source of `item` for this plan -> [(facility, [ings])].

    Appearing in an output slot isn't enough: emptying a filled bottle/canister only returns
    what was poured in, a reverse transmute undoes a state change, and Purification's
    4 Cuprium Solution -> 1 Precipitation Acid eats more acid upstream than it gives back.
    So per ingredient I: if I is made from `item` in one step, the round trip item->I->item
    must gain (>1, e.g. 1 seed -> 2 Yazhen); otherwise I must already be `available` in the
    plan (made internally / surplus byproduct) or be pumped/gathered."""
    def cnt(side, it):
        return sum(float(s["count"]) for s in side if s["item"] == it)

    free = available | GATHERED | {"Clean Water"}
    out = []
    for r in recipes:
        if (not r.get("time") or r["facility"] in MANUAL_FACILITIES | TRANSMUTERS
                or r["facility"] == "Thermal Bank"):
            continue
        made = cnt(r["products"], item)
        if made <= 0 or cnt(r["ingredients"], item) > 0:
            continue
        ings = [g["item"] for g in r["ingredients"]
                if not g.get("_activator") and g["item"] != "Power"]
        ok = True
        for ing in ings:
            gains = [made / cnt(r["ingredients"], ing) * cnt(d["products"], ing)
                     / cnt(d["ingredients"], item)
                     for d in recipes if d.get("time") and cnt(d["ingredients"], item) > 0
                     and cnt(d["products"], ing) > 0]
            if gains:
                if max(gains) <= 1 + 1e-9:
                    ok = False
                    break
            elif ing not in free:
                ok = False
                break
        if ok:
            out.append((r["facility"], ings))
    return out


def net_per_run(recipe: dict, item: str) -> Fraction:
    v = Fraction(0)
    for p in recipe["products"]:
        if p["item"] == item:
            v += Fraction(str(p["count"]))
    for g in recipe["ingredients"]:
        if g["item"] == item:
            v -= Fraction(str(g["count"]))
    return v


# --------------------------------------------------- exact linear solve
def solve(A: list[list[Fraction]], b: list[Fraction]) -> list[Fraction] | None:
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = next((r for r in range(col, n) if M[r][col] != 0), None)
        if piv is None:
            return None
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        M[col] = [v / pivval for v in M[col]]
        for r in range(n):
            if r != col and M[r][col] != 0:
                factor = M[r][col]
                M[r] = [a - factor * b for a, b in zip(M[r], M[col])]
    return [M[i][n] for i in range(n)]


# ---------------------------------------------- LP planner (closed loops)
def plan_lp(data, targets, maximize, caps, selfpower, raws, banned_facs, banned_items,
            supply=None, revenue=None):
    """General closed-loop LP.
      targets   {item: min net /min} — required outputs (floors).
      maximize  item to maximise (objective); None -> minimise total machines.
      caps      {facility: max machines} — e.g. {"Forge of the Sky": 12}.
      supply    {item: max external supply /min} — raw input caps, e.g. {源矿: 540}.
      selfpower (battery_item, 发电容量) -> inject a 热能池 burn recipe + require
                generated Power >= total machine draw (self-powered base).
    Recipes chosen by ANY output, so byproduct/seed cycles close. Returns
    (recipes_with_injected, x{ridx:runs/min}, raws) or None if infeasible."""
    from scipy.optimize import linprog

    recipes = list(data["recipes"])
    if selfpower:
        sp_item, sp_cap = selfpower
        recipes.append({"facility": "Thermal Bank", "time": 40, "mode": "Power",
                        "ingredients": [{"item": sp_item, "count": 1}],
                        "products": [{"item": "Power", "count": sp_cap}],
                        "_selfpower": True})
    R = [i for i, r in enumerate(recipes)
         if r["facility"] not in MANUAL_FACILITIES and r.get("time")
         and r["facility"] not in banned_facs
         and not (r["facility"] == "Thermal Bank" and not r.get("_selfpower"))
         and not any(g["item"] in banned_items for g in r["ingredients"])]
    item_set, produced = set(), set()
    for i in R:
        for s in recipes[i]["ingredients"] + recipes[i]["products"]:
            if s["item"] != "Power":
                item_set.add(s["item"])
        for p in recipes[i]["products"]:
            if p["item"] != "Power":
                produced.add(p["item"])
    raws = set(raws) | {it for it in item_set if it not in produced}
    constrained = [it for it in item_set if it not in raws]

    A_ub, b_ub = [], []
    for it in constrained:                                  # net >= demand  (-net <= -d)
        A_ub.append([-float(net_per_run(recipes[i], it)) for i in R])
        b_ub.append(-float(targets.get(it, 0.0)))
    for fac, cap in caps.items():                           # facility machine cap
        A_ub.append([float(recipes[i]["time"]) / 60 if recipes[i]["facility"] == fac
                     else 0.0 for i in R])
        b_ub.append(float(cap))
    for it, cap in (supply or {}).items():                  # raw input supply cap
        A_ub.append([-float(net_per_run(recipes[i], it)) for i in R])  # consumption ≤ cap
        b_ub.append(float(cap))
    if selfpower:                                           # draw - gen <= 0
        row = []
        for i in R:
            r = recipes[i]
            gen = selfpower[1] if r.get("_selfpower") else 0
            row.append((POWER.get(r["facility"], 0) - gen) * float(r["time"]) / 60)
        A_ub.append(row)
        b_ub.append(0.0)

    if revenue:   # maximise Σ price·net over sale items (tiny machine tie-break)
        c = [-sum(p * float(net_per_run(recipes[i], it)) for it, p in revenue.items())
             + 1e-4 * float(recipes[i]["time"]) / 60 for i in R]
    elif maximize:  # lexicographic: maximise the item, then tie-break on fewest machines
        c = [-1e6 * float(net_per_run(recipes[i], maximize))
             + float(recipes[i]["time"]) / 60 for i in R]
    else:
        c = [float(recipes[i]["time"]) / 60 for i in R]
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=(0, None), method="highs")
    if not res.success:
        return None
    return recipes, {i: res.x[k] for k, i in enumerate(R) if res.x[k] > 1e-6}, raws


def report(recipes, x, raws, items, targets, maximize, selfpower, open_items=frozenset()):
    net = defaultdict(float)
    gross_in: dict[str, float] = defaultdict(float)   # total consumed (for open-loop items)
    gross_out: dict[str, float] = defaultdict(float)  # total produced  (for open-loop items)
    rows, pwr, gen, burned = [], 0.0, 0.0, 0.0
    fac_machines: dict[str, float] = defaultdict(float)
    for ridx, runs in x.items():
        r = recipes[ridx]
        m = runs * float(r["time"]) / 60
        for p in r["products"]:
            net[p["item"]] += float(p["count"]) * runs
            gross_out[p["item"]] += float(p["count"]) * runs
        for g in r["ingredients"]:
            net[g["item"]] -= float(g["count"]) * runs
            gross_in[g["item"]] += float(g["count"]) * runs
        pwr += POWER.get(r["facility"], 0) * m
        fac_machines[r["facility"]] += m
        if r.get("_selfpower"):
            gen += selfpower[1] * m
            burned = runs
        it0 = r["products"][0]["item"] if r["products"] else "?"
        cn = (items.get(it0) or {}).get("cn", "") or it0
        rows.append((r["facility"], cn, runs, m))
    net.pop("Power", None)
    sale = list(targets) + ([maximize] if maximize else [])

    print("## Machines")
    print(f"{'facility':<20} {'product':<24} {'runs/min':>9} {'machines':>9}")
    tot = 0.0
    for fac, cn, runs, m in sorted(rows):
        tot += m
        print(f"{fac:<20} {cn:<24} {fnum(runs):>9} {fnum(m):>9}")
    print(f"{'TOTAL':<20} {'':<24} {'':>9} {fnum(tot):>9}")

    assumed = [f for f in fac_machines if f in POWER_ASSUMED and fac_machines[f] > 1e-6]
    pnote = f"  (assumed power: {', '.join(sorted(assumed))})" if assumed else ""
    print(f"\n## Power  draw {fnum(pwr)}{pnote}", end="")
    if selfpower:
        print(f" · gen {fnum(gen)} (烧 {fnum(burned)} {label(selfpower[0], items)}/min)"
              f" · net {fnum(gen - pwr)}")
    else:
        print()

    print("\n## Output for sale (/min)")
    rev_by_cur: dict[str, float] = defaultdict(float)
    for it in sale:
        v = net.get(it, 0.0)
        pc = TRADE_PRICE.get(it)
        if pc:
            price, cur = pc
            rev_by_cur[cur] += v * price
            tag = f" = {fnum(v * price)} {cur}"
        else:
            tag = " (price?)"
        print(f"  {label(it, items):<34} {fnum(v):>8}{tag}")
    for cur, tot in rev_by_cur.items():
        print(f"  {'TOTAL ' + cur + '/min':<34} {'':>8}   {fnum(tot)}")

    inputs = sorted(((-v, it) for it, v in net.items()
                     if v < -1e-6 and it not in open_items), reverse=True)
    if inputs:
        print("\n## External inputs (/min)")
        for v, it in inputs:
            print(f"  {label(it, items):<34} {fnum(v):>8}")

    open_rows = sorted(open_items, key=lambda it: -gross_in.get(it, 0.0))
    if open_rows:
        print("\n## Open-loop fluids (not recycled — supply & dump separately)")
        for it in open_rows:
            supply, dump = gross_in.get(it, 0.0), gross_out.get(it, 0.0)
            net_v = supply - dump
            net_s = f"+{fnum(net_v)}" if net_v >= 0 else fnum(net_v)
            print(f"  {label(it, items):<34} supply {fnum(supply):>7}/min · "
                  f"dump {fnum(dump):>7}/min  (net {net_s})")

    surplus = sorted(((v, it) for it, v in net.items()
                      if it not in sale and it not in open_items and v > 1e-6), reverse=True)
    if surplus:
        print("\n## Surplus byproducts (/min)")
        for v, it in surplus:
            print(f"  {label(it, items):<34} {fnum(v):>8}")
    env_report([(recipes[i], runs * float(recipes[i]["time"]) / 60)
                for i, runs in x.items()], items)


# ----------------------------------------------------- flow tree (CLI diagram)
def _prod_per_run(recipe: dict, item: str) -> Fraction:
    return sum((Fraction(str(p["count"])) for p in recipe["products"]
                if p["item"] == item), Fraction(0))


def render_tree(roots, producer, runs_of, items, raw_items):
    """ASCII pipeline diagram: each producer node shows facility · machines, each
    edge shows the per-/min flow into its parent; leaves are raw/external inputs.

    roots      [(item, rate/min)]  — one sub-tree per target.
    producer   item -> recipe dict that makes it (its main-product recipe).
    runs_of    item -> runs/min of that recipe (drives machine count & edge rates).
    raw_items  set of externally-supplied items (rendered as leaves).
    Shared intermediates are expanded once (later hits show `↑ above`); any cycle
    (possible in an LP plan) is broken with `↻ loop`."""
    expanded: set[str] = set()
    out: list[str] = []

    def walk(item, edge_rate, prefix, is_last, visiting):
        branch = "" if is_last is None else ("└─ " if is_last else "├─ ")
        lbl = label(item, items)
        rate_s = f"{fnum(edge_rate)}/min"
        if item not in producer or item in raw_items:
            out.append(f"{prefix}{branch}{lbl}  ◂ {rate_s}  · raw")
            return
        r = producer[item]
        runs = runs_of.get(item, Fraction(0))
        machines = runs * Fraction(str(r["time"])) / 60
        total = _prod_per_run(r, item) * runs
        diff = float(total - edge_rate)
        if diff > 1e-9:               # this line makes more than this edge takes (downstream split)
            shared = f" of {fnum(total)}/min"
        elif diff < -1e-9:            # LP: remainder of this edge comes from another producer line
            shared = f" (+{fnum(-diff)}/min from other lines)"
        else:
            shared = ""
        if item in visiting:
            out.append(f"{prefix}{branch}{lbl}  ◂ {rate_s}  ↻ loop via {r['facility']}")
            return
        if item in expanded:
            out.append(f"{prefix}{branch}{lbl}  ◂ {rate_s}  ↑ {r['facility']} (above)")
            return
        out.append(f"{prefix}{branch}{lbl}  ◂ {rate_s}{shared}"
                   f"  ▸ [{r['facility']} ×{fnum(machines)}]")
        expanded.add(item)
        child_prefix = prefix + ("" if is_last is None else ("   " if is_last else "│  "))
        ings = [g for g in r["ingredients"] if g["item"] != "Power"]
        for i, g in enumerate(ings):
            walk(g["item"], Fraction(str(g["count"])) * runs,
                 child_prefix, i == len(ings) - 1, visiting | {item})

    for item, rate in roots:
        walk(item, Fraction(str(rate)), "", None, set())
    print("legend: ◂ flow into parent /min · ▸ [machine ×count]")
    print("\n".join(out))


# ----------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default="",
                    help='item(s) to produce: "item" or "item=rate,item2=rate2" (中文/EN)')
    ap.add_argument("--rate", type=float, default=0.0,
                    help="target output /min (single-target; default: one full-speed machine)")
    ap.add_argument("--pick", action="append", default=[],
                    help='force a recipe: "Item=Facility" (repeatable)')
    ap.add_argument("--raw", default="",
                    help="comma-list of items to treat as externally supplied")
    ap.add_argument("--open", dest="open_items", default="",
                    help="LP: comma-list of items to run OPEN-loop — supplied externally AND "
                         "their production dumped (report shows gross supply + dump, not net). "
                         "Use for a byproduct you'd rather supply/drain than pipe back (e.g. 污水)")
    ap.add_argument("--lp", action="store_true",
                    help="closed-loop LP solve (auto-closes byproduct/seed cycles)")
    ap.add_argument("--max", dest="maximize", default="",
                    help="LP: item to maximise (the targets become floors)")
    ap.add_argument("--max-revenue", dest="max_revenue", action="store_true",
                    help="LP: maximise total 凭证 from selling the target items")
    ap.add_argument("--cap", action="append", default=[],
                    help='LP: facility machine cap "Facility=N" (repeatable, 中文 ok)')
    ap.add_argument("--supply", action="append", default=[],
                    help='LP: max raw supply "Item=rate/min" (repeatable, e.g. 源石矿=540)')
    ap.add_argument("--selfpower", default="",
                    help="LP: power the base by burning this battery (中文/EN); "
                         "its target becomes net surplus after the burn")
    ap.add_argument("--ban", default="",
                    help="LP: comma-list of facilities/items to exclude (e.g. force a path)")
    ap.add_argument("--allow", default="",
                    help="LP: re-enable a default-banned item (default-banned: 原木/Wood)")
    ap.add_argument("--tree", "--graph", dest="tree", action="store_true",
                    help="draw the pipeline as a CLI flow diagram (machines · inputs · rates)")
    ap.add_argument("--event", action="store_true",
                    help="allow limited-time event recipes (auto-on if a target needs one)")
    ap.add_argument("--no-activator", dest="activator", action="store_false",
                    help="don't charge Transmuting Units their 6/min activator feed")
    ap.add_argument("--env", default="",
                    help='ENV zones you have / will build: "Stable,Acrid" or "all" — '
                         "unlocks (and prefers) the cheaper ENV recipe variants")
    args = ap.parse_args()
    envs = {f"{e.strip().capitalize()} ENV" for e in args.env.split(",") if e.strip()}
    if args.env.strip().lower() == "all":
        envs = set(ENV_GAS)

    if not RECIPES_JSON.exists():
        print(f"(no recipe data; run `python3 scripts/scrape_recipes.py` first)")
        return 1
    data = json.loads(RECIPES_JSON.read_text(encoding="utf-8"))
    items = data["items"]
    for fac, info in data.get("facilities", {}).items():   # newer machines: scraped data
        if info.get("power") is not None:
            POWER.setdefault(fac, info["power"])
        if info.get("cn"):
            FACILITY_CN.setdefault(info["cn"], fac)
    recipes = prepare_recipes(data, args.event, args.activator, envs)

    def need_events(targets) -> bool:
        """True if some target is made only by event recipes (so turn them on)."""
        if args.event:
            return False
        made = {p["item"] for r in recipes for p in r["products"]}
        ev = {p["item"] for r in data["recipes"] if r.get("event") for p in r["products"]}
        return any(t in ev and t not in made for t in targets)
    # recipes.json is the source of truth for prices (scrape_recipes.py merges them);
    # fall back to the hardcoded TRADE_PRICE only if the mirror predates price support.
    loaded = {n: (v["price"], v.get("currency", "")) for n, v in items.items()
              if v.get("price") is not None}
    if loaded:
        TRADE_PRICE.clear()
        TRADE_PRICE.update(loaded)

    def resolve1(tok):
        r = resolve(items, tok)
        if r is None:
            print(f"(no item matching {tok!r})")
        elif isinstance(r, list):
            print(f"ambiguous {tok!r} — be specific:")
            for n in sorted(r)[:30]:
                print(f"  {label(n, items)}")
        return r if isinstance(r, str) else None

    def default_rate(it):
        cand = next((r for r in recipes if r["products"]
                     and r["products"][0]["item"] == it and r.get("time")
                     and r["facility"] not in MANUAL_FACILITIES), None)
        return (float(cand["products"][0]["count"]) * 60 / float(cand["time"])
                if cand else 6.0)

    if args.lp:
        floor0 = bool(args.maximize or args.max_revenue)  # targets are floors, default 0
        targets = {}
        for part in (p.strip() for p in args.target.split(",") if p.strip()):
            name, _, rate = part.partition("=")
            it = resolve1(name)
            if not it:
                return 1
            targets[it] = float(rate) if rate else (0.0 if floor0
                                                    else (args.rate or default_rate(it)))
        maximize = resolve1(args.maximize) if args.maximize else None
        if args.maximize and not maximize:
            return 1
        revenue = None
        if args.max_revenue:
            pcs = {it: TRADE_PRICE[it] for it in targets if it in TRADE_PRICE}
            curs = {c for _, c in pcs.values()}
            if not pcs:
                print("(--max-revenue needs target items with a known 凭证 price)")
                return 1
            if len(curs) > 1:
                print(f"(targets span currencies {curs}; --max-revenue needs one currency)")
                return 1
            revenue = {it: price for it, (price, _) in pcs.items()}
        if not targets and not maximize:
            print("(give a target item, or --max <item>)")
            return 1
        if need_events(list(targets) + ([maximize] if maximize else [])):
            print("(target is event-only — including event recipes)\n")
            recipes = prepare_recipes(data, True, args.activator, envs)
        caps = {}
        for spec in args.cap:
            fac, _, n = spec.partition("=")
            fac = fac.strip()
            caps[FACILITY_CN.get(fac, fac)] = float(n)
        supply = {}
        for spec in args.supply:
            name, _, n = spec.partition("=")
            it = resolve1(name)
            if not it:
                return 1
            supply[it] = float(n)
        selfpower = None
        if args.selfpower:
            sp = resolve1(args.selfpower)
            if not sp:
                return 1
            if sp not in BATTERY_POWER:
                print(f"(no 发电容量 known for {label(sp, items)}; known: "
                      f"{', '.join(BATTERY_POWER)})")
                return 1
            selfpower = (sp, BATTERY_POWER[sp])
        raws = DEFAULT_RAW_LP | GATHERED | {x for x in (resolve(items, t.strip())
                                             for t in args.raw.split(",")) if isinstance(x, str)}
        open_items = set()
        for o in (o.strip() for o in args.open_items.split(",") if o.strip()):
            it = resolve1(o)
            if not it:
                return 1
            open_items.add(it)
        raws |= open_items  # open-loop items are free external for the optimizer
        banned_facs, banned_items = set(), set(DEFAULT_BAN_ITEMS)
        for b in (b.strip() for b in args.ban.split(",") if b.strip()):
            if b in data["machines"] or b in FACILITY_CN:
                banned_facs.add(FACILITY_CN.get(b, b))
            else:
                it = resolve(items, b)
                banned_items.add(it if isinstance(it, str) else b)
        for a in (a.strip() for a in args.allow.split(",") if a.strip()):
            it = resolve(items, a)
            banned_items.discard(it if isinstance(it, str) else a)

        out = plan_lp({**data, "recipes": recipes}, targets, maximize, caps, selfpower, raws,
                      banned_facs, banned_items, supply, revenue)
        if not out:
            print("(LP infeasible — relax --cap/--supply, lower targets, or check --raw/--ban)")
            return 1
        rec2, x, raws = out
        head = ("maximise 凭证 revenue" if revenue else
                f"maximise {label(maximize, items)}" if maximize else "min machines")
        tgt = ", ".join(f"{label(k, items)}≥{fnum(v)}" for k, v in targets.items()) or "—"
        cap = ", ".join(f"{k}≤{fnum(v)}" for k, v in caps.items())
        sup = ", ".join(f"{label(k, items)}≤{fnum(v)}" for k, v in supply.items())
        opn = ", ".join(label(k, items) for k in open_items)
        print(f"# Plan (LP): {head}; targets {tgt}"
              f"{'; caps ' + cap if cap else ''}"
              f"{'; supply ' + sup if sup else ''}"
              f"{'; open-loop ' + opn if opn else ''}"
              f"{'; self-power via ' + label(selfpower[0], items) if selfpower else ''}\n")
        report(rec2, x, raws, items, targets, maximize, selfpower, open_items)
        if args.tree:
            # build a main-product producer map from the LP solution (largest run wins
            # per item); net flow gives each root's surplus rate. Cycles are guarded.
            producer, runs_of, net = {}, {}, defaultdict(Fraction)
            for ridx, runs in sorted(x.items(), key=lambda kv: -kv[1]):
                r = rec2[ridx]
                for p in r["products"]:
                    net[p["item"]] += Fraction(str(p["count"])) * Fraction(str(runs))
                for g in r["ingredients"]:
                    net[g["item"]] -= Fraction(str(g["count"])) * Fraction(str(runs))
                main = r["products"][0]["item"] if r["products"] else None
                if main and main != "Power" and main not in producer:
                    producer[main] = r
                    runs_of[main] = Fraction(str(runs))
            roots = [(it, net.get(it, Fraction(0)))
                     for it in (list(targets) + ([maximize] if maximize else []))
                     if it in producer]
            if roots:
                print("\n## Flow")
                render_tree(roots, producer, runs_of, items, raws)
        return 0

    target = resolve1(args.target)
    if not target:
        return 1
    if need_events([target]):
        print("(target is event-only — including event recipes)\n")
        recipes = prepare_recipes(data, True, args.activator, envs)

    # main-product producers (continuous only): item -> [recipe idx]
    producers: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(recipes):
        if r["facility"] in MANUAL_FACILITIES or not r.get("time"):
            continue
        if r["products"]:
            producers[r["products"][0]["item"]].append(i)

    pick = {}
    for spec in args.pick:
        it, _, fac = spec.partition("=")
        it = resolve(items, it.strip())
        if isinstance(it, str):
            pick[it] = FACILITY_CN.get(fac.strip(), fac.strip())
    raw = (ALWAYS_RAW | ((GATHERED | {"Clean Water"}) - set(pick))
           | {x for x in (resolve(items, t.strip()) for t in args.raw.split(","))
              if isinstance(x, str)})

    # expand the main-product dependency graph from the target as a DFS, rejecting any
    # transmute that would loop back into the chain (fluid⇄gas / solid⇄gas pairs are
    # lossless reversals -> singular system). Returns False only when *every* candidate
    # loops back — the caller then tries its next recipe, or becomes a leaf itself (so
    # Precipitation Acid stays external instead of un-gasified Acridgen).
    chosen: dict[str, int] = {}

    def expand(it: str, path: frozenset) -> bool:
        if it in chosen or it in raw:   # (includes everything on `path`: productive
            return True                 #  loops like plant⇄seed solve fine)
        cands = producers.get(it, [])
        if it in pick:
            cands = [i for i in cands if recipes[i]["facility"] == pick[it]] or cands
        else:  # real lines first, state-change transmutes last; an ENV recipe that
            # survived prepare_recipes is either unlocked by --env or the only way: prefer it
            cands = sorted(cands, key=lambda i: (recipes[i]["facility"] in TRANSMUTERS,
                                                 not recipes[i].get("environment")))
        if not cands:
            raw.add(it)            # no continuous recipe -> external leaf
            return True
        tried = False
        for ridx in cands:
            ings = {g["item"] for g in recipes[ridx]["ingredients"]} - {it}  # self-feed ok
            # an activator feed looping back is a small, solvable cycle — not a dead end
            act = {g["item"] for g in recipes[ridx]["ingredients"] if g.get("_activator")}
            if recipes[ridx]["facility"] in TRANSMUTERS and (ings - act) & path:
                continue                # A⇄B state change back into the chain: singular
            tried = True
            snap_c, snap_r = dict(chosen), set(raw)
            chosen[it] = ridx
            if all(expand(g, path | {it}) or g in act for g in ings):
                return True
            chosen.clear(); chosen.update(snap_c)
            raw.clear(); raw.update(snap_r)
        if tried:
            raw.add(it)            # every route dead-ends in a loop -> external leaf
            return True
        return False

    if not expand(target, frozenset()):
        raw.add(target)

    internal = list(chosen)
    if target not in internal:
        print(f"(target {label(target, items)} has no continuous recipe — it's a raw/"
              f"gathered item)")
        return 1

    # default target rate = one full-speed machine of its chosen recipe
    tr = recipes[chosen[target]]
    out_per_run = sum(Fraction(str(p["count"])) for p in tr["products"]
                      if p["item"] == target)
    full_speed = out_per_run * 60 / Fraction(str(tr["time"]))
    rate = Fraction(str(args.rate)) if args.rate else full_speed

    # A x = b over internal items: row=item balance, col=that item's chosen recipe runs/min
    idx = {it: k for k, it in enumerate(internal)}
    n = len(internal)
    A = [[Fraction(0)] * n for _ in range(n)]
    b = [Fraction(0)] * n
    for ci, it in enumerate(internal):
        for ri, jt in enumerate(internal):
            A[ci][ri] = net_per_run(recipes[chosen[jt]], it)
        b[ci] = rate if it == target else Fraction(0)
    sol = solve(A, b)
    if sol is None:
        print("(could not solve — graph may be degenerate; try --pick/--raw)")
        return 1
    runs = {it: sol[idx[it]] for it in internal}

    # net flow per item across the whole plan
    net: dict[str, Fraction] = defaultdict(Fraction)
    for it in internal:
        x = runs[it]
        r = recipes[chosen[it]]
        for p in r["products"]:
            net[p["item"]] += Fraction(str(p["count"])) * x
        for g in r["ingredients"]:
            net[g["item"]] -= Fraction(str(g["count"])) * x

    # ---- report
    print(f"# Plan: {label(target, items)} @ {fnum(rate)}/min "
          f"(1 full-speed line = {fnum(full_speed)}/min)\n")
    if args.tree:
        print("## Flow")
        producer = {it: recipes[chosen[it]] for it in internal}
        render_tree([(target, rate)], producer, runs, items, raw)
        print()
    print("## Machines")
    print(f"{'facility':<20} {'product':<26} {'runs/min':>9} {'machines':>9}")
    for it in sorted(internal, key=lambda x: (recipes[chosen[x]]['facility'], x)):
        r = recipes[chosen[it]]
        x = runs[it]
        machines = x * Fraction(str(r["time"])) / 60
        cn = (items.get(it) or {}).get("cn", "") or it
        print(f"{r['facility']:<20} {cn:<26} {fnum(x):>9} {fnum(machines):>9}")

    inputs = sorted(((-v, it) for it, v in net.items()
                     if it not in internal and v < 0), reverse=True)
    print("\n## External inputs (/min)")
    avail = set(internal) | {it for it, v in net.items() if v > 0}
    for v, it in inputs:
        srcs = recycle_sources(it, recipes, avail)  # real net sources only
        tag = ("   ← recyclable via " + "; ".join(
            f"{fac} ({' + '.join(label(g, items) for g in ings)})" for fac, ings in srcs)
            if srcs else "")
        print(f"  {label(it, items):<34} {fnum(v):>8}{tag}")

    surplus = sorted(((v, it) for it, v in net.items()
                      if it != target and v > 0), reverse=True)
    if surplus:
        print("\n## Surplus byproducts (/min)")
        for v, it in surplus:
            print(f"  {label(it, items):<34} {fnum(v):>8}")
    env_report([(recipes[chosen[it]], float(runs[it] * Fraction(str(recipes[chosen[it]]["time"])) / 60))
                for it in internal], items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
