#!/usr/bin/env python3
"""Build the data bundle + icon set for the ./web factory planner.

The web planner is a static site: it can't run Python or hit the wiki at
runtime, so this script bakes everything it needs into ./web ahead of time.

It:
  1. reads wiki/recipes/recipes.json (the source of truth, built by
     scrape_recipes.py) and keeps only the *continuous* recipes the line
     planner can solve (drops instant Crafting/Construct);
  2. enriches every item with its rarity `tier` (1-6) from the
     `Module:Item/*` Lua data modules on endfield.wiki.gg (the same source
     the wiki itself uses), defaulting each item's icon file to its name;
  3. downloads the PNG icon for every item + facility it needs into
     web/icons/{items,facilities}/ (rate-limited, descriptive UA — be a good
     wiki citizen), skipping ones already on disk;
  4. writes web/data/planner.json — {items, facilities, recipes} — the single
     file app.js fetches.

Usage:
  python3 scripts/build_web_data.py            # full build (data + icons)
  python3 scripts/build_web_data.py --no-icons # data only (fast, offline)
  python3 scripts/build_web_data.py --force-icons  # re-download every icon

Game data and icons (c) Hypergryph; recipe/item data from endfield.wiki.gg,
CC BY-SA 4.0 (see DATA-LICENSE.md).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RECIPES_JSON = ROOT / "wiki" / "recipes" / "recipes.json"
WEB = ROOT / "web"
OUT_JSON = WEB / "data" / "planner.json"
ICON_ITEMS = WEB / "icons" / "items"
ICON_FACS = WEB / "icons" / "facilities"

API = "https://endfield.wiki.gg/api.php"
RAW_URL = "https://endfield.wiki.gg/index.php"

# Names the wiki leaves blank (|cnname= / |jpname=), filled from the item's own family
# so the web planner is fully localized. Cuprium Ore: derived from Cuprium (赤铜块 /
# 赤銅塊 …) + Ore, exactly parallel to Ferrium Ore (蓝铁矿 / 青鉄鉱物).
NAME_OVERRIDES = {"Cuprium Ore": {"cn": "赤铜矿", "ja": "赤銅鉱物"}}

# Facility footprint in grid tiles (w×h). Not on wiki.gg (its infobox has power but no
# size) nor on the BiliGame wiki — sourced from the JP wiki arknights-endfield.wikiru.jp
# (設備設置上限・各設備簡易データ表), verified: every listed power value matches POWER above.
# Expanded Crucible isn't a separate building there — it's the Reactor Crucible's upgrade,
# so it keeps the reactor's 5x5 footprint.
FACILITY_SIZE = {
    "Refining Unit": "3x3", "Shredding Unit": "3x3", "Fitting Unit": "3x3",
    "Moulding Unit": "3x3", "Purification Unit": "3x3",
    "Planting Unit": "5x5", "Seed-Picking Unit": "5x5",
    "Reactor Crucible": "5x5", "Forge of the Sky": "5x5", "Expanded Crucible": "5x5",
    "Gearing Unit": "4x6", "Filling Unit": "4x6", "Packaging Unit": "4x6",
    "Grinding Unit": "4x6", "Separating Unit": "4x6",
    "Thermal Bank": "2x2",
    # 1.4 gas machines — not on wikiru yet; official wiki.skport.com entries 1085/1086/1084
    # ("Grid size: length 5 x width 5")
    "Fluid-Gas Transmuting Unit": "5x5", "Solid-Gas Transmuting Unit": "5x5",
    "Gas Reactor Globe": "5x5",
}
DISPERSER = "Gas Dispersing Unit"
# environment -> (中文, 日本語, gas that makes it) — gas mapping per endfield.wiki.gg/wiki/Environment
ENV_ITEMS = {"Stable ENV": ("稳定环境", "安定環境", "Inergen"),
             "Acrid ENV": ("酸性环境", "酸性環境", "Acridgen")}
UA = "EndfieldAICPlanner/1.0 (+https://github.com/MILES-FAN/endfield-aic-planner)"
DELAY = 0.4  # polite gap between network requests, seconds

MANUAL_FACILITIES = {"Crafting", "Construct"}  # instant / not continuous lines

# Per-machine power draw (Power units) — mirrored from scripts/balance.py so the
# web planner reports the same numbers. Keep in sync if balance.py changes.
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

# The Module:Item/* submodules that hold item metadata (name/tier/image). From
# Module:Item's mw.loadData(...) calls. We merge them all into one name->meta map.
ITEM_MODULES = [
    "AIC Products", "Progression_Materials", "Rare_Materials", "Gatherables",
    "Naturals", "Seeds", "Consumables", "Containers", "Miscellaneous",
    "Currency", "Facilities", "Gear", "Weapons", "Formulas", "Essences",
]


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _get(session, url, params=None, stream=False):
    for attempt in range(5):
        try:
            r = session.get(url, params=params, timeout=40, stream=stream)
            if r.status_code == 200:
                return r
            time.sleep(2 + attempt)
        except requests.RequestException:
            time.sleep(2 + attempt)
    raise RuntimeError(f"request failed: {url} {params}")


# ------------------------------------------------------ Module:Item/* parsing
def _match_brace(s: str, i: int) -> int:
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


def parse_item_module(lua: str) -> dict[str, dict]:
    """`["Key"]={name=..,tier=..,image=..}` entries -> {key: {tier, image}}."""
    out: dict[str, dict] = {}
    for m in re.finditer(r'\[\"([^\"]+)\"\]\s*=\s*\{', lua):
        key = m.group(1)
        start = m.end() - 1
        end = _match_brace(lua, start)
        if end == -1:
            continue
        block = lua[start:end + 1]
        tier = re.search(r"tier\s*=\s*([0-9]+)", block)
        image = re.search(r'image\s*=\s*"([^"]*)"', block)
        out[key] = {
            "tier": int(tier.group(1)) if tier else None,
            "image": image.group(1).strip() if image else None,
        }
    return out


def fetch_item_meta(session, offline_cache: Path) -> dict[str, dict]:
    if offline_cache.exists():
        cached = json.loads(offline_cache.read_text(encoding="utf-8"))
        if cached:
            return cached
    meta: dict[str, dict] = {}
    for mod in ITEM_MODULES:
        title = "Module:Item/" + mod.replace(" ", "_")
        try:
            txt = _get(session, RAW_URL, {"title": title, "action": "raw"}).text
        except RuntimeError:
            sys.stderr.write(f"  ! could not fetch {title}\n")
            continue
        time.sleep(DELAY)
        got = parse_item_module(txt)
        meta.update(got)
        sys.stderr.write(f"  {mod}: {len(got)} entries\n")
    offline_cache.write_text(json.dumps(meta, ensure_ascii=False, indent=0),
                             encoding="utf-8")
    return meta


# --------------------------------------------------------------- icon download
def icon_filename(name: str) -> str:
    """Local on-disk filename for an item/facility icon (safe, stable)."""
    return re.sub(r'[\\/:*?"<>|]+', "_", name) + ".png"


def resolve_image_urls(session, files: list[str]) -> dict[str, str]:
    """{`File:X.png`: direct image url} via the API imageinfo (batched)."""
    urls: dict[str, str] = {}
    for i in range(0, len(files), 40):
        batch = files[i:i + 40]
        data = _get(session, API, {
            "action": "query", "prop": "imageinfo", "iiprop": "url",
            "titles": "|".join(batch), "format": "json",
        }).json()
        time.sleep(DELAY)
        norm = {x["to"]: x["from"]
                for x in data.get("query", {}).get("normalized", [])}
        for page in data.get("query", {}).get("pages", {}).values():
            title = page.get("title", "")
            asked = norm.get(title, title)
            info = page.get("imageinfo")
            if info:
                urls[asked] = info[0]["url"]
    return urls


def download_icons(session, wanted: dict[str, tuple[str, Path]], force: bool) -> int:
    """wanted: display_name -> (image_basename, dest_dir). Returns #downloaded."""
    # figure out which are missing on disk
    todo = {}
    for name, (base, dest) in wanted.items():
        path = dest / icon_filename(name)
        if force or not path.exists():
            todo[name] = (base, dest, path)
    if not todo:
        return 0
    files = sorted({f"File:{base}.png" for base, _, _ in todo.values()})
    sys.stderr.write(f"  resolving {len(files)} icon URLs...\n")
    urls = resolve_image_urls(session, files)
    got = 0
    for n, (name, (base, dest, path)) in enumerate(sorted(todo.items()), 1):
        url = urls.get(f"File:{base}.png")
        if not url:
            sys.stderr.write(f"  ! no icon for {name} (File:{base}.png)\n")
            continue
        try:
            r = _get(session, url, stream=True)
            path.write_bytes(r.content)
            got += 1
        except RuntimeError:
            sys.stderr.write(f"  ! download failed for {name}\n")
        time.sleep(DELAY)
        sys.stderr.write(f"  icons {n}/{len(todo)}\r")
    sys.stderr.write("\n")
    return got


