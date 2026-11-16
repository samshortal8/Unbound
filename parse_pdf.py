"""
parse_pdf.py - turn the Chrome-rendered Unbound NG+ Expert walkthrough PDF into a
structured document model (chapters -> blocks) using PyMuPDF.

The source PDF was printed from HTML, so its structure survives as geometry:
  * frames (callouts, catch boxes, boss/team cards) are navy-stroked rectangles,
    or navy fills with an inset white fill (dashed and page-split borders);
  * callouts carry a small "label tab" across the top border;
  * cards carry a full-width coloured header bar (gold = catch, red = boss,
    teal = your team) with a white badge;
  * tables have one navy header rect per column and 1pt separator rects per cell;
  * stop headers are full-width navy bars with a gold left stripe;
  * area bars are full-width gold bars;
  * checkboxes / bullets are tiny drawn squares;
  * sprite placeholders are 50x50 grey-bordered boxes holding "Name" + "#dex".

Run directly to dump JSON:  python parse_pdf.py walkthrough.pdf out.json
"""
from __future__ import annotations

import html
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import pymupdf as fitz

# ----------------------------------------------------------------- constants
TOP, BOTTOM = 50.0, 795.0          # content band (running header/footer outside)
PAGE_TOP_CONT = 60.0               # a block starting above this continues the previous page
PAGE_BOTTOM_CONT = 785.0           # a frame ending below this is split onto the next page

NAVY = (30, 34, 51)
GOLD = (232, 176, 39)
RED = (163, 50, 44)
SEP = (201, 207, 220)
PH_BORDER = (138, 144, 166)
WHITE = (255, 255, 255)

T_NAVY = 0x1E2233
T_GRAY = 0x4A5068
T_RED = 0xA3322C
T_WHITE = 0xFFFFFF
T_BLACK = 0x000000

TYPES = {"Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting", "Poison",
         "Ground", "Flying", "Psychic", "Bug", "Rock", "Ghost", "Dragon", "Dark",
         "Steel", "Fairy"}


def rgb(c):
    if c is None:
        return None
    return tuple(int(round(x * 255)) for x in c[:3])


def near(c1, c2, tol=8):
    return c1 is not None and all(abs(a - b) <= tol for a, b in zip(c1, c2))


# ------------------------------------------------------------- primitives
@dataclass
class Span:
    text: str
    font: str
    size: float
    color: int
    bbox: tuple

    @property
    def bold(self):
        return "Bol" in self.font or "Bold" in self.font

    @property
    def italic(self):
        return "Ita" in self.font

    @property
    def pixel(self):
        return "PressStart" in self.font

    @property
    def mono(self):
        return "Mono" in self.font or "Courier" in self.font

    @property
    def x0(self):
        return self.bbox[0]


@dataclass
class Line:
    bbox: tuple
    spans: list

    x0 = property(lambda s: s.bbox[0])
    y0 = property(lambda s: s.bbox[1])
    x1 = property(lambda s: s.bbox[2])
    y1 = property(lambda s: s.bbox[3])
    cx = property(lambda s: (s.bbox[0] + s.bbox[2]) / 2)
    cy = property(lambda s: (s.bbox[1] + s.bbox[3]) / 2)

    @property
    def text(self):
        return "".join(s.text for s in self.spans)

    @property
    def first(self):
        return self.spans[0]


@dataclass
class R:
    x0: float
    y0: float
    x1: float
    y1: float
    fill: Optional[tuple] = None
    stroke: Optional[tuple] = None
    lw: float = 0.0

    w = property(lambda s: s.x1 - s.x0)
    h = property(lambda s: s.y1 - s.y0)
    cx = property(lambda s: (s.x0 + s.x1) / 2)
    cy = property(lambda s: (s.y0 + s.y1) / 2)

    def contains_pt(self, x, y, pad=0.0):
        return self.x0 - pad <= x <= self.x1 + pad and self.y0 - pad <= y <= self.y1 + pad

    def contains(self, o, pad=0.5):
        return (o.x0 >= self.x0 - pad and o.y0 >= self.y0 - pad
                and o.x1 <= self.x1 + pad and o.y1 <= self.y1 + pad)

    def holds_line(self, ln: Line, pad=1.0):
        return self.contains_pt(ln.cx, ln.cy, pad)

    def same(self, o, tol=3.5):
        return (abs(self.x0 - o.x0) <= tol and abs(self.y0 - o.y0) <= tol
                and abs(self.x1 - o.x1) <= tol and abs(self.y1 - o.y1) <= tol)


def page_prims(page):
    lines = []
    flags = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP
    for b in page.get_text("dict", flags=flags)["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            spans = [Span(s["text"], s["font"], float(s["size"]), int(s["color"]), tuple(s["bbox"]))
                     for s in l["spans"] if s["text"] != ""]
            if not spans or not "".join(s.text for s in spans).strip():
                continue
            bb = tuple(l["bbox"])
            if bb[3] < TOP or bb[1] > BOTTOM:
                continue
            lines.append(Line(bb, spans))
    rects = []
    for dr in page.get_drawings():
        r = dr["rect"]
        fill = rgb(dr.get("fill")) if "f" in dr.get("type", "") else None
        stroke = rgb(dr.get("color")) if "s" in dr.get("type", "") else None
        rects.append(R(r.x0, r.y0, r.x1, r.y1, fill, stroke, float(dr.get("width") or 0)))
    return lines, rects


