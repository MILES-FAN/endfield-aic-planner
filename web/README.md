# AIC Factory Planner (web)

A visual, browser-based production-line planner for **Arknights: Endfield**'s AIC
factory. Pick a target product and a throughput; it lays out the machines, raw
inputs, byproducts and power draw as a left→right flow diagram, with item and
facility icons mirrored from `endfield.wiki.gg`.

The solvers are JavaScript ports of `scripts/balance.py` — machine counts, rates
and power match it exactly. Two modes:

- **Direct** — acyclic main-product expansion (the common case).
- **Closed-loop (LP)** — a two-phase simplex (browser stand-in for scipy's
  `linprog`) that auto-recycles byproduct/seed cycles, honors facility caps and
  open-loop items, can self-power the base by burning a battery, and optimizes
  an objective (fewest machines / max output / max revenue).

## Run it

It's a static site, but it `fetch`es `data/planner.json`, so open it over HTTP
(not `file://`):

```bash
cd web
python3 -m http.server 8000
# then open http://localhost:8000
```

## What's here

```
web/
  index.html          app shell
  style.css           Endfield-style theme (light + dark, follows OS; ◐ button forces) + rarity colors
  i18n.js             language state + EN/中文 string tables + t() (loaded before app.js)
  app.js              solver (port of balance.py) + DAG layout + render + UI
  data/planner.json   baked data bundle: items (cn/tier/icon/price), facilities, continuous recipes
  icons/items/*.png       item icons  (from endfield.wiki.gg, File:<name>.png)
  icons/facilities/*.png  facility icons
```

## Rebuild the data + icons

The bundle and icons are generated from the local wiki mirror + `endfield.wiki.gg`
by:

```bash
python3 scripts/build_web_data.py            # data + download missing icons
python3 scripts/build_web_data.py --no-icons # data only (fast, offline)
python3 scripts/build_web_data.py --force-icons  # re-download every icon
```

It reads `wiki/recipes/recipes.json` (built by `scripts/scrape_recipes.py`),
keeps only continuous recipes, enriches each item with its rarity `tier` and icon
filename from the `Module:Item/*` data modules, fetches the 中文 / 日本語 names for every
item + facility (each page's `cnname` / `jpname`), downloads the PNG icons (rate-limited,
skipping ones already on disk), and writes `web/data/planner.json`. Re-run
`scrape_recipes.py` first if the game has patched.

## Using it

- **Language** — the selector (top-left) switches the whole UI between **English /
  中文 / 日本語**; the choice persists (localStorage) and auto-detects from the browser
  on first load. Item, machine and currency names follow the language (from each
  item/facility's `cn`/`ja` in the data, falling back to English); no mixing. Search
  still matches any language.
- **Search** a target by English name or 中文, or press ↓/Enter in the box.
- **Rate /min** — leave blank for one full-speed line, or set a target throughput.
- **Recipe choices** — items with more than one recipe get a dropdown; switching re-plans (Direct mode).
- **Closed-loop** toggle — turn on the LP options bar:
  - **Objective** — fewest machines / maximize output / maximize revenue.
  - **Self-power** — burn a battery in a Thermal Bank to power the base; the target's
    output becomes the net surplus after the burn.
  - **Facility caps** — cap a facility's machine count (e.g. `Forge of the Sky ≤ 12`).
  - **Open-loop items** — awkward byproducts you'd rather supply & drain than pipe back
    (e.g. `Sewage / 污水`); the panel reports gross supply + dump instead of net.
  - **Extra raw** — items to treat as externally supplied.
  - _Note:_ "maximize output/revenue" needs at least one facility cap, or the LP is unbounded.
- **ENV zones** (gas, 1.4+) — a zone is a player-built machine: a Gas Dispersing Unit (3×3)
  fed 6/min of gas makes a 13×13 environment. Each zone is counted as a machine and its gas
  feed is planned like any input (e.g. Acridgen from Precipitation Acid). Selector:
  **Optimal** (default) tries every combination of the optional ENVs and keeps the plan
  with the fewest total machines (or best output/revenue in LP max modes); **Minimal**
  builds only zones a recipe can't do without. Zone count is a packing estimate (4 corner
  slots ≤5×5 + 4 edge slots ≤3×3 per zone, belts not counted). In the graph, the zone is
  a tinted node linked by dotted lines to the machines inside it, which get a frame in
  that ENV's color (Stable blue / Acrid amber, as on wiki.gg) plus a badge.
  Transmuting Units' 6/min activator feed is baked into their recipes; event-only recipes
  are used only when the target exists solely in an event.
- **Pinyin search** (like Minecraft's Just Enough Characters): type full pinyin, initials
  or a mix per syllable — `ztlj` / `zhuotlj` / `灼tong` → 灼铜零件; polyphones work
  (`chongxirang` / `zhongxirang` → 重息壤), `v` = ü. Also accepted in the LP item inputs.
  Powered by [pinyin-pro](https://github.com/zh-lx/pinyin-pro) 3.29.4 (MIT, vendored in
  `vendor/` with its license) — JEC's own matcher, PinIn, is Java-only.
- Drag to pan, scroll to zoom, **Fit** to re-frame. Hover a node for the full recipe.

## Scope

Both the direct planner and the closed-loop LP (`--lp`, `--cap`, `--open`,
`--selfpower`, `--max`/`--max-revenue`) run entirely in the browser. Supply caps
(`--supply`) and item/facility bans (`--ban`) are still CLI-only in
`scripts/balance.py`. Licensing of the data and icons: see `DATA-LICENSE.md` in the
repo root.
