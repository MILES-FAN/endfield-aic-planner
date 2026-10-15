#!/usr/bin/env python3
"""Mirror Endfield AIC production recipes (machines + products + rates) locally.

The English **Endfield Talos Wiki** (endfield.wiki.gg) stores every recipe in Lua
data modules `Module:Recipe/<Module>`, each a list of
    { time, mode, event, eventName, ingredients = {{item,count}..}, products = {{item,count}..},
      facility, enviroment, category }
The authoritative module list lives in `Module:Recipe` (its `mw.loadData(...)` calls).
Modules are *usually* one per facility, but event modules (e.g. `Palm-Top_Savior`) hold
recipes for several existing machines — so each recipe's own `facility =` field wins.
Since 1.4 (gas update) a recipe may also need an `enviroment` ("Acrid ENV" / "Stable ENV"):
the machine must sit inside a Gas Dispersing Unit zone (wiki.gg spells the key
`enviroment`; we store it as `environment`).

Each facility's page infobox is also read for `|power=` and its activation feed
("requires a minimum input of N/min {{I|Item}}" — the Transmuting Units need a steady
Liquid Xiranite / Xiragen feed) -> recipes.json `facilities`.
Exact per-minute rate of any ingredient/product = count * 60 / time (time in seconds;
time 0 / Construct = "instant", no continuous rate).

This tool fetches those modules, computes rates, cross-indexes every item
(produced-by / used-in), enriches each item with its Chinese name from the item
page infobox (`cnname=`), and writes:

    wiki/recipes/recipes.json      structured source of truth (bilingual, with rates)
    wiki/recipes/<Facility>.md     one readable recipe table per machine
    wiki/recipes/_index.md         machine list + EN<->CN item glossary

Usage:
    python3 scripts/scrape_recipes.py                 # full build
    python3 scripts/scrape_recipes.py --limit 3       # only first N machines (smoke test)
    python3 scripts/scrape_recipes.py --no-cn         # skip Chinese-name enrichment
    python3 scripts/scrape_recipes.py --offline       # reuse wiki/recipes/raw/*.lua cache

Be a good citizen: rate-limited, sends maxlag, descriptive UA.
Game data (c) Hypergryph; wiki text CC BY-SA 4.0 (see DATA-LICENSE.md).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "wiki" / "recipes"
RAW_DIR = OUT_DIR / "raw"

API = "https://endfield.wiki.gg/api.php"
RAW_URL = "https://endfield.wiki.gg/index.php"
WIKI_PAGE = "https://endfield.wiki.gg/wiki/"
UA = "EndfieldAICPlanner/1.0 (+https://github.com/MILES-FAN/endfield-aic-planner)"
DELAY = 0.7  # polite gap between requests, seconds


# --------------------------------------------------------------------------- io
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", title).strip().replace(" ", "_")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _get(session: requests.Session, url: str, params: dict) -> requests.Response:
    for attempt in range(5):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r
            time.sleep(2 + attempt)
        except requests.RequestException:
            time.sleep(2 + attempt)
    raise RuntimeError(f"request failed: {url} {params}")


def fetch_raw(session: requests.Session, title: str, offline: bool) -> str:
    """Raw page source (wikitext / Lua), cached under wiki/recipes/raw/."""
    cache = RAW_DIR / f"{slugify(title)}.lua"
    if offline:
        return cache.read_text(encoding="utf-8") if cache.exists() else ""
    r = _get(session, RAW_URL, {"title": title, "action": "raw"})
    time.sleep(DELAY)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(r.text, encoding="utf-8")
    return r.text


# ------------------------------------------------------------------ lua parsing
def _match_brace(s: str, i: int) -> int:
    """Given s[i] == '{', return index of the matching '}' (string-aware)."""
    depth, in_str, esc = 0, False, False
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _top_level_tables(inner: str) -> list[str]:
    """Split the body of a Lua array into its top-level `{...}` element strings."""
    out, i = [], 0
    while i < len(inner):
        if inner[i] == "{":
            end = _match_brace(inner, i)
            if end == -1:
                break
            out.append(inner[i:end + 1])
            i = end + 1
        else:
            i += 1
    return out


def _item_count_pairs(block: str) -> list[dict]:
    """Parse `{ item = "X", count = N } , ...` inside an ingredients/products list."""
    pairs = []
    for tbl in _top_level_tables(block):
        m_item = re.search(r'item\s*=\s*"([^"]*)"', tbl)
        m_cnt = re.search(r"count\s*=\s*([0-9.]+)", tbl)
        if m_item:
            cnt = float(m_cnt.group(1)) if m_cnt else 0.0
            pairs.append({"item": m_item.group(1), "count": cnt})
    return pairs


def _named_list(recipe: str, key: str) -> list[dict]:
    m = re.search(rf"{key}\s*=\s*", recipe)
    if not m:
        return []
    brace = recipe.find("{", m.end())
    if brace == -1:
        return []
    end = _match_brace(recipe, brace)
    return _item_count_pairs(recipe[brace + 1:end])


def parse_recipe_module(lua: str, facility: str) -> list[dict]:
    """Parse a `Module:Recipe/<Module>` Lua source into recipe dicts. `facility` is the
    fallback when a recipe has no `facility =` field of its own."""
    m = re.search(r"local\s+data\s*=\s*", lua)
    if not m:
        return []
    brace = lua.find("{", m.end())
    if brace == -1:
        return []
    end = _match_brace(lua, brace)
    recipes = []
    for block in _top_level_tables(lua[brace + 1:end]):
        ings = _named_list(block, "ingredients")
        prods = _named_list(block, "products")
        if not ings and not prods:
            continue  # not a recipe entry
        m_time = re.search(r"time\s*=\s*([0-9.]+)", block)
        m_mode = re.search(r'mode\s*=\s*"([^"]*)"', block)
        m_ev = re.search(r"event\s*=\s*(true|false)", block)
        m_evn = re.search(r'eventName\s*=\s*"([^"]*)"', block)
        m_fac = re.search(r'facility\s*=\s*"([^"]+)"', block)
        m_env = re.search(r'envi?ron?ment\s*=\s*"([^"]*)"', block)
        m_cat = re.search(r'category\s*=\s*"([^"]*)"', block)
        recipes.append({
            "facility": m_fac.group(1) if m_fac else facility,
            "module": facility,
            "time": float(m_time.group(1)) if m_time else 0.0,
            "mode": m_mode.group(1) if m_mode else "",
            "event": m_ev.group(1) == "true" if m_ev else False,
            "eventName": m_evn.group(1) if m_evn else "",
            "environment": m_env.group(1) if m_env else "",
            "category": m_cat.group(1) if m_cat else "",
            "ingredients": ings,
            "products": prods,
        })
    return recipes


def list_modules(session: requests.Session, offline: bool) -> list[str]:
    """Authoritative recipe-module list = the modules loaded by `Module:Recipe`."""
    lua = fetch_raw(session, "Module:Recipe", offline)
    mods = re.findall(r"mw\.loadData\(\s*'Module:Recipe/([^']+)'\s*\)", lua)
    seen, out = set(), []
    for mod in mods:
        name = mod.replace("_", " ")
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


# --------------------------------------------------------------- rates / index
def rate(count: float, t: float) -> float | None:
    return round(count * 60.0 / t, 3) if t else None


def num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def with_rates(items: list[dict], t: float) -> list[dict]:
    return [{**it, "rate": rate(it["count"], t)} for it in items]


# ---------------------------------------------------------- facility infobox
def fetch_facility_info(session: requests.Session, names: list[str],
                        offline: bool) -> dict[str, dict]:
    """Facility page infobox -> {name: {cn, power, activator?}}. `activator` is the steady
    feed some machines need just to run (Transmuting Units: 6/min Liquid Xiranite/Xiragen),
    parsed from "requires a minimum input of N/min {{I|Item}}"."""
    cache = RAW_DIR / "_facilities.json"
    if offline:
        return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    out: dict[str, dict] = {}
    names = [n for n in names if n not in ("Crafting", "Construct")]
    for i in range(0, len(names), 40):
        r = _get(session, API, {
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": "|".join(names[i:i + 40]),
            "redirects": "1", "format": "json"})
        time.sleep(DELAY)
        for page in r.json().get("query", {}).get("pages", {}).values():
            try:
                text = page["revisions"][0]["slots"]["main"]["*"]
            except (KeyError, IndexError):
                continue
            info: dict = {}
            m = re.search(r"\|\s*cnname\s*=\s*([^\n|}]+)", text)
            if m:
                info["cn"] = m.group(1).strip()
            m = re.search(r"\|\s*power\s*=\s*([0-9.]+)", text)
            if m:
                info["power"] = float(m.group(1))
            m = re.search(r"requires a (?:minimum|steady) input of ([0-9.]+)/min"
                          r"\s*\{\{I\|([^}|]+)", text)
            if m:
                info["activator"] = {"item": m.group(2).strip(), "rate": float(m.group(1))}
            out[page["title"]] = info
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ------------------------------------------------------------- chinese names
def fetch_cn_names(session: requests.Session, names: list[str],
                   offline: bool) -> dict[str, str]:
    """EN item name -> Chinese name, scraped from each item page infobox `cnname=`."""
    cache = RAW_DIR / "_cnnames.json"
    cn: dict[str, str] = {}
    if cache.exists():
        cn.update(json.loads(cache.read_text(encoding="utf-8")))
    if offline:
        return cn
    todo = [n for n in names if n not in cn]
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        r = _get(session, API, {
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": "|".join(batch),
            "redirects": "1", "format": "json",
        })
        time.sleep(DELAY)
        data = r.json()
        # map any redirect-normalised titles back to what we asked for
        norm = {x["to"]: x["from"] for x in data.get("query", {}).get("redirects", [])}
        norm.update({x["to"]: x["from"]
                     for x in data.get("query", {}).get("normalized", [])})
        for page in data.get("query", {}).get("pages", {}).values():
            title = page.get("title", "")
            asked = norm.get(title, title)
            try:
                text = page["revisions"][0]["slots"]["main"]["*"]
            except (KeyError, IndexError):
                continue
            m = re.search(r"cnname\s*=\s*([^\n|}]+)", text)
            if m:
                cn[asked] = m.group(1).strip()
        sys.stderr.write(f"  cn-names {min(i + 40, len(todo))}/{len(todo)}\r")
    sys.stderr.write("\n")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(cn, ensure_ascii=False, indent=0), encoding="utf-8")
    return cn


# --------------------------------------------------------------- outpost prices
CUR_CN = {"Wuling Stock Bill": "武陵券", "Valley Stock Bill": "谷地券"}


def fetch_outpost_prices(session: requests.Session, offline: bool) -> dict[str, list]:
    """Sell prices from every endfield.wiki.gg Category:Outposts page -> {item:[price,cur]}.

    Two wikitext formats: `== Trade ==` tables (`{{I|item}}||{{Cur|currency|N}}`, the Wuling
    outposts) and `{{Outpost level|currency=..|itemN=..|priceN=..}}` prosperity templates (the
    Valley outposts). 武陵券 and 谷地券 are different currencies — not interchangeable."""
    cache = RAW_DIR / "_prices.json"
    if offline:
        return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    data = _get(session, API, {
        "action": "query", "list": "categorymembers", "cmtitle": "Category:Outposts",
        "cmlimit": "500", "cmtype": "page", "format": "json"}).json()
    outposts = [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
    prices: dict[str, list] = {}

    def add(item, cur, price):
        prices[item.strip()] = [price, CUR_CN.get(cur.strip(), cur.strip())]

    for op in outposts:
        txt = _get(session, RAW_URL, {"title": op, "action": "raw"}).text
        time.sleep(DELAY)
        for item, cur, pr in re.findall(
                r"\{\{I\|([^}|]+?)\}\}\s*\|\|\s*\{\{[Cc]ur\|([^|}]+)\|([0-9,]+)\}\}", txt):
            add(item, cur, int(pr.replace(",", "")))
        for blk in re.findall(r"\{\{Outpost level(.*?)\}\}", txt, re.S):
            cm = re.search(r"currency\s*=\s*([^\n|]+)", blk)
            cur = cm.group(1) if cm else "?"
            its = {m.group(1): m.group(2) for m in re.finditer(r"item(\d+)\s*=\s*([^\n|]+)", blk)}
            ps = {m.group(1): m.group(2) for m in re.finditer(r"price(\d+)\s*=\s*([0-9,]+)", blk)}
            for k, it in its.items():
                if k in ps:
                    add(it, cur, int(ps[k].replace(",", "")))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(prices, ensure_ascii=False, indent=0), encoding="utf-8")
    return prices


# ------------------------------------------------------------------ rendering
def label(name: str, cn: dict[str, str]) -> str:
    c = cn.get(name)
    return f"{c} {name}" if c else name


def part_str(items: list[dict], cn: dict[str, str]) -> str:
    bits = []
    for it in items:
        r = it["rate"]
        rs = f" ({num(r)}/min)" if r is not None else ""
        bits.append(f"{label(it['item'], cn)} ×{num(it['count'])}{rs}")
    return " + ".join(bits) if bits else "—"


def write_machine_md(facility: str, recipes: list[dict], cn: dict[str, str],
                     fac_info: dict | None = None) -> None:
    mods = sorted({r.get("module", facility) for r in recipes})
    src = " · ".join(WIKI_PAGE + "Module:Recipe/" + slugify(m) for m in mods)
    head = f"# {facility}" + (f" {fac_info['cn']}" if (fac_info or {}).get("cn") else "")
    lines = [head, f"source: {src}", ""]
    if fac_info:
        bits = []
        if fac_info.get("power") is not None:
            bits.append(f"power {num(fac_info['power'])}")
        act = fac_info.get("activator")
        if act:
            bits.append(f"needs a steady {num(act['rate'])}/min {label(act['item'], cn)} "
                        f"feed per machine to run")
        if bits:
            lines += [" · ".join(bits), ""]
    lines += ["| Inputs (rate/min) | Output (rate/min) | Time | Mode |",
              "|---|---|---|---|"]
    for r in recipes:
        t = f"{num(r['time'])}s" if r["time"] else "instant"
        mode = r["mode"] + (" · event" if r["event"] else "")
        if r.get("environment"):
            mode += f" · needs {r['environment']}"
        lines.append(f"| {part_str(r['ingredients'], cn)} "
                     f"| {part_str(r['products'], cn)} | {t} | {mode} |")
    (OUT_DIR / f"{slugify(facility)}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def write_index_md(machines: list[str], recipes: list[dict],
                   items: dict, cn: dict[str, str]) -> None:
    lines = [
        "# Endfield AIC Recipes — local mirror",
        f"source: endfield.wiki.gg `Module:Recipe/*` · generated: {now_iso()}",
        f"{len(machines)} machines · {len(recipes)} recipes · {len(items)} items",
        "",
        "Rate/min for any item in a recipe = count × 60 ÷ time(s). "
        "Structured data: `recipes.json`.",
        "",
        "Gas (1.4+): Fluid-Gas / Solid-Gas Transmuting Units convert fluid⇄gas / "
        "solid⇄gas and each needs a steady 6/min activator feed; Gas Reactor Globe "
        "recipes need an ENV zone (Gas Dispersing Unit fed ≥6/min gas, 13×13). "
        "Inergen / Xiragen are gathered (Gas Extractors).",
        "",
        "## Machines",
    ]
    by_fac: dict[str, int] = {}
    for r in recipes:
        by_fac[r["facility"]] = by_fac.get(r["facility"], 0) + 1
    for mfac in machines:
        lines.append(f"- [{mfac}]({slugify(mfac)}.md) — {by_fac.get(mfac, 0)} recipes")
    lines += ["", "## Item glossary (EN ↔ 中文)", "",
              "| Item | 中文 | 售价 | Produced by | Used in |", "|---|---|---|---|---|"]
    for name in sorted(items):
        info = items[name]
        prod = ", ".join(sorted({p for p, _ in info["produced_by"]})) or "—"
        use = ", ".join(sorted({f for f, _ in info["used_in"]})) or "—"
        price = (f"{info['price']} {info.get('currency', '')}"
                 if info.get("price") is not None else "")
        lines.append(f"| {name} | {cn.get(name, '')} | {price} | {prod} | {use} |")
    (OUT_DIR / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only first N machines")
    ap.add_argument("--no-cn", action="store_true", help="skip Chinese-name enrichment")
    ap.add_argument("--offline", action="store_true", help="reuse raw/ cache, no network")
    ap.add_argument("--machine", help="scrape only this one facility")
    ap.add_argument("--no-prices", action="store_true", help="skip outpost sell prices")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()

    modules = [args.machine] if args.machine else list_modules(session, args.offline)
    if args.limit:
        modules = modules[:args.limit]
    print(f"modules ({len(modules)}): {', '.join(modules)}")

    recipes: list[dict] = []
    for mod in modules:
        lua = fetch_raw(session, f"Module:Recipe/{slugify(mod)}", args.offline)
        got = parse_recipe_module(lua, mod)
        recipes += got
        print(f"  {mod}: {len(got)} recipes")
    # real facilities, in module order (event modules fold into their machines)
    machines = list(dict.fromkeys(r["facility"] for r in recipes))
    facilities = fetch_facility_info(session, machines, args.offline)

    # cross-index every item that appears as an ingredient or product
    item_names: set[str] = set()
    for r in recipes:
        for it in r["ingredients"] + r["products"]:
            item_names.add(it["item"])

    cn = {} if args.no_cn else fetch_cn_names(session, sorted(item_names), args.offline)

    # attach rates + build produced_by / used_in indexes
    items: dict[str, dict] = {n: {"cn": cn.get(n, ""), "produced_by": [], "used_in": []}
                              for n in sorted(item_names)}
    for ri, r in enumerate(recipes):
        r["ingredients"] = with_rates(r["ingredients"], r["time"])
        r["products"] = with_rates(r["products"], r["time"])
        for it in r["products"]:
            items[it["item"]]["produced_by"].append((r["facility"], ri))
        for it in r["ingredients"]:
            items[it["item"]]["used_in"].append((r["facility"], ri))

    # attach outpost sell prices (adds entries for priced items not in the recipe graph)
    prices = {} if args.no_prices else fetch_outpost_prices(session, args.offline)
    for name, pc in prices.items():
        it = items.setdefault(name, {"cn": cn.get(name, ""), "produced_by": [], "used_in": []})
        it["price"], it["currency"] = pc[0], pc[1]

    # write outputs
    for fac in machines:
        write_machine_md(fac, [r for r in recipes if r["facility"] == fac], cn,
                         facilities.get(fac))
    write_index_md(machines, recipes, items, cn)
    (OUT_DIR / "recipes.json").write_text(json.dumps({
        "source": "endfield.wiki.gg Module:Recipe/*",
        "note": "rate/min = count * 60 / time; time in seconds; time 0 = instant",
        "generated_at": now_iso(),
        "machines": machines,
        "modules": modules,
        "facilities": facilities,
        "recipes": recipes,
        "items": items,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nwrote {len(machines)} machine pages + recipes.json to {OUT_DIR}")
    print(f"{len(recipes)} recipes, {len(items)} items, "
          f"{sum(1 for v in items.values() if v.get('price') is not None)} priced"
          f"{'' if args.no_cn else f', {sum(1 for v in items.values() if v[chr(99)+chr(110)])} with 中文'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