# ------------------------------------------------------------- inline html
def _style(s: Span):
    return (s.bold, s.italic, s.mono, s.pixel, s.color == T_RED and s.bold)


def runs_from_lines(lines):
    """Flatten lines into styled runs, joining wrapped lines with a single space."""
    runs = []
    for i, ln in enumerate(lines):
        for j, s in enumerate(ln.spans):
            t = s.text
            if j == 0 and runs:
                runs[-1][0] = runs[-1][0].rstrip()
                t = " " + t.lstrip()
            runs.append([t, _style(s)])
    merged = []
    for t, st in runs:
        if merged and merged[-1][1] == st:
            merged[-1][0] += t
        else:
            merged.append([t, st])
    return merged


def runs_html(runs):
    out = []
    for t, (b, i, m, px, hl) in runs:
        t = re.sub(r"[ \t]+", " ", t)
        lead = t[: len(t) - len(t.lstrip())]
        trail = t[len(t.rstrip()):]
        core = t.strip()
        if not core:
            out.append(" " if t else "")
            continue
        h = html.escape(core, quote=False)
        if m:
            h = f"<code>{h}</code>"
        if px:
            h = f'<span class="px">{h}</span>'
        if i:
            h = f"<em>{h}</em>"
        if b:
            h = f'<strong class="hl">{h}</strong>' if hl else f"<strong>{h}</strong>"
        out.append((" " if lead else "") + h + (" " if trail else ""))
    s = "".join(out)
    return re.sub(r" {2,}", " ", s).strip()


def lines_html(lines):
    return runs_html(runs_from_lines(lines))


def plain(lines):
    t = " ".join(ln.text.strip() for ln in lines)
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------- detection helpers
def is_checkbox(r: R):
    return (r.stroke is not None and near(r.stroke, NAVY, 12) and r.fill is not None
            and near(r.fill, WHITE, 6) and 6.5 <= r.w <= 12.5 and 6.5 <= r.h <= 12.5)


def is_bullet(r: R):
    return r.fill is not None and near(r.fill, NAVY, 12) and r.stroke is None and r.w <= 5 and r.h <= 5


def is_pill(r: R):
    return (r.stroke is not None and 0.5 <= r.lw <= 1.0
            and 11 <= r.h <= 18 and 18 <= r.w <= 200 and r.fill is not None and near(r.fill, WHITE, 6))


def dedupe(boxes, tol=3.5):
    out = []
    for b in sorted(boxes, key=lambda r: -(r.w * r.h)):
        if any(o.same(b, tol) for o in out):
            continue
        out.append(b)
    return out


def find_frames(rects):
    cands = []
    for r in rects:
        if r.w < 150 or r.h < 18:
            continue
        if r.stroke is not None and near(r.stroke, NAVY, 12) and r.lw >= 1.3:
            cands.append(R(r.x0, r.y0, r.x1, r.y1))
    navy_fills = [r for r in rects if r.fill is not None and near(r.fill, NAVY, 12) and r.w > 150 and r.h > 18]
    for r in navy_fills:
        for q in rects:
            if q is r or q.fill is None or near(q.fill, NAVY, 12):
                continue
            if (abs(q.x0 - r.x0) <= 3.5 and abs(q.x1 - r.x1) <= 3.5 and abs(q.y0 - r.y0) <= 3.5
                    and q.y1 <= r.y1 + 1 and q.h >= r.h * 0.5):
                cands.append(R(r.x0, r.y0, r.x1, r.y1))
                break
    cands = [c for c in cands if c.y0 < BOTTOM - 4 and c.y1 > TOP + 4]
    frames = dedupe(cands)
    # drop frames nested in other frames (keep outermost) - cards never nest here
    frames = [f for f in frames if not any(o is not f and o.contains(f, 1) and (o.w * o.h) > (f.w * f.h) for o in frames)]
    return sorted(frames, key=lambda r: r.y0)


def find_stop_headers(rects, lines, frames):
    out = []
    for r in rects:
        if r.fill is None or not near(r.fill, NAVY, 12) or r.w < 380 or not (18 <= r.h <= 80):
            continue
        if any(f.contains(r, 1) for f in frames):
            continue
        tl = [ln for ln in lines if r.holds_line(ln) and ln.first.pixel and ln.first.size >= 9.5 and ln.first.color == T_WHITE]
        if tl:
            out.append((R(r.x0 - 4, r.y0, r.x1, r.y1), tl))
    uniq = []
    for r, tl in sorted(out, key=lambda t: t[0].y0):
        if any(u[0].same(r, 4) for u in uniq):
            continue
        uniq.append((r, tl))
    return uniq


def find_area_bars(rects, lines, frames):
    out = []
    for r in rects:
        if r.fill is None or not near(r.fill, GOLD, 10) or r.w < 380 or not (10 <= r.h <= 26):
            continue
        if any(f.contains(r, 1) for f in frames):
            continue
        tl = [ln for ln in lines if r.holds_line(ln)]
        if tl:
            out.append((r, tl))
    return out


