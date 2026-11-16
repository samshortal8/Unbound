#!/usr/bin/env python3
"""
parse_prima.py - rebuild the document model (the JSON render_html.py consumes) from a
finished Prima edition PDF made by this same pipeline (render_html.py + prima_unbound.css,
WeasyPrint), for when the original walkthrough PDF is gone.

    python parse_prima.py prima_build.pdf -o data/model.json
    python parse_prima.py prima_build.pdf --page 15        # debug: blocks parsed from one page

Every CSS class leaves a distinct signature in the PDF, so the parse works from text style
(font, size, colour) plus the boxes WeasyPrint draws:

  stop-header   Barlow 16 italic white on a navy bar           -> stop (title from the outline)
  sec-head      Barlow 12.5 italic violet                       -> h3   (text from the outline)
  sub-label     Barlow 10.5 navy                                -> label
  area-bar      Barlow 12 italic navy behind an AREA pill       -> area
  prima-box     Barlow 9.8 italic header; box colours = kind    -> callout
  boss-card     Barlow 13 italic white on crimson / violet bar  -> card (mon rows, meta grid)
  catch-box     CATCH HERE pill, two-column encounter grid      -> catch
  data-table    gold-on-navy th cells, cell borders             -> table
  steps/bullets/checklist markers, Atkinson body text           -> ol / ul / checklist / p

The outline gives every h1/h2/h3 in its original case and the "Key battles" table gives boss
card titles; other CSS-uppercased text (mon names, labels, headers) is re-cased from the
Pokédex table and the document's own mixed-case vocabulary. Page furniture (running heads,
page badge, bottom stop title, thumb tab) and the generated parts (contents, "In this chapter",
"Key battles") are skipped. Blocks split by page breaks (tables, cards, catch grids,
paragraphs, lists) are stitched back together.

Inline markup is read back exactly enough to reproduce every line break: Atkinson Bold is
<strong>, Italic <em>, 7.4pt cell text <span class="sub">; a <br> sits wherever the next word
would have fitted on the line; a bold span whose space has the regular face's advance was two
<strong>s. Data tables use auto layout, so a cell's single-line width moves every column: each
table is rendered alone with WeasyPrint and wrapped hyphens are read as 'X-Y' or parse_pdf's
'X- Y' until the column edges and every cell's line breaks match the PDF (--no-table-check
skips this).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# ------------------------------------------------------------------ page geometry (A4, @page margins)
MM = 72 / 25.4
PAGE_H = 841.89
CT = 14 * MM            # content top     39.69
CB = PAGE_H - 15 * MM   # content bottom  799.37
CL = 12 * MM            # content left    34.02
CR = 595.28 - 12 * MM   # content right   561.26

NAVY, VIOLET, GOLD, CRIMSON, EMERALD = 0x16192B, 0x5B3E96, 0xE8B027, 0xB8292F, 0x1F7A54
WHITE, BODY, MUTED, META, SLATE = 0xFFFFFF, 0x1A1D2E, 0x5A6078, 0x3A405A, 0x2D324A

SHORT_FIELDS = ("Nature", "EVs", "Ability", "Item")
TYPES = ["Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting", "Poison", "Ground",
         "Flying", "Psychic", "Bug", "Rock", "Ghost", "Dragon", "Dark", "Steel", "Fairy"]
SPRITE_NOTES = (r"\s*Sprite slots show type-coloured fallback badges in this build\.?",
                r"\s*Sprites: .*?used here for personal reference\.?")


def near(a, b, tol=0.06):
    return abs(a - b) <= tol


# ------------------------------------------------------------------ primitives
@dataclass
class Span:
    text: str
    kind: str          # atk atkB atkI | bB bU bUI bH | ps psB | mono
    size: float
    color: int
    x0: float
    y0: float
    x1: float
    y1: float
    oy: float          # baseline
    chars: list        # [(c, x0, x1)]

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    def is_(self, kind, size, color=None, tol=0.06):
        return self.kind == kind and abs(self.size - size) <= tol and (color is None or self.color == color)

    @property
    def atk(self):
        return self.kind.startswith("atk")


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    fill: int
    n: int             # path items
    simple: bool       # a single 're' item

    w = property(lambda s: s.x1 - s.x0)
    h = property(lambda s: s.y1 - s.y0)
    cx = property(lambda s: (s.x0 + s.x1) / 2)
    cy = property(lambda s: (s.y0 + s.y1) / 2)

    def has(self, o, pad=1.0):
        return o.x0 >= self.x0 - pad and o.y0 >= self.y0 - pad and o.x1 <= self.x1 + pad and o.y1 <= self.y1 + pad

    def has_pt(self, x, y, pad=0.5):
        return self.x0 - pad <= x <= self.x1 + pad and self.y0 - pad <= y <= self.y1 + pad

    def same(self, o, tol=1.0):
        return all(abs(a - b) <= tol for a, b in ((self.x0, o.x0), (self.y0, o.y0), (self.x1, o.x1), (self.y1, o.y1)))


@dataclass
class Seg:
    x0: float
    y0: float
    x1: float
    y1: float
    color: int


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def has(self, o, pad=1.0):
        return o.x0 >= self.x0 - pad and o.y0 >= self.y0 - pad and o.x1 <= self.x1 + pad and o.y1 <= self.y1 + pad

    def has_pt(self, x, y, pad=0.5):
        return self.x0 - pad <= x <= self.x1 + pad and self.y0 - pad <= y <= self.y1 + pad


def is_cbox(b):
    """Checkbox square: .cbox (7.5pt) or the checklist marker (9.7pt), white fill."""
    return b.simple and b.fill == WHITE and 7.0 <= b.w <= 10.3 and abs(b.w - b.h) < 0.6


def font_kind(name, flags):
    if "Atkinson" in name:
        return "atk" + ("B" if "Bo" in name else "") + ("I" if "It" in name else "")
    if "Barlow" in name:
        if "Heavy" in name:
            return "bH"
        if "Ultra" in name:
            return "bUI" if flags & 2 else "bU"
        return "bB"
    if "Press" in name:
        return "psB" if "Bold" in name else "ps"
    if "Mono" in name:
        return "mono"
    return "other"


def rgb_int(c):
    if c is None:
        return None
    r, g, b = (int(round(x * 255)) for x in c[:3])
    return (r << 16) | (g << 8) | b


class PageData:
    def __init__(self, page):
        self.pno = page.number + 1
        self.spans: list[Span] = []
        raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
        for b in raw["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    chars = [(c["c"], c["bbox"][0], c["bbox"][2]) for c in s["chars"]]
                    text = "".join(c[0] for c in chars)
                    if not text:
                        continue
                    x0, y0, x1, y1 = s["bbox"]
                    self.spans.append(Span(text, font_kind(s["font"], s["flags"]), round(s["size"], 2),
                                           s["color"], x0, y0, x1, y1, s["origin"][1], chars))
        self.boxes: list[Box] = []
        self.segs: list[Seg] = []
        for dr in page.get_drawings():
            r = dr["rect"]
            if "f" in dr.get("type", "") and dr.get("fill") is not None:
                items = dr["items"]
                self.boxes.append(Box(r.x0, r.y0, r.x1, r.y1, rgb_int(dr["fill"]), len(items),
                                      len(items) == 1 and items[0][0] == "re"))
            if "s" in dr.get("type", "") and dr.get("color") is not None:
                self.segs.append(Seg(r.x0, r.y0, r.x1, r.y1, rgb_int(dr["color"])))

    def body_spans(self):
        """Spans inside the content box (drops running heads, page badge, bottom title, thumb tab)."""
        return [s for s in self.spans if s.y1 > CT - 0.5 and s.y0 < CB + 0.5 and s.x0 < CR + 3 and s.text.strip()]


# ------------------------------------------------------------------ re-casing CSS-uppercased text
WORD_RE = re.compile(r"[^\W_](?:[\w'’.+&/-]*[\w+'’])?", re.U)


class Recaser:
    """Rebuilds the case of text that CSS uppercased, from the document's own mixed-case text."""

    def __init__(self):
        self.forms = defaultdict(Counter)       # words seen mid-sentence (their case is meaningful)
        self.forms_init = defaultdict(Counter)  # sentence-initial words (fallback only)
        self.pairs = defaultdict(Counter)       # (prev, word) -> case of word after prev ("Super Rod")
        self.fixed = {}

    def feed(self, text, weight=1):
        for m in WORD_RE.finditer(text):
            w = m.group(0)
            if not any(ch.isalpha() for ch in w):
                continue
            before = text[:m.start()].rstrip()
            initial = not before or before[-1] in ".:!?(\"\u201c"
            (self.forms_init if initial else self.forms)[w.lower()][w] += weight
            pm = re.search(r"([^\W_][\w'\u2019.+&/-]*)\s+$", text[:m.start()])
            if pm and not initial:
                self.pairs[(pm.group(1).lower(), w.lower())][w] += weight

    def word(self, w, first, prev=None):
        """prev: the previous output word when it is capitalized (compound names: 'Super Rod')."""
        key = w.lower()
        pair = self.pairs.get((prev.lower(), key)) if prev else None
        if key in self.fixed:
            f = self.fixed[key]
        elif pair and sum(pair.values()) >= 2 and pair.most_common(1)[0][0][:1].isupper():
            f = pair.most_common(1)[0][0]
        elif key in self.forms or key in self.forms_init:
            c = self.forms.get(key) or self.forms_init[key]
            best = max(c.items(), key=lambda kv: (kv[1], kv[0] != kv[0].upper(), kv[0][:1].isupper()))[0]
            # headings are sentence case: a common word stays lower case even if it is also a move/item name
            low = c.get(key, 0)
            if best != key and low and low >= 0.1 * c[best]:
                best = key
            f = best
        elif "-" in w.strip("-") and len(w) > 2:
            parts = w.split("-")
            return "-".join(self.word(p, first and i == 0, prev if i == 0 else None) if p else p
                            for i, p in enumerate(parts))
        elif any(ch.isdigit() for ch in w):
            f = w
        else:
            f = w[:1].upper() + w[1:].lower()
        if first and f[:1].islower() and not any(ch.isdigit() for ch in f):
            f = f[:1].upper() + f[1:]
        return f

    def __call__(self, text, first=True):
        out = []
        pos = 0
        is_first = first
        prev = None
        for m in WORD_RE.finditer(text):
            gap = text[pos:m.start()]
            out.append(gap)
            if prev is not None and gap.strip():
                prev = None  # punctuation between words breaks the pair
            if re.search(r"[.:!?]\s+$", gap) and pos > 0:
                is_first = True  # new sentence, or the item after "Later:"
            wd = self.word(m.group(0), is_first, prev)
            out.append(wd)
            if any(ch.isalpha() for ch in wd):
                is_first = False
            prev = wd if wd[:1].isupper() else None
            pos = m.end()
        out.append(text[pos:])
        return "".join(out)


