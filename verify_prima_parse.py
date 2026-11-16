#!/usr/bin/env python3
"""
verify_prima_parse.py - round-trip check for parse_prima.py.

Renders a reconstructed model with the unchanged pipeline (render_html.build_html + an offline
SpriteBook + prima_unbound.css + WeasyPrint, exactly as build.py does) and compares the result
with the reference Prima edition PDF:

  1. body text   normalised word sequences in reading order (running heads, page badge, bottom
                 stop title, thumb tab, contents pages and fallback-badge text left out), difflib,
                 every differing span printed; then the font/size/colour of every matched word
                 (catches a wrong <strong>/<em> or paragraph class)
  2. page count  re-rendered vs reference, plus the page and position of every named anchor
                 (ch-N, stop-N, boss-N) so the first place pagination drifts is visible
  3. visual      every page compared pixel by pixel (sprite images masked: WeasyPrint and
                 patch_sprites resample them differently), and a varied set of pages written side
                 by side as PNGs to look at

The sprite cache is only read (the resolver runs offline and is never saved).

    python verify_prima_parse.py data/model.json
    python verify_prima_parse.py data/model.json --ref ../Unbound_Prima_Edition.pdf --cache ../sprite_cache
    python verify_prima_parse.py --pdf build/verify/reparsed.pdf   # compare an existing render again
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import sys
import time
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def find_upwards(name):
    """name in this directory or the nearest parent that has it (a git worktree sits below the checkout)."""
    for d in (HERE, *HERE.parents):
        if (d / name).exists():
            return d / name
    return Path(name)

MM = 72 / 25.4
CT, CB = 14 * MM, 841.89 - 15 * MM
CR = 595.28 - 12 * MM


# ------------------------------------------------------------------ render (mirrors build.py)
def iter_mons(blocks):
    for b in blocks:
        if b.get("type") in ("card", "catch"):
            yield from b.get("mons", [])
            yield from iter_mons(b.get("extra", []))
        elif b.get("type") == "callout":
            yield from iter_mons(b.get("blocks", []))


def render(model, out_pdf: Path, build_dir: Path, cache: Path):
    from render_html import SpriteBook, build_html
    from sprites import SpriteResolver, parse_species

    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    build_dir.mkdir(parents=True, exist_ok=True)
    mons = [m for c in model["chapters"] for m in iter_mons(c["blocks"])]
    keys = {}
    for m in mons:
        k = parse_species(m.get("name", ""), m.get("label", ""), m.get("dex"))
        keys.setdefault(k.slug, k)
    resolver = SpriteResolver(cache_dir=cache, overrides_dir=None, offline=True,
                              dpe_index_file=HERE / "data" / "dpe_frontspr_index.txt", log=lambda *x: None)
    book = SpriteBook(resolver)
    for slug, k in sorted(keys.items()):
        book.memo[slug] = resolver.get(k)
    # (no resolver.save(): the sprite cache is read-only here)
    got = sum(1 for v in book.memo.values() if v)
    if got:
        srcs = sorted({resolver.manifest.get(s, {}).get("source", "override") for s, v in book.memo.items() if v})
        names = {"wiki": "unboundwiki.com", "github": "Dynamic Pokémon Expansion (Skeli789)", "override": "local overrides",
                 "cache": "cache"}
        note = (f"Sprites: {', '.join(names.get(s, s) for s in srcs)}. Pokémon sprites © Nintendo, Creatures Inc. "
                f"and GAME FREAK inc., used here for personal reference.")
    else:
        note = "Sprite slots show type-coloured fallback badges in this build."
    print(f"sprites: {got}/{len(keys)} resolved from the cache")
    html = build_html(model, book, HERE / "prima_unbound.css", build_dir / "assets", sprite_note=note)
    (build_dir / "guide.html").write_text(html, "utf-8")
    from weasyprint import HTML
    t0 = time.time()
    doc = HTML(string=html, base_url=str(HERE)).render()
    doc.metadata.title = "Pokémon Unbound NG+ Expert: Story Walkthrough (Prima-style edition)"
    doc.write_pdf(out_pdf)
    print(f"rendered {out_pdf} ({len(doc.pages)} pages) in {time.time() - t0:.0f}s")


# ------------------------------------------------------------------ text extraction
def first_chapter_page(doc):
    names = doc.resolve_names()
    pages = [v["page"] for k, v in names.items() if re.fullmatch(r"ch-\d+", k)]
    return min(pages) if pages else 0, {v["page"] for k, v in names.items() if re.fullmatch(r"ch-\d+", k)}


BADGES = [0]


def span_style(s):
    """Visible style of a span: font face, size, colour (what a wrong <strong>/<em>/class would change)."""
    font = s["font"].split("+")[-1]
    return f"{font}/{s['size']:.1f}/{s['color']:06x}"


def page_words(doc):
    """[(word, page_no, style)] in reading order, furniture and contents pages removed,
    wrapped hyphens joined."""
    first, splash = first_chapter_page(doc)
    BADGES[0] = 0
    out = []
    for pno in range(doc.page_count):
        if 0 < pno < first:
            continue          # contents pages (regenerated)
        page = doc[pno]
        spans = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    x0, y0, x1, y1 = s["bbox"]
                    if not s["text"].strip():
                        continue
                    if pno > 0 and (y1 < CT - 0.5 or y0 > CB + 0.5 or x0 > CR + 3):
                        continue
                    if pno in splash and "Press" in s["font"] and s["size"] < 7.4 and re.fullmatch(r"[\s.\d]*", s["text"]):
                        continue  # leader dots and page refs of the generated chapter lists
                    sz = round(s["size"], 2)
                    if ("Press" in s["font"] and sz in (5.6, 4.2)) or ("Barlow" in s["font"] and sz == 6.6):
                        BADGES[0] += sz == 5.6
                        continue  # fallback-badge text (the reference has sprites there)
                    spans.append((s["origin"][1], x0, x1, s["text"], span_style(s)))
        # geometric reading order (content-stream order is not stable: patch_sprites' redactions rewrite it)
        spans.sort(key=lambda t: (t[0], t[1]))
        rows, cur, cur_y = [], [], None
        for oy, x0, x1, t, st in spans:
            if cur and abs(oy - cur_y) > 1.0:
                rows.append(cur)
                cur = []
            if not cur:
                cur_y = oy
            cur.append((x0, x1, t, st))
        if cur:
            rows.append(cur)
        for row in rows:
            row.sort()
            toks = []        # [word, style]
            glue = False     # previous span ended mid-word, directly adjacent
            px1 = None
            for x0, x1, t, st in row:
                adjacent = px1 is not None and x0 - px1 <= 1.5
                parts = t.split()
                if parts and toks and glue and adjacent and not t[:1].isspace():
                    toks[-1][0] += parts[0]
                    parts = parts[1:]
                toks += [[w, st] for w in parts]
                glue = not t[-1:].isspace()
                px1 = x1
            if not toks:
                continue
            if out and out[-1][0].endswith("-") and len(out[-1][0]) > 1 and out[-1][3]:
                w0, p0, st0, _ = out.pop()
                toks[0] = [w0 + toks[0][0], st0]
            for i, (w, st) in enumerate(toks):
                out.append((w, pno + 1, st, i == len(toks) - 1))
    return [(w, p, st) for w, p, st, _ in out]


def norm(w):
    return w.replace("\u2019", "'").replace("\u00a0", " ")


def compare_text(ref_doc, new_doc, max_show=400):
    a = page_words(ref_doc)
    ref_badges = BADGES[0]
    b = page_words(new_doc)
    print(f"fallback badges left: reference {ref_badges}, re-rendered {BADGES[0]}")
    aw = [norm(w) for w, _, _ in a]
    bw = [norm(w) for w, _, _ in b]
    sm = difflib.SequenceMatcher(None, aw, bw, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    shown = 0
    lost = added = 0
    for tag, i1, i2, j1, j2 in ops:
        lost += i2 - i1
        added += j2 - j1
        if shown < max_show:
            pa = a[i1][1] if i1 < len(a) else a[-1][1]
            pb = b[j1][1] if j1 < len(b) else b[-1][1]
            ctx = " ".join(aw[max(0, i1 - 6):i1])
            print(f"  [{tag}] ref p{pa} / new p{pb}  ...{ctx}")
            print(f"      ref: {' '.join(aw[i1:i2])[:400]!r}")
            print(f"      new: {' '.join(bw[j1:j2])[:400]!r}")
            shown += 1
    ratio = sm.ratio()
    print(f"text: {len(aw)} ref words, {len(bw)} new words, {len(ops)} differing spans, "
          f"{lost} ref words not matched, {added} new words not matched, ratio {ratio:.5f}")
    # style of every matched word (font face / size / colour): catches <strong>, <em>, class mix-ups
    sdiff = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            ra, nb = a[blk.a + k], b[blk.b + k]
            if ra[2] != nb[2]:
                sdiff.append((ra, nb))
    print(f"style: {len(sdiff)} matched words with a different font/size/colour")
    for ra, nb in sdiff[:max_show]:
        print(f"  ref p{ra[1]} {ra[0]!r} {ra[2]}  |  new p{nb[1]} {nb[2]}")
    return sm, a, b, ops


def drift(ref_doc, new_doc):
    """Where pagination starts to differ: page/position of every named anchor (ch-N, stop-N, boss-N)."""
    rn, nn = ref_doc.resolve_names(), new_doc.resolve_names()

    def key(k):
        m = re.fullmatch(r"([a-z]+)-(\d+)", k)
        return (m.group(1), int(m.group(2))) if m else (k, 0)

    anchors = sorted(set(rn) & set(nn), key=lambda k: (rn[k]["page"], -rn[k]["to"][1]))
    last = None
    changes = 0
    for k in anchors:
        dp = nn[k]["page"] - rn[k]["page"]
        dy = (rn[k]["to"][1] - nn[k]["to"][1])  # PDF y is bottom-up
        sig = (dp, round(dy))
        if last is None or dp != last[0] or abs(dy - last[1]) > 2:
            print(f"  {k:9} ref p{rn[k]['page'] + 1} y{841.89 - rn[k]['to'][1]:6.1f}  new p{nn[k]['page'] + 1} "
                  f"y{841.89 - nn[k]['to'][1]:6.1f}  (pages {dp:+d}, y {dy:+.1f})")
            changes += 1
        last = (dp, dy)
    missing = sorted(set(rn) ^ set(nn), key=key)
    if missing:
        print(f"  anchors only in one document: {', '.join(missing[:20])}{' ...' if len(missing) > 20 else ''}")
    print(f"  {len(anchors)} anchors compared, {changes} places where the offset changes")


# ------------------------------------------------------------------ visual
def page_map(sm, a, b):
    """ref page -> new page with the most aligned words."""
    votes = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            pa, pb = a[blk.a + k][1], b[blk.b + k][1]
            votes.setdefault(pa, {}).setdefault(pb, 0)
            votes[pa][pb] += 1
    return {pa: max(v, key=v.get) for pa, v in votes.items()}


def sprite_boxes(page):
    """Bounding boxes of the sprite images (46pt frames): resampled differently by WeasyPrint
    (image-rendering: pixelated) and by patch_sprites' insert_image, so they are left out."""
    out = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if 30 < r.width < 60 and 30 < r.height < 60:
            out.append(r + (-2, -2, 2, 2))
    return out