# ------------------------------------------------------------- tables
def find_tables(rects, lines, clip: R, exclude, limit_y=None):
    """Return list of (bbox, table_dict, consumed_lines)."""
    heads = []
    for r in rects:
        if r.fill is None or not near(r.fill, NAVY, 12) or not (8 <= r.h <= 45) or r.w < 12:
            continue
        if not clip.contains(r, 2) or any(e.contains(r, 1) for e in exclude):
            continue
        heads.append(r)
    heads = dedupe(heads, 1.0)
    rows = {}
    for r in heads:
        key = (round(r.y0), round(r.y1))
        rows.setdefault(key, []).append(r)
    tables = []
    for key in sorted(rows):
        cells = sorted(rows[key], key=lambda r: r.x0)
        hdr_lines = [ln for ln in lines if any(c.holds_line(ln) for c in cells)]
        if not hdr_lines or not all(ln.first.color == T_WHITE and ln.first.size <= 8 for ln in hdr_lines):
            continue
        # contiguous cells only
        cols = [cells[0]]
        for c in cells[1:]:
            if abs(c.x0 - cols[-1].x1) <= 2.5:
                cols.append(c)
        tables.append((cols, [ln for ln in hdr_lines if any(c.holds_line(ln) for c in cols)]))
    out = []
    for ti, (cols, hdr_lines) in enumerate(tables):
        hy1 = max(c.y1 for c in cols)
        hy0 = min(c.y0 for c in cols)
        nxt = min([t[0][0].y0 for t in tables[ti + 1:]] + [limit_y or 1e9])
        tx0, tx1 = cols[0].x0, cols[-1].x1
        seps = sorted({round(r.y0 * 2) / 2 for r in rects
                       if r.fill is not None and near(r.fill, SEP, 8) and r.h <= 1.8 and hy1 - 1 <= r.y0 < nxt
                       and any(abs(r.x0 - c.x0) <= 1.5 and abs(r.x1 - c.x1) <= 1.5 for c in cols)})
        bounds = [hy1]
        for y in seps:
            if y - bounds[-1] > 1.6:
                bounds.append(y)
        if len(bounds) < 2:
            continue
        ty1 = bounds[-1]
        bbox = R(tx0, hy0, tx1, ty1)
        headers = []
        for c in cols:
            hl = sorted([ln for ln in hdr_lines if c.holds_line(ln)], key=lambda l: (l.y0, l.x0))
            headers.append(plain(hl))
        body = [ln for ln in lines if hy1 - 0.5 <= ln.cy <= ty1 + 0.5 and tx0 - 1 <= ln.cx <= tx1 + 1]
        checks = [r for r in rects if is_checkbox(r) and bbox.contains(r, 1)]
        trows = []
        for a, b in zip(bounds, bounds[1:]):
            band = [ln for ln in body if a - 0.5 <= ln.cy <= b + 0.5]
            if not band:
                continue
            cells_html = []
            for c in cols:
                cl = sorted([ln for ln in band if c.x0 - 1 <= ln.x0 < c.x1 - 1], key=lambda l: (round(l.y0), l.x0))
                cells_html.append(cell_html(cl))
            check = any(a - 1 <= r.cy <= b + 1 for r in checks)
            if not any(cells_html):
                continue
            trows.append({"cells": cells_html, "check": check})
        if not trows:
            continue
        consumed = hdr_lines + body
        out.append((bbox, {"type": "table", "headers": headers, "rows": trows,
                           "check": any(r["check"] for r in trows)}, consumed))
    return out


def cell_html(cl):
    if not cl:
        return ""
    groups = [[cl[0]]]
    for ln in cl[1:]:
        prev = groups[-1][-1]
        same_style = abs(prev.first.size - ln.first.size) < 0.6 and prev.first.color == ln.first.color
        if same_style:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    parts = []
    for gi, g in enumerate(groups):
        h = lines_html(g)
        if gi > 0 and (g[0].first.color == T_GRAY or g[0].first.size < 9):
            h = f'<span class="sub">{h}</span>'
        parts.append(h)
    return "<br>".join(parts)


# ------------------------------------------------------------- free flow
def merge_visual_lines(lines):
    lines = sorted(lines, key=lambda l: (l.y0, l.x0))
    out = []
    for ln in lines:
        if out and abs(ln.cy - out[-1].cy) < 2.6 and ln.x0 >= out[-1].x1 - 2:
            prev = out[-1]
            out[-1] = Line((prev.x0, min(prev.y0, ln.y0), max(prev.x1, ln.x1), max(prev.y1, ln.y1)),
                           prev.spans + ln.spans)
        else:
            out.append(ln)
    return out