# ------------------------------------------------------------------ lines
@dataclass
class Line:
    spans: list

    @property
    def x0(self):
        return self.spans[0].x0

    @property
    def x1(self):
        return max(s.x1 for s in self.spans)

    @property
    def y0(self):
        return min(s.y0 for s in self.spans)

    @property
    def y1(self):
        return max(s.y1 for s in self.spans)

    @property
    def oy(self):
        return self.spans[0].oy

    @property
    def text(self):
        return "".join(s.text for s in self.spans)

    @property
    def first(self):
        return self.spans[0]

    @property
    def size(self):
        c = Counter()
        for s in self.spans:
            c[s.size] += len(s.text.strip())
        return c.most_common(1)[0][0]

    def first_word_width(self):
        """Width of the line's first word (up to the first space)."""
        chars = [c for s in self.spans for c in s.chars]
        i = 0
        while i < len(chars) and chars[i][0].isspace():
            i += 1
        j = i
        while j < len(chars) and not chars[j][0].isspace():
            j += 1
        if j <= i:
            return 0.0
        return chars[j - 1][2] - chars[i][1]

    def space_width(self):
        for s in self.spans:
            for c in s.chars:
                if c[0] == " " and c[2] > c[1]:
                    return c[2] - c[1]
        return self.size * 0.25


def make_lines(spans, tol=2.0):
    """Group spans into visual lines by baseline."""
    out = []
    for s in sorted(spans, key=lambda s: (round(s.oy, 1), s.x0)):
        if out and abs(s.oy - out[-1].oy) < tol:
            out[-1].spans.append(s)
        else:
            out.append(Line([s]))
    for l in out:
        l.spans.sort(key=lambda s: s.x0)
    out.sort(key=lambda l: (l.oy, l.x0))
    return out


# ------------------------------------------------------------------ inline html
def span_style(s: Span):
    if s.kind == "atkB" or s.kind == "psB":
        return "b"
    if s.kind == "atkI":
        return "i"
    if s.kind == "mono":
        return "code"
    if s.kind == "ps":
        return "px"
    return ""


class Joiner:
    """Decides how two wrapped lines join (space or not) using the document's hyphenated words."""

    def __init__(self):
        self.hyph = Counter()      # 'post-game' seen mid-line
        self.spaced = Counter()    # 'post- game' seen mid-line
        self.dot = Counter()       # ('lv', False): 'Lv.32' seen mid-line; ('lv', True): 'Lv. 32'

    def feed(self, text):
        for m in re.finditer(r"(\w+)-(\w+)", text):
            self.hyph[(m.group(1).lower(), m.group(2).lower())] += 1
        for m in re.finditer(r"(\w+)- (\w+)", text):
            self.spaced[(m.group(1).lower(), m.group(2).lower())] += 1
        for m in re.finditer(r"(\w+)\.( ?)\d", text):
            self.dot[(m.group(1).lower(), bool(m.group(2)))] += 1

    def sep(self, prev_text, next_text):
        """Pango only breaks a line without a space after a hyphen, slash, dash or 'Lv.'-style dot."""
        p = prev_text.rstrip()
        n = next_text.lstrip()
        if not p or not n or len(p) < 2:
            return " "
        last = p[-1]
        if last in "-‐‑/–—":
            if p[-2] == " ":
                return " "        # 'Atk /' + '252': the break was at the space after the slash
            if last in "-‐‑":
                m1 = re.search(r"(\w+)-$", p)
                m2 = re.match(r"(\w+)", n)
                if m1 and m2:
                    k = (m1.group(1).lower(), m2.group(1).lower())
                    if self.spaced[k] > self.hyph[k]:
                        return " "
            return ""
        if last == "." and n[0].isdigit():
            m = re.search(r"(\w+)\.$", p)
            if m and self.dot[(m.group(1).lower(), False)] > self.dot[(m.group(1).lower(), True)]:
                return ""
        return " "


JOIN = Joiner()


def _runs_html(runs):
    merged = []
    for t, st in runs:
        if merged and merged[-1][1] == st:
            merged[-1][0] += t
        else:
            merged.append([t, st])
    out = []
    for t, st in merged:
        t = re.sub(r"[ \t\n]+", " ", t)
        lead = " " if t[:1] == " " else ""
        trail = " " if t[-1:] == " " and t.strip() else ""
        core = t.strip(" ")
        if not core:
            out.append(" " if t else "")
            continue
        h = html.escape(core, quote=False)
        if st == "b":
            h = f"<strong>{h}</strong>"
        elif st == "i":
            h = f"<em>{h}</em>"
        elif st == "code":
            h = f"<code>{h}</code>"
        elif st == "px":
            h = f'<span class="px">{h}</span>'
        out.append(lead + h + trail)
    return "".join(out)


def runs_to_html(runs):
    """runs: [(text, style)] -> inline html, whitespace kept outside tags.
    style is 'b' / 'i' / '' ... or (style, wrapper_class) for runs inside <span class=wrapper>."""
    groups = []
    for t, st in runs:
        st, wrap = st if isinstance(st, tuple) else (st, None)
        if groups and groups[-1][0] == wrap:
            groups[-1][1].append((t, st))
        else:
            groups.append([wrap, [(t, st)]])
    out = []
    for wrap, rs in groups:
        h = _runs_html(rs)
        if wrap and h.strip():
            lead = " " if h[:1] == " " else ""
            trail = " " if h[-1:] == " " else ""
            h = f'{lead}<span class="{wrap}">{h.strip()}</span>{trail}'
        out.append(h)
    return re.sub(r" {2,}", " ", "".join(out)).strip()


REGULAR_SPACE_EM, BOLD_SPACE_EM = 0.28, 0.32   # Atkinson Hyperlegible space advances


def span_runs(s, style_fn):
    """A span's text as styled runs. In a bold span, a space with the regular face's advance sat
    between two <strong>s ('<strong>Dig</strong> <strong>unverified</strong>'): it is kept as a
    plain run so the round trip shapes it the same way (the 0.04em matters for line breaks)."""
    st = style_fn(s)
    base = st[0] if isinstance(st, tuple) else st
    if s.kind != "atkB" or base != "b" or " " not in s.text.strip():
        return [(s.text, st)]
    plain_st = ("",) + tuple(st[1:]) if isinstance(st, tuple) else ""
    cut = (REGULAR_SPACE_EM + BOLD_SPACE_EM) / 2 * s.size
    out = []
    for i, (c, x0, x1) in enumerate(s.chars):
        inner = 0 < i < len(s.chars) - 1
        cur = plain_st if c == " " and inner and (x1 - x0) < cut else st
        if out and out[-1][1] == cur:
            out[-1][0] += c
        else:
            out.append([c, cur])
    return [(t, x) for t, x in out]


def lines_runs(lines, style_fn=span_style, flips=(), base=0):
    """Join wrapped lines; flips: join indices (base + i) whose hyphen join is inverted."""
    runs = []
    prev_text = None
    for i, ln in enumerate(lines):
        spans = ln.spans
        if prev_text is not None:
            sep = JOIN.sep(prev_text, ln.text)
            if base + i - 1 in flips and is_hyphen_join(prev_text, ln.text):
                sep = "" if sep == " " else " "
            if runs:
                runs[-1] = (runs[-1][0].rstrip(), runs[-1][1])
            first = span_runs(spans[0], style_fn)
            first[0] = (sep + first[0][0].lstrip(), first[0][1])
            runs += first
            spans = spans[1:]
        for s in spans:
            runs += span_runs(s, style_fn)
        prev_text = ln.text
    return runs


def is_hyphen_join(prev_text, next_text):
    p, n = prev_text.rstrip(), next_text.lstrip()
    return len(p) > 1 and p[-1] == "-" and p[-2].isalnum() and n[:1].isalnum()


def lines_html(lines):
    return runs_to_html(lines_runs(lines))


def plain(lines):
    return re.sub(r"\s+", " ", " ".join(l.text for l in lines)).strip()


def would_fit(prev: Line, nxt: Line, right):
    """True if nxt's first word would have fitted at the end of prev (so the break was not a wrap)."""
    w = nxt.first_word_width()
    if w <= 0:
        return False
    return prev.x1 + prev.space_width() + w <= right


