# Unbound NG+ Expert walkthrough: Prima HGSS restyle

Rebuilds the 349-page fan walkthrough as a print-ready A4 PDF laid out like the 2010 Prima
HeartGold/SoulSilver guide, re-skinned in the Borrius palette (navy #16192B, violet #5B3E96,
gold #E8B027, crimson #B8292F, emerald #1F7A54).

```
pip install -r requirements.txt
python build.py "Unbound_NG+_Expert_Story_Walkthrough.pdf" -o Unbound_Prima_Edition.pdf
```

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
--dex-icons               also add a small sprite to all 809 Pokédex table rows
--reparse                 ignore the cached parse
```

Misses are cached for 14 days so the wiki isn't hammered; delete an entry from
`sprite_cache/manifest.json` (or the whole folder) to retry. A network outage never gets
cached as a miss.

## Notes

* The wiki refused connections from the sandbox this was built in, so the wiki scraper's
  selectors were written blind and are adaptive on purpose. If it picks a wrong image, drop the
  right PNG into `overrides/` under the slug from `--list-sprites`.
* Pokémon sprites are © Nintendo, Creatures Inc. and GAME FREAK inc. Fetch them for your own copy
  only; the cover footer credits whichever sources were used.