def parse_flow(lines, rects, clip: R, exclude=(), base_x=None, limit_y=None):
    """Parse a single-column flow into blocks. Returns list of (y0, y1, block)."""
    tabs = find_tables(rects, lines, clip, exclude, limit_y)
    used = set()
    items = []
    for bbox, tb, consumed in tabs:
        for ln in consumed:
            used.add(id(ln))
        tb["_y0"], tb["_y1"] = bbox.y0, bbox.y1
        items.append((bbox.y0, bbox.y1, tb))
    rest = [ln for ln in lines if id(ln) not in used]
    vlines = merge_visual_lines(rest)
    checks = [r for r in rects if is_checkbox(r) and clip.contains(r, 2)]
    bullets = [r for r in rects if is_bullet(r) and clip.contains(r, 2)]
    if base_x is None:
        base_x = clip.x0
    blocks = []
    cur = None

    def marker(vl):
        for r in checks:
            if r.x1 <= vl.x0 + 1 and vl.x0 - r.x1 < 14 and vl.y0 - 3 <= r.cy <= vl.y0 + 12:
                return "check"
        for r in bullets:
            if r.x1 <= vl.x0 + 1 and vl.x0 - r.x1 < 16 and vl.y0 - 2 <= r.cy <= vl.y0 + 11:
                return "bullet"
        f = vl.first
        if f.color == T_RED and f.bold and re.match(r"^\s*\d+\.\s*$", f.text) and len(vl.spans) > 1:
            return "num"
        if f.color == T_RED and f.bold and re.match(r"^\s*\d+\.\s+\S", f.text):
            return "num"
        return None

    for vl in vlines:
        f = vl.first
        mk = marker(vl)
        gap = (vl.y0 - cur["_last"].y1) if cur else 99
        if f.pixel and 7.0 <= f.size <= 9.0 and f.color == T_NAVY and mk is None:
            if cur and cur["type"] == "h3" and gap < 8:
                cur["_lines"].append(vl)
                cur["_last"] = vl
                continue
            cur = {"type": "h3", "_lines": [vl], "_last": vl, "_y0": vl.y0}
            blocks.append(cur)
            continue
        if mk:
            ltype = {"num": "ol", "bullet": "ul", "check": "checklist"}[mk]
            spans = vl.spans
            num = None
            text_x = vl.x0
            if mk == "num":
                m = re.match(r"^\s*(\d+)\.\s*(.*)$", f.text, re.S)
                num = int(m.group(1))
                rest_txt = m.group(2)
                spans = ([Span(rest_txt, f.font, f.size, f.color, f.bbox)] if rest_txt.strip() else []) + spans[1:]
                text_x = spans[0].bbox[0] if spans else vl.x0 + 15
            item = {"lines": [Line(vl.bbox, spans)], "num": num}
            if cur and cur["type"] == ltype and gap < 14:
                cur["items"].append(item)
            else:
                cur = {"type": ltype, "items": [item], "_text_x": text_x, "_y0": vl.y0}
                blocks.append(cur)
            cur["_text_x"] = text_x
            cur["_last"] = vl
            continue
        # plain text line
        if cur and cur["type"] in ("ol", "ul", "checklist") and vl.x0 >= cur["_text_x"] - 3 and gap < 12:
            cur["items"][-1]["lines"].append(vl)
            cur["_last"] = vl
            continue
        para_gap = 3.6 if f.size >= 10 else 2.0
        if cur and cur["type"] == "p" and abs(vl.x0 - cur["_x0"]) < 3 and gap < para_gap:
            cur["_lines"].append(vl)
            cur["_last"] = vl
            continue
        cur = {"type": "p", "_lines": [vl], "_last": vl, "_x0": vl.x0, "_y0": vl.y0,
               "_indent": vl.x0 - base_x}
        blocks.append(cur)

    for b in blocks:
        b["_y1"] = b["_last"].y1
        b["_lastx1"] = b["_last"].x1
        b.pop("_last", None)
        if b["type"] == "h3":
            b["text"] = plain(b.pop("_lines"))
        elif b["type"] == "p":
            ls = b.pop("_lines")
            b["html"] = lines_html(ls)
            allbold = all(s.bold or not s.text.strip() for ln in ls for s in ln.spans)
            muted = all(s.color == T_GRAY or not s.text.strip() for ln in ls for s in ln.spans)
            small = max(s.size for ln in ls for s in ln.spans) < 9.6
            if allbold and len(b["html"]) < 90:
                b["type"] = "label"
            elif muted:
                b["cls"] = "muted"
            elif small:
                b["cls"] = "small"
        else:
            b["items"] = [{"html": lines_html(it["lines"]), "num": it["num"]} for it in b["items"]]
        items.append((b["_y0"], b["_y1"], b))
    items.sort(key=lambda t: t[0])
    return items


# ------------------------------------------------------------- cards & callouts
def label_tab(frame: R, rects):
    best = None
    for r in rects:
        if r.fill is None or near(r.fill, WHITE, 6) or not (15 <= r.w <= 300):
            continue
        if not (frame.x0 - 2 <= r.x0 <= frame.x0 + 40 and r.y0 < frame.y0 + 3 and r.y1 > frame.y0 - 1):
            continue
        stroked = r.stroke is not None and near(r.stroke, NAVY, 12) and 1.0 <= r.lw <= 2.0 and 9 <= r.h <= 20
        clipped = r.stroke is None and not near(r.fill, NAVY, 12) and 3.5 <= r.h <= 20 and r.y0 <= TOP + 2
        if stroked or clipped:
            if best is None or stroked:
                best = r
    if best is not None and best.y0 <= TOP + 2:
        # tab clipped by the page box: grow it so the label text is caught
        best = R(best.x0, best.y0 - 4, best.x1, max(best.y1, best.y0 + 10), best.fill, best.stroke, best.lw)
    return best


def header_bar(frame: R, rects):
    best = None
    for r in rects:
        if r.fill is None or near(r.fill, WHITE, 6) or near(r.fill, NAVY, 12):
            continue
        if abs(r.y0 - frame.y0) <= 3.5 and r.w >= frame.w - 8 and 18 <= r.h <= 95:
            if best is None or r.h > best.h:
                best = r
    return best


CALLOUT_KINDS = [
    ("at this stop", "gold"), ("the short version", "crimson"), ("warning", "warning"),
    ("return trip", "violet"), ("before you move on", "emerald"), ("tip", "tip"),
    ("note", "note"), ("unverified", "unverified"), ("golden rule", "gold"),
    ("how to read", "note"),
]