# ------------------------------------------------------------------ the parser
class PrimaParser:
    def __init__(self, path, log=None):
        self.doc = pymupdf.open(path)
        self.log = log or (lambda *a: print(*a, file=sys.stderr))
        self.pages = {}
        self.recase = Recaser()
        self.warnings = []
        self.check_tables = True
        self.dex_names = {}
        names = self.doc.resolve_names()
        self.ch_pages = sorted(v["page"] + 1 for k, v in names.items() if re.fullmatch(r"ch-\d+", k))
        self.outline = defaultdict(list)    # page -> [(level, title, y)]
        for lvl, title, pno, dest in self.doc.get_toc(simple=False):
            y = dest.get("to").y if dest.get("to") is not None else 0
            self.outline[pno].append([lvl, title, y, False])

    def warn(self, msg):
        self.warnings.append(msg)

    def page(self, pno):
        if pno not in self.pages:
            self.pages[pno] = PageData(self.doc[pno - 1])
        return self.pages[pno]

    # -------------------------------------------------------------- vocabulary pass
    def build_vocab(self):
        for pno in range(1, self.doc.page_count + 1):
            pd = self.page(pno)
            for s in pd.body_spans():
                if s.atk:
                    self.recase.feed(s.text)
                    JOIN.feed(s.text)
        for lst in self.outline.values():
            for lvl, title, y, _ in lst:
                self.recase.feed(title, 3)
        for k, v in {"evs": "EVs", "pokémon": "Pokémon", "pokédex": "Pokédex", "ng+": "NG+", "hp": "HP",
                     "tm": "TM", "hm": "HM", "bp": "BP", "pp": "PP", "ivs": "IVs", "ev": "EV", "iv": "IV"}.items():
            self.recase.fixed[k] = v

    def outline_text(self, pno, visible, y, levels=(1, 2, 3)):
        """Original-case heading text from the outline entry on pno that matches the visible text."""
        vis = re.sub(r"\s+", " ", visible).strip().upper()
        cands = [e for e in self.outline.get(pno, []) if e[0] in levels and not e[3]]
        exact = [e for e in cands if re.sub(r"\s+", " ", e[1]).strip().upper() == vis]
        pool = exact or cands
        if not pool:
            return None
        e = min(pool, key=lambda e: abs(e[2] - y))
        if not exact and abs(e[2] - y) > 30:
            return None
        e[3] = True
        if not exact:
            self.warn(f"p{pno}: outline text {e[1]!r} does not match visible {visible!r}")
        return e[1]

    # -------------------------------------------------------------- containers on a page
    def find_containers(self, pd: PageData, spans):
        boxes = pd.boxes
        cons = []

        # stop headers
        for s in spans:
            if s.is_("bUI", 16, WHITE):
                bar = min((b for b in boxes if b.fill == NAVY and b.simple and b.w > 300 and b.has_pt(s.cx, s.cy)),
                          key=lambda b: b.w * b.h, default=None)
                if bar and not any(c["rect"].same(bar) for c in cons):
                    cons.append({"kind": "stop", "rect": bar})

        # callouts (header text Barlow 9.8 italic)
        for s in spans:
            if s.kind == "bUI" and near(s.size, 9.8, 0.06):
                cands = [b for b in boxes if b.simple and b.w > 100 and b.has(s, 0.5) and s.y0 - b.y0 < 14
                         and b.fill in (0xFDF8E8, 0xF3EFFA, 0xFDF0F0, 0xEEF8F3, 0xFFFFFF, 0xF5F7FC)]
                box = max(cands, key=lambda b: b.h, default=None)
                if box is None:
                    self.warn(f"p{pd.pno}: callout header {s.text!r} without a box")
                    continue
                if any(c["rect"].same(box) for c in cons):
                    continue
                ring = next((b for b in boxes if b.n >= 12 and b.same(box, 1.0) and not b.simple), None)
                cons.append({"kind": "callout", "rect": box, "ring": ring.fill if ring else None, "head": s})

        # boss / team cards
        for s in spans:
            if s.is_("bUI", 13, WHITE):
                bar = next((b for b in boxes if b.simple and b.fill in (CRIMSON, VIOLET) and b.has(s, 0.5)), None)
                if bar is None:
                    continue
                ring = min((b for b in boxes if b.fill == NAVY and b.n >= 12 and b.has(bar, 2.0)),
                           key=lambda b: b.w * b.h, default=None)
                if ring and not any(c["rect"].same(ring) for c in cons):
                    cons.append({"kind": "card", "rect": ring, "bar": bar})
        # catch boxes
        for s in spans:
            if s.kind == "psB" and s.text.strip().upper() == "CATCH HERE":
                ring = min((b for b in boxes if b.fill == NAVY and b.n >= 12 and b.has(s, 0.5) and s.y0 - b.y0 < 16),
                           key=lambda b: b.w * b.h, default=None)
                if ring and not any(c["rect"].same(ring) for c in cons):
                    cons.append({"kind": "catch", "rect": ring})
        # card / catch continued from the previous page: navy ring starting at the content top
        for b in boxes:
            if b.fill == NAVY and b.n >= 12 and not b.simple and b.w > 150 and b.h > 30 and abs(b.y0 - CT) < 1.0:
                if any(c["rect"].same(b) for c in cons) or any(c["kind"] == "callout" and c["rect"].same(b) for c in cons):
                    continue
                cells = [q for q in boxes if q.simple and q.fill in (0xFFFFFF, 0xF9FAFD) and b.has(q, 1)
                         and abs(q.w - (b.w - 3.8) / 2) < 3]
                cons.append({"kind": "catch" if cells else "card", "rect": b, "cont": True})

        # tables: header rows of navy th cells holding gold Barlow 8.4 text
        th_cells = []
        for s in spans:
            if s.is_("bU", 8.4, GOLD):
                c = next((b for b in boxes if b.simple and b.fill == NAVY and b.has(s, 0.6)), None)
                if c and not any(c is t for t in th_cells):
                    th_cells.append(c)
        rows = defaultdict(list)
        for c in th_cells:
            rows[(round(c.y0, 1), round(c.y1, 1))].append(c)
        for (y0, y1), cells in rows.items():
            # include empty th cells (check column) in the same header row
            extra = [b for b in boxes if b.simple and b.fill == NAVY and near(b.y0, y0, 0.3) and near(b.y1, y1, 0.3)
                     and not any(b is c for c in cells) and b.w < 600]
            cells = sorted(cells + extra, key=lambda b: b.x0)
            # contiguous run(s)
            groups = [[cells[0]]]
            for c in cells[1:]:
                if abs(c.x0 - groups[-1][-1].x1) < 1.5:
                    groups[-1].append(c)
                else:
                    groups.append([c])
            for g in groups:
                if not any(s.is_("bU", 8.4, GOLD) and any(c.has(s, 0.6) for c in g) for s in spans):
                    continue
                cons.append(self.table_geometry(pd, g))

        # p.strategy (tinted paragraph with a coloured left border)
        for b in boxes:
            if b.simple and b.fill in (0xF3EFFA, 0xFDF0F0) and b.w > 200:
                if any(c["rect"].same(b) for c in cons if c["kind"] == "callout"):
                    continue
                # (a boss card's meta grid is #F3EFFA too, but has no coloured ring of its own)
                ring = next((q for q in boxes if not q.simple and q.fill in (VIOLET, CRIMSON) and q.same(b, 1.0)), None)
                if ring is None:
                    continue
                cons.append({"kind": "strategy", "rect": b})
        for c in cons:
            r = c["rect"]
            c["rect"] = Rect(r.x0, r.y0, r.x1, r.y1)
        return cons

    def table_geometry(self, pd, cells):
        x0, x1 = cells[0].x0, cells[-1].x1
        hy0, hy1 = min(c.y0 for c in cells), max(c.y1 for c in cells)
        # rows: chain of vertical cell borders at the table's left edge
        verts = sorted([s for s in pd.segs if abs(s.x0 - s.x1) < 0.2 and abs(s.x0 - x0) < 1.2 and s.y1 - s.y0 > 3
                        and s.color != NAVY], key=lambda s: s.y0)
        rows = []
        cur = hy1
        for s in verts:
            top, bot = s.y0 + 0.375, s.y1 - 0.375
            if abs(top - cur) < 1.2:
                rows.append((top, bot))
                cur = bot
        cols = [(c.x0, c.x1) for c in cells]
        y1 = rows[-1][1] if rows else hy1
        return {"kind": "table", "rect": Rect(x0 - 0.5, hy0, x1 + 0.5, y1), "cols": cols, "head": (hy0, hy1), "rows": rows}

    # -------------------------------------------------------------- generic region parse
    def parse_region(self, pd, region: Rect, spans, cons, base_x, right):
        """Blocks inside region: outermost containers + flow text. Returns [(y0, block)]."""
        inside = [c for c in cons if region.has(c["rect"], 1.5) and not self._same_rect(c["rect"], region)]
        top = [c for c in inside if not any(o is not c and o["kind"] in ("card", "catch", "callout")
                                            and o["rect"].has(c["rect"], 1.5) and not self._same_rect(o["rect"], c["rect"])
                                            for o in inside)]
        items = []
        claimed = set()
        for c in top:
            r = c["rect"]
            cs = [s for s in spans if r.has_pt(s.cx, s.cy, 0.8)]
            claimed.update(id(s) for s in cs)
            blk = self.parse_container(pd, c, cs, cons)
            if blk is not None:
                items.append((r.y0, blk))
        rest = [s for s in spans if id(s) not in claimed and region.has_pt(s.cx, s.cy, 0.8)]
        items += self.parse_flow(pd, region, rest, base_x, right)
        items.sort(key=lambda t: t[0])
        return items

    @staticmethod
    def _same_rect(a, b, tol=1.0):
        return all(abs(p - q) <= tol for p, q in ((a.x0, b.x0), (a.y0, b.y0), (a.x1, b.x1), (a.y1, b.y1)))

    def parse_container(self, pd, c, spans, cons):
        k = c["kind"]
        if k == "stop":
            return self.parse_stop(pd, c, spans)
        if k == "callout":
            return self.parse_callout(pd, c, spans, cons)
        if k == "card":
            return self.parse_card(pd, c, spans, cons)
        if k == "catch":
            return self.parse_catch(pd, c, spans, cons)
        if k == "table":
            return self.parse_table(pd, c, spans)
        if k == "strategy":
            r = c["rect"]
            lines = make_lines(spans)
            if not lines:
                return None
            blk = {"type": "p", "html": lines_html(lines), "cls": ""}
            self._flow_meta(blk, lines, r.x1 - 8)
            blk["_y1"] = r.y1
            return blk
        return None

    # -------------------------------------------------------------- flow text
    def markers(self, pd, region):
        steps, bullets, checks = [], [], []
        for b in pd.boxes:
            if not b.simple or not region.has(b, 1):
                continue
            if b.fill == NAVY and near(b.w, 15, 0.6) and near(b.h, 13, 0.6):
                steps.append(b)
            elif b.fill == GOLD and near(b.w, 5.5, 0.6) and near(b.h, 5.5, 0.6):
                bullets.append(b)
            elif is_cbox(b):
                checks.append(b)
        return steps, bullets, checks

    def _flow_meta(self, blk, lines, right):
        blk["_lines_first"] = lines[0]
        blk["_lines_last"] = lines[-1]
        blk["_right"] = right
        blk["_y1"] = lines[-1].y1

    def parse_flow(self, pd, region, spans, base_x, right):
        steps, bullets, checks = self.markers(pd, region)
        step_txt = [s for s in spans if s.is_("ps", 6, GOLD) and any(b.has(s, 0.8) for b in steps)]
        sid = {id(s) for s in step_txt}
        # pills inside area bars ("AREA") are decoration
        spans = [s for s in spans if id(s) not in sid and not (s.kind == "psB" and near(s.size, 5.5) and s.color == GOLD)]
        lines = make_lines(spans)
        out = []
        cur = None

        def marker_for(ln):
            for b in steps:
                if b.x1 <= ln.x0 + 1 and ln.x0 - b.x1 < 12 and ln.y0 - 3 <= b.cy <= ln.y1 + 1:
                    t = "".join(s.text for s in step_txt if b.has(s, 0.8)).strip()
                    return "ol", int(t) if t.isdigit() else None
            for b in bullets:
                if b.x1 <= ln.x0 + 1 and ln.x0 - b.x1 < 12 and ln.y0 - 2 <= b.cy <= ln.y1:
                    return "ul", None
            for b in checks:
                if b.x1 <= ln.x0 + 1 and ln.x0 - b.x1 < 9 and ln.y0 - 3 <= b.cy <= ln.y1 + 1:
                    return "checklist", None
            return None, None

        def pitch(sz):
            return sz * 1.42

        for ln in lines:
            f = ln.first
            gap = (ln.oy - cur["_last"].oy) if cur else 999
            if f.is_("bUI", 12.5, VIOLET):
                if cur and cur["type"] == "h3" and gap < 15.5:
                    cur["_lines"].append(ln)
                else:
                    cur = {"type": "h3", "_lines": [ln], "_y0": ln.y0}
                    out.append(cur)
                cur["_last"] = ln
                continue
            if f.is_("bB", 10.5, NAVY):
                if cur and cur["type"] == "label" and gap < 16:
                    cur["_lines"].append(ln)
                else:
                    cur = {"type": "label", "_lines": [ln], "_y0": ln.y0}
                    out.append(cur)
                cur["_last"] = ln
                continue
            if f.is_("bUI", 12, NAVY):
                if cur and cur["type"] == "area" and gap < 18:
                    cur["_lines"].append(ln)
                else:
                    cur = {"type": "area", "_lines": [ln], "_y0": ln.y0}
                    out.append(cur)
                cur["_last"] = ln
                continue
            if not f.atk and not any(s.atk for s in ln.spans):
                self.warn(f"p{pd.pno}: unclassified line {ln.text[:60]!r} ({f.kind} {f.size} {f.color:06x})")
            mk, num = marker_for(ln)
            if mk:
                item = {"lines": [ln], "num": num}
                if cur and cur["type"] == mk:
                    prev_last = cur["_last"]
                    extra = (ln.oy - prev_last.oy) - pitch(prev_last.size)
                    limit = {"ol": 6.3, "ul": 4.8, "checklist": 5.2}[mk]
                    restart = mk == "ol" and num is not None and cur["items"][-1].get("num") is not None \
                        and num != cur["items"][-1]["num"] + 1
                    if extra < limit and not restart:
                        cur["items"].append(item)
                        cur["_last"] = ln
                        continue
                cur = {"type": mk, "items": [item], "_y0": ln.y0, "_text_x": ln.x0, "_last": ln}
                out.append(cur)
                continue
            # continuation of a list item
            if cur and cur["type"] in ("ol", "ul", "checklist") and abs(ln.x0 - cur["_text_x"]) < 1.5 \
                    and gap < pitch(ln.size) + 1.5:
                cur["items"][-1]["lines"].append(ln)
                cur["_last"] = ln
                continue
            # page-top continuation of a list item split from the previous page
            if cur is None and ln.x0 > base_x + 5 and not out:
                cur = {"type": "_licont", "_lines": [ln], "_y0": ln.y0, "_text_x": ln.x0, "_last": ln}
                out.append(cur)
                continue
            if cur and cur["type"] == "_licont" and abs(ln.x0 - cur["_text_x"]) < 1.5 and gap < pitch(ln.size) + 1.5:
                cur["_lines"].append(ln)
                cur["_last"] = ln
                continue
            # paragraph
            if cur and cur["type"] == "p" and abs(ln.x0 - cur["_x0"]) < 1.5 and gap < pitch(ln.size) + 2.5 \
                    and near(ln.size, cur["_size"], 0.3):
                cur["_lines"].append(ln)
                cur["_last"] = ln
                continue
            cur = {"type": "p", "_lines": [ln], "_y0": ln.y0, "_x0": ln.x0, "_size": ln.size, "_last": ln}
            out.append(cur)

        items = []
        for b in out:
            t = b["type"]
            y0 = b["_y0"]
            if t == "h3":
                vis = plain(b["_lines"])
                txt = self.outline_text(pd.pno, vis, b["_lines"][0].y0)
                if txt is None:
                    self.warn(f"p{pd.pno}: h3 {vis!r} not in outline")
                    txt = self.recase(vis)
                blk = {"type": "h3", "text": txt}
            elif t == "label":
                blk = {"type": "label", "html": f"<strong>{html.escape(self.recase(plain(b['_lines'])), quote=False)}</strong>"}
            elif t == "area":
                blk = {"type": "area", "text": self.recase(plain(b["_lines"]))}
            elif t in ("ol", "ul", "checklist"):
                its = []
                for it in b["items"]:
                    d = {"html": lines_html(it["lines"])}
                    if t == "ol":
                        d["num"] = it["num"]
                    its.append(d)
                blk = {"type": t, "items": its}
                blk["_first_item_lines"] = b["items"][0]["lines"]
                last = b["items"][-1]["lines"]
                self._flow_meta(blk, last, right)
                blk["_lines_first"] = b["items"][0]["lines"][0]
            elif t == "_licont":
                blk = {"type": "_licont", "html": lines_html(b["_lines"])}
                self._flow_meta(blk, b["_lines"], right)
            else:
                ls = b["_lines"]
                blk = {"type": "p", "html": lines_html(ls), "cls": self.p_class(ls)}
                self._flow_meta(blk, ls, right)
            blk.setdefault("_y1", b["_last"].y1)
            items.append((y0, blk))
        return items

    def p_class(self, lines):
        regular = [s for l in lines for s in l.spans if s.kind == "atk" and s.text.strip()]
        ref = regular or [s for l in lines for s in l.spans if s.text.strip()]
        sz = Counter(s.size for s in ref).most_common(1)[0][0]
        col = Counter(s.color for s in ref).most_common(1)[0][0]
        if near(sz, 8.4, 0.05) and col == MUTED:
            return "muted"
        if near(sz, 8.6, 0.05):
            return "small"
        return ""

    # -------------------------------------------------------------- stop header
    def parse_stop(self, pd, c, spans):
        r = c["rect"]
        h2 = sorted([s for s in spans if s.is_("bUI", 16, WHITE)], key=lambda s: (s.oy, s.x0))
        vis = plain(make_lines(h2))
        cap_val = "".join(s.text for s in spans if s.is_("ps", 7, NAVY)).strip()
        tags = [s.text.strip() for s in sorted(spans, key=lambda s: s.x0) if s.is_("ps", 5, WHITE)]
        title = self.outline_text(pd.pno, vis, r.y0)
        if title is None:
            self.warn(f"p{pd.pno}: stop {vis!r} not in outline")
            title = self.recase(vis)
        tags = ["Difficult data" if t.upper().startswith("DIFFICULT") else "Expert" for t in tags]
        return {"type": "stop", "title": join_stop_title(title, cap_val or None, tags)}

    # -------------------------------------------------------------- callout
    def parse_callout(self, pd, c, spans, cons):
        r = c["rect"]
        head = c["head"]
        hl = [s for s in spans if s.kind == "bUI" and near(s.size, 9.8, 0.06)]
        label = self.recase(plain(make_lines(hl)))
        ring = c.get("ring")
        bg = next((b.fill for b in pd.boxes if b.simple and b.same(Box(r.x0, r.y0, r.x1, r.y1, 0, 1, True), 0.8)), None)
        kind = {0xD49B1A: "gold", VIOLET: "violet", EMERALD: "emerald", SLATE: "note", 0xB07F0E: "unverified"}.get(ring)
        if ring == CRIMSON:
            kind = "warning" if bg == WHITE else "crimson"
        elif ring == NAVY:
            kind = "tip"
        if kind is None:
            kind = {GOLD: "tip", 0x7A5A08: "unverified", NAVY: "gold"}.get(head.color, "note")
            self.warn(f"p{pd.pno}: callout {label!r} kind guessed as {kind}")
        body_spans = [s for s in spans if s not in hl]
        head_y1 = max(s.y1 for s in hl) + 3
        region = Rect(r.x0, head_y1, r.x1, r.y1)
        items = self.parse_region(pd, region, body_spans, cons, r.x0 + 11.2, r.x1 - 11.2)
        blocks = [b for _, b in items]
        if any(b.get("type") == "_licont" for b in blocks):
            blocks = [dict(b, type="p", cls="") if b.get("type") == "_licont" else b for b in blocks]
        return {"type": "callout", "kind": kind, "label": label, "blocks": blocks, "_y1": r.y1}

    # -------------------------------------------------------------- tables
    def parse_table(self, pd, c, spans):
        hy0, hy1 = c["head"]
        cols = c["cols"]
        headers = []
        for x0, x1 in cols:
            hs = [s for s in spans if s.is_("bU", 8.4, GOLD) and x0 - 0.5 <= s.cx <= x1 + 0.5 and hy0 - 1 <= s.cy <= hy1 + 1]
            headers.append(self.recase(plain(make_lines(hs))))
        dex = any(h.lower().startswith("pok") for h in headers) and any(h.lower() == "method" for h in headers)
        pad = 5 if dex else 6
        checks = [b for b in pd.boxes if is_cbox(b) and c["rect"].has(b, 1)]
        rows = []
        for top, bot in c["rows"]:
            cells = []
            src = []
            row_check = False
            for ci, (x0, x1) in enumerate(cols):
                cs = [s for s in spans if x0 <= s.cx <= x1 and top <= s.cy <= bot and not s.is_("bU", 8.4, GOLD)]
                lines = make_lines(cs)
                cells.append(self.cell_html(lines, x1 - 0.375 - pad))
                src.append((lines, x1 - 0.375 - pad))
                if any(x0 <= b.cx <= x1 and top <= b.cy <= bot for b in checks):
                    if ci == 0:
                        row_check = True
            rows.append({"cells": cells, "check": row_check, "_src": src})
        check = bool(headers) and not headers[0].strip() and any(r["check"] for r in rows)
        if not check:
            for r in rows:
                r["check"] = False
        blk = {"type": "table", "headers": headers, "rows": rows, "check": check}
        blk["_y1"] = c["rect"].y1
        x0 = cols[0][0]
        blk["_cols"] = [(a - x0, b - x0) for a, b in cols]
        blk["_page"] = pd.pno
        return blk

    def cell_html(self, lines, right, flips=frozenset(), sub=True, breaks=frozenset()):
        """Table cell: wrapped lines joined, <br> where the next word would have fitted (or the join
        index is in breaks), .sub (7.4pt) runs wrapped in <span class="sub">."""
        if not lines:
            return ""

        def style(s):
            st = span_style(s)
            return (st, "sub") if sub and s.atk and near(s.size, 7.4, 0.03) else st

        def is_sub(s):
            return sub and s.atk and near(s.size, 7.4, 0.03)

        groups = [[0, [lines[0]]]]
        for i, (prev, ln) in enumerate(zip(lines, lines[1:]), 1):
            # a .sub note that starts a line follows a <br> (parse_pdf's cell layout); that also
            # keeps the cell's max-content width, which the table's auto layout depends on
            if i - 1 in breaks or would_fit(prev, ln, right) or (is_sub(ln.first) and not is_sub(prev.spans[-1])):
                groups.append([i, [ln]])
            else:
                groups[-1][1].append(ln)
        return "<br>".join(runs_to_html(lines_runs(g, style, flips, base)) for base, g in groups)

    @staticmethod
    def wrap_cands(lines, right):
        """Join indices of ordinary wraps (an explicit <br> there would look the same)."""
        return [i for i, (a, b) in enumerate(zip(lines, lines[1:])) if not would_fit(a, b, right)]

    @staticmethod
    def hyphen_cands(lines, right):
        """Join indices where a wrapped line ends in a hyphen: 'Heavy-Duty' or parse_pdf's 'Heavy- Duty'."""
        return [i for i, (a, b) in enumerate(zip(lines, lines[1:]))
                if not would_fit(a, b, right) and is_hyphen_join(a.text, b.text)]

    # -------------------------------------------------------------- sprites / mon heads
    def badges(self, pd, rect):
        return sorted([b for b in pd.boxes if b.fill == NAVY and not b.simple and near(b.w, 46, 0.8) and near(b.h, 46, 0.8)
                       and rect.has(b, 1.5)], key=lambda b: (round(b.y0), b.x0))

    def badge_info(self, badge, spans):
        inside = [s for s in spans if badge.has(s, 0.8)]
        dex = None
        name = ""
        for s in inside:
            if s.is_("ps", 5.6, WHITE):
                m = re.search(r"\d+", s.text)
                dex = int(m.group(0)) if m else None
            elif s.is_("bU", 6.6):
                name += s.text
        return dex, name.strip(), inside

    def mon_name(self, upper_name, dex):
        upper_name = re.sub(r"\s+", " ", upper_name).strip()
        if not upper_name:
            return ""
        if dex and dex in self.dex_names and self.dex_names[dex].upper() == upper_name.upper():
            return self.dex_names[dex]
        return self.recase(upper_name)

    def types_from(self, spans):
        out = []
        for s in sorted(spans, key=lambda s: (round(s.oy), s.x0)):
            t = s.text.strip().capitalize()
            if t:
                out.append(t)
        return out

    # -------------------------------------------------------------- boss / team cards
    def parse_card(self, pd, c, spans, cons):
        R = c["rect"]
        bar = c.get("bar")
        card = {"type": "card", "variant": "boss", "badge": "", "title": "", "meta": [], "mons": [],
                "lead_fields": [], "extra": []}
        y = R.y0
        if bar:
            card["variant"] = "team" if bar.fill == VIOLET else "boss"
            bl = [s for s in spans if s.kind == "psB" and s.color == GOLD and bar.has(s, 0.8)]
            tl = [s for s in spans if s.is_("bUI", 13, WHITE)]
            card["badge"] = self.recase(plain(make_lines(bl)))
            card["title"] = self.recase(plain(make_lines(tl)))
            y = bar.y1
        else:
            card["_cont"] = True
        inner = Rect(R.x0 + 1.5, y, R.x1 - 1.5, R.y1)
        # meta grid
        meta = next((b for b in pd.boxes if b.simple and b.fill == 0xF3EFFA and abs(b.y0 - y) < 1.2
                     and abs(b.x0 - inner.x0) < 1.2 and abs(b.x1 - inner.x1) < 1.2), None)
        if meta:
            bag = next((b for b in pd.boxes if b.simple and b.fill == 0xFDF8E8 and meta.has(b, 1)), None)
            card["meta"] = self.parse_meta(meta, bag, spans)
            y = meta.y1
        rows = sorted([b for b in pd.boxes if b.simple and b.fill in (0xFFFFFF, 0xF8F9FD) and abs(b.x0 - inner.x0) < 1.2
                       and abs(b.x1 - inner.x1) < 1.2 and b.y0 >= y - 1 and R.has(b, 1.5) and b.h > 30], key=lambda b: b.y0)
        badges = self.badges(pd, R)
        rows = [r for r in rows if any(r.has(bd, 1) for bd in badges)]
        # lead fields: card-lead divs between the meta grid and the first mon row
        first_row_y = rows[0].y0 if rows else R.y1
        leads = sorted([b for b in pd.boxes if b.simple and b.fill == 0xF5F7FC and abs(b.x0 - inner.x0) < 1.2
                        and abs(b.x1 - inner.x1) < 1.2 and b.y0 >= y - 1 and b.y1 <= first_row_y + 1], key=lambda b: b.y0)
        extra_box = None
        if not rows and leads:
            # a card fragment holding only the extra section
            extra_box = leads[-1] if self._has_extra_rule(pd, leads[-1]) else None
            if extra_box:
                leads = [b for b in leads if b is not extra_box]
        for lb in leads:
            ls = make_lines([s for s in spans if lb.has_pt(s.cx, s.cy, 0.5)])
            if ls:
                card["lead_fields"].append(self.label_value(ls, lb.x1 - 10))
        for rb in rows:
            rs = [s for s in spans if rb.has_pt(s.cx, s.cy, 0.5)]
            card["mons"].append(self.parse_mon_row(pd, rb, rs, card["variant"] == "team"))
        # extra
        ex_y0 = rows[-1].y1 if rows else (leads[-1].y1 if leads else y)
        if extra_box is not None:
            ex_y0 = extra_box.y0
        ex_spans = [s for s in spans if s.cy > ex_y0 + 0.5]
        if ex_spans:
            region = Rect(R.x0, ex_y0, R.x1, R.y1)
            items = self.parse_region(pd, region, ex_spans, cons, R.x0 + 11.5, R.x1 - 11.5)
            card["extra"] = [b for _, b in items]
        card["_open"] = R.y1 > CB - 1.0
        card["_y1"] = R.y1
        return card

    def _has_extra_rule(self, pd, box):
        return any(b.fill == NAVY and abs(b.y0 - box.y0) < 1.2 and b.w > 200 for b in pd.boxes)

    def parse_meta(self, meta, bag, spans):
        labels = sorted([s for s in spans if s.is_("bU", 7.2, VIOLET) and meta.has(s, 0.8)], key=lambda s: (round(s.oy), s.x0))
        out = []
        bands = defaultdict(list)
        for s in labels:
            bands[round(s.oy)].append(s)
        keys = sorted(bands)
        colw = (meta.x1 - meta.x0) / 4
        vals = [s for s in spans if meta.has(s, 0.8) and not s.is_("bU", 7.2, VIOLET)]
        for bi, k in enumerate(keys):
            labs = sorted(bands[k], key=lambda s: s.x0)
            y_lo = labs[0].y1 - 0.5
            y_hi = keys[bi + 1] - 8 if bi + 1 < len(keys) else meta.y1
            in_bag = bag is not None and bag.has_pt(labs[0].cx, labs[0].cy)
            for lab in labs:
                if in_bag:
                    x_lo, x_hi = meta.x0, meta.x1
                else:
                    col = int((lab.x0 - meta.x0) // colw)
                    x_lo, x_hi = meta.x0 + col * colw, meta.x0 + (col + 1) * colw
                vs = [s for s in vals if x_lo <= s.cx < x_hi and y_lo <= s.cy < y_hi]
                v = self.cell_html(make_lines(vs), x_hi - 8.4, sub=False) if vs else ""
                if v in ("–", "&ndash;"):
                    v = ""
                out.append([self.recase(lab.text.strip()), v])
        return out

    def label_value(self, lines, right):
        """'<fld-k>Label</> value' lines -> [label, html]."""
        first = lines[0]
        if first.first.kind in ("bU", "bB") and first.first.color == VIOLET:
            lab = first.first.text.strip()
            rest = [Line(first.spans[1:])] if len(first.spans) > 1 else []
            rest += lines[1:]
            rest = [l for l in rest if l.spans]
            return [self.recase(lab), lines_html(rest) if rest else ""]
        return ["", lines_html(lines)]

    def parse_mon_row(self, pd, rb, spans, team):
        badge = next((b for b in self.badges(pd, Rect(rb.x0, rb.y0, rb.x1, rb.y1))), None)
        dex_b, bname, bspans = self.badge_info(badge, spans) if badge else (None, "", [])
        bid = {id(s) for s in bspans}
        main = [s for s in spans if id(s) not in bid]
        mon = {"name": "", "label": "", "dex": None, "types": [], "avail": None, "fields": [], "moves": [],
               "level": None, "check": False}
        lv = [s for s in main if s.is_("ps", 6.5, GOLD)]
        if lv:
            mon["level"] = re.sub(r"^\s*Lv\.?\s*", "", "".join(s.text for s in lv)).strip()
        names = [s for s in main if s.is_("bU", 12.5, NAVY)]
        name_up = plain(make_lines(names))
        dexs = [s for s in main if s.is_("ps", 5.5, VIOLET)]
        dtxt = re.sub(r"\D", "", "".join(s.text for s in dexs))
        mon["dex"] = int(dtxt) if dtxt else None
        if mon["dex"] is None and dex_b is not None:
            self.warn(f"p{pd.pno}: mon {name_up!r} has badge dex {dex_b} but no printed dex")
        head_y1 = max((s.y1 for s in names + dexs), default=rb.y0 + 20)
        types = [s for s in main if s.is_("bU", 7.4) and s.color in (WHITE, NAVY)]
        mon["types"] = self.types_from(types)
        mon["name"] = self.mon_name(name_up, mon["dex"])
        mon["label"] = mon["name"] or self.recase(bname)
        # move pills
        pills = sorted([b for b in pd.boxes if b.simple and b.fill in (0xF7F5FC, 0xFFF7F7) and rb.has(b, 1)],
                       key=lambda b: (round(b.y0), b.x0))
        used = {id(s) for s in lv + names + dexs + types}
        for p in pills:
            ps = [s for s in main if p.has(s, 1.0) and id(s) not in used]
            used.update(id(s) for s in ps)
            mv = "".join(s.text for s in sorted(ps, key=lambda s: s.x0)).strip()
            if mv:
                mon["moves"].append(mv)
        rest = [s for s in main if id(s) not in used]
        lines = make_lines(rest)
        pill_y0 = pills[0].y0 if pills else None
        pill_y1 = pills[-1].y1 if pills else None
        main_x = (badge.x1 + 8.7) if badge else rb.x0 + 62
        right = rb.x1 - 8.75   # mon-row padding + the td default 1px padding
        if pills:
            short = [l for l in lines if l.oy < pill_y0]
            long_ = [l for l in lines if l.oy > pill_y1]
        else:
            short, long_ = [], []
            for l in lines:
                f = l.first
                is_lab = f.kind == "bU" and f.color == VIOLET
                if not long_ and (is_lab and f.text.strip().upper() in ("NATURE", "EVS", "ABILITY", "ITEM")
                                  or (short and not is_lab and not would_fit(short[-1], l, right))):
                    short.append(l)
                else:
                    long_.append(l)
        mon["fields"] = self.short_fields(short) + self.long_fields(long_, right, main_x)
        return mon

    def _field_tokens(self, lines):
        """Flatten field lines into [('lab', text) | ('val', Line-fragment)] keeping line structure for joins."""
        toks = []
        for ln in lines:
            cur = []
            for s in ln.spans:
                if s.kind in ("bU", "bB") and s.color == VIOLET and s.text.strip():
                    if cur:
                        toks.append(("val", Line(cur)))
                        cur = []
                    toks.append(("lab", s.text.strip()))
                else:
                    cur.append(s)
            if cur:
                toks.append(("val", Line(cur)))
            toks.append(("nl", None))
        return toks

    def short_fields(self, lines):
        fields = []
        cur_lab, cur_lines = None, []
        for kind, v in self._field_tokens(lines):
            if kind == "lab":
                if cur_lab is not None:
                    fields.append([cur_lab, lines_html(cur_lines) if cur_lines else ""])
                cur_lab, cur_lines = self.field_label(v, True), []
            elif kind == "val":
                cur_lines.append(v)
        if cur_lab is not None:
            fields.append([cur_lab, lines_html(cur_lines) if cur_lines else ""])
        return fields

    def field_label(self, up, short):
        m = {"NATURE": "Nature", "EVS": "EVs", "ABILITY": "Ability", "ITEM": "Item", "NOTE": "Note", "ROLE": "Role",
             "MOVE SOURCES": "Move sources", "MOVE SOURCE": "Move source"}
        lab = m.get(up.upper()) or self.recase(up)
        if not short and lab in SHORT_FIELDS:
            lab = lab.lower()
        return lab

    def long_fields(self, lines, right, main_x):
        fields = []
        prev = None
        for ln in lines:
            f = ln.first
            is_lab = f.kind in ("bU", "bB") and f.color == VIOLET and abs(ln.x0 - main_x) < 3
            if is_lab:
                lab = self.field_label(f.text.strip(), False)
                rest = Line(ln.spans[1:]) if len(ln.spans) > 1 else None
                fields.append([lab, [rest] if rest and rest.spans else []])
            elif fields and prev is not None and not would_fit(prev, ln, right) \
                    and (ln.oy - prev.oy) < prev.size * 1.35 + 0.9:
                fields[-1][1].append(ln)
            else:
                fields.append(["", [ln]])
            prev = ln
        return [[l, lines_html(ls) if ls else ""] for l, ls in fields]

    # -------------------------------------------------------------- catch boxes
    def parse_catch(self, pd, c, spans, cons):
        R = c["rect"]
        blk = {"type": "catch", "title": "", "lead_fields": [], "mons": [], "extra": []}
        cells = sorted([b for b in pd.boxes if b.simple and b.fill in (0xFFFFFF, 0xF9FAFD) and R.has(b, 1.5)
                        and abs(b.w - (R.x1 - R.x0 - 3.8) / 2) < 3 and b.h > 20], key=lambda b: (round(b.y0), b.x0))
        # (the hatched filler cell of an odd-sized grid has no plain fill, so it is not listed)
        head_y1 = R.y0
        if c.get("cont"):
            blk["_cont"] = True
        else:
            tl = [s for s in spans if s.is_("bUI", 12.5, NAVY)]
            blk["title"] = self.recase(plain(make_lines(tl)))
            cnt = [s for s in spans if s.kind == "psB" and s.color == NAVY]
            blk["_count"] = plain(make_lines(cnt))
            pill = next((b for b in pd.boxes if b.simple and b.fill == NAVY and R.has(b) and b.y0 - R.y0 < 12 and b.w < 120), None)
            head_y1 = (pill.y1 + 4) if pill else R.y0 + 25
        grid_y0 = cells[0].y0 if cells else R.y1
        if not c.get("cont"):
            lead = [s for s in spans if head_y1 < s.cy < grid_y0 and s.atk]
            for ln_group in self.split_paras(make_lines(lead)):
                lv = self.lead_field(ln_group)
                if lv:
                    blk["lead_fields"].append(lv)
        rows = defaultdict(list)
        for b in cells:
            rows[round(b.y0)].append(b)
        grid_y1 = grid_y0
        for ky in sorted(rows):
            rcells = sorted(rows[ky], key=lambda b: b.x0)
            grid_y1 = max(grid_y1, max(b.y1 for b in rcells))
            for cb in rcells:
                cs = [s for s in spans if cb.has_pt(s.cx, s.cy, 0.3)]
                if not self.badges(pd, Rect(cb.x0, cb.y0, cb.x1, cb.y1)) and not cs:
                    continue
                blk["mons"].append(self.parse_enc_cell(pd, cb, cs))
        ex_spans = [s for s in spans if s.cy > grid_y1 + 0.5]
        if ex_spans:
            region = Rect(R.x0, grid_y1, R.x1, R.y1)
            items = self.parse_region(pd, region, ex_spans, cons, R.x0 + 1.5, R.x1 - 1.5)
            blk["extra"] = [b for _, b in items]
        blk["_open"] = R.y1 > CB - 1.0
        blk["_y1"] = R.y1
        return blk

    def split_paras(self, lines):
        groups = []
        for ln in lines:
            if groups and ln.oy - groups[-1][-1].oy < ln.size * 1.42 + 2:
                groups[-1].append(ln)
            else:
                groups.append([ln])
        return groups

    def lead_field(self, lines):
        h = lines_html(lines)
        m = re.match(r"<strong>(.*?)</strong>\s*(.*)$", h, re.S)
        if m:
            return [html.unescape(m.group(1)), m.group(2)]
        return ["", h]

    def parse_enc_cell(self, pd, cb, spans):
        rect = Rect(cb.x0, cb.y0, cb.x1, cb.y1)
        badge = next(iter(self.badges(pd, rect)), None)
        dex_b, bname, bspans = self.badge_info(badge, spans) if badge else (None, "", [])
        bid = {id(s) for s in bspans}
        main = [s for s in spans if id(s) not in bid]
        mon = {"name": "", "label": "", "dex": None, "types": [], "avail": None, "fields": [], "moves": [],
               "level": None, "check": False}
        mon["check"] = any(is_cbox(b) and rect.has(b, 1) for b in pd.boxes)
        names = [s for s in main if s.is_("bU", 11.5, NAVY)]
        dexs = [s for s in main if s.is_("ps", 5.5, VIOLET)]
        dtxt = re.sub(r"\D", "", "".join(s.text for s in dexs))
        mon["dex"] = int(dtxt) if dtxt else None
        name_up = plain(make_lines(names))
        mon["name"] = self.mon_name(name_up, mon["dex"])
        mon["label"] = mon["name"] or self.recase(bname)
        types = [s for s in main if s.is_("bU", 7.4) and s.color in (WHITE, NAVY)]
        mon["types"] = self.types_from(types)
        av = [s for s in main if s.is_("ps", 5.0)]
        used = {id(s) for s in names + dexs + types + av}
        if av:
            t = plain(make_lines(av))
            pill = next((b for b in pd.boxes if b.simple and b.has(av[0], 1.5) and b.fill in (EMERALD, GOLD, VIOLET)), None)
            kind = {EMERALD: "now", GOLD: "later", VIOLET: "optional"}.get(pill.fill if pill else None)
            if kind is None:
                kind = "now" if t.upper().startswith("NOW") else "later" if t.upper().startswith("LATER") else "optional"
            mon["avail"] = {"kind": kind, "text": self.recase(t)}
        rest = [s for s in main if id(s) not in used]
        lines = make_lines(rest)
        right = rect.x1 - 0.375 - 7 - 0.75   # half border, padding, td default 1px
        method_lines, notes = [], []
        for ln in lines:
            f = ln.first
            is_lab = f.is_("bB", 7.6, VIOLET)
            if is_lab and f.text.strip().upper() == "METHOD":
                method_lines = [Line(ln.spans[1:])] if len(ln.spans) > 1 else []
                continue
            # the method value is 8pt (.avail-row); notes below it are 7.8pt (.mon-meta)
            if method_lines and not notes and not is_lab and near(ln.size, 8.0, 0.05):
                method_lines.append(ln)
                continue
            if is_lab:
                notes.append([self.recase(f.text.strip()), [Line(ln.spans[1:])] if len(ln.spans) > 1 else []])
            elif notes and not would_fit(notes[-1][1][-1] if notes[-1][1] else ln, ln, right) \
                    and notes[-1][1] and (ln.oy - notes[-1][1][-1].oy) < ln.size * 1.3 + 0.5:
                notes[-1][1].append(ln)
            else:
                notes.append(["", [ln]])
        if method_lines:
            mon["fields"].append(["Method", lines_html([l for l in method_lines if l.spans])])
        for lab, ls in notes:
            ls = [l for l in ls if l.spans]
            mon["fields"].append([lab, lines_html(ls) if ls else ""])
        return mon

    # -------------------------------------------------------------- pages
    def parse_body_page(self, pno):
        pd = self.page(pno)
        spans = pd.body_spans()
        cons = self.find_containers(pd, spans)
        region = Rect(CL - 1, CT - 1, CR + 1, CB + 1)
        items = self.parse_region(pd, region, spans, cons, CL, CR)
        return [b for _, b in items]

    def parse_splash(self, pno):
        pd = self.page(pno)
        sp = pd.body_spans()
        ch = {"num": "", "title": "", "eyebrow": "", "progress": None, "stats": [], "toc_heading": "", "blocks": []}
        num = [s for s in sp if s.kind == "ps" and near(s.size, 22.2, 0.3)]
        ch["num"] = "".join(s.text for s in num).strip()
        eb = [s for s in sp if s.is_("ps", 7.5, GOLD)]
        ch["eyebrow"] = self.recase(plain(make_lines(eb)))
        tl = [s for s in sp if s.kind == "bH" and near(s.size, 30, 0.3)]
        vis = plain(make_lines(tl))
        ch["title"] = self.outline_text(pno, vis, min((s.y0 for s in tl), default=0), (1,)) or self.recase(vis)
        hp = [s for s in sp if (s.is_("ps", 6.5, WHITE) or s.kind == "mono") and s.y0 < 220]
        t = "".join(s.text for s in hp).strip()
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", t)
        ch["progress"] = [int(m.group(1)), int(m.group(2))] if m else None
        vals = sorted([s for s in sp if s.is_("ps", 15, NAVY)], key=lambda s: s.x0)
        labs = [s for s in sp if s.is_("bB", 9, VIOLET)]
        for v in vals:
            lab = [s for s in labs if abs(s.x0 - v.x0) < 2 and 0 < s.y0 - v.y0 < 40]
            ch["stats"].append({"value": v.text.strip(), "label": self.recase(plain(make_lines(lab)))})
        heads = sorted([s for s in sp if s.is_("bUI", 13, VIOLET)], key=lambda s: s.y0)
        if heads:
            ch["toc_heading"] = self.recase(heads[0].text.strip())
        # "Key battles" lists boss card titles in their original case (the cards themselves uppercase them)
        ch["_kb"] = []
        for c in self.find_containers(pd, sp):
            if c["kind"] == "table":
                t = self.parse_table(pd, c, [s for s in sp if c["rect"].has_pt(s.cx, s.cy, 0.8)])
                if [h.lower() for h in t["headers"][:2]] == ["boss", "where"]:
                    ch["_kb"] = [html.unescape(re.sub(r"<[^>]+>", "", r["cells"][0])) for r in t["rows"]]
        return ch

    @staticmethod
    def apply_kb_titles(ch):
        kb = ch.get("_kb") or []
        bosses = []

        def walk(blocks):
            for b in blocks:
                if b.get("type") == "card" and b.get("variant") != "team":
                    bosses.append(b)
                for k in ("blocks", "extra"):
                    if isinstance(b.get(k), list):
                        walk(b[k])
        walk(ch["blocks"])
        for b, t in zip(bosses, kb):
            if re.sub(r"\s+", " ", b["title"]).upper() == re.sub(r"\s+", " ", t).upper():
                b["title"] = t

    def parse_cover(self):
        pd = self.page(1)
        sp = pd.spans
        cov = {"eyebrow": [], "title": [], "subtitle": "", "dedication": [], "footer": ""}
        cov["eyebrow"] = [self.recase(plain([l])) for l in make_lines([s for s in sp if s.is_("ps", 6.5, NAVY)])]
        if [e.lower() for e in cov["eyebrow"][:1]] == ["unofficial fan-made guide"]:
            cov["eyebrow"][0] = "Unofficial fan-made guide"
        cov["title"] = [plain([l]) for l in make_lines([s for s in sp if s.kind == "ps" and s.size >= 20])]
        sub = self.recase(plain(make_lines([s for s in sp if s.kind == "bUI" and s.size > 15])))
        cov["subtitle"] = "Story Walkthrough Edition" if sub.lower() == "story walkthrough edition" else sub
        ded1 = plain(make_lines([s for s in sp if s.is_("ps", 7.5, GOLD)]))
        ded2 = plain(make_lines([s for s in sp if s.is_("ps", 7.5, WHITE)]))
        cov["dedication"] = [x for x in (ded1, ded2) if x]
        foot = plain(make_lines([s for s in sp if s.atk and near(s.size, 7.2, 0.1)]))
        for pat in SPRITE_NOTES:
            foot = re.sub(pat, "", foot).strip()
        cov["footer"] = foot
        return cov

    # -------------------------------------------------------------- driver
    def parse(self, progress=None):
        self.build_vocab()
        n = self.doc.page_count
        first_body = self.ch_pages[0]
        # Pokédex names (mixed case) for re-casing mon names: scan dex tables first
        self.dex_names = self.scan_dex_names()
        model = {"meta": {"title": ""}, "cover": self.parse_cover(), "chapters": []}
        model["meta"]["title"] = " ".join(model["cover"]["title"]) or "Pokémon Unbound NG+ Expert"
        chapters = model["chapters"]
        for pno in range(first_body, n + 1):
            if progress:
                progress(pno, n)
            if pno in self.ch_pages:
                chapters.append(self.parse_splash(pno))
                continue
            if not chapters:
                continue
            blocks = self.parse_body_page(pno)
            for i, b in enumerate(blocks):
                b["_page"] = pno
                b["_first"] = i == 0
                b["_lastonpage"] = i == len(blocks) - 1
            stitch_page(chapters[-1]["blocks"], blocks, self)
        for ch in chapters:
            self.apply_kb_titles(ch)
        self.check_catch_counts(model)
        if self.check_tables:
            self.resolve_table_hyphens(model)
        model = strip_private(model)
        return model

    def check_catch_counts(self, model):
        """The catch head prints '<n> species · <k> now', derived from the mons: a free consistency check
        on the grid parse and the page stitching."""
        for blk in iter_blocks(model, "catch"):
            m = re.match(r"(\d+)\s+SPECIES(?:\s*·\s*(\d+)\s+NOW)?", blk.get("_count", ""), re.I)
            if not m:
                continue
            n, k = int(m.group(1)), int(m.group(2) or 0)
            got_k = sum(1 for x in blk["mons"] if (x.get("avail") or {}).get("kind") == "now")
            if (n, k) != (len(blk["mons"]), got_k):
                self.warn(f"p{blk.get('_page')}: catch {blk['title']!r} prints {n} species/{k} now, "
                          f"parsed {len(blk['mons'])}/{got_k}")

    # -------------------------------------------------------------- table auto-layout check
    def resolve_table_hyphens(self, model):
        """A wrapped line ending in '-' may hide 'Heavy-Duty' or parse_pdf's 'Heavy- Duty'. Inside a
        data table the extra space changes the column's max-content width and so the auto layout,
        which moves every line break in the table; WeasyPrint also breaks 'X- Y' and 'X-Y' differently
        inside cells. So every table is rendered alone with WeasyPrint and its column edges and the
        line breaks of every cell are compared with the PDF; where they differ, hyphen joins (and,
        failing that, plain wraps read as <br>) are flipped greedily until they match."""
        for blk in iter_blocks(model, "table"):
            rows = blk["rows"]
            cands, wraps, width = [], [], {}
            for ri, row in enumerate(rows):
                for ci, (lines, right) in enumerate(row.get("_src", [])):
                    width[(ri, ci)] = max_content(lines)
                    hy = self.hyphen_cands(lines, right)
                    cands += [(ri, ci, j, "hyph") for j in hy]
                    wraps += [(ri, ci, j, "br") for j in self.wrap_cands(lines, right) if j not in hy]
            ncol = len(blk["headers"])
            colmax = [max([width.get((ri, ci), 0) for ri in range(len(rows))] or [0]) for ci in range(ncol)]
            # most likely to matter first: cells whose single-line width is closest to their column's widest
            rank = lambda c: colmax[c[1]] - width[(c[0], c[1])]
            rel = sorted(cands, key=rank)[:16] + sorted(wraps, key=rank)[:8]
            target = blk["_cols"]

            touched = set()

            def apply(flips):
                cells = {(c[0], c[1]) for c in flips} | touched
                touched.clear()
                touched.update((c[0], c[1]) for c in flips)
                for ri, ci in cells:
                    lines, right = rows[ri]["_src"][ci]
                    mine = [(j, k) for r, c, j, k in flips if (r, c) == (ri, ci)]
                    rows[ri]["cells"][ci] = self.cell_html(lines, right, frozenset(j for j, k in mine if k == "hyph"),
                                                           breaks=frozenset(j for j, k in mine if k == "br"))

            want = [[cell_lines_text(lines) for lines, _ in row.get("_src", [])] for row in rows]

            def err(flips):
                apply(flips)
                got_cols, got_rows = render_table(self, blk, target[-1][1] + 0.75)
                if got_cols is None or len(got_cols) != len(target) or len(got_rows) != len(want):
                    return (10 ** 6, 1e9), set()
                bad = {(ri, ci) for ri, (g, w) in enumerate(zip(got_rows, want)) for ci, (x, y) in enumerate(zip(g, w))
                       if x != y}
                ce = max(abs(a - c) + abs(b - d) for (a, b), (c, d) in zip(got_cols, target))
                return (len(bad), round(ce, 2) if ce > 0.05 else 0.0), bad

            best = set()
            e, bad = err(best)
            tried = 1
            if e == (0, 0.0):
                continue
            # hyphen readings first; plain wraps read as <br> only if hyphens cannot explain the layout
            for pool in (cands, cands + wraps):
                improved = True
                while e != (0, 0.0) and improved and tried < 40:
                    improved = False
                    # candidates in cells that break differently first, then those that may set column widths
                    order = [c for c in pool if (c[0], c[1]) in bad][:12] + [c for c in rel if c in pool]
                    for c in dict.fromkeys(order):
                        if c in best:
                            continue
                        e2, bad2 = err(best | {c})
                        tried += 1
                        if e2 < e:
                            best, e, bad, improved = best | {c}, e2, bad2, True
                            break
            apply(best)
            nh = sum(1 for c in best if c[3] == "hyph")
            msg = (f"p{blk.get('_page')}: table {blk['headers'][:3]}: read {nh} of {len(cands)} wrapped hyphens "
                   f"as 'X- Y' and {len(best) - nh} wraps as <br> ({tried} trial renders); left: {e[0]} cells "
                   f"breaking differently, column error {e[1]:.2f}pt")
            if bad:
                ri, ci = sorted(bad)[0]
                msg += f" (e.g. row {ri + 1}: {want[ri][ci]})"
            self.log("  " + msg)
            if e != (0, 0.0):
                self.warn(msg)

    def scan_dex_names(self):
        names = {}
        for pno in range(1, self.doc.page_count + 1):
            pd = self.page(pno)
            sp = pd.body_spans()
            if not any(s.is_("bU", 8.4, GOLD) and s.text.strip().upper().startswith("POK") for s in sp):
                continue
            if not any(s.is_("bU", 8.4, GOLD) and s.text.strip().upper() == "METHOD" for s in sp):
                continue
            nums = [s for s in sp if s.is_("atk", 7.5) and re.fullmatch(r"\d{1,4}", s.text.strip())]
            bolds = [s for s in sp if s.is_("atkB", 7.5)]
            for nsp in nums:
                row = sorted([b for b in bolds if abs(b.oy - nsp.oy) < 1 and b.x0 > nsp.x1], key=lambda b: b.x0)
                if row:
                    names.setdefault(int(nsp.text.strip()), row[0].text.strip())
        return names


# ------------------------------------------------------------------ table helpers
HERE = Path(__file__).resolve().parent


def iter_blocks(model, typ):
    def walk(blocks):
        for b in blocks:
            if b.get("type") == typ:
                yield b
            for k in ("blocks", "extra"):
                if isinstance(b.get(k), list):
                    yield from walk(b[k])
    for ch in model["chapters"]:
        yield from walk(ch["blocks"])


def max_content(lines):
    """Rough single-line width of a cell's text (an upper bound when it has <br>s)."""
    if not lines:
        return 0.0
    return sum(l.x1 - l.x0 for l in lines) + sum(l.space_width() for l in lines[1:])


def cell_lines_text(lines):
    return [re.sub(r"\s+", " ", l.text).strip() for l in lines]


def render_table(parser, blk, width):
    """Render blk alone at the given width with the real stylesheet; return (column edges relative to
    the first column, [[line texts of each cell] for each row])."""
    import logging
    from render_html import Renderer, SpriteBook
    from weasyprint import HTML
    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    body = Renderer(SpriteBook(None)).b_table(strip_private(blk))
    css = (HERE / "prima_unbound.css").as_uri()
    nomargin = " ".join(f"@{m} {{ content: none; }}" for m in ("top-left", "top-right", "bottom-center", "bottom-left"))
    doc = (f'<html><head><link rel="stylesheet" href="{css}"><style>@page {{ size: {width + 40:.3f}pt 2400pt; '
           f'margin: 20pt; {nomargin} }} table.data-table {{ margin: 0; }}</style></head><body>'
           f'<div style="width:{width:.3f}pt">{body}</div></body></html>')
    pdf = pymupdf.open(stream=HTML(string=doc, base_url=str(HERE)).write_pdf(), filetype="pdf")
    cols, rows = None, []
    for page in pdf:
        pd = PageData(page)
        for c in parser.find_containers(pd, pd.spans):
            if c["kind"] != "table":
                continue
            if cols is None:
                x0 = c["cols"][0][0]
                cols = [(a - x0, b - x0) for a, b in c["cols"]]
            for top, bot in c["rows"]:
                rows.append([cell_lines_text(make_lines([sp for sp in pd.spans if a <= sp.cx <= b and top <= sp.cy <= bot
                                                         and not sp.is_("bU", 8.4, GOLD)]))
                             for a, b in c["cols"]])
    return cols, rows


# ------------------------------------------------------------------ stop titles
def join_stop_title(title, cap, tags):
    """Inverse of render_html.split_stop_title."""
    t = title
    if cap:
        m = re.fullmatch(r"(\d+)(?:\s*/\s*(\d+))?", cap.strip())
        if m:
            t += f" (cap {m.group(1)}" + (f", then {m.group(2)}" if m.group(2) else "") + ")"
    for tag in tags:
        if tag == "Expert" and re.search(r"\b(for|on|in|of|to|at)$", t):
            t += " Expert"
        else:
            t += f". {tag}."
            t = t.replace(".. ", ". ")
    return t


# ------------------------------------------------------------------ stitching across pages
def ends_full(prev_blk, nxt_first_line):
    last = prev_blk.get("_lines_last")
    right = prev_blk.get("_right")
    if last is None or nxt_first_line is None or right is None:
        return False
    return not would_fit(last, nxt_first_line, right)


def stitch_page(seq, blocks, parser):
    for i, b in enumerate(blocks):
        if i == 0 and seq and merge_cont(seq[-1], b, parser):
            continue
        if b.get("type") == "_licont":
            b = {"type": "p", "html": b["html"], "cls": ""}
            parser.warn(f"p{blocks[0].get('_page')}: orphan list continuation kept as a paragraph")
        seq.append(b)


def merge_cont(prev, b, parser):
    t, pt = b.get("type"), prev.get("type")
    if t == "table" and pt == "table" and prev["headers"] == b["headers"]:
        prev["rows"].extend(b["rows"])
        prev["check"] = prev["check"] or b["check"]
        return True
    if t in ("card", "catch") and b.get("_cont"):
        if pt == t and (prev.get("_open") or prev.get("_lastonpage")):
            prev["mons"].extend(b["mons"])
            if b.get("lead_fields"):
                prev["lead_fields"].extend(b["lead_fields"])
            ex = b.get("extra", [])
            if ex and prev.get("extra") and merge_cont(prev["extra"][-1], ex[0], parser):
                ex = ex[1:]
            prev.setdefault("extra", []).extend(ex)
            prev["_open"] = b.get("_open")
            return True
        parser.warn(f"p{b.get('_page')}: {t} continuation without an open {t} before it (prev {pt})")
        return False
    if t == "p" and pt == "p" and prev.get("cls", "") == b.get("cls", ""):
        if ends_full(prev, b.get("_lines_first")):
            prev["html"] = prev["html"] + " " + b["html"]
            for k in ("_lines_last", "_right", "_y1"):
                prev[k] = b.get(k)
            return True
        return False
    if t == "_licont" and pt in ("ol", "ul", "checklist"):
        sep = JOIN.sep(prev["_lines_last"].text, b["_lines_first"].text) if prev.get("_lines_last") else " "
        prev["items"][-1]["html"] = prev["items"][-1]["html"] + sep + b["html"]
        return True
    if t in ("ol", "ul", "checklist") and pt == t:
        if t == "ol":
            ln = prev["items"][-1].get("num")
            fn = b["items"][0].get("num")
            if ln is not None and fn is not None and fn != ln + 1:
                return False
        prev["items"].extend(b["items"])
        for k in ("_lines_last", "_right", "_y1"):
            prev[k] = b.get(k)
        return True
    return False


def strip_private(o):
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o


def finalize(model):
    """Renderer-equivalent tidying: auto step numbers stay explicit only where they break the sequence."""
    def walk(blocks):
        for b in blocks:
            if b.get("type") == "ol":
                n = 0
                for it in b["items"]:
                    num = it.get("num")
                    n = num if num else n + 1
                    it["num"] = n
            for k in ("blocks", "extra"):
                if isinstance(b.get(k), list):
                    walk(b[k])
    for ch in model["chapters"]:
        walk(ch["blocks"])
    return model


def debug_page(path, pno):
    p = PrimaParser(path)
    p.build_vocab()
    p.dex_names = p.scan_dex_names()
    blocks = p.parse_body_page(pno)
    for w in p.warnings:
        print("!", w, file=sys.stderr)
    return strip_private(blocks)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default=str(HERE / "data" / "model.json"))
    ap.add_argument("--page", type=int, help="debug: print the blocks parsed from one body page")
    ap.add_argument("--no-table-check", action="store_true",
                    help="skip rendering each table alone to settle wrapped hyphens (faster, less exact)")
    a = ap.parse_args(argv)
    if a.page:
        print(json.dumps(debug_page(a.pdf, a.page), indent=1, ensure_ascii=False))
        return 0
    p = PrimaParser(a.pdf)
    p.check_tables = not a.no_table_check
    model = finalize(p.parse(progress=lambda i, n: (i % 50 == 0 or i == n) and print(f"  page {i}/{n}", file=sys.stderr)))
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    nb = sum(len(c["blocks"]) for c in model["chapters"])
    print(f"wrote {a.out}: {len(model['chapters'])} chapters, {nb} top-level blocks", file=sys.stderr)
    for w in p.warnings:
        print("!", w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
