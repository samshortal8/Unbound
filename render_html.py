"""
render_html.py - turn the parsed document model into Prima-style HTML for WeasyPrint.

Layout vocabulary (classes live in prima_unbound.css):
  cover-page, toc-page                       front matter
  chapter-splash / chapter-banner            Prima chapter divider page
  stop-header                                navy location bar (gold stripe, cap pill)
  sec-head                                   Prima sub-heading with gold square
  area-bar                                   gold area strip
  prima-box box-<kind>                       callouts with coloured header bars
  catch-box + encounter-grid/encounter-card  2-column wild Pokémon grid
  boss-card (.is-boss / .is-team)            trainer roster block
  data-table, checklist, steps, bullets      body furniture
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from sprites import parse_species

TYPE_COLORS = {
    "normal": "#9099A1", "fire": "#FF9C54", "water": "#4D90D5", "electric": "#F3D23B",
    "grass": "#63BB5B", "ice": "#74CEC0", "fighting": "#CE4069", "poison": "#AB6AC8",
    "ground": "#D97746", "flying": "#8FA8DD", "psychic": "#F97176", "bug": "#90C12C",
    "rock": "#C7B78B", "ghost": "#5269AC", "dragon": "#0A6DC4", "dark": "#5A5366",
    "steel": "#5A8EA1", "fairy": "#EC8FE6",
}
SHORT_FIELDS = ("Nature", "EVs", "Ability", "Item")
CALLOUT_LABELS = {"gold": "At this stop", "crimson": "The short version", "warning": "Warning",
                  "violet": "Return trip", "emerald": "Before you move on", "tip": "Tip",
                  "note": "Note", "unverified": "Unverified", "?": ""}
CHAPTER_SHORT = {1: "START HERE", 2: "PREP", 3: "EARLY GAME", 4: "MID GAME", 5: "LATE GAME",
                 6: "LEAGUE", 7: "POST-GAME", 8: "REFERENCE", 9: "CHECKLISTS"}


def esc(s):
    return html.escape(str(s or ""), quote=True)


# ------------------------------------------------------------------ stops
def split_stop_title(title):
    """'Tarmigan Town: Successor Maxima (cap 40, then45). Expert.' ->
       ('Tarmigan Town: Successor Maxima', '40 / 45', ['Expert'])"""
    t = re.sub(r"\s+", " ", title or "").strip()
    tags = []
    while True:
        m = re.search(r"[.\s]+(Expert|Difficult ?data)\.?\s*$", t, re.I)
        if not m:
            break
        tags.insert(0, "Difficult data" if m.group(1).lower().startswith("difficult") else "Expert")
        t = t[: m.start()].rstrip(" .")
    cap = None
    m = re.search(r"\(([^()]*?)\bcap ?(\d+)(?:\s*,?\s*then ?(\d+))?\)\s*$", t)
    if m:
        pre = m.group(1).strip().rstrip(",").strip()
        cap = m.group(2) + (f" / {m.group(3)}" if m.group(3) else "")
        t = t[: m.start()].rstrip() + (f" ({pre})" if pre else "")
    return t.strip(), cap, tags


# ------------------------------------------------------------------ sprites
class SpriteBook:
    """Maps parsed mons to sprite files (or None) via a SpriteResolver; builds <img>/badge HTML."""

    def __init__(self, resolver=None):
        self.resolver = resolver
        self.memo = {}

    def key(self, mon):
        return parse_species(mon.get("name", ""), mon.get("label", ""), mon.get("dex"))

    def path(self, mon):
        k = self.key(mon)
        if k.slug not in self.memo:
            self.memo[k.slug] = self.resolver.get(k) if self.resolver else None
        return self.memo[k.slug], k

    def html(self, mon, size_class="sprite-wrap"):
        p, k = self.path(mon)
        name = mon.get("name") or k.species
        if p:
            return (f'<div class="{size_class} has-sprite"><img src="{Path(p).resolve().as_uri()}" '
                    f'alt="{esc(name)}"></div>')
        types = [t.lower() for t in mon.get("types", [])] or ["normal"]
        c1 = TYPE_COLORS.get(types[0], "#9099A1")
        c2 = TYPE_COLORS.get(types[1], c1) if len(types) > 1 else c1
        dex = f"#{k.dex:03d}" if k.dex else "#???"
        form = {"mega": "MEGA", "mega-x": "MEGA X", "mega-y": "MEGA Y", "primal": "PRIMAL",
                "alola": "ALOLA", "galar": "GALAR", "hisui": "HISUI", "unbound": "UNBOUND",
                "therian": "THERIAN", "mow": "MOW", "fan": "FAN"}.get(k.form, (k.form or "").upper())
        ribbon = f'<span class="fb-form">{esc(form)}</span>' if form else ""
        initials = esc(k.species[:10])
        return (f'<div class="{size_class} sprite-fallback" style="background: linear-gradient(135deg, {c1} 0%, '
                f'{c1} 50%, {c2} 50%, {c2} 100%);">{ribbon}<span class="fb-dex">{dex}</span>'
                f'<span class="fb-name">{initials}</span></div>')


# ------------------------------------------------------------------ blocks
def type_badges(types):
    return "".join(f'<span class="type-badge type-{esc(t.lower())}">{esc(t)}</span>' for t in types)


def avail_tag(av):
    if not av:
        return ""
    return f'<span class="avail avail-{esc(av["kind"])}">{esc(av["text"])}</span>'


def pokemon_cols(headers):
    """(name column, dex-number column or None) for tables that list Pokémon, else None."""
    low = [re.sub(r"<[^>]+>", "", h).strip().lower() for h in headers]
    ni = next((i for i, h in enumerate(low) if h.startswith(("pokémon", "pokemon"))), None)
    if ni is None:
        return None
    xi = next((i for i, h in enumerate(low) if h in ("no.", "no", "#", "dex", "dex no.", "nat. no.", "number")), None)
    return ni, xi


def cell_text(c):
    return html.unescape(re.sub(r"<[^>]+>", "", c or "")).strip()


ITEM_COLS = re.compile(r"\b(item|items|held|reward|rewards|prize|gift)\b", re.I)
TM_RE = re.compile(r"(?<![A-Za-z0-9#])(TM|HM)\s?0*(\d{1,3})(?!\d)")
MON_REF_RE = re.compile(r"(?<![\w-])((?:(?:Alolan|Galarian|Mega) )?[A-Z][\w.'\u00e9\-]+(?: [A-Z][\w.'\u00e9\-]+)?) \(#(\d{3})\)")


class ItemDecorator:
    """Adds small item / TM icons to guide HTML (see ItemIcons in sprites.py)."""

    def __init__(self, items, sprites=None, dex_names=None):
        self.items = items
        self.sp = sprites
        self.dex_names = dex_names or {}
        self.memo = {}
        self.count = 0

    def _img(self, path, cls="item-ico"):
        self.count += 1
        return f'<img class="{cls}" src="{Path(path).resolve().as_uri()}" alt="">'

    def _icon(self, ident):
        if ident not in self.memo:
            self.memo[ident] = self.items.icon(ident)
        return self.memo[ident]

    def _tm(self, m):
        key = (m.group(1).upper(), int(m.group(2)))
        if key not in self.memo:
            self.memo[key] = self.items.tm_icon(*key)
        p = self.memo[key]
        return (self._img(p) + m.group(0)) if p else m.group(0)

    def _lead(self, text, allow_ambiguous, strict):
        hit = self.items.leading(html.unescape(text), allow_ambiguous)
        if not hit:
            return None
        ident, n = hit
        plain = html.unescape(text)
        rest = plain[n:]
        if strict and re.match(r"\s+[A-Z0-9]", rest):  # 'Iron Head', 'Quick Claw Lv 2' ...
            return None
        p = self._icon(ident)
        if not p:
            return None
        lead_ws = len(plain) - len(plain.lstrip())
        return html.escape(plain[:lead_ws]) + self._img(p) + html.escape(plain[lead_ws:], quote=False)

    def _bag(self, text):
        out = []
        for piece in re.split(r"(\s*(?:[,;/]|\band\b)\s*)", text):
            m = re.match(r"(\s*(?:\d+\s*[x\u00d7]?\s*|[x\u00d7]\s*\d+\s*)?)(.*)", piece, re.S)
            q, rest = m.group(1), m.group(2)
            dec = self._lead(rest, True, False) if rest.strip() else None
            out.append(q + dec if dec else piece)
        return "".join(out)

    def _mon(self, m):
        """'Basculin (#550)' -> mini sprite before the name, when the name matches that dex entry."""
        name, num = m.group(1), int(m.group(2))
        known = self.dex_names.get(num, "").lower()
        words = name.split(" ")
        # allow a leading verb ('Catch Basculin'): match the name against the tail words
        for i in range(len(words)):
            tail = " ".join(words[i:])
            base = re.sub(r"^(Alolan|Galarian|Mega) ", "", tail)
            if known and known in (base.lower(), tail.lower()):
                p, _ = self.sp.path({"name": tail, "label": tail, "dex": num})
                if p:
                    head = " ".join(words[:i]) + (" " if i else "")
                    return f'{head}{self._img(p, "mon-ico")}{tail} (#{m.group(2)})'
                break
        return m.group(0)

    def __call__(self, h, lead=False, bag=False):
        if self.sp and self.dex_names and h and "(#" in h:
            h = "".join(MON_REF_RE.sub(self._mon, part) if not part.startswith("<") else part
                        for part in re.split(r"(<[^>]+>)", h))
        if not self.items or not h:
            return h
        parts = re.split(r"(<[^>]+>)", h)
        first_text = True
        after_strong = False
        for i, part in enumerate(parts):
            if part.startswith("<"):
                if re.match(r"<strong\b", part):
                    after_strong = True
                continue
            if not part.strip():
                continue
            new = None
            if bag:
                new = self._bag(part)
            elif lead and first_text:
                new = self._lead(part, True, False)
            elif after_strong:
                new = self._lead(part, False, True)
            new = TM_RE.sub(self._tm, new if new is not None else part)
            parts[i] = new
            first_text = after_strong = False
        return "".join(parts)


def dex_str(d):
    return f"#{d:03d}" if isinstance(d, int) else ""


class Renderer:
    def __init__(self, sprites: SpriteBook, dex_icons=False, items=None, dex_names=None):
        self.sp = sprites
        self.deco = ItemDecorator(items, sprites, dex_names)
        self.stop_n = 0
        self.dex_icons = dex_icons
        self.toc = []          # [(chapter_idx, chapter, [(id, title, cap, tags)])]
        self.boss_n = 0
        self.bosses = []

    # -------------------------------------------------------------- flow
    def blocks(self, blocks, in_box=False):
        out = []
        for b in blocks:
            fn = getattr(self, "b_" + b["type"], None)
            if fn:
                out.append(fn(b, in_box) if fn.__code__.co_argcount > 2 else fn(b))
        return "\n".join(out)

    def b_p(self, b, in_box=False):
        h = self.deco(b["html"])
        cls = [b.get("cls", "")]
        plain_lead = re.match(r"<strong>([^<]{2,40}?):?</strong>", h)
        if plain_lead:
            lead = plain_lead.group(1).lower()
            if lead.startswith("expect the switch"):
                cls.append("strategy expect-switch")
            elif lead.startswith("why it") or lead.startswith("why "):
                cls.append("strategy why-hard")
            elif lead.startswith("pokémon here") or lead.startswith("pokemon here"):
                cls.append("here-intro")
        c = " ".join(x for x in cls if x)
        return f'<p class="{c}">{h}</p>' if c else f"<p>{h}</p>"

    def b_label(self, b, in_box=False):
        return f'<p class="sub-label">{self.deco(b["html"])}</p>'

    def b_h3(self, b, in_box=False):
        return f'<h3 class="sec-head"><span class="sec-mark"></span>{esc(b["text"])}</h3>'

    def b_area(self, b, in_box=False):
        return f'<div class="area-bar"><span class="tag-pill">Area</span>{esc(b["text"])}</div>'

    def b_ol(self, b, in_box=False):
        items = []
        n = 0
        for it in b["items"]:
            n = it.get("num") or n + 1
            items.append(f'<li><span class="step-num">{n}</span><div class="step-body">{self.deco(it["html"])}</div></li>')
        return f'<ol class="steps">{"".join(items)}</ol>'

    def b_ul(self, b, in_box=False):
        return '<ul class="bullets">' + "".join(f"<li>{self.deco(it['html'])}</li>" for it in b["items"]) + "</ul>"

    def b_checklist(self, b, in_box=False):
        return '<ul class="checklist">' + "".join(f"<li>{self.deco(it['html'])}</li>" for it in b["items"]) + "</ul>"

    def b_table(self, b, in_box=False):
        hdr = b["headers"]
        check_col = b.get("check") and hdr and not hdr[0].strip()
        pc = pokemon_cols(hdr) if self.dex_icons else None
        is_dex = any(h.lower().startswith("pok") for h in hdr) and any(h.lower() == "method" for h in hdr)
        item_cols = {i for i, h in enumerate(hdr) if ITEM_COLS.search(h or "")}
        ths = []
        for i, h in enumerate(hdr):
            if i == 0 and check_col:
                ths.append('<th class="col-check"></th>')
            else:
                ths.append(f"<th>{esc(h)}</th>")
        if pc:
            ths.insert(pc[0], '<th class="col-icon"></th>')
        rows = []
        for r in b["rows"]:
            tds = []
            for i, c in enumerate(r["cells"]):
                if i == 0 and check_col:
                    tds.append(f'<td class="col-check"><span class="cbox"></span></td>')
                else:
                    tds.append(f"<td>{self.deco(c, lead=i in item_cols)}</td>")
            if pc:
                ni, xi = pc
                cells = r["cells"]
                nm = cell_text(cells[ni]) if ni < len(cells) else ""
                dx = re.sub(r"\D", "", cell_text(cells[xi]))[:4] if xi is not None and xi < len(cells) else ""
                mon = {"name": nm, "label": nm, "dex": int(dx) if dx else None, "types": []}
                p, _ = self.sp.path(mon) if nm else (None, None)
                icon = self.sp.html(mon, "dex-icon") if (p or (nm and xi is not None)) else ""
                tds.insert(ni, f'<td class="col-icon">{icon}</td>')
            rows.append("<tr>" + "".join(tds) + "</tr>")
        cls = ("data-table" + (" has-check" if check_col else "") + (" dex-table" if is_dex else "")
               + (" has-icons" if pc else ""))
        return f'<table class="{cls}"><thead><tr>{"".join(ths)}</tr></thead><tbody>{"".join(rows)}</tbody></table>'

    def b_callout(self, b, in_box=False):
        kind = b.get("kind") or "note"
        if kind == "?":
            kind = "note"
        label = b.get("label") or CALLOUT_LABELS.get(kind, "")
        body = self.blocks(b.get("blocks", []), True)
        return (f'<div class="prima-box box-{kind}"><div class="prima-box-header"><span class="pbh-mark"></span>'
                f'{esc(label)}</div><div class="prima-box-body">{body}</div></div>')

    def b_stop(self, b, in_box=False):
        self.stop_n += 1
        sid = f"stop-{self.stop_n}"
        title, cap, tags = split_stop_title(b["title"])
        self.toc[-1][2].append((sid, title, cap, tags))
        cap_html = f'<span class="cap-pill"><span class="cap-k">LV CAP</span> {esc(cap)}</span>' if cap else ""
        tag_html = "".join(f'<span class="stop-tag tag-{"expert" if t == "Expert" else "data"}">{esc(t)}</span>' for t in tags)
        return (f'<div class="stop-header" id="{sid}" data-stop="{esc(title)}">{cap_html}'
                f'<h2>{esc(title)}</h2>{("<div class=stop-tags>" + tag_html + "</div>") if tags else ""}</div>')

    # -------------------------------------------------------------- catch grid
    def b_catch(self, b, in_box=False):
        mons = b["mons"]
        title = b.get("title") or "Pokémon"
        n_now = sum(1 for m in mons if (m.get("avail") or {}).get("kind") == "now")
        count = f'<span class="catch-count">{len(mons)} species{f" · {n_now} now" if n_now else ""}</span>'
        cells = [self.enc_cell(m) for m in mons]
        if len(cells) % 2:
            cells.append('<td class="encounter-card empty"></td>')
        rows = "".join(f'<tr class="encounter-row">{cells[i]}{cells[i + 1]}</tr>' for i in range(0, len(cells), 2))
        extra = self.blocks(b.get("extra", []), True)
        lead = "".join(f'<p class="small"><strong>{esc(l)}</strong> {self.deco(v)}</p>' for l, v in b.get("lead_fields", []))
        return (f'<div class="catch-box"><div class="catch-head"><span class="tag-pill">Catch here</span>'
                f'<span class="catch-title">{esc(title)}</span>{count}</div>{lead}'
                f'<table class="encounter-grid">{rows}</table>{extra}</div>')

    def enc_cell(self, m):
        method = ""
        notes = []
        for lab, val in m.get("fields", []):
            val = self.deco(val, lead=bool(ITEM_COLS.search(lab or "")))
            if lab.lower() == "method":
                method = f'<span class="meth"><span class="meth-k">Method</span> {val}</span>'
            elif lab:
                notes.append(f'<span class="meth-k">{esc(lab)}</span> {val}')
            else:
                notes.append(val)
        check = '<span class="cbox enc-check"></span>' if m.get("check") else ""
        return (f'<td class="encounter-card">{check}<table class="enc-inner"><tr><td class="enc-sprite">'
                f'{self.sp.html(m)}</td><td class="enc-body">'
                f'<div class="mon-title">{esc(m.get("name"))} <span class="mon-dex">{dex_str(m.get("dex"))}</span></div>'
                f'<div class="type-row">{type_badges(m.get("types", []))}</div>'
                f'<div class="avail-row">{avail_tag(m.get("avail"))}{method}</div>'
                + "".join(f'<div class="mon-meta">{n}</div>' for n in notes) +
                "</td></tr></table></td>")

    # -------------------------------------------------------------- boss / team cards
    def b_card(self, b, in_box=False):
        variant = b.get("variant") or "boss"
        team = variant == "team"
        badge = b.get("badge") or ("Your team" if team else "Boss")
        title = b.get("title") or ""
        anchor = ""
        if not team:
            self.boss_n += 1
            anchor = f' id="boss-{self.boss_n}"'
            meta = {l.lower(): re.sub(r"<[^>]+>", "", v or "") for l, v in (b.get("meta") or [])}
            self.bosses.append((f"boss-{self.boss_n}", title, meta.get("location", ""), meta.get("format", ""),
                                meta.get("level cap", ""), len(b.get("mons", []))))
        head = (f'<div class="boss-card-title"{anchor}><span class="boss-badge">{esc(badge)}</span>{esc(title)}</div>')
        meta = b.get("meta") or []
        meta_html = ""
        if meta:
            meta = [(l, self.deco(v, bag=l.lower() == "bag items")) for l, v in meta]
            short = [(l, v) for l, v in meta if l.lower() != "bag items"]
            long_ = [(l, v) for l, v in meta if l.lower() == "bag items"]
            rows = []
            for i in range(0, len(short), 4):
                chunk = short[i:i + 4]
                tds = "".join(f'<td><span class="boss-meta-label">{esc(l)}</span>{v or "&ndash;"}</td>' for l, v in chunk)
                tds += '<td></td>' * (4 - len(chunk))
                rows.append(f"<tr>{tds}</tr>")
            for l, v in long_:
                rows.append(f'<tr class="bag-row"><td colspan="4"><span class="boss-meta-label">{esc(l)}</span>{v or "&ndash;"}</td></tr>')
            meta_html = f'<table class="boss-meta-grid">{"".join(rows)}</table>'
        lead = "".join(f'<div class="card-lead"><span class="fld-k">{esc(l)}</span> {self.deco(v)}</div>' if l
                       else f'<div class="card-lead">{self.deco(v)}</div>' for l, v in b.get("lead_fields", []))
        mons = "".join(self.mon_row(m, team) for m in b.get("mons", []))
        extra = self.blocks(b.get("extra", []), True)
        extra = f'<div class="card-extra">{extra}</div>' if extra else ""
        cls = "boss-card is-team" if team else "boss-card is-boss"
        return f'<div class="{cls}">{head}{meta_html}{lead}<div class="mon-rows">{mons}</div>{extra}</div>'

    def mon_row(self, m, team):
        short, long_ = [], []
        for lab, val in m.get("fields", []):
            val = self.deco(val, lead=lab.lower() in ("item", "held item"))
            (short if lab in SHORT_FIELDS else long_).append((lab, val))
        short_html = "".join(f'<span class="fld"><span class="fld-k">{esc(l)}</span> {v}</span>' for l, v in short)
        moves = "".join(f'<span class="move-pill">{esc(mv)}</span>' for mv in m.get("moves", []))
        long_html = ""
        for l, v in long_:
            k = l.lower()
            cls = "fld-note" if k == "note" else "fld-role" if k == "role" else "fld-src" if k.startswith("move source") else "fld-free"
            lab = f'<span class="fld-k">{esc(l)}</span> ' if l else ""
            long_html += f'<div class="{cls}">{lab}{v}</div>'
        lvl = f'<span class="mon-level-badge">Lv {esc(m["level"])}</span>' if m.get("level") else ""
        return (f'<div class="mon-row"><table class="mon-row-t"><tr><td class="mon-sprite-cell">{self.sp.html(m)}</td>'
                f'<td class="mon-main">{lvl}<div class="mon-head"><span class="mon-name">{esc(m.get("name"))}</span>'
                f'<span class="mon-dex">{dex_str(m.get("dex"))}</span>{type_badges(m.get("types", []))}</div>'
                f'{("<div class=mon-fields>" + short_html + "</div>") if short_html else ""}'
                f'{("<div class=move-pills>" + moves + "</div>") if moves else ""}'
                f'{long_html}</td></tr></table></div>')

    # -------------------------------------------------------------- chapters
    def chapter(self, idx, ch, n_chapters):
        self.toc.append((idx, ch, []))
        self.bosses = []
        body = self.blocks(ch.get("blocks", []))
        kb = ""
        if self.bosses:
            rows = "".join(f'<tr><td><strong>{esc(t)}</strong></td><td>{esc(loc)}</td><td>{esc(fmt)}</td>'
                           f'<td class="kb-cap">{esc(cap)}</td><td class="kb-n">{n}</td><td class="kb-pg"><a class="pg-ref" href="#{bid}"></a></td></tr>'
                           for bid, t, loc, fmt, cap, n in self.bosses[:14])
            kb = (f'<div class="key-battles"><div class="in-chapter-head">Key battles</div><table class="data-table kb-table">'
                  f'<thead><tr><th>Boss</th><th>Where</th><th>Format</th><th>Cap</th><th>Mons</th><th>Page</th></tr></thead>'
                  f'<tbody>{rows}</tbody></table></div>')
        num = ch.get("num") or str(idx)
        prog = ch.get("progress")
        pct = int(round(100 * prog[0] / prog[1])) if prog else 100
        prog_txt = f"{prog[0]}/{prog[1]}" if prog else "&#9733;"
        eyebrow = ch.get("eyebrow") or f"Chapter {idx} of {n_chapters}"
        stats = "".join(f'<div class="stat-tile"><div class="stat-val">{esc(s["value"])}</div>'
                        f'<div class="stat-lab">{esc(s["label"])}</div></div>' for s in ch.get("stats", []))
        title = ch.get("title", "")
        stops = self.toc[-1][2]
        toc_items = "".join(f'<li><a class="toc-link" href="#{sid}">{esc(t)}{(" <span class=toc-cap>cap " + esc(c) + "</span>") if c else ""}</a></li>'
                            for sid, t, c, _ in stops)
        hexagon = (f'<svg class="ch-hex" viewBox="0 0 100 100" width="74pt" height="74pt">'
                   f'<polygon points="50,3 93,27 93,73 50,97 7,73 7,27" fill="#E8B027" stroke="#0D0F1A" stroke-width="5"/>'
                   f'<polygon points="50,14 83,33 83,67 50,86 17,67 17,33" fill="none" stroke="#16192B" stroke-width="2.5"/>'
                   f'<text x="50" y="62" text-anchor="middle" font-family="Press Start 2P" font-size="30" fill="#16192B">{esc(num)}</text></svg>')
        splash = f"""