def callout_kind(label, fill):
    l = label.lower()
    for key, kind in CALLOUT_KINDS:
        if l.startswith(key):
            return kind
    if near(fill, GOLD, 30):
        return "gold"
    if near(fill, RED, 30):
        return "crimson"
    return "note"


def placeholder_anchors(frame: R, rects):
    a = [r for r in rects if r.fill is not None and near(r.fill, PH_BORDER, 6) and 40 <= r.w <= 60 and 40 <= r.h <= 60
         and frame.contains(r, 2)]
    return sorted(dedupe(a, 2.0), key=lambda r: (r.y0, r.x0))


def placeholder_info(anchor: R, lines):
    inside = sorted([ln for ln in lines if anchor.holds_line(ln, 0.5)], key=lambda l: (l.y0, l.x0))
    dex = None
    words = []
    for ln in inside:
        t = ln.text.strip()
        m = re.fullmatch(r"#\s*(\d{1,4})", t)
        if m:
            dex = int(m.group(1))
        else:
            words.append(t)
    return " ".join(words).strip(), dex, inside


def is_label_span(s: Span):
    return s.bold and s.color == T_GRAY and s.size <= 8.6 and not s.pixel


def parse_fields(lines, right_edge=None):
    """Lines like 'Ability Rough Skin   Item none' -> [(label, html), ...].
    A line without a leading label continues the previous field only when the
    previous line ran to the right edge (i.e. it wrapped); otherwise it is a
    free note (label "")."""
    fields = []
    lines = sorted(lines, key=lambda l: (round(l.y0), l.x0))
    if right_edge is None and lines:
        right_edge = max(l.x1 for l in lines)
    prev_line = None
    for ln in lines:
        spans = list(ln.spans)
        while spans and not spans[0].text.strip():
            spans.pop(0)
        if not spans:
            continue
        ln = Line(ln.bbox, spans)
        wrapped = prev_line is not None and prev_line.x1 > right_edge - 48 and ln.y0 - prev_line.y1 < 4
        prev_line = ln
        if not is_label_span(spans[0]):
            if fields and wrapped:
                v = runs_html(runs_from_lines([ln]))
                fields[-1][1] = (fields[-1][1] + " " + v).strip()
            else:
                fields.append(["", runs_html(runs_from_lines([ln]))])
            continue
        curl, curv = None, []
        for s in spans:
            if is_label_span(s) and s.text.strip():
                if curl is not None:
                    fields.append([curl, runs_html(runs_from_lines([Line(ln.bbox, curv)])) if curv else ""])
                curl, curv = s.text.strip().rstrip(":"), []
            else:
                curv.append(s)
        if curl is not None:
            fields.append([curl, runs_html(runs_from_lines([Line(ln.bbox, curv)])) if curv else ""])
    return [[l, v.strip()] for l, v in fields]


def parse_mon_region(region_lines, anchor, rects, card_kind, right_edge=None):
    label, dex, ph_lines = placeholder_info(anchor, region_lines) if anchor else ("", None, [])
    phid = {id(l) for l in ph_lines}
    ls = [l for l in region_lines if id(l) not in phid]
    pills = [r for r in rects if is_pill(r)]
    checks = [r for r in rects if is_checkbox(r)]
    mon = {"label": label, "dex": dex, "name": "", "types": [], "level": None, "moves": [],
           "fields": [], "avail": None, "check": False}
    used = set()
    # name = biggest bold Atkinson line near the top
    names = [l for l in ls if not l.first.pixel and l.first.size >= 10.0 and l.first.color == T_NAVY]
    if names:
        nm = min(names, key=lambda l: (l.y0, l.x0))
        mon["name"] = nm.text.strip()
        used.add(id(nm))
        name_y = nm.y0
    else:
        name_y = anchor.y0 if anchor else (min(l.y0 for l in ls) if ls else 0)
    for l in ls:
        t = l.text.strip()
        if id(l) in used:
            continue
        if t in TYPES and l.first.size <= 8.5 and abs(l.y0 - name_y) < 22:
            mon["types"].append((l.x0, t))
            used.add(id(l))
        elif l.first.pixel and re.fullmatch(r"#\s*\d{1,4}", t):
            if mon["dex"] is None:
                mon["dex"] = int(t.lstrip("#").strip())
            used.add(id(l))
        elif l.first.pixel and re.fullmatch(r"Lv\.?\s*\d+.*", t):
            mon["level"] = t.replace("Lv", "").replace(".", "").strip()
            used.add(id(l))
        elif l.first.pixel and re.match(r"(now|later|optional)\b", l.first.text.strip(), re.I):
            # availability tag: "Now" / "Later: Surf" / "Optional: Swarm", then fields
            tag = l.first.text.strip()
            kind = "now" if tag.lower().startswith("now") else "later" if tag.lower().startswith("later") else "optional"
            mon["avail"] = {"kind": kind, "text": tag}
            rest = Line(l.bbox, [s for s in l.spans[1:]])
            mon["fields"].extend(parse_fields([rest]) if rest.spans else [])
            used.add(id(l))
        elif card_kind != "catch" and any(p.holds_line(l, 0.8) for p in pills):
            mon["moves"].append((round(l.y0), l.x0, t))
            used.add(id(l))
    mon["types"] = [t for _, t in sorted(mon["types"])]
    mon["moves"] = [t for _, _, t in sorted(mon["moves"])]
    remaining = [l for l in ls if id(l) not in used]
    mon["fields"].extend(parse_fields(remaining, right_edge))
    # fields with empty label = free notes
    mon["fields"] = [f for f in mon["fields"] if f[0] or f[1]]
    if anchor:
        reg = R(anchor.x0 - 22, anchor.y0 - 4, anchor.x1 + 400, anchor.y1 + 40)
        mon["check"] = any(reg.contains(c, 1) and c.x1 <= anchor.x0 + 2 for c in checks)
    return mon


