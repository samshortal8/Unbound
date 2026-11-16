"""
sprites.py - resolve, fetch and cache Pokémon sprites for the restyled guide.

Source order (first hit wins, every hit is cached so nothing is downloaded twice):
  1. overrides/   your own PNGs, named <slug>.png (e.g. 0027-sandshrew-alola.png)
  2. cache        sprite_cache/img/<slug>.png + manifest.json (incl. negative hits)
  3. wiki         https://www.unboundwiki.com  (requests + BeautifulSoup)
  4. github       Skeli789/Dynamic-Pokemon-Expansion, Unbound branch - the sprite
                  sheet the game itself is built from (raw.githubusercontent.com)
  5. None         the renderer draws a type-coloured fallback badge instead

The wiki scraper is deliberately adaptive because the site's markup can change:
it discovers Pokémon page URLs from the /pokemon/ index, tries slug guesses,
then scores every <img>/og:image/linked image on the page against the species,
dex number and form (mega / alola / primal / unbound / therian ...). Pages and
robots.txt are cached under sprite_cache/html so re-runs are offline.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib import robotparser

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # parsing/rendering still works offline without them
    requests = None
    BeautifulSoup = None

try:
    from PIL import Image
except ImportError:
    Image = None

WIKI_BASE = "https://unboundwiki.com"  # the TLS certificate does not cover www.
# Every wiki sprite lives in one folder as <species>[-<form>].png, e.g. gible, sandshrew-a,
# garchomp-mega, charizard-mega-x, hoopa-unbound, rotom-mow, landorus-therian.
WIKI_SPRITES = "https://static.unboundwiki.com/wp-content/assets/sprites/pokemon/"
WIKI_FORM_SUFFIX = {"alola": "a", "galar": "g", "hisui": "h"}
DPE_RAW = "https://raw.githubusercontent.com/Skeli789/Dynamic-Pokemon-Expansion/Unbound/graphics/frontspr/"
DPE_API = "https://api.github.com/repos/Skeli789/Dynamic-Pokemon-Expansion/git/trees/Unbound?recursive=1"
USER_AGENT = "UnboundPrimaGuideBuilder/1.0 (personal fan guide; sprite cache, polite rate limit)"

# Names whose punctuation is part of the species name (never split on it)
KEEP_HYPHEN = {"porygon-z", "ho-oh", "kommo-o", "hakamo-o", "jangmo-o", "type: null", "mr. mime",
               "mime jr.", "mr. rime", "farfetch'd", "sirfetch'd", "nidoran f", "nidoran m",
               "tapu koko", "tapu lele", "tapu bulu", "tapu fini", "flabébé"}

FORM_PREFIX = {"mega": "mega", "primal": "primal", "alolan": "alola", "galarian": "galar",
               "hisuian": "hisui", "black": "black", "white": "white", "ultra": "ultra"}
FORM_SUFFIX = {"unbound": "unbound", "therian": "therian", "origin": "origin", "-t": "therian",
               "-therian": "therian", "-mow": "mow", "-fan": "fan", "-heat": "heat", "-wash": "wash",
               "-frost": "frost", "-a": "alola", "-alola": "alola", "-g": "galar", "-galar": "galar",
               "-o": "origin", "-origin": "origin", "-b": "black", "-w": "white", "-sky": "sky",
               "-attack": "attack", "-defense": "defense", "-speed": "speed", "-dusk": "dusk",
               "-midnight": "midnight", "-10": "10", "-complete": "complete", "-blade": "blade"}
PAREN_FORMS = {"alolan": "alola", "alola": "alola", "galarian": "galar", "hisuian": "hisui",
               "mega": "mega", "primal": "primal", "therian": "therian", "origin": "origin"}

# tokens used when scoring wiki images for a form
FORM_TOKENS = {
    "mega": ["mega"], "mega-x": ["megax", "mega-x", "mega_x"], "mega-y": ["megay", "mega-y", "mega_y"],
    "primal": ["primal"], "alola": ["alola", "alolan"], "galar": ["galar", "galarian"],
    "hisui": ["hisui", "hisuian"], "unbound": ["unbound"], "therian": ["therian"],
    "origin": ["origin"], "mow": ["mow"], "fan": ["fan"], "heat": ["heat"], "wash": ["wash"],
    "frost": ["frost"], "black": ["black"], "white": ["white"], "sky": ["sky"], "ultra": ["ultra"],
}
ALL_FORM_WORDS = {"mega", "primal", "alola", "alolan", "galar", "galarian", "hisui", "hisuian",
                  "shiny", "back", "female", "gmax", "gigantamax"}
# species whose base sprite has a different DPE file stem
DPE_ALIAS = {"minior": "miniorred", "basculin": "basculinred", "castform": "castform"}
# DPE filename suffix per form
DPE_SUFFIX = {None: "", "alola": "a", "galar": "g", "hisui": "h", "mega": "mega", "mega-x": "megax",
              "mega-y": "megay", "primal": "primal", "unbound": "unbound", "therian": "therian",
              "origin": "origin", "mow": "mow", "fan": "fan", "heat": "heat", "wash": "wash",
              "frost": "frost", "black": "black", "white": "white", "sky": "sky", "ultra": "ultra",
              "attack": "attack", "defense": "defense", "speed": "speed", "dusk": "dusk",
              "10": "10", "complete": "complete", "blade": "blade"}


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", ascii_fold(s).lower())


def slugify(s: str) -> str:
    s = ascii_fold(s).lower().replace("'", "").replace(".", "").replace(":", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


@dataclass(frozen=True)
class SpriteKey:
    dex: Optional[int]
    species: str          # display species name, e.g. "Sandshrew"
    form: Optional[str]   # None | mega | mega-x | primal | alola | unbound | therian | mow ...

    @property
    def slug(self) -> str:
        d = f"{self.dex:04d}" if self.dex else "0000"
        return f"{d}-{slugify(self.species)}" + (f"-{self.form}" if self.form else "")

    @property
    def display(self) -> str:
        return self.species + (f" ({self.form})" if self.form else "")


def parse_species(name: str, label: str = "", dex: Optional[int] = None) -> SpriteKey:
    """'Mega Garchomp' / 'Grimer (Alolan)' / 'Rotom-Mow' / 'Gabite (swap)' -> SpriteKey."""
    raw = (name or label or "").strip()
    raw = re.sub(r"\s+", " ", raw)
    form = None
    # parentheticals: keep only real form words; '(probably Mega)' stays base form
    for par in re.findall(r"\(([^)]*)\)", raw):
        p = par.strip().lower()
        if p in PAREN_FORMS:
            form = PAREN_FORMS[p]
    raw = re.sub(r"\([^)]*\)?", "", raw).strip()
    raw = re.split(r"\s+or\s+", raw)[0].strip()          # 'Clefairy or Clefable' -> Clefairy
    low = raw.lower()
    if low in KEEP_HYPHEN:
        return SpriteKey(dex, raw, form)
    words = raw.split(" ")
    if len(words) > 1 and words[0].lower() in FORM_PREFIX:
        form = FORM_PREFIX[words[0].lower()]
        raw = " ".join(words[1:])
        if form == "mega" and raw.split(" ")[-1] in ("X", "Y"):
            form = "mega-" + raw.split(" ")[-1].lower()
            raw = " ".join(raw.split(" ")[:-1])
    words = raw.split(" ")
    if len(words) > 1 and words[-1].lower() in FORM_SUFFIX:
        form = FORM_SUFFIX[words[-1].lower()]
        raw = " ".join(words[:-1])
    if raw.lower() not in KEEP_HYPHEN and "-" in raw:
        base, _, suf = raw.partition("-")
        key = "-" + suf.lower()
        if key in FORM_SUFFIX:
            form = FORM_SUFFIX[key]
            raw = base
    return SpriteKey(dex, raw.strip(), form)


# --------------------------------------------------------------------------
class SpriteResolver:
    def __init__(self, cache_dir="sprite_cache", sources=("wiki", "github"), overrides_dir="overrides",
                 offline=False, delay=0.6, timeout=20, retry_missing_after_days=14,
                 dpe_index_file=None, log=print):
        self.cache = Path(cache_dir)
        (self.cache / "img").mkdir(parents=True, exist_ok=True)
        (self.cache / "html").mkdir(parents=True, exist_ok=True)
        self.overrides = Path(overrides_dir) if overrides_dir else None
        self.sources = [s for s in sources if s and s != "none"]
        self.offline = offline or requests is None
        self.delay = delay
        self.timeout = timeout
        self.retry_missing = retry_missing_after_days * 86400
        self.log = log
        self.manifest_path = self.cache / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text("utf-8")) if self.manifest_path.exists() else {}
        self._lock = threading.Lock()
        self._last_hit = {}
        self._fail_streak = {}
        self._disabled = set()
        self._robots = {}
        self._wiki_index = None
        self._dpe_index = None
        self.dpe_index_file = Path(dpe_index_file) if dpe_index_file else None
        self.stats = {"override": 0, "cache": 0, "wiki": 0, "github": 0, "missing": 0}
        self.session = None
        if not self.offline:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = USER_AGENT

    # ------------------------------------------------------------ public
    def save(self):
        self.manifest_path.write_text(json.dumps(self.manifest, indent=1, sort_keys=True), "utf-8")

    def get(self, key: SpriteKey) -> Optional[Path]:
        slug = key.slug
        if self.overrides and (self.overrides / f"{slug}.png").exists():
            self.stats["override"] += 1
            return (self.overrides / f"{slug}.png").resolve()
        ent = self.manifest.get(slug)
        if ent and ent.get("file") and (self.cache / "img" / ent["file"]).exists():
            self.stats["cache"] += 1
            return (self.cache / "img" / ent["file"]).resolve()
        if ent and ent.get("missing") and time.time() - ent.get("ts", 0) < self.retry_missing:
            self.stats["missing"] += 1
            return None
        if self.offline:
            self.stats["missing"] += 1
            return None
        tried = []
        for src in self.sources:
            if src in self._disabled:
                continue
            try:
                res = self._fetch_wiki(key) if src == "wiki" else self._fetch_github(key) if src == "github" else None
            except Exception as e:  # never let one sprite kill the build
                self.log(f"  ! {src} error for {key.display}: {e}")
                res = None
            tried.append(src)
            if res:
                data, url = res
                path = self._store(slug, data)
                if path:
                    self.manifest[slug] = {"file": path.name, "source": src, "url": url,
                                           "species": key.species, "form": key.form, "dex": key.dex,
                                           "ts": int(time.time())}
                    self.stats[src] += 1
                    return path.resolve()
        if tried and not any(s in self._disabled for s in tried):
            # only remember a miss when every source was reachable (not a network outage)
            self.manifest[slug] = {"missing": True, "tried": tried, "ts": int(time.time()),
                                   "species": key.species, "form": key.form, "dex": key.dex}
        self.stats["missing"] += 1
        return None

    # ------------------------------------------------------------ http
    def _get(self, url, binary=False, cache_html=True):
        host = urlparse(url).netloc
        src = "wiki" if "unboundwiki" in host else "github"
        if src in self._disabled:
            return None
        if cache_html and not binary:
            cp = self.cache / "html" / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".html")
            if cp.exists():
                return cp.read_text("utf-8", errors="replace")
        wait = self.delay - (time.time() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.time()
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            n = self._fail_streak.get(src, 0) + 1
            self._fail_streak[src] = n
            if n >= 3:
                self._disabled.add(src)
                self.log(f"  ! {src}: {n} connection failures in a row ({type(e).__name__}); "
                         f"disabling this source for the rest of the run")
            return None
        self._fail_streak[src] = 0
        if r.status_code in (403, 429) and src == "wiki":
            self.log(f"  ! wiki answered {r.status_code}; backing off this source for the run")
            self._disabled.add("wiki")
            return None
        if r.status_code != 200:
            return None
        if binary:
            return r.content
        if cache_html:
            cp.write_text(r.text, "utf-8")
        return r.text

    def _allowed(self, url):
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            txt = self._get(base + "/robots.txt")
            if txt is None:
                rp = None  # robots unreachable: treat as allowed (standard behaviour)
            else:
                rp.parse(txt.splitlines())
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # ------------------------------------------------------------ wiki
    def _wiki_pages_index(self):
        if self._wiki_index is not None:
            return self._wiki_index
        idx_path = self.cache / "wiki_index.json"
        if idx_path.exists():
            self._wiki_index = json.loads(idx_path.read_text("utf-8"))
            return self._wiki_index
        index = {}
        for path in ("/pokemon/", "/pokedex/", "/"):
            url = WIKI_BASE + path
            if not self._allowed(url):
                self.log(f"  ! robots.txt disallows {url}; skipping")
                continue
            page = self._get(url)
            if not page:
                continue
            soup = BeautifulSoup(page, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"]).split("#")[0]
                pu = urlparse(href)
                if "unboundwiki" not in pu.netloc:
                    continue
                parts = [p for p in pu.path.split("/") if p]
                if len(parts) < 2 or parts[0].lower() not in ("pokemon", "pokedex"):
                    continue
                text = a.get_text(" ", strip=True)
                for k in {alnum(text), alnum(parts[-1])}:
                    if k and k not in index:
                        index[k] = href
            if len(index) > 50:
                break
        self._wiki_index = index
        if index:
            idx_path.write_text(json.dumps(index, indent=0), "utf-8")
        self.log(f"  wiki index: {len(index)} Pokémon page links discovered")
        return index

    def _wiki_candidate_pages(self, key: SpriteKey):
        idx = self._wiki_pages_index()
        sp = slugify(key.species)
        fw = FORM_TOKENS.get(key.form, [key.form] if key.form else [])
        names = []
        if key.form:
            for t in fw:
                names += [t + sp, sp + t]
        names.append(sp)
        urls = []
        for n in names:
            u = idx.get(alnum(n))
            if u and u not in urls:
                urls.append(u)
        guesses = []
        if key.form:
            for t in fw[:2]:
                guesses += [f"/pokemon/{t}-{sp}/", f"/pokemon/{sp}-{t}/"]
        guesses += [f"/pokemon/{sp}/"]
        if key.dex:
            guesses += [f"/pokemon/{key.dex:03d}/", f"/pokedex/{sp}/"]
        for g in guesses:
            u = WIKI_BASE + g
            if u not in urls:
                urls.append(u)
        return urls

    def _score_img(self, key: SpriteKey, src: str, alt: str, cls: str, w, h):
        s = 0.0
        hay = alnum(src.split("?")[0].rsplit("/", 1)[-1] + " " + alt)
        path = src.lower()
        sp = alnum(key.species)
        if sp and sp in hay:
            s += 6
        if key.dex and (f"{key.dex:03d}" in hay or f"{key.dex:04d}" in hay):
            s += 4
        if any(t in path or t in cls.lower() for t in ("sprite", "front", "pokemon")):
            s += 2
        if path.endswith((".png", ".gif", ".webp")):
            s += 1
        if path.endswith(".svg") or any(t in path for t in ("/type", "type-", "item", "logo", "badge", "ball", "favicon", "avatar")):
            s -= 12
        if "shiny" in hay:
            s -= 5
        if "back" in hay and "backsprite" in hay or re.search(r"(^|[^a-z])back", path):
            s -= 5
        wanted = [alnum(t) for t in FORM_TOKENS.get(key.form, [])]
        present = {wd for wd in ALL_FORM_WORDS | {"unbound", "therian", "origin"} if wd in hay}
        if key.form:
            s += 6 if any(t in hay for t in wanted) else -8
        elif present - {"unbound"} if sp != "hoopa" else present:
            s -= 6
        try:
            if w and int(str(w).rstrip("px")) < 32:
                s -= 4
        except ValueError:
            pass
        return s

    def _fetch_wiki(self, key: SpriteKey):
        # 1) the wiki's sprite folder, by its naming scheme (no page scrape needed)
        name = slugify(key.species)
        if key.form:
            name += "-" + WIKI_FORM_SUFFIX.get(key.form, key.form)
        url = WIKI_SPRITES + name + ".png"
        data = self._get(url, binary=True)
        if data:
            return data, url
        if "wiki" in self._disabled or BeautifulSoup is None:
            return None
        # 2) fall back to scoring the images on the species' wiki page
        best = None
        for url in self._wiki_candidate_pages(key):
            if "wiki" in self._disabled:
                return None
            if not self._allowed(url):
                continue
            page = self._get(url)
            if not page:
                continue
            soup = BeautifulSoup(page, "html.parser")
            cands = []
            for img in soup.find_all("img"):
                src = img.get("data-src") or img.get("src") or ""
                if not src and img.get("srcset"):
                    src = img["srcset"].split(",")[0].split()[0]
                if not src or src.startswith("data:"):
                    continue
                cands.append((urljoin(url, src), img.get("alt", "") + " " + img.get("title", ""),
                              " ".join(img.get("class", [])), img.get("width"), img.get("height")))
            for a in soup.find_all("a", href=True):
                if re.search(r"\.(png|gif|webp)(\?|$)", a["href"], re.I):
                    cands.append((urljoin(url, a["href"]), a.get_text(" ", strip=True), "", None, None))
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                cands.append((urljoin(url, og["content"]), "og:image " + key.species, "", None, None))
            for src, alt, cls, w, h in cands:
                sc = self._score_img(key, src, alt, cls, w, h)
                if best is None or sc > best[0]:
                    best = (sc, src)
            if best and best[0] >= 9:
                break
        if not best or best[0] < 6:
            return None
        data = self._get(best[1], binary=True)
        return (data, best[1]) if data else None

    # ------------------------------------------------------------ github (DPE)
    def _dpe_files(self):
        if self._dpe_index is not None:
            return self._dpe_index
        names = []
        if self.dpe_index_file and self.dpe_index_file.exists():
            names = [l.strip() for l in self.dpe_index_file.read_text().splitlines() if l.strip()]
        elif not self.offline:
            txt = self._get(DPE_API, cache_html=True)
            if txt:
                try:
                    tree = json.loads(txt)["tree"]
                    names = [t["path"].split("graphics/", 1)[1].replace("frontspr/", "") for t in tree
                             if t["path"].startswith(("graphics/frontspr/", "graphics/castform/gFront"))]
                except (KeyError, ValueError):
                    names = []
        idx = {}
        for n in names:
            m = re.match(r"(?:castform/)?gFrontSprite\d*([A-Za-z0-9]+)\.png$", n)
            if m:
                idx.setdefault(m.group(1).lower(), n)
        self._dpe_index = idx
        return idx

    def _fetch_github(self, key: SpriteKey):
        idx = self._dpe_files()
        if not idx:
            return None
        base = alnum(key.species)
        suffix = DPE_SUFFIX.get(key.form, alnum(key.form or ""))
        fname = idx.get(base + suffix) or (idx.get(DPE_ALIAS.get(base, "")) if not suffix else None)
        if not fname and key.form in ("mega-x", "mega-y"):
            fname = idx.get(base + "mega")
        if not fname:
            return None
        url = DPE_RAW.replace("frontspr/", "") + (fname if "/" in fname else "frontspr/" + fname)
        data = self._get(url, binary=True)
        return (data, url) if data else None

    # ------------------------------------------------------------ storage
    def _store(self, slug, data: bytes) -> Optional[Path]:
        out = self.cache / "img" / f"{slug}.png"
        if Image is None:
            out.write_bytes(data)
            return out
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:
            return None
        im = normalise_sprite(im)
        im.save(out, "PNG", optimize=True)
        return out


def normalise_sprite(im):
    """Transparent background, first animation frame only, trimmed to a square."""
    if getattr(im, "is_animated", False):
        im.seek(0)
    if im.mode == "P":
        trans = im.info.get("transparency")
        if trans is None:
            im.info["transparency"] = 0  # GBA sprite sheets: palette index 0 is the background
        im = im.convert("RGBA")
    else:
        im = im.convert("RGBA")
    w, h = im.size
    if h == 2 * w:  # two-frame sheets (DPE): keep the first frame
        im = im.crop((0, 0, w, w))
    px = im.load()
    corners = [px[0, 0], px[im.width - 1, 0], px[0, im.height - 1], px[im.width - 1, im.height - 1]]
    if all(c[3] == 255 for c in corners) and len(set(corners)) == 1:
        bg = corners[0]
        data = [(r, g, b, 0) if (r, g, b, a) == bg else (r, g, b, a) for (r, g, b, a) in im.getdata()]
        im.putdata(data)
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    side = max(im.size) + 4
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, side - im.height - 2))
    if side < 192:  # keep pixels crisp at print size: integer nearest-neighbour upscale
        k = -(-192 // side)
        canvas = canvas.resize((side * k, side * k), Image.NEAREST)
    return canvas
