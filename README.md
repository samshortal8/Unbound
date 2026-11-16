# Unbound NG+ Expert walkthrough: Prima HGSS restyle

Rebuilds the 349-page fan walkthrough as a print-ready A4 PDF laid out like the 2010 Prima
HeartGold/SoulSilver guide, re-skinned in the Borrius palette (navy #16192B, violet #5B3E96,
gold #E8B027, crimson #B8292F, emerald #1F7A54).

```
pip install -r requirements.txt
python build.py --model data/model.json -o Unbound_Prima_Edition.pdf
```

`data/model.json` is the whole guide as structured content, so you don't need the original
source PDF. It was recovered from a finished Prima build with `parse_prima.py`, and a
re-render matches that build word for word. If you do have the original walkthrough PDF,
`python build.py "Unbound_NG+_Expert_Story_Walkthrough.pdf" -o Unbound_Prima_Edition.pdf`
still works.

What the build adds on top of the layout:

* **Pokémon sprites** on every encounter card and roster card (unboundwiki.com).
* **A sprite column** in the Pokédex (all 809 entries) and in every other table that lists
  Pokémon (Legendaries, team tables ...).
* **Item icons** before bold item names, held items on roster cards, trainers' bag items,
  and Item / Reward table columns (PokeAPI item sprites; names in `data/items.tsv`).
* **TM / HM discs** in the move's type colour before every TM or HM code
  (`data/unbound_tms.json`, from the Unbound wiki's TM table).
* **Mini sprites** in lines like "Catch Basculin (#550)".

`--no-items` and `--no-dex-icons` turn the icon features off.

First run fetches ~590 sprites (polite 0.6 s gap per host, roughly 10-20 minutes). Every run
after that is offline and takes about a minute.

WeasyPrint needs Pango on the system: macOS `brew install pango`, Debian/Ubuntu
`apt install libpango-1.0-0 libpangoft2-1.0-0`, Windows: install the GTK runtime / MSYS2 Pango
(see the WeasyPrint install docs).

## Pipeline

| Step | File | What it does |
|---|---|---|
| 1 | `parse_pdf.py` | PyMuPDF reads every page. The source was printed from HTML, so structure is recovered from geometry: navy frames, label tabs, header bars, table header cells, separator lines, drawn checkboxes/bullets and the 50x50 sprite placeholders. Page-split boxes, tables, lists and paragraphs are stitched back together. Output: chapters -> blocks (cached as `build/parsed-<hash>.json`). |
| 2 | `sprites.py` | Resolves each Pokémon (with form) to a sprite and caches it. |
| 3 | `render_html.py` | Emits Prima-style HTML: cover, contents with live page numbers, chapter splash pages (eyebrow, title, HP progress bar, stat tiles, "In this chapter", "Key battles"), stop bars, 2-column encounter grids, boss/team roster cards, callouts, tables, side thumb tabs. |
| 4 | `prima_unbound.css` | The stylesheet (your attachment, extended). Fonts are bundled in `fonts/` (SIL OFL). |
| 5 | `build.py` | CLI that runs the lot and writes the PDF with WeasyPrint. |
| 6 | `parse_prima.py` | Rebuilds `data/model.json` from a finished Prima PDF (fallback-badge build). `verify_prima_parse.py` re-renders the model and diffs it against a reference PDF. |

## Sprites

Lookup order, first hit wins:

1. `overrides/<slug>.png` (your own files always win)
2. `sprite_cache/img/<slug>.png` via `sprite_cache/manifest.json`
3. **unboundwiki.com**: first tries the sprite folder directly
   (`static.unboundwiki.com/.../sprites/pokemon/<species>[-<form>].png`), then discovers Pokémon page links from `/pokemon/`, tries slug guesses
   (`/pokemon/gible/`, `/pokemon/alolan-sandshrew/`, `/pokemon/sandshrew-alola/` ...), then scores
   every `<img>`, linked image and `og:image` on the page against species, dex number and form.
   Honours robots.txt, caches HTML in `sprite_cache/html/`.
4. **GitHub fallback**: `Skeli789/Dynamic-Pokemon-Expansion`, Unbound branch, `graphics/frontspr/`.
   That is the sprite set the game is built from (Alolan = `SandshrewA`, `HoopaUnbound`,
   `GroudonPrimal`, `GarchompMega` ...). The filename index ships in `data/`.
5. Nothing found: a type-coloured fallback badge with the dex number (and a MEGA/ALOLA/PRIMAL ribbon).

Forms are read from the names in the guide: `Mega Garchomp`, `Grimer (Alolan)`, `Alolan Ninetales`,
`Hoopa Unbound`, `Primal Groudon`, `Rotom-Mow`, `Landorus-T`. `(probably Mega)`, `(swap)` and
similar notes keep the base sprite.

Useful flags:

```
--sources wiki            wiki only, no GitHub fallback
--offline                 no network; cache/overrides/badges only
--list-sprites            print every slug the guide needs (names for overrides/)
--chapters 3              quick preview of one chapter
--no-dex-icons            no sprite column in Pokémon tables (on by default)
--no-items                no item / TM icons (on by default)
--model data/model.json   build from the model instead of a source PDF
--reparse                 ignore the cached parse
```

Misses are cached for 14 days so the wiki isn't hammered; delete an entry from
`sprite_cache/manifest.json` (or the whole folder) to retry. A network outage never gets
cached as a miss.

## Already have a Prima build with fallback badges?

If you only have a finished Prima edition PDF (built while the sprite sources were
unreachable) and not the original walkthrough PDF, `patch_sprites.py` puts the sprites in
afterwards. It finds each fallback badge, reads the full name and dex number printed beside
it, resolves the sprite the same way `build.py` does, and draws it in the stylesheet's sprite
frame. The rest of the PDF is untouched, and the cover credit is updated.

```
python patch_sprites.py Unbound_NG_Expert_Prima_Edition.pdf -o Unbound_Prima_Edition.pdf
python patch_sprites.py Unbound_NG_Expert_Prima_Edition.pdf --list   # names only, no fetching
```

It shares `sprite_cache/` and `overrides/` with `build.py`.

## Notes

* The wiki's TLS certificate covers `unboundwiki.com` but not `www.unboundwiki.com`, which is
  why it looked unreachable at first. The builder now uses the bare domain and fetches sprites
  straight from the wiki's sprite folder, falling back to scoring page images. If it ever
  picks a wrong image, drop the right PNG into `overrides/` under the slug from `--list-sprites`.
* Pokémon sprites are © Nintendo, Creatures Inc. and GAME FREAK inc. Fetch them for your own copy
  only; the cover footer credits whichever sources were used.