def parse_card(frame: R, bar: Optional[R], kind, lines, rects):
    inner = [l for l in lines if frame.holds_line(l)]
    badge, title = "", ""
    body_top = frame.y0
    if bar:
        hl = sorted([l for l in inner if bar.holds_line(l, 1)], key=lambda l: (l.y0, l.x0))
        bl = [l for l in hl if l.first.pixel and l.first.size <= 7]
        tl = [l for l in hl if l not in bl]
        badge = plain(bl)
        title = plain(tl)
        body_top = bar.y1
        inner = [l for l in inner if l not in hl]
    anchors = placeholder_anchors(frame, rects)
    card = {"type": "catch" if kind == "catch" else "card", "variant": kind, "badge": badge,
            "title": title, "meta": [], "mons": [], "lead_fields": [], "extra": []}
    first_anchor_y = anchors[0].y0 if anchors else frame.y1
    if kind == "catch":
        mid = (frame.x0 + frame.x1) / 2
        cols = {0: [], 1: []}
        for a in anchors:
            cols[0 if a.cx < mid else 1].append(a)
        assigned = set()
        mons = []
        for ci, al in cols.items():
            cx0, cx1 = (frame.x0, mid) if ci == 0 else (mid, frame.x1)
            al.sort(key=lambda r: r.y0)
            for k, a in enumerate(al):
                ry0 = a.y0 - 8
                ry1 = (al[k + 1].y0 - 8) if k + 1 < len(al) else frame.y1
                reg = [l for l in inner if cx0 <= l.cx < cx1 and ry0 <= l.cy < ry1]
                for l in reg:
                    assigned.add(id(l))
                m = parse_mon_region(reg, a, rects, kind, cx1 - 10)
                m["_y"], m["_col"] = a.y0, ci
                mons.append(m)
        # reading order: row by row, left then right (like the source grid)
        mons.sort(key=lambda m: (round(m["_y"] / 6), m["_col"]))
        card["mons"] = mons
        lead = [l for l in inner if id(l) not in assigned and l.cy < first_anchor_y - 8]
        if lead:
            card["lead_fields"] = parse_fields(lead, frame.x1 - 10)
        extra = [l for l in inner if id(l) not in assigned and l.cy >= first_anchor_y - 8]
        if extra:
            card["extra"] = [b for _, _, b in parse_flow(extra, rects, frame)]
        return card
    # boss / team cards: optional meta grid, then one row per placeholder
    meta_lines = [l for l in inner if body_top - 1 <= l.cy < first_anchor_y - 6]
    if kind == "boss" or (meta_lines and any(l.first.size <= 7.8 and l.first.color == T_GRAY and not l.first.bold for l in meta_lines)):
        labels = [l for l in meta_lines if l.first.size <= 7.8 and l.first.color == T_GRAY and len(l.spans) == 1]
        bands = {}
        for l in labels:
            bands.setdefault(round(l.y0 / 4), []).append(l)
        band_keys = sorted(bands)
        for bi, bk in enumerate(band_keys):
            labs = sorted(bands[bk], key=lambda l: l.x0)
            y_lo = labs[0].y1 - 0.5
            y_hi = (bands[band_keys[bi + 1]][0].y0 - 0.5) if bi + 1 < len(band_keys) else first_anchor_y
            for li, lab in enumerate(labs):
                x_lo = lab.x0 - 3
                x_hi = labs[li + 1].x0 - 3 if li + 1 < len(labs) else frame.x1
                vals = sorted([l for l in meta_lines if l not in labels and x_lo <= l.x0 < x_hi and y_lo <= l.cy < y_hi],
                              key=lambda l: (l.y0, l.x0))
                card["meta"].append([lab.text.strip(), lines_html(vals)])
        used_meta = set(id(l) for l in meta_lines)
    else:
        used_meta = set()
        if meta_lines:
            card["lead_fields"] = parse_fields(meta_lines, frame.x1 - 10)
            used_meta = set(id(l) for l in meta_lines)
    assigned = set(used_meta)
    for k, a in enumerate(anchors):
        ry0 = a.y0 - 8
        ry1 = (anchors[k + 1].y0 - 8) if k + 1 < len(anchors) else frame.y1
        reg = [l for l in inner if ry0 <= l.cy < ry1 and id(l) not in assigned]
        for l in reg:
            assigned.add(id(l))
        card["mons"].append(parse_mon_region(reg, a, rects, kind, frame.x1 - 10))
    extra = [l for l in inner if id(l) not in assigned]
    if extra:
        card["extra"] = [b for _, _, b in parse_flow(extra, rects, frame)]
    return card