def pixel_diff(ref_doc, new_doc, pm, dpi=36, thresh=48):
    """Per page: share of pixels that differ clearly between the reference and the re-render
    (sprite images masked out)."""
    from PIL import Image, ImageChops, ImageDraw
    out = []
    for pa in range(1, ref_doc.page_count + 1):
        pb = pm.get(pa, pa if pa <= new_doc.page_count else None)
        if pb is None or pb > new_doc.page_count:
            out.append((pa, pb, 1.0))
            continue
        ims = []
        for doc, p in ((ref_doc, pa), (new_doc, pb)):
            pix = doc[p - 1].get_pixmap(dpi=dpi)
            ims.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
        if ims[0].size != ims[1].size:
            out.append((pa, pb, 1.0))
            continue
        d = ImageChops.difference(ims[0], ims[1]).convert("L").point(lambda v: 255 if v > thresh else 0)
        draw = ImageDraw.Draw(d)
        k = dpi / 72
        for r in sprite_boxes(ref_doc[pa - 1]) + sprite_boxes(new_doc[pb - 1]):
            draw.rectangle([r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k], fill=0)
        out.append((pa, pb, sum(d.histogram()[255:]) / (d.width * d.height)))
    return out


def side_by_side(ref_doc, new_doc, pairs, out_dir: Path, dpi=70):
    from PIL import Image
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for label, pa, pb in pairs:
        ims = []
        for doc, p in ((ref_doc, pa), (new_doc, pb)):
            pix = doc[p - 1].get_pixmap(dpi=dpi)
            ims.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
        w = ims[0].width + ims[1].width + 12
        h = max(i.height for i in ims)
        canvas = Image.new("RGB", (w, h), (120, 120, 120))
        canvas.paste(ims[0], (0, 0))
        canvas.paste(ims[1], (ims[0].width + 12, 0))
        p = out_dir / f"{label}_ref{pa:03d}_new{pb:03d}.png"
        canvas.save(p)
        paths.append(p)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", type=Path, nargs="?", default=HERE / "data" / "model.json")
    ap.add_argument("--ref", type=Path, default=find_upwards("Unbound_Prima_Edition.pdf"),
                    help="reference Prima edition PDF (default: nearest Unbound_Prima_Edition.pdf above this script)")
    ap.add_argument("--cache", type=Path, default=find_upwards("sprite_cache"),
                    help="sprite cache, used read-only (default: nearest sprite_cache/ above this script)")
    ap.add_argument("--build-dir", type=Path, default=HERE / "build" / "verify")
    ap.add_argument("--pdf", type=Path, help="compare this already-rendered PDF instead of rendering")
    ap.add_argument("--pages", default="", help="reference pages for the side-by-side PNGs (default: a varied set)")
    ap.add_argument("--max-show", type=int, default=400)
    ap.add_argument("--dpi", type=int, default=72, help="resolution of the page-by-page pixel comparison")
    a = ap.parse_args(argv)

    new_pdf = a.pdf
    if new_pdf is None:
        model = json.loads(a.model.read_text("utf-8"))
        new_pdf = a.build_dir / "reparsed.pdf"
        render(model, new_pdf, a.build_dir, a.cache)
    ref_doc = pymupdf.open(a.ref)
    new_doc = pymupdf.open(new_pdf)

    print("\n== 1. body text ==")
    sm, aw, bw, ops = compare_text(ref_doc, new_doc, a.max_show)

    print("\n== 2. page count ==")
    print(f"reference {ref_doc.page_count} pages, re-rendered {new_doc.page_count} pages "
          f"({new_doc.page_count - ref_doc.page_count:+d})")
    drift(ref_doc, new_doc)

    print("\n== 3. visual ==")
    pm = page_map(sm, aw, bw)
    diffs = pixel_diff(ref_doc, new_doc, pm, dpi=a.dpi)
    bad = sorted([d for d in diffs if d[2] > 0.0002], key=lambda d: -d[2])
    print(f"pixel diff ({a.dpi} dpi, sprite images masked): {len(diffs) - len(bad)} of {len(diffs)} pages identical "
          f"(under 0.02% of pixels differ)")
    for pa, pb, f in bad[:40]:
        print(f"  ref p{pa} vs new p{pb}: {100 * f:.3f}% of pixels differ")
    if a.pages:
        want = [("p", int(x)) for x in a.pages.split(",") if x.strip()]
    else:
        want = [("cover", 1), ("splash", 13), ("stop_steps_callouts", 18), ("catch_grid", 17), ("boss_team", 15),
                ("card_continued", 16), ("table_continued", 7), ("dex", 250)]
    pairs = []
    for label, pa in want:
        pb = 1 if pa == 1 else pm.get(pa)
        if pb:
            pairs.append((label, pa, pb))
    for p in side_by_side(ref_doc, new_doc, pairs, a.build_dir / "compare"):
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
