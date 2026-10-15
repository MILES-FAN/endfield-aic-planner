# Endfield AIC Factory Planner

A browser-based production-line planner for the **AIC** factory in
*Arknights: Endfield*（明日方舟：终末地）. Pick a target product and a throughput.
It lays out the machines, raw inputs, byproducts and power draw as a
left→right flow diagram, with item and facility icons.

**Live:** https://miles-fan.github.io/endfield-aic-planner/

- **English / 中文 / 日本語** UI. Search by English, 中文, or pinyin (full, initials, or mixed: `ztlj` → 灼铜零件).
- **Direct mode**: expands the main recipe chain. Machine counts, rates and power match `scripts/balance.py`.
- **Closed-loop (LP)**: an in-browser simplex solver.
  - Recycles byproducts automatically.
  - Honors facility caps and open-loop items.
  - Can self-power the base by burning a battery.
  - Minimizes machines, or maximizes output or revenue.
- **ENV zones** (1.4 gas update): plans Gas Dispersing Units and their gas feed as part of the line.
- Light and dark themes in the game's visual style. Works on desktop and mobile.

See [`web/README.md`](web/README.md) for the full feature guide.

## Run locally

It's a static site, but it `fetch`es `data/planner.json`, so serve it over HTTP
(opening the file directly with `file://` won't work):

```bash
cd web
python3 -m http.server 8000      # open http://localhost:8000
```

## Command-line planner

```bash
python3 scripts/balance.py "Heavy Xiranite"            # one full line
python3 scripts/balance.py 钢块 --rate 30 --tree        # target rate + flow diagram
python3 scripts/balance.py "Heavy Xiranite" --lp        # closed-loop LP (needs scipy)
```

## Refresh the data after a game patch

```bash
pip install -r scripts/requirements.txt
python3 scripts/scrape_recipes.py      # wiki/recipes/  ← endfield.wiki.gg Module:Recipe/*
python3 scripts/build_web_data.py      # web/data/planner.json + missing icons
```

The scrapers are rate-limited and send `maxlag`. Keep them that way.

## Repository layout

```
web/            the static site (index.html, app.js, i18n.js, style.css, data/, icons/, vendor/)
scripts/        scrape_recipes.py · build_web_data.py · balance.py (CLI planner)
wiki/recipes/   recipes + item metadata mirrored from endfield.wiki.gg (recipes.json, per-machine tables)
.github/        GitHub Pages deployment of web/
```

## License

- **Code** (`web/*.js`, `web/*.html`, `web/*.css`, `scripts/`): [GPL-3.0](LICENSE).
- **Data** (`wiki/recipes/`, `web/data/`): derived from the
  [Endfield Talos Wiki](https://endfield.wiki.gg) under
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- **Game content and icons**: © Hypergryph / GRYPHLINE.
- **pinyin-pro** (vendored): MIT.

[`DATA-LICENSE.md`](DATA-LICENSE.md) has the details.

This is an unofficial fan tool. It is not affiliated with or endorsed by Hypergryph or GRYPHLINE.