def parse_frame(frame: R, lines, rects):
    tab = label_tab(frame, rects)
    bar = None if tab else header_bar(frame, rects)
    inner = [l for l in lines if frame.holds_line(l) or (tab and tab.holds_line(l, 1))]
    if tab:
        lab_lines = [l for l in inner if tab.holds_line(l, 1)]
        label = plain(lab_lines)
        body = [l for l in inner if l not in lab_lines]
        kind = callout_kind(label, tab.fill)
        blocks = [b for _, _, b in parse_flow(body, rects, frame, base_x=frame.x0 + 12)]
        return {"type": "callout", "kind": kind, "label": label, "blocks": blocks}
    if bar:
        hl = [l for l in inner if bar.holds_line(l, 1)]
        badge = " ".join(l.text.strip() for l in hl if l.first.pixel and l.first.size <= 7).lower()
        if "catch" in badge or near(bar.fill, GOLD, 12):
            kind = "catch"
        elif "team" in badge:
            kind = "team"
        else:
            kind = "boss"
        return parse_card(frame, bar, kind, lines, rects)
    # continuation frame (split across pages) - decide by content
    if placeholder_anchors(frame, rects):
        anchors = placeholder_anchors(frame, rects)
        two_col = len({a.cx < frame.cx for a in anchors}) > 1
        has_level = any(l.first.pixel and re.match(r"Lv\.?\s*\d", l.text.strip()) for l in inner)
        has_pills = any(is_pill(r) and frame.contains(r, 1) for r in rects)
        card_labels = any(is_label_span(sp) and sp.text.strip().rstrip(":") in ("Nature", "EVs", "Ability", "Item", "Role")
                          for l in inner for sp in l.spans)
        is_catch = two_col or not (has_level or has_pills or card_labels)
        card = parse_card(frame, None, "catch" if is_catch else "boss", lines, rects)
        card["cont"] = True
        if not is_catch:
            card["variant"] = "?"
        return card
    blocks = [b for _, _, b in parse_flow(inner, rects, frame, base_x=frame.x0 + 12)]
    return {"type": "callout", "kind": "?", "label": "", "blocks": blocks, "cont": True}


# ------------------------------------------------------------- pages
def parse_page(page):
    lines, rects = page_prims(page)
    frames = find_frames(rects)
    stops = find_stop_headers(rects, lines, frames)
    areas = find_area_bars(rects, lines, frames)
    items = []
    used = set()
    frames = [f for f in frames if any(f.holds_line(l) for l in lines)]
    # A page sitting entirely inside a long card can come out with no border
    # drawn at all: placeholders with no frame around them -> virtual frame.
    loose = [a for a in placeholder_anchors(R(0, 0, 1e4, 1e4), rects) if not any(f.contains(a, 2) for f in frames)]
    if loose and not frames and not stops:
        frames = [R(41, TOP + 4, 555, BOTTOM - 5)]
    for f in frames:
        blk = parse_frame(f, lines, rects)
        blk["_y0"], blk["_y1"] = f.y0, f.y1
        blk["_top"] = f.y0 <= PAGE_TOP_CONT
        blk["_bottom"] = f.y1 >= PAGE_BOTTOM_CONT
        tab = label_tab(f, rects)
        for l in lines:
            if f.holds_line(l) or (tab and tab.holds_line(l, 1)):
                used.add(id(l))
        items.append((f.y0, f.y1, blk))
    for r, tl in stops:
        for l in tl:
            used.add(id(l))
        items.append((r.y0, r.y1, {"type": "stop", "title": plain(sorted(tl, key=lambda l: (l.y0, l.x0)))}))
    for r, tl in areas:
        for l in tl:
            used.add(id(l))
        items.append((r.y0, r.y1, {"type": "area", "text": plain(tl)}))
    rest = [l for l in lines if id(l) not in used]
    clip = R(38, TOP, 558, BOTTOM)
    excl = frames + [r for r, _ in stops]
    for y0, y1, b in parse_flow(rest, rects, clip, exclude=excl, base_x=40):
        b["_top"] = y0 <= PAGE_TOP_CONT
        items.append((y0, y1, b))
    items.sort(key=lambda t: (t[0], t[1]))
    return [b for _, _, b in items]


def parse_splash(page):
    lines, rects = page_prims(page)
    out = {"eyebrow": "", "num": "", "title": "", "progress": None, "stats": [], "toc_heading": "", "extra": []}
    title_lines = []
    stat_nums, stat_labels = [], []
    toc_y = None
    for l in lines:
        f = l.first
        t = l.text.strip()
        if f.pixel and f.color == 0xE8B027 and f.size >= 7 and ("chapter" in t.lower() or "appendix" in t.lower()):
            out["eyebrow"] = t
        elif f.pixel and f.size >= 15 and f.color == T_WHITE:
            title_lines.append(l)
        elif f.pixel and 18 <= f.size <= 32 and f.color == T_NAVY:
            out["num"] = t
        elif f.pixel and re.fullmatch(r"\d+/\d+", t):
            a, b = t.split("/")
            out["progress"] = [int(a), int(b)]
        elif f.pixel and 12 <= f.size <= 16 and f.color == T_NAVY:
            stat_nums.append(l)
        elif f.pixel and 7 <= f.size <= 8 and f.color == T_NAVY:
            out["toc_heading"] = t
            toc_y = l.y0
    for l in lines:
        if not l.first.pixel and abs(l.first.size - 9.0) < 0.2 and l.first.color == T_GRAY:
            stat_labels.append(l)
    for n in sorted(stat_nums, key=lambda l: (l.y0, l.x0)):
        lab = min(stat_labels, key=lambda s: abs(s.x0 - n.x0) + abs(s.y0 - n.y1) * 2, default=None)
        out["stats"].append({"value": n.text.strip(), "label": lab.text.strip() if lab else ""})
    out["title"] = plain(sorted(title_lines, key=lambda l: (l.y0, l.x0)))
    return out