<section class="chapter-splash" id="ch-{idx}">
  <div class="chapter-banner" data-title="{esc(title)}">
    <table class="banner-t"><tr><td class="banner-hex">{hexagon}</td><td class="banner-txt">
      <div class="chapter-eyebrow">{esc(eyebrow)}</div>
      <h1 class="chapter-heading">{esc(title)}</h1>
    </td></tr></table>
    <div class="hp-row"><span class="hp-k">HP</span><div class="chapter-progress"><div class="chapter-progress-fill" style="width:{pct}%"></div></div><span class="hp-v">{prog_txt}</span></div>
  </div>
  {('<div class="stat-row">' + stats + '</div>') if stats else ''}
  <div class="in-chapter">
    <div class="in-chapter-head">{esc(ch.get("toc_heading") or "In this chapter")}</div>
    <ol class="in-chapter-list">{toc_items}</ol>
  </div>
  {kb}
</section>"""
        return f'<div class="chapter ch-{idx}" style="page: ch{idx}">{splash}\n<div class="chapter-body">{body}</div></div>'


# ------------------------------------------------------------------ front matter
GYM_SHAPES = [
    "50,8 88,30 88,70 50,92 12,70 12,30", "50,6 94,50 50,94 6,50",
    "34,8 66,8 92,34 92,66 66,92 34,92 8,66 8,34", "50,6 93,38 77,92 23,92 7,38",
    "50,8 94,90 6,90", None, "12,12 88,12 88,88 12,88", "30,8 70,8 92,50 70,92 30,92 8,50",
]


def gym_badges_svg():
    out = []
    for i, pts in enumerate(GYM_SHAPES):
        x = i * 60
        if pts is None:
            shape = f'<circle cx="{x + 25}" cy="25" r="21" fill="none" stroke="#E8B027" stroke-width="3"/>'
        else:
            p = " ".join(f"{x + float(a) * 0.5},{float(b) * 0.5}" for a, b in (q.split(",") for q in pts.split()))
            shape = f'<polygon points="{p}" fill="none" stroke="#E8B027" stroke-width="3" stroke-linejoin="round"/>'
        out.append(shape)
    return f'<svg class="gym-row" viewBox="0 0 470 50" width="100%">{"".join(out)}</svg>'


def render_cover(cover, sprite_note):
    eyebrow = "<br>".join(esc(x) for x in cover.get("eyebrow", [])) or "Unofficial fan-made guide"
    t = cover.get("title") or ["Pokémon", "Unbound", "NG+ Expert"]
    title = "<br>".join(esc(x) for x in t[:-1]) + f'<br><span>{esc(t[-1])}</span>'
    ded = cover.get("dedication") or []
    ded_html = (f'<div class="ded-first">{esc(ded[0])}</div><div>{esc(" ".join(ded[1:]))}</div>') if ded else ""
    footer = cover.get("footer", "")
    footer = re.sub(r"\s*Contains no official artwork\.?\s*Dashed boxes are sprite placeholders\.?", "", footer).strip()
    # a footer recovered from an earlier build already carries that build's sprite note
    footer = re.sub(r"\s*Sprite slots show type-coloured fallback badges in this build\.?", "", footer)
    footer = re.sub(r"\s*Sprites: .*?for personal reference\.(\s*Item and TM icons: [^.]*\.)?", "", footer).strip()
    return f"""
