# Data and asset licensing

The code in this repository is licensed under GPL-3.0 (see `LICENSE`). The data and
assets below are **not** covered by GPL-3.0; each has its own terms.

## Recipe and item data: CC BY-SA 4.0

- `wiki/recipes/` (including `recipes.json` and `raw/`)
- `web/data/planner.json`

These are derived from the **Endfield Talos Wiki** (https://endfield.wiki.gg). Sources
include its `Module:Recipe/*` and `Module:Item/*` data modules and the facility and item
page infoboxes (power, activation feed, 中文 / 日本語 names, outpost prices). Wiki text is
licensed under
[Creative Commons Attribution-ShareAlike 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Contributors are listed in each page's history on endfield.wiki.gg. These derived files
are shared under the same license: CC BY-SA 4.0.

A few numbers are hard-coded in `scripts/` and cite their source in the code comments:
facility footprints, some facility power values, and battery output. They come from
arknights-endfield.wikiru.jp and the official wiki at wiki.skport.com. They are short
factual values, not copied text.

## Game content and icons: © Hypergryph / GRYPHLINE

- `web/icons/` (item and facility icons, downloaded from endfield.wiki.gg)
- All item, facility and game names and values

*Arknights: Endfield* and all related content are © Hypergryph / GRYPHLINE. This is an
unofficial, non-commercial fan tool. It is not affiliated with or endorsed by them. The
icons are included only so the planner can show them. **If you are a rights holder and
want anything removed, please open an issue and it will be taken down.**

## Third-party code

- `web/vendor/pinyin-pro-3.29.4.min.js`: [pinyin-pro](https://github.com/zh-lx/pinyin-pro),
  MIT License. See `web/vendor/pinyin-pro.LICENSE`.