def parse_cover(page):
    """Cover text by role: eyebrow lines, title lines, subtitle, dedication, footer."""
    lines, _ = page_prims(page)
    out = {"eyebrow": [], "title": [], "subtitle": "", "dedication": [], "footer": ""}
    foot = []
    for l in sorted(lines, key=lambda l: (l.y0, l.x0)):
        f, t = l.first, l.text.strip()
        if f.color == T_BLACK or f.mono or not t or t in ("HP", "100%"):
            continue
        if f.pixel and f.size >= 20:
            out["title"].append(t)
        elif f.pixel and 11 <= f.size < 20:
            out["subtitle"] = t
        elif f.pixel and l.y0 < 150:
            out["eyebrow"].append(t)
        elif f.pixel and l.y0 > 550:
            out["dedication"].append(t)
        elif not f.pixel and l.y0 > 700:
            foot.append(t)
    out["footer"] = " ".join(foot)
    return out


# ------------------------------------------------------------- document assembly
def ends_mid_paragraph(prev):
    return prev.get("type") == "p" and prev.get("_y1", 0) > 740 and prev.get("_lastx1", 0) > 470


def merge_into(seq, blk):
    """Append blk to seq, merging page-split continuations."""
    if not seq:
        seq.append(blk)
        return
    prev = seq[-1]
    t = blk.get("type")
    top = blk.get("_top")
    if top and blk.get("cont"):
        if t == "callout" and prev.get("type") == "callout" and prev.get("_bottom"):
            for b in blk["blocks"]:
                b["_top"] = True
                merge_into(prev["blocks"], b)
                b.pop("_top", None)
            prev["_bottom"] = blk.get("_bottom")
            return
        if t == "catch" and prev.get("type") == "catch":
            prev["mons"].extend(blk["mons"])
            prev["extra"].extend(blk.get("extra", []))
            prev["_bottom"] = blk.get("_bottom")
            return
        if t == "card" and prev.get("type") == "catch":
            prev["mons"].extend(blk["mons"])
            prev["_bottom"] = blk.get("_bottom")
            return
        if t in ("catch", "card") and prev.get("type") == "card":
            if blk.get("lead_fields") and prev["mons"]:
                prev["mons"][-1]["fields"].extend(blk["lead_fields"])
            elif blk.get("lead_fields"):
                prev["lead_fields"].extend(blk["lead_fields"])
            prev["mons"].extend(blk["mons"])
            prev["extra"].extend(blk.get("extra", []))
            prev["_bottom"] = blk.get("_bottom")
            return
    if top and t == "table" and prev.get("type") == "table" and prev["headers"] == blk["headers"]:
        prev["rows"].extend(blk["rows"])
        prev["check"] = prev["check"] or blk["check"]
        prev["_y1"] = blk.get("_y1")
        return
    if top and t == "p":
        if prev.get("type") in ("ol", "ul", "checklist") and blk.get("_indent", 0) > 8:
            prev["items"][-1]["html"] += " " + blk["html"]
            return
        if prev.get("type") == "p" and ends_mid_paragraph(prev) and blk.get("_indent", 0) < 4:
            prev["html"] += " " + blk["html"]
            prev["_y1"], prev["_lastx1"] = blk.get("_y1"), blk.get("_lastx1")
            return
    if top and t in ("ol", "ul", "checklist") and prev.get("type") == t:
        prev["items"].extend(blk["items"])
        return
    seq.append(blk)


def strip_private(o):
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o


def parse(pdf_path, progress=None):
    doc = fitz.open(pdf_path)
    toc = doc.get_toc()
    lvl1 = [(t, p) for lvl, t, p in toc if lvl == 1]
    # splash pages = level-1 outline entries after the cover/contents
    splash_pages = sorted({p for t, p in lvl1 if p > 2 and "contents" not in t.lower()})
    first_chapter = splash_pages[0] if splash_pages else 1
    model = {"meta": dict(doc.metadata or {}), "cover": parse_cover(doc[0]), "chapters": []}
    chapters = model["chapters"]
    for pno in range(first_chapter - 1, doc.page_count):
        page = doc[pno]
        if progress:
            progress(pno + 1, doc.page_count)
        if pno + 1 in splash_pages:
            ch = parse_splash(page)
            ch["blocks"] = []
            ch["source_page"] = pno + 1
            chapters.append(ch)
            continue
        if not chapters:
            continue
        for blk in parse_page(page):
            blk["_page"] = pno + 1
            merge_into(chapters[-1]["blocks"], blk)
    return strip_private(model)


def debug_page(pdf_path, pno):
    doc = fitz.open(pdf_path)
    return strip_private(parse_page(doc[pno - 1]))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[2].isdigit():
        print(json.dumps(debug_page(sys.argv[1], int(sys.argv[2])), indent=1, ensure_ascii=False))
    else:
        m = parse(sys.argv[1], progress=lambda i, n: (i % 50 == 0 or i == n) and print(f"parsing page {i}/{n}", file=sys.stderr))
        print(file=sys.stderr)
        out = sys.argv[2] if len(sys.argv) > 2 else "parsed.json"
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1, ensure_ascii=False)
        print("wrote", out, file=sys.stderr)