<section class="cover-page">
  <div class="cover-badge">{eyebrow}</div>
  <h1 class="cover-title">{title}</h1>
  <div class="cover-subtitle">{esc(cover.get("subtitle") or "Story Walkthrough Edition")}</div>
  <div class="cover-strap">PRIMA-STYLE EDITION &middot; BORRIUS PALETTE</div>
  <div class="cover-hp-bar"><span class="hp-k">HP</span><span class="hp-v">100%</span><div class="cover-hp-fill"></div></div>
  {gym_badges_svg()}
  <div class="cover-dedication">{ded_html}</div>
  <div class="cover-footer">{esc(footer)} {esc(sprite_note)}</div>
</section>"""


def render_contents(toc):
    parts = []
    for idx, ch, stops in toc:
        items = "".join(f'<li><a class="toc-link" href="#{sid}">{esc(t)}'
                        f'{(" <span class=toc-cap>cap " + esc(c) + "</span>") if c else ""}</a></li>' for sid, t, c, _ in stops)
        parts.append(f'<div class="toc-ch"><a class="toc-ch-row" href="#ch-{idx}"><span class="toc-num">{esc(ch.get("num") or idx)}</span>'
                     f'<span class="toc-ch-title">{esc(ch.get("title"))}</span></a><ul class="toc-stops">{items}</ul></div>')
    return f'<section class="toc-page"><h1 class="toc-title">Contents</h1>{"".join(parts)}</section>'


def thumb_tab_svg(idx, n, label, out_dir: Path, num=None):
    """Full-page SVG used as the chapter's @page background: a Prima side tab whose
    vertical position steps down per chapter, so the closed book edge works as an index."""
    top, bottom = 34.0, 262.0
    h = (bottom - top) / n
    y = top + (idx - 1) * h
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">
<rect x="208.2" y="{top}" width="1.8" height="{bottom - top}" fill="#16192B" opacity="0.18"/>
<rect x="201.2" y="{y + 0.6:.2f}" width="12" height="{h - 1.2:.2f}" rx="2.2" fill="#16192B"/>
<rect x="201.2" y="{y + 0.6:.2f}" width="1.3" height="{h - 1.2:.2f}" fill="#E8B027"/>
<text x="205.9" y="{y + 6.4:.2f}" font-family="Press Start 2P" font-size="3.3" fill="#E8B027" text-anchor="middle">{esc(num or str(idx))}</text>
<text transform="translate(206.9,{y + 9.2:.2f}) rotate(90)" font-family="Barlow Condensed" font-weight="800" font-style="italic" font-size="3.0" letter-spacing="0.15" fill="#FFFFFF">{esc(label)}</text>
</svg>"""
    p = out_dir / f"thumbtab-{idx}.svg"
    p.write_text(svg, "utf-8")
    return p


