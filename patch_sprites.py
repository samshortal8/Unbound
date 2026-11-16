#!/usr/bin/env python3
"""
patch_sprites.py - put real sprites into an already-built Prima edition PDF.

For when you have the finished Prima-style PDF (built with type-coloured fallback
badges) but not the original walkthrough PDF to rebuild from:

    python patch_sprites.py Unbound_NG_Expert_Prima_Edition.pdf -o Unbound_Prima_Edition.pdf

For every fallback badge it reads the Pokémon's full name and dex number printed beside
it, resolves the sprite exactly as build.py does (overrides/ -> sprite_cache/ ->
unboundwiki.com -> Dynamic Pokémon Expansion on GitHub), removes the badge's text, and
draws the sprite in the same frame the stylesheet uses for real sprites (.sprite-wrap:
#EEF1F8 fill, 1pt #B8C0D8 border, 4pt radius). Badges with no sprite found are left as
they are. The cover's "fallback badges" sentence is replaced with the sprite credit.

  --list            print what each badge resolves to, fetch nothing
  --offline         cache and overrides only
  --sources wiki    wiki only (default wiki,github)
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sprites import SpriteResolver, parse_species  # noqa: E402

FB_DEX_SIZE = 5.6        # .fb-dex font size in the 46pt badge
NAME_MIN_SIZE = 11.0     # .mon-name 12.5pt / .mon-title 11.5pt
WRAP_FILL = (0xEE / 255, 0xF1 / 255, 0xF8 / 255)
WRAP_BORDER = (0xB8 / 255, 0xC0 / 255, 0xD8 / 255)
OLD_NOTE = "Sprite slots show type-coloured fallback badges in this build."


def spans(page):
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                yield s


def find_badges(page):
    """Yield (box Rect, name text, dex int|None, badge label) for each fallback badge."""
    all_spans = list(spans(page))
    navy_boxes = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()
                  if d.get("fill") and 44 <= d["rect"].width <= 48 and 44 <= d["rect"].height <= 48]
    for s in all_spans:
        if "Press" not in s["font"] or abs(s["size"] - FB_DEX_SIZE) > 0.05 or not s["text"].startswith("#"):
            continue
        r = pymupdf.Rect(s["bbox"])
        box = next((b for b in navy_boxes if b.contains(r)), None)
        if box is None:  # fall back to the badge geometry around the label
            cx = (r.x0 + r.x1) / 2
            box = pymupdf.Rect(cx - 23, r.y0 - 17.4, cx + 23, r.y0 + 28.6)
        # full name: big Barlow spans just right of the box, on the box's first line
        name_spans = [t for t in all_spans
                      if "Barlow" in t["font"] and t["size"] >= NAME_MIN_SIZE
                      and box.x1 < t["bbox"][0] < box.x1 + 24
                      and box.y0 - 4 < t["bbox"][1] < box.y0 + 14]
        name = ""
        if name_spans:
            first = min(name_spans, key=lambda t: (t["bbox"][1], t["bbox"][0]))
            line_y = first["bbox"][1]
            row = sorted((t for t in all_spans
                          if abs(t["bbox"][1] - line_y) < 2 and "Barlow" in t["font"] and t["size"] >= NAME_MIN_SIZE
                          and first["bbox"][0] <= t["bbox"][0] < first["bbox"][0] + 250),
                         key=lambda t: t["bbox"][0])
            name = "".join(t["text"] for t in row).strip()
        label = " ".join(t["text"].strip() for t in all_spans
                         if box.contains(pymupdf.Rect(t["bbox"])) and t is not s).strip()
        dx = re.sub(r"\D", "", s["text"])
        yield box, name or label, int(dx) if dx else None, label


def patch_cover(page, note):
    hits = page.search_for(OLD_NOTE)
    if not hits:
        return False
    # Read the footer style from the original text, then redact and rewrite the sentence.
    span = next((s for s in spans(page) if "fallback badges" in s["text"]), None)
    size = span["size"] if span else 7.2
    color = pymupdf.sRGB_to_pdf(span["color"]) if span else (0.65, 0.68, 0.79)
    area = hits[0]
    for h in hits[1:]:
        area |= h
    for h in hits:
        page.add_redact_annot(h, fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    font = HERE / "fonts" / "AtkinsonHyperlegible-Regular.ttf"
    page.insert_font(fontname="atk", fontfile=str(font))
    box = pymupdf.Rect(hits[0].x0, hits[0].y0, page.rect.width - 51, hits[0].y0 + 4 * size * 1.45)
    page.insert_textbox(box, note, fontname="atk", fontsize=size, color=color, lineheight=1.35)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path, help="Prima edition PDF built with fallback badges")
    ap.add_argument("-o", "--out", type=Path, default=Path("Unbound_Prima_Edition.pdf"))
    ap.add_argument("--cache", type=Path, default=Path("sprite_cache"))
    ap.add_argument("--overrides", type=Path, default=Path("overrides"))
    ap.add_argument("--sources", default="wiki,github")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()

    doc = pymupdf.open(a.pdf)
    print(f"[1/3] scanning {len(doc)} pages for fallback badges ...")
    found = []
    for pno, page in enumerate(doc):
        for box, name, dex, label in find_badges(page):
            found.append((pno, box, name, dex, label, parse_species(name, label, dex)))
    keys = {}
    for *_, k in found:
        keys.setdefault(k.slug, k)
    print(f"      {len(found)} badges, {len(keys)} unique sprites")
    if a.list:
        names = Counter((k.slug, n) for _, _, n, _, _, k in found)
        for (slug, n), c in sorted(names.items()):
            print(f"  {slug:36} x{c:<3} {n}")
        return 0

    sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    resolver = SpriteResolver(cache_dir=a.cache, sources=sources, overrides_dir=a.overrides,
                              offline=a.offline or sources == ["none"], delay=a.delay,
                              dpe_index_file=HERE / "data" / "dpe_frontspr_index.txt",
                              log=lambda *x: print("     ", *x))
    print(f"[2/3] sprites ({'offline' if resolver.offline else ' > '.join(sources)})")
    paths = {}
    for i, (slug, k) in enumerate(sorted(keys.items()), 1):
        paths[slug] = resolver.get(k)
        if i % 50 == 0 or i == len(keys):
            print(f"      {i}/{len(keys)}  {resolver.stats}")
            resolver.save()
    resolver.save()
    missing = sorted(k.display for s, k in keys.items() if not paths[s])

    print("[3/3] patching pages ...")
    by_page = {}
    for pno, box, name, dex, label, k in found:
        if paths[k.slug]:
            by_page.setdefault(pno, []).append((box, k))
    xrefs = {}
    for pno, items in by_page.items():
        page = doc[pno]
        for box, _ in items:
            page.add_redact_annot(box + (-0.5, -0.5, 0.5, 0.5), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
        for box, k in items:
            page.draw_rect(box + (0.5, 0.5, -0.5, -0.5), color=WRAP_BORDER, fill=WRAP_FILL,
                           width=1, radius=4 / 45)
            img_rect = box + (1, 1, -1, -1)
            if k.slug in xrefs:
                page.insert_image(img_rect, xref=xrefs[k.slug])
            else:
                xrefs[k.slug] = page.insert_image(img_rect, filename=str(paths[k.slug]))

    used = sorted({resolver.manifest.get(s, {}).get("source", "override") for s, p in paths.items() if p})
    src_names = {"wiki": "unboundwiki.com", "github": "Dynamic Pokémon Expansion (Skeli789)",
                 "override": "local overrides"}
    note = (f"Sprites: {', '.join(src_names.get(s, s) for s in used)}. Pokémon sprites © Nintendo, "
            f"Creatures Inc. and GAME FREAK inc., used here for personal reference.")
    if xrefs and patch_cover(doc[0], note):
        print("      cover credit updated")
    doc.save(a.out, garbage=3, deflate=True)
    n_done = sum(len(v) for v in by_page.values())
    print(f"      wrote {a.out}: {n_done}/{len(found)} badges replaced, {a.out.stat().st_size / 1e6:.1f} MB, "
          f"{time.time() - t0:.0f}s")
    if missing:
        print(f"      no sprite found for {len(missing)}: {', '.join(missing)}")
        print("      (drop PNGs into overrides/ named by slug; see --list)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