def fetch_alt_names(session, titles: list[str], cache: Path) -> dict[str, dict]:
    """EN name -> {'cn':.., 'ja':..} from each page's infobox `cnname=` / `jpname=`.

    Batched (≤40 titles/request) and cached, so it works for the full item set and the
    facilities. Used to localize item + machine names in the web planner (中文 / 日本語)."""
    out: dict[str, dict] = {}
    if cache.exists():
        out.update(json.loads(cache.read_text(encoding="utf-8")))
    todo = [n for n in titles if n not in out]
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        data = _get(session, API, {
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": "|".join(batch), "redirects": "1", "format": "json",
        }).json()
        time.sleep(DELAY)
        norm = {x["to"]: x["from"] for x in data.get("query", {}).get("redirects", [])}
        norm.update({x["to"]: x["from"] for x in data.get("query", {}).get("normalized", [])})
        for page in data.get("query", {}).get("pages", {}).values():
            title = page.get("title", "")
            asked = norm.get(title, title)
            try:
                text = page["revisions"][0]["slots"]["main"]["*"]
            except (KeyError, IndexError):
                continue
            e = {}
            for lang, key in (("cn", "cnname"), ("ja", "jpname")):
                m = re.search(rf"{key}\s*=\s*([^\n|}}]+)", text)
                if m and m.group(1).strip():
                    e[lang] = m.group(1).strip()
            out[asked] = e
        sys.stderr.write(f"  alt-names {min(i + 40, len(todo))}/{len(todo)}\r")
    if todo:
        sys.stderr.write("\n")
        cache.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    return out


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-icons", action="store_true", help="skip icon download")
    ap.add_argument("--force-icons", action="store_true", help="re-download all icons")
    ap.add_argument("--offline", action="store_true",
                    help="reuse cached item metadata (no network for the data step)")
    args = ap.parse_args()

    if not RECIPES_JSON.exists():
        print("(no recipes.json; run scripts/scrape_recipes.py first)")
        return 1
    data = json.loads(RECIPES_JSON.read_text(encoding="utf-8"))
    src_recipes, src_items = data["recipes"], data["items"]

    # keep only continuous recipes (drop instant Crafting/Construct). A machine's activator
    # feed (Transmuting Units: steady 6/min Liquid Xiranite / Xiragen) is baked in as an
    # extra ingredient flagged `activator` — rate × time/60 per run, as balance.py does.
    src_facs = data.get("facilities", {})
    recipes = []
    for r in src_recipes:
        if r["facility"] in MANUAL_FACILITIES or not r.get("time"):
            continue
        ings = [{"item": g["item"], "count": g["count"]} for g in r["ingredients"]]
        act = (src_facs.get(r["facility"]) or {}).get("activator")
        if act:
            ings.append({"item": act["item"], "count": act["rate"] * r["time"] / 60,
                         "activator": True})
        rec = {
            "facility": r["facility"],
            "time": r["time"],
            "mode": r.get("mode", ""),
            "ingredients": ings,
            "products": [{"item": p["item"], "count": p["count"]}
                         for p in r["products"]],
        }
        if r.get("event"):
            rec["event"] = True
        if r.get("environment"):
            rec["environment"] = r["environment"]   # "Stable ENV" / "Acrid ENV"
        recipes.append(rec)

    # every item / facility the continuous graph touches
    item_names, fac_names = set(), set()
    for r in recipes:
        fac_names.add(r["facility"])
        for s in r["ingredients"] + r["products"]:
            if s["item"] != "Power":
                item_names.add(s["item"])

    session = make_session()
    cache = ROOT / "wiki" / "recipes" / "raw" / "_item_meta.json"
    sys.stderr.write("fetching item metadata (tier/image)...\n")
    meta = ({} if args.offline and not cache.exists()
            else fetch_item_meta(session, cache) if not args.offline
            else json.loads(cache.read_text(encoding="utf-8")))

    # 中文 / 日本語 names for every item + facility (from each page's cnname/jpname)
    alt_cache = ROOT / "wiki" / "recipes" / "raw" / "_alt_names.json"
    sys.stderr.write("fetching item/facility names (中文/日本語)...\n")
    alt = (json.loads(alt_cache.read_text(encoding="utf-8")) if args.offline and alt_cache.exists()
           else {} if args.offline
           else fetch_alt_names(session, sorted(item_names | fac_names), alt_cache))

    # assemble items map for the web bundle
    items_out: dict[str, dict] = {}
    for name in sorted(item_names):
        info = src_items.get(name, {})
        m = meta.get(name, {})
        a = alt.get(name, {})
        ov = NAME_OVERRIDES.get(name, {})
        image = m.get("image") or name
        entry = {
            "cn": info.get("cn") or a.get("cn") or ov.get("cn", ""),
            "ja": a.get("ja") or ov.get("ja", ""),
            "tier": m.get("tier"),
            "icon": icon_filename(name),
        }
        if info.get("price") is not None:
            entry["price"] = info["price"]
            entry["currency"] = info.get("currency", "")
        items_out[name] = entry
        items_out[name]["_image"] = image  # temp, used for download, stripped below

    facilities_out = {}
    for f in sorted(fac_names):
        a = alt.get(f, {})
        sf = src_facs.get(f, {})
        facilities_out[f] = {"power": POWER.get(f, sf.get("power", 0)),
                             "icon": icon_filename(f),
                             "cn": a.get("cn", "") or sf.get("cn", ""), "ja": a.get("ja", ""),
                             "size": FACILITY_SIZE.get(f, "")}
        if sf.get("activator"):
            facilities_out[f]["activator"] = sf["activator"]

    # ENV zones (gas, 1.4+): the Gas Dispersing Unit is a player-built machine that turns
    # ≥6/min of a gas into a 13×13 environment; the planner treats it as a real machine and
    # each environment as a pseudo-item it "produces". Official wiki.skport.com entry 1090:
    # grid 3×3, area 13×13, "Power Use: 0. Can be powered by gases", 6/min per ENV.
    facilities_out[DISPERSER] = {"power": 0,
                                 "icon": icon_filename(DISPERSER),
                                 "cn": "气体散布机", "ja": "ガス散布機", "size": "3x3"}
    for env, (cn, ja, gas) in ENV_ITEMS.items():
        items_out[env] = {"cn": cn, "ja": ja, "tier": None, "icon": icon_filename(env),
                          "env": True, "gas": gas, "_image": f"{env} icon"}

    # download icons
    if not args.no_icons:
        sys.stderr.write("downloading item icons...\n")
        wanted = {name: (items_out[name]["_image"], ICON_ITEMS) for name in items_out}
        n1 = download_icons(session, wanted, args.force_icons)
        sys.stderr.write("downloading facility icons...\n")
        wanted_f = {f: (f, ICON_FACS) for f in facilities_out}
        n2 = download_icons(session, wanted_f, args.force_icons)
        sys.stderr.write(f"downloaded {n1} item + {n2} facility icons\n")

    for e in items_out.values():
        e.pop("_image", None)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "source": "endfield.wiki.gg Module:Recipe/* + Module:Item/*",
        "note": "rate/min = count * 60 / time; time in seconds. Continuous recipes only.",
        "generated_at": data.get("generated_at", ""),
        "facilities": facilities_out,
        "items": items_out,
        "recipes": recipes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {OUT_JSON.relative_to(ROOT)}: "
          f"{len(recipes)} recipes, {len(items_out)} items, "
          f"{len(facilities_out)} facilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