def dex_names_from(model):
    """National dex number -> species name, from every Pokémon table that has a number column."""
    out = {}

    def walk(blocks):
        for b in blocks:
            if b.get("type") == "table":
                pc = pokemon_cols(b["headers"])
                if pc and pc[1] is not None:
                    for r in b["rows"]:
                        c = r["cells"]
                        dx = re.sub(r"\D", "", cell_text(c[pc[1]])) if pc[1] < len(c) else ""
                        if dx and pc[0] < len(c):
                            out.setdefault(int(dx), cell_text(c[pc[0]]))
            for key in ("blocks", "extra"):
                if isinstance(b.get(key), list):
                    walk(b[key])
    for ch in model.get("chapters", []):
        walk(ch.get("blocks", []))
    return out


def build_html(model, sprites: SpriteBook, css_path: Path, asset_dir: Path, dex_icons=False, sprite_note="",
               items=None):
    asset_dir.mkdir(parents=True, exist_ok=True)
    r = Renderer(sprites, dex_icons=dex_icons, items=items, dex_names=dex_names_from(model))
    chapters = model["chapters"]
    n = len(chapters)
    body_ch = [r.chapter(i + 1, ch, sum(1 for c in chapters if (c.get("progress") or [0])[0])) for i, ch in enumerate(chapters)]
    page_css = []
    total = max(n, max((int(c["num"]) for c in chapters if str(c.get("num", "")).isdigit()), default=0)
                + sum(1 for c in chapters if not str(c.get("num", "")).isdigit()))
    for i, ch in enumerate(chapters, 1):
        num = str(ch.get("num") or i)
        pos = int(num) if num.isdigit() else total
        label = CHAPTER_SHORT.get(pos, (ch.get("title") or "").upper()[:16])
        tab = thumb_tab_svg(pos, total, label, asset_dir, num)
        page_css.append(f"@page ch{i} {{ background: url('{tab.resolve().as_uri()}') no-repeat; "
                        f"background-size: 210mm 297mm; background-position: -12mm -14mm; }}")
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{esc(model.get("meta", {}).get("title") or "Pokémon Unbound NG+ Expert")} (Prima edition)</title>
<link rel="stylesheet" href="{css_path.resolve().as_uri()}">
<style>
{chr(10).join(page_css)}
</style></head><body>
{render_cover(model.get("cover", {}), sprite_note)}
{render_contents(r.toc)}
{chr(10).join(body_ch)}
</body></html>"""
    return doc
