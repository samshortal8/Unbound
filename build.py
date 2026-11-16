#!/usr/bin/env python3
"""
build.py - Prima-style restyle of the Pokémon Unbound NG+ Expert walkthrough.

    pip install pymupdf requests beautifulsoup4 weasyprint pillow
    python build.py Unbound_NG_Expert_Story_Walkthrough.pdf -o Unbound_Prima_Edition.pdf

Pipeline:  PyMuPDF parse (parse_pdf.py)  ->  sprite resolve + cache (sprites.py)
           ->  HTML (render_html.py) + prima_unbound.css  ->  WeasyPrint PDF

Useful flags
  --sources wiki,github   sprite sources in priority order (default). 'wiki' only = strict.
  --offline               never touch the network; cached/override sprites or fallback badges
  --overrides DIR         drop-in PNGs named <slug>.png win over everything (see --list-sprites)
  --chapters 1,3          build a subset (quick previews)
  --dex-icons             also put a small sprite on each of the 809 Pokédex table rows
  --list-sprites          print every sprite slug the guide needs, then exit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import parse_pdf  # noqa: E402
from render_html import SpriteBook, build_html, cell_text, mon_name_from_cell, pokemon_cols  # noqa: E402
from sprites import ItemIcons, SpriteResolver, parse_species  # noqa: E402


def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def iter_mons(blocks):
    for b in blocks:
        if b.get("type") in ("card", "catch"):
            yield from b.get("mons", [])
            yield from iter_mons(b.get("extra", []))
        elif b.get("type") == "callout":
            yield from iter_mons(b.get("blocks", []))


def dex_rows(blocks):
    """Pokémon named in any table with a Pokémon column (these get sprite icons)."""
    for b in blocks:
        if b.get("type") == "table":
            pc = pokemon_cols(b["headers"])
            if pc:
                ni, xi = pc
                for r in b["rows"]:
                    c = r["cells"]
                    nm = mon_name_from_cell(c[ni]) if ni < len(c) else ""
                    dx = re.sub(r"\D", "", cell_text(c[xi]))[:4] if xi is not None and xi < len(c) else ""
                    if nm:
                        yield {"name": nm, "label": nm, "dex": int(dx) if dx else None}
        for key in ("blocks", "extra"):
            if isinstance(b.get(key), list):
                yield from dex_rows(b[key])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path, nargs="?", help="source walkthrough PDF (or use --model)")
    ap.add_argument("--model", type=Path, help="build from a model JSON instead (e.g. data/model.json from parse_prima.py)")
    ap.add_argument("-o", "--out", type=Path, default=Path("Unbound_NG_Expert_Prima_Edition.pdf"))
    ap.add_argument("--build-dir", type=Path, default=Path("build"))
    ap.add_argument("--cache", type=Path, default=Path("sprite_cache"))
    ap.add_argument("--overrides", type=Path, default=Path("overrides"))
    ap.add_argument("--sources", default="wiki,github", help="comma list: wiki,github (or 'none')")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--delay", type=float, default=0.6, help="seconds between requests to one host")
    ap.add_argument("--chapters", default="", help="e.g. 1,3,9 (default: all)")
    ap.add_argument("--dex-icons", action="store_true", help="(default) sprite column in Pokémon tables")
    ap.add_argument("--no-dex-icons", action="store_true", help="no sprite column in Pokémon tables")
    ap.add_argument("--no-items", action="store_true", help="no item / TM icons")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--list-sprites", action="store_true")
    ap.add_argument("--reparse", action="store_true", help="ignore the cached parse")
    a = ap.parse_args(argv)
    if not a.pdf and not a.model:
        ap.error("give the source PDF or --model")
    a.dex_icons = not a.no_dex_icons

    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    a.build_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # 1) parse ------------------------------------------------------------
    parsed = a.model or a.build_dir / f"parsed-{sha1_file(a.pdf)}.json"
    if a.model:
        model = json.loads(a.model.read_text("utf-8"))
        print(f"[1/4] model: {a.model}")
    elif parsed.exists() and not a.reparse:
        model = json.loads(parsed.read_text("utf-8"))
        print(f"[1/4] parse: cached ({parsed.name})")
    else:
        print("[1/4] parse: reading PDF with PyMuPDF ...")
        model = parse_pdf.parse(str(a.pdf), progress=lambda i, n: (i % 50 == 0 or i == n)
                                and print(f"      page {i}/{n}"))
        parsed.write_text(json.dumps(model, ensure_ascii=False), "utf-8")
    if a.chapters:
        keep = {int(x) for x in a.chapters.split(",") if x.strip()}
        model["chapters"] = [c for i, c in enumerate(model["chapters"], 1) if i in keep]
    n_blocks = sum(len(c["blocks"]) for c in model["chapters"])
    print(f"      {len(model['chapters'])} chapters, {n_blocks} top-level blocks")

    # 2) sprites ----------------------------------------------------------
    mons = [m for c in model["chapters"] for m in iter_mons(c["blocks"])]
    if a.dex_icons:
        mons += [m for c in model["chapters"] for m in dex_rows(c["blocks"])]
    keys = {}
    for m in mons:
        k = parse_species(m.get("name", ""), m.get("label", ""), m.get("dex"))
        keys.setdefault(k.slug, k)
    if a.list_sprites:
        for slug, k in sorted(keys.items()):
            print(f"{slug:38} {k.display}")
        print(f"{len(keys)} unique sprites")
        return 0
    sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    resolver = SpriteResolver(cache_dir=a.cache, sources=sources, overrides_dir=a.overrides,
                              offline=a.offline or sources == ["none"], delay=a.delay,
                              dpe_index_file=HERE / "data" / "dpe_frontspr_index.txt",
                              log=lambda *x: print("     ", *x))
    print(f"[2/4] sprites: {len(keys)} unique ({'offline' if resolver.offline else ' > '.join(sources)})")
    book = SpriteBook(resolver)
    for i, (slug, k) in enumerate(sorted(keys.items()), 1):
        p = resolver.get(k)
        book.memo[slug] = p
        if i % 50 == 0 or i == len(keys):
            print(f"      {i}/{len(keys)}  {resolver.stats}")
            resolver.save()
    resolver.save()
    got = sum(1 for v in book.memo.values() if v)
    if got:
        srcs = sorted({resolver.manifest.get(s, {}).get("source", "override") for s, v in book.memo.items() if v})
        names = {"wiki": "unboundwiki.com", "github": "Dynamic Pokémon Expansion (Skeli789)", "override": "local overrides",
                 "cache": "cache"}
        note = (f"Sprites: {', '.join(names.get(s, s) for s in srcs)}. Pokémon sprites © Nintendo, Creatures Inc. "
                f"and GAME FREAK inc., used here for personal reference.")
    else:
        note = "Sprite slots show type-coloured fallback badges in this build."
    print(f"      resolved {got}/{len(keys)}; the rest use fallback badges")

    # 3) html -------------------------------------------------------------
    items = None if a.no_items else ItemIcons(resolver, HERE / "data" / "items.tsv", HERE / "data" / "unbound_tms.json")
    if items:
        note += " Item and TM icons: PokeAPI sprites."
    css = HERE / "prima_unbound.css"
    html = build_html(model, book, css, a.build_dir / "assets", dex_icons=a.dex_icons, sprite_note=note, items=items)
    if items:
        items.save()
        print(f"      item/TM icons: {items.stats}")
    html_path = a.build_dir / "guide.html"
    html_path.write_text(html, "utf-8")
    print(f"[3/4] html: {html_path} ({len(html) // 1024} KB)")
    if a.html_only:
        return 0

    # 4) pdf --------------------------------------------------------------
    print("[4/4] pdf: WeasyPrint layout (a few minutes for the full guide) ...")
    from weasyprint import HTML
    doc = HTML(string=html, base_url=str(HERE)).render()
    doc.metadata.title = "Pokémon Unbound NG+ Expert: Story Walkthrough (Prima-style edition)"
    doc.write_pdf(a.out)
    print(f"      wrote {a.out}  ({len(doc.pages)} pages, {a.out.stat().st_size / 1e6:.1f} MB) "
          f"in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
