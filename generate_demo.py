#!/usr/bin/env python3
"""Demo data scraper — fase 1, uitgebreid.

`scrape_site_data(url)` haalt voor een lead-URL zoveel mogelijk echte data van
de site af zodat een latere template-stap een persoonlijke demo kan renderen:

  visual:  logo, favicon, kleuren (primair/secundair/achtergrond), fonts
  team:    hero-foto + losse teamleden met naam/functie/portret
  content: tagline, over-ons-tekst, diensten, blogposts, reviews, sterrenrating
  contact: telefoon, e-mail, adres, openingstijden, socials
  extra:   jaar opgericht, certificeringen/awards

Velden die niet gevonden worden zijn `null`. Elke extractor is geïsoleerd
zodat een crash in één veld de rest niet meeneemt.

Daarna doet het script `git add + commit + push` zodat de data direct gesynct
is met de remote, en optioneel `verify_deploy(slug)` om te checken of de
gerenderde demo-pagina live staat (skipt zolang er nog geen template is).

Gebruik:
    python3 generate_demo.py --url https://voorbeeld-bakkerij.nl
    python3 generate_demo.py --url https://… --slug voorbeeld-bakkerij
    python3 generate_demo.py --url https://… --no-push
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = REPO_ROOT / "data"
PUBLIC_ROOT = REPO_ROOT / "public"

REQUEST_TIMEOUT_S = 15
MAX_STYLESHEETS = 5
USER_AGENT = "HarvDemoGenerator/0.2 (+https://harvagency.com)"

DEPLOY_BASE_URL = "https://harv-demos.vercel.app"
DEPLOY_POLL_INTERVAL_S = 10
DEPLOY_TIMEOUT_S = 180

HEX_COLOR_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
RGB_COLOR_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
PHONE_RE = re.compile(r"(?:\+31[\s\-]?|\+31\(0\)|0)(?:[\s\-]?\d){8,9}\b")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
POSTAL_RE = re.compile(r"\b\d{4}\s?[A-Z]{2}\b")
STREET_RE = re.compile(
    r"[A-Z][a-zA-Z\-']+(?:weg|straat|laan|plein|kade|dijk|hof|gracht|park|baan|dreef|singel|markt|steeg)"
    r"\s+\d+[a-zA-Z]?",
)
FOUNDED_RE = re.compile(
    r"(?:opgericht(?:\s+in)?|sinds|since|est\.?|gevestigd\s+sinds)\s+(\d{4})",
    re.IGNORECASE,
)
RATING_TEXT_RE = re.compile(r"\b(\d(?:[.,]\d)?)\s*/\s*(?:5|10)\b")

TEAM_KEYWORDS = (
    "team", "ons-team", "onsteam", "personeel", "medewerkers",
    "collega", "about", "over-ons", "wie-zijn-wij", "staff", "crew",
)
LOGO_KEYWORDS = ("logo", "brand", "company-mark", "site-logo", "navbar-brand")
ABOUT_KEYWORDS = (
    "over-ons", "over_ons", "overons", "about", "about-us", "aboutus",
    "wie-zijn-wij", "ons-verhaal", "ons_verhaal", "company", "bedrijf",
)
SERVICE_KEYWORDS = (
    "diensten", "services", "wat-wij-doen", "aanbod", "expertise", "specialiteiten",
)
BLOG_PATH_HINTS = ("/blog/", "/nieuws/", "/news/", "/artikel", "/post/", "/journal/")
# Sectoren worden hier toegevoegd zodra de templates klaar zijn. Nu alleen: makelaardij.
NAV_FALLBACKS: dict[str, list[str]] = {
    "makelaardij": ["Home", "Woningaanbod", "Over ons", "Diensten", "Contact"],
}
NAV_FALLBACK_DEFAULT = ["Home", "Over ons", "Diensten", "Contact"]
_NAV_SKIP_CLASSES = (
    "skip", "sr-only", "screen-reader", "screenreader", "visually-hidden", "a11y",
)
_NAV_NOISE_CONTAINER_HINTS = (
    "filter", "toolbar", "search-bar", "sort-bar", "view-toggle", "viewtoggle", "tabbar",
)
CERT_KEYWORDS = (
    "gecertificeerd", "erkend lid", "iso 9001", "iso 14001",
    "nvm makelaar", "vastgoedcert", "keurmerk", "award winner",
    "best of houzz",
)
SOCIAL_DOMAINS = {
    "facebook": ("facebook.com",),
    "instagram": ("instagram.com",),
    "linkedin": ("linkedin.com",),
    "twitter": ("twitter.com", "x.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}
DAY_HINTS = ("ma", "di", "wo", "do", "vr", "za", "zo", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ─── helpers ────────────────────────────────────────────────────────────────

def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9\-]+", "-", value.lower())
    return re.sub(r"-+", "-", value).strip("-") or "lead"


def slug_from_url(url: str) -> str:
    host = urlparse(url).hostname or url
    host = host.removeprefix("www.")
    return slugify(host.split(".")[0])


def _safe(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Roep een extractor aan; geef `default` terug bij élke exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — bewust breed: extractors mogen nooit crashen
        print(f"  ⚠ {fn.__name__}: {type(exc).__name__}: {exc}")
        return default


def _abs(href: str, base_url: str) -> str:
    return urljoin(base_url, (href or "").strip())


def _text(tag: Optional[Tag]) -> str:
    if not tag:
        return ""
    return " ".join(tag.get_text(separator=" ").split())


def _fetch_text(url: str, headers: dict[str, str]) -> str:
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        if resp.ok:
            return resp.text
    except requests.RequestException:
        pass
    return ""


def _collect_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            blocks.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            blocks.append(data)
            if "@graph" in data and isinstance(data["@graph"], list):
                blocks.extend(d for d in data["@graph"] if isinstance(d, dict))
    return blocks


def _ld_find(blocks: list[dict[str, Any]], type_name: str) -> Optional[dict[str, Any]]:
    type_name = type_name.lower()
    for block in blocks:
        t = block.get("@type")
        if isinstance(t, list):
            if any(str(x).lower() == type_name for x in t):
                return block
        elif isinstance(t, str) and t.lower() == type_name:
            return block
    return None


# ─── kleur-extractie ────────────────────────────────────────────────────────

def _normalize_hex(raw: str) -> Optional[str]:
    raw = raw.lower().lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) in (6, 8):
        return "#" + raw[:6]
    return None


def _hex_from_rgb(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


def _is_grayscale(hex_color: str, tolerance: int = 12) -> bool:
    r, g, b = _rgb(hex_color)
    return max(r, g, b) - min(r, g, b) <= tolerance


def _brightness(hex_color: str) -> float:
    r, g, b = _rgb(hex_color)
    return (r + g + b) / 3


def _color_distance(a: str, b: str) -> float:
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    return ((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2) ** 0.5


def _collect_colors(text: str) -> list[str]:
    out: list[str] = []
    for match in HEX_COLOR_RE.findall(text):
        norm = _normalize_hex(match)
        if norm:
            out.append(norm)
    for r, g, b in RGB_COLOR_RE.findall(text):
        out.append(_hex_from_rgb(int(r), int(g), int(b)))
    return out


def _build_color_counter(html: str, soup: BeautifulSoup, base_url: str) -> Counter[str]:
    headers = {"User-Agent": USER_AGENT}
    counter: Counter[str] = Counter()
    for tag in soup.find_all("style"):
        for color in _collect_colors(tag.get_text() or ""):
            counter[color] += 1
    stylesheet_links = [
        _abs(link["href"], base_url)
        for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r)
        if link.get("href")
    ][:MAX_STYLESHEETS]
    for href in stylesheet_links:
        for color in _collect_colors(_fetch_text(href, headers)):
            counter[color] += 1
    return counter


def extract_color_palette(html: str, soup: BeautifulSoup, base_url: str) -> dict[str, Optional[str]]:
    palette: dict[str, Optional[str]] = {"primary": None, "secondary": None, "background": None}

    meta = soup.find("meta", attrs={"name": re.compile(r"^theme-color$", re.I)})
    if meta and meta.get("content"):
        palette["primary"] = _normalize_hex(meta["content"])

    counter = _build_color_counter(html, soup, base_url)
    ranked = [c for c, _ in counter.most_common()]

    if not palette["primary"]:
        for color in ranked:
            if _is_grayscale(color):
                continue
            if _brightness(color) > 235 or _brightness(color) < 25:
                continue
            palette["primary"] = color
            break

    for color in ranked:
        if _is_grayscale(color) or _brightness(color) > 235 or _brightness(color) < 25:
            continue
        if palette["primary"] and _color_distance(color, palette["primary"]) < 60:
            continue
        palette["secondary"] = color
        break

    for color in ranked:
        if 230 <= _brightness(color) <= 255 and _is_grayscale(color, tolerance=20):
            palette["background"] = color
            break

    return palette


# ─── fonts ──────────────────────────────────────────────────────────────────

def extract_fonts(soup: BeautifulSoup) -> dict[str, Any]:
    families: list[str] = []
    google_fonts_url: Optional[str] = None
    for link in soup.find_all("link", href=True):
        href = link["href"]
        if "fonts.googleapis.com" in href:
            google_fonts_url = href
            qs = parse_qs(urlparse(href).query)
            for fam in qs.get("family", []):
                for entry in fam.split("|"):
                    name = entry.split(":")[0].replace("+", " ").strip()
                    if name and name not in families:
                        families.append(name)
            break

    if not families:
        for tag in soup.find_all("style"):
            for match in re.finditer(r"font-family:\s*([^;}\n]+)", tag.get_text() or ""):
                first = match.group(1).split(",")[0].strip().strip("\"'")
                if first and first.lower() not in ("inherit", "sans-serif", "serif") and first not in families:
                    families.append(first)
            if len(families) >= 3:
                break

    return {"families": families[:5] or None, "google_fonts_url": google_fonts_url}


# ─── logo / favicon ─────────────────────────────────────────────────────────

def extract_logo(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    candidates: list[tuple[int, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        haystack = " ".join(
            filter(None, [
                (img.get("alt") or "").lower(),
                src.lower(),
                " ".join(img.get("class") or []).lower(),
                (img.get("id") or "").lower(),
            ])
        )
        score = sum(3 for kw in LOGO_KEYWORDS if kw in haystack)
        # Header-locatie geeft een extra punt.
        if img.find_parent(["header", "nav"]):
            score += 2
        if score:
            candidates.append((score, _abs(src, base_url)))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def extract_favicon(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    for rel in ("icon", "shortcut icon", "apple-touch-icon"):
        link = soup.find("link", rel=lambda r: r and rel in [str(x).lower() for x in (r if isinstance(r, list) else [r])])
        if link and link.get("href"):
            return _abs(link["href"], base_url)
    # Browser-fallback.
    return _abs("/favicon.ico", base_url)


# ─── team ───────────────────────────────────────────────────────────────────

def _img_team_score(img_tag: Tag, base_url: str) -> tuple[int, str]:
    src = img_tag.get("src") or img_tag.get("data-src") or ""
    if not src or src.startswith("data:"):
        return 0, ""
    absolute = _abs(src, base_url)
    haystack = " ".join(filter(None, [
        (img_tag.get("alt") or "").lower(),
        (img_tag.get("title") or "").lower(),
        " ".join(img_tag.get("class") or []).lower(),
        absolute.lower(),
    ]))
    score = sum(5 for kw in TEAM_KEYWORDS if kw in haystack)
    try:
        w = int(img_tag.get("width") or 0)
        h = int(img_tag.get("height") or 0)
        if w >= 400 or h >= 400:
            score += 2
        if w >= 800 or h >= 600:
            score += 2
    except (TypeError, ValueError):
        pass
    for negative in ("logo", "icon", "sprite", "avatar-default", "placeholder"):
        if negative in haystack:
            score -= 6
    return score, absolute


def extract_team_hero(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    best_score = 0
    best_url: Optional[str] = None
    for img in soup.find_all("img"):
        score, url = _img_team_score(img, base_url)
        if score > best_score and url:
            best_score = score
            best_url = url
    if best_score >= 3 and best_url:
        return best_url
    og = soup.find("meta", attrs={"property": re.compile(r"^og:image$", re.I)})
    if og and og.get("content"):
        return _abs(og["content"], base_url)
    return None


def _find_text_near(tag: Tag, max_chars: int = 120) -> list[str]:
    """Verzamel korte tekstregels rond een tag — voor naam/functie heuristiek."""
    lines: list[str] = []
    container = tag.find_parent(["figure", "article", "li", "div", "section"]) or tag.parent
    if not container:
        return lines
    for piece in container.stripped_strings:
        if len(piece) <= max_chars and piece.strip():
            lines.append(piece.strip())
        if len(lines) >= 6:
            break
    return lines


def extract_team_members(soup: BeautifulSoup, base_url: str) -> list[dict[str, Optional[str]]]:
    members: list[dict[str, Optional[str]]] = []
    seen_urls: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        absolute = _abs(src, base_url)
        if absolute in seen_urls:
            continue

        alt = (img.get("alt") or "").strip()
        # Heuristiek: alt is een persoonsnaam (2-4 woorden, mostly capitalized).
        words = alt.split()
        looks_like_name = (
            2 <= len(words) <= 4
            and sum(1 for w in words if w[:1].isupper()) >= len(words) - 1
            and all(len(w) >= 2 for w in words)
            and not any(kw in alt.lower() for kw in ("logo", "icon", "banner", "background"))
        )
        if not looks_like_name:
            continue

        nearby = _find_text_near(img)
        # Naam = alt; functie = eerste regel dichtbij die *niet* gelijk is aan de naam.
        title = next(
            (line for line in nearby if line.lower() != alt.lower() and 3 <= len(line) <= 80),
            None,
        )
        members.append({"name": alt, "title": title, "photo": absolute})
        seen_urls.add(absolute)
        if len(members) >= 10:
            break
    return members


# ─── content ────────────────────────────────────────────────────────────────

def extract_nav_items(
    soup: BeautifulSoup,
    sector: Optional[str],
    base_url: Optional[str] = None,
) -> list[str]:
    """Haalt nav-items op uit een page, met sector-specifieke fallback.

    Volgorde van zoeken:
      1. <nav> element
      2. <header> element
      3. elementen met class 'menu' / 'nav' / 'navigation'

    Per kandidaat-container: prefereer top-level structuur
    (`<ul> > <li> > <a>`) zodat mega-menu submenu-items niet meekomen, en val
    pas daarna terug op een flat anchor-scan. Filters per anchor:
      * externe links (http(s)... naar ander domein dan eigen host)
      * tekst > 25 chars / leeg / symbol-only
      * skip-to-content / screen-reader-only / aria-hidden anchors
      * duplicaten (case-insensitive)
    Containers die duidelijk een filter-/toolbar-/sorteer-UI zijn, worden
    overgeslagen. Bij geen treffer: sector-fallback (anders generic default).
    """
    own_host = (urlparse(base_url).hostname or "").lower() if base_url else None

    def _is_external(href: str) -> bool:
        href = (href or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            return False
        if not own_host:
            return False
        host = (urlparse(href).hostname or "").lower()
        if host == own_host:
            return False
        if own_host in host or host in own_host:
            return False
        return True

    def _is_skip_link(a_tag: Tag) -> bool:
        classes = " ".join(a_tag.get("class") or []).lower()
        if any(skip in classes for skip in _NAV_SKIP_CLASSES):
            return True
        if (a_tag.get("aria-hidden") or "").lower() == "true":
            return True
        return False

    def _is_noise_container(container: Tag) -> bool:
        blob = " ".join(
            filter(None, [
                " ".join(container.get("class") or []).lower(),
                (container.get("id") or "").lower(),
                (container.get("role") or "").lower(),
            ])
        )
        return any(hint in blob for hint in _NAV_NOISE_CONTAINER_HINTS)

    def _is_acceptable(text: str) -> bool:
        if not text or len(text) > 25:
            return False
        # Symbol-only / single-char items uitsluiten ('▼', '☰', '×').
        if not any(c.isalpha() for c in text):
            return False
        return True

    def _collect_top_level(container: Tag) -> list[str]:
        """Pak de eerste <a> uit elk direct <li>-kind van de eerste <ul>."""
        ul = container.find("ul")
        if ul is None:
            return []
        out: list[str] = []
        seen_local: set[str] = set()
        for li in ul.find_all("li", recursive=False):
            a = li.find("a")  # eerste anchor = top-level link
            if a is None or _is_skip_link(a):
                continue
            text = _text(a)
            if not _is_acceptable(text):
                continue
            if _is_external(a.get("href", "")):
                continue
            key = text.lower()
            if key in seen_local:
                continue
            seen_local.add(key)
            out.append(text)
            if len(out) >= 6:
                break
        return out

    def _collect_flat(container: Tag) -> list[str]:
        out: list[str] = []
        seen_local: set[str] = set()
        for a in container.find_all("a"):
            if _is_skip_link(a):
                continue
            text = _text(a)
            if not _is_acceptable(text):
                continue
            if _is_external(a.get("href", "")):
                continue
            key = text.lower()
            if key in seen_local:
                continue
            seen_local.add(key)
            out.append(text)
            if len(out) >= 6:
                break
        return out

    candidates: list[Tag] = []
    seen_containers: set[int] = set()

    def _add(container) -> None:
        if container is None or id(container) in seen_containers:
            return
        seen_containers.add(id(container))
        candidates.append(container)

    _add(soup.find("nav"))
    _add(soup.find("header"))
    for cls in ("menu", "nav", "navigation"):
        for el in soup.find_all(attrs={"class": re.compile(rf"\b{cls}\b", re.I)}):
            _add(el)

    weak: list[str] = []
    for container in candidates:
        if _is_noise_container(container):
            continue
        top = _collect_top_level(container)
        if len(top) >= 3:
            return top
        flat = _collect_flat(container)
        if len(flat) >= 3:
            return flat
        if (top or flat) and not weak:
            weak = top or flat

    if len(weak) >= 2:
        return weak

    sector_key = (sector or "").strip().lower()
    return list(NAV_FALLBACKS.get(sector_key, NAV_FALLBACK_DEFAULT))


def extract_tagline(soup: BeautifulSoup) -> Optional[str]:
    for tag_name in ("h1", "h2"):
        tag = soup.find(tag_name)
        text = _text(tag)
        if text and len(text) <= 160:
            return text
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta and meta.get("content"):
        return meta["content"].strip()[:200] or None
    return None


def extract_about(soup: BeautifulSoup) -> Optional[str]:
    # Zoek sectie met about-keyword in id/class.
    section = None
    for selector in ABOUT_KEYWORDS:
        section = soup.find(attrs={"id": re.compile(selector, re.I)}) or soup.find(
            attrs={"class": re.compile(selector, re.I)}
        )
        if section:
            break
    if not section:
        # Fallback: heading met 'over ons' / 'wie zijn wij' tekst.
        for h in soup.find_all(re.compile(r"^h[1-4]$")):
            if any(kw in _text(h).lower() for kw in ("over ons", "wie zijn wij", "about us", "ons verhaal")):
                section = h.find_parent(["section", "article", "div"]) or h.parent
                break
    if not section:
        return None
    paragraphs = [
        p for p in (_text(p_tag) for p_tag in section.find_all("p"))
        if p and len(p) >= 30  # filtert nav-links / kop-fragmenten weg
    ]
    if not paragraphs:
        # Geen echte paragrafen → waarschijnlijk een nav-item, niet de about sectie.
        return None
    joined = " ".join(paragraphs)
    return (joined[:300] + "…") if len(joined) > 300 else joined or None


def extract_services(soup: BeautifulSoup) -> list[str]:
    # Strategie 1: sectie met service-keyword, lijst-items eruit halen.
    for selector in SERVICE_KEYWORDS:
        section = soup.find(attrs={"id": re.compile(selector, re.I)}) or soup.find(
            attrs={"class": re.compile(selector, re.I)}
        )
        if not section:
            continue
        items = [
            _text(li)
            for li in section.find_all(["li", "h3", "h4"])
            if 2 <= len(_text(li)) <= 80
        ]
        items = [i for i in items if i]
        if items:
            return items[:6]

    # Strategie 2: top-level nav items.
    nav = soup.find("nav")
    if nav:
        excluded = {
            "home", "contact", "about", "over ons", "blog", "nieuws",
            "menu", "search", "login", "inloggen", "account", "winkelmand",
            "close", "skip to content",
        }
        items: list[str] = []
        for a in nav.find_all("a"):
            text = _text(a)
            if not (2 <= len(text) <= 50):
                continue
            if text.lower() in excluded:
                continue
            # Filter symbol-only links (▼ ▶ ☰ enz.) en single-char items.
            if not any(c.isalpha() for c in text) or len(text.split()[0]) < 2:
                continue
            items.append(text)
        # Pas terugvallen op nav als we minimaal 3 items hebben — anders is
        # het waarschijnlijk geen diensten-menu maar een willekeurige nav.
        if len(items) >= 3:
            return items[:6]
    return []


def extract_blog_posts(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    seen_urls: set[str] = set()
    posts: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(hint in href.lower() for hint in BLOG_PATH_HINTS):
            continue
        absolute = _abs(href, base_url)
        if absolute in seen_urls:
            continue
        # Titel: link-tekst, of inner heading.
        title = _text(a) or _text(a.find(re.compile(r"^h[1-4]$")))
        if not title or len(title) < 5:
            continue
        seen_urls.add(absolute)
        posts.append({"title": title[:160], "url": absolute})
        if len(posts) >= 3:
            break
    return posts


def extract_reviews(soup: BeautifulSoup, ld_blocks: list[dict[str, Any]]) -> list[dict[str, Optional[str]]]:
    reviews: list[dict[str, Optional[str]]] = []

    # Strategie 1: schema.org Review JSON-LD.
    for block in ld_blocks:
        t = str(block.get("@type", "")).lower()
        if t == "review":
            body = block.get("reviewBody") or block.get("description")
            author = block.get("author")
            if isinstance(author, dict):
                author = author.get("name")
            if body:
                reviews.append({"quote": str(body)[:400], "name": str(author) if author else None, "company": None})

    # Strategie 2: blockquote / div.testimonial / div.review.
    for selector in [
        "blockquote",
        {"class": re.compile(r"testimonial|review|quote", re.I)},
    ]:
        if reviews and len(reviews) >= 3:
            break
        elements = soup.find_all(selector) if isinstance(selector, str) else soup.find_all(attrs=selector)
        for el in elements:
            quote_tag = el.find("p") or el
            quote = _text(quote_tag)
            if not quote or len(quote) < 20 or len(quote) > 500:
                continue
            # Naam/bedrijf zoeken in <cite>, <footer>, kleine elementen.
            name = company = None
            attribution_tag = el.find("cite") or el.find("footer")
            if attribution_tag:
                attribution = _text(attribution_tag)
                parts = [p.strip() for p in re.split(r"[,–—|]", attribution) if p.strip()]
                if parts:
                    name = parts[0][:80]
                    if len(parts) > 1:
                        company = parts[1][:80]
            reviews.append({"quote": quote[:400], "name": name, "company": company})
            if len(reviews) >= 3:
                break
    return reviews[:3]


def extract_rating(soup: BeautifulSoup, ld_blocks: list[dict[str, Any]]) -> Optional[float]:
    # Strategie 1: JSON-LD AggregateRating.
    for block in ld_blocks:
        agg = block.get("aggregateRating") or (
            block if str(block.get("@type", "")).lower() == "aggregaterating" else None
        )
        if isinstance(agg, dict):
            value = agg.get("ratingValue")
            if value is not None:
                try:
                    return float(str(value).replace(",", "."))
                except (TypeError, ValueError):
                    pass

    # Strategie 2: microdata itemprop="ratingValue".
    meta = soup.find(attrs={"itemprop": "ratingValue"})
    if meta:
        raw = meta.get("content") or _text(meta)
        try:
            return float(raw.replace(",", "."))
        except (TypeError, ValueError):
            pass

    # Strategie 3: zichtbare tekst zoals "9.4 / 10" of "4.8/5".
    match = RATING_TEXT_RE.search(soup.get_text(" "))
    if match:
        try:
            return float(match.group(1).replace(",", "."))
        except (TypeError, ValueError):
            pass
    return None


# ─── contact ────────────────────────────────────────────────────────────────

def extract_phone(soup: BeautifulSoup, text: str) -> Optional[str]:
    # `tel:` link is meest betrouwbaar.
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            return a["href"].split("tel:", 1)[1].strip() or None
    for match in PHONE_RE.finditer(text):
        cleaned = re.sub(r"\s+", " ", match.group(0)).strip()
        digits = re.sub(r"\D", "", cleaned)
        # Nederlandse nummers: 0X-XXXXXXXX = 10 digits, +31 variant = 11.
        if 10 <= len(digits) <= 12:
            return cleaned
    return None


def extract_email(soup: BeautifulSoup, text: str) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            return a["href"].split("mailto:", 1)[1].split("?")[0].strip() or None
    match = EMAIL_RE.search(text)
    return match.group(0) if match else None


def extract_address(soup: BeautifulSoup, ld_blocks: list[dict[str, Any]], text: str) -> Optional[str]:
    # Strategie 1: JSON-LD PostalAddress / Organization.address.
    for block in ld_blocks:
        addr = block.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress"),
                addr.get("postalCode"),
                addr.get("addressLocality"),
            ]
            joined = ", ".join(p for p in parts if p)
            if joined:
                return joined

    # Strategie 2: microdata.
    item = soup.find(attrs={"itemtype": re.compile(r"PostalAddress$", re.I)})
    if item:
        parts = []
        for prop in ("streetAddress", "postalCode", "addressLocality"):
            t = item.find(attrs={"itemprop": prop})
            if t:
                parts.append(_text(t))
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined

    # Strategie 3: regex op straat + postcode in volgorde.
    street = STREET_RE.search(text)
    postal = POSTAL_RE.search(text)
    if street and postal:
        return f"{street.group(0)}, {postal.group(0)}"
    if street:
        return street.group(0)
    return None


def extract_opening_hours(soup: BeautifulSoup, ld_blocks: list[dict[str, Any]], text: str) -> list[str]:
    # JSON-LD OpeningHoursSpecification.
    for block in ld_blocks:
        oh = block.get("openingHours") or block.get("openingHoursSpecification")
        if isinstance(oh, str):
            return [oh]
        if isinstance(oh, list):
            out: list[str] = []
            for item in oh:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    days = item.get("dayOfWeek")
                    opens = item.get("opens")
                    closes = item.get("closes")
                    if days and opens and closes:
                        if isinstance(days, list):
                            days = "/".join(str(d).split("/")[-1] for d in days)
                        out.append(f"{days} {opens}-{closes}")
            if out:
                return out

    # Tekstpatroon "ma-vr 09:00-17:00".
    pattern = re.compile(
        r"\b(ma|di|wo|do|vr|za|zo)(?:[\-–](ma|di|wo|do|vr|za|zo))?\s+\d{1,2}[:.]\d{2}\s*[\-–]\s*\d{1,2}[:.]\d{2}",
        re.IGNORECASE,
    )
    hits = [m.group(0).strip() for m in pattern.finditer(text)]
    return hits[:7]


def extract_socials(soup: BeautifulSoup) -> dict[str, Optional[str]]:
    socials: dict[str, Optional[str]] = {k: None for k in SOCIAL_DOMAINS}
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if href in seen:
            continue
        for platform, domains in SOCIAL_DOMAINS.items():
            if socials[platform]:
                continue
            if any(d in low for d in domains):
                socials[platform] = href
                seen.add(href)
                break
    return socials


# ─── extra ──────────────────────────────────────────────────────────────────

def extract_founded_year(text: str) -> Optional[int]:
    match = FOUNDED_RE.search(text)
    if match:
        try:
            year = int(match.group(1))
            if 1800 <= year <= datetime.now().year:
                return year
        except ValueError:
            pass
    return None


def extract_certifications(soup: BeautifulSoup, text: str) -> list[str]:
    found: list[str] = []
    lower = text.lower()
    for kw in CERT_KEYWORDS:
        idx = lower.find(kw)
        if idx == -1:
            continue
        # Pak een snipper rond de match (max 80 chars) zodat we context bewaren.
        snippet = text[max(0, idx - 20): idx + 80].strip()
        snippet = re.sub(r"\s+", " ", snippet)
        if snippet and snippet not in found:
            found.append(snippet)
        if len(found) >= 5:
            break
    # Alt-tekst van mogelijke badge-images.
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        low = alt.lower()
        if any(kw in low for kw in CERT_KEYWORDS) and alt not in found:
            found.append(alt[:120])
            if len(found) >= 5:
                break
    return found


# ─── orchestratie ───────────────────────────────────────────────────────────

def scrape_site_data(url: str, sector: Optional[str] = None) -> dict[str, Any]:
    """Haal alle data van een leadwebsite en geef een nested dict terug.

    Crasht nooit: iedere extractor zit in een isolerende `_safe` wrapper.
    Ontbrekende velden krijgen `null`. `sector` voedt de fallback-lijst van
    `extract_nav_items` als er geen echte nav-items gevonden worden.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"}
    start = time.monotonic()
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
    laadtijd_ms = int((time.monotonic() - start) * 1000)
    resp.raise_for_status()
    html = resp.text
    final_url = resp.url

    soup = BeautifulSoup(html, "html.parser")
    page_text = " ".join(soup.stripped_strings)
    ld_blocks = _safe(_collect_json_ld, soup, default=[]) or []
    nav_items = _safe(extract_nav_items, soup, sector, final_url, default=[]) or []

    visual = {
        "logo": _safe(extract_logo, soup, final_url),
        "favicon": _safe(extract_favicon, soup, final_url),
        "colors": _safe(extract_color_palette, html, soup, final_url, default={"primary": None, "secondary": None, "background": None}),
        "fonts": _safe(extract_fonts, soup, default={"families": None, "google_fonts_url": None}),
    }
    team = {
        "hero_photo": _safe(extract_team_hero, soup, final_url),
        "members": _safe(extract_team_members, soup, final_url, default=[]) or [],
    }
    content = {
        "tagline": _safe(extract_tagline, soup),
        "about": _safe(extract_about, soup),
        "services": _safe(extract_services, soup, default=[]) or [],
        "blog_posts": _safe(extract_blog_posts, soup, final_url, default=[]) or [],
        "reviews": _safe(extract_reviews, soup, ld_blocks, default=[]) or [],
        "rating": _safe(extract_rating, soup, ld_blocks),
        "nav_items": nav_items,
    }
    contact = {
        "phone": _safe(extract_phone, soup, page_text),
        "email": _safe(extract_email, soup, page_text),
        "address": _safe(extract_address, soup, ld_blocks, page_text),
        "opening_hours": _safe(extract_opening_hours, soup, ld_blocks, page_text, default=[]) or [],
        "socials": _safe(extract_socials, soup, default={k: None for k in SOCIAL_DOMAINS}),
    }
    extra = {
        "founded_year": _safe(extract_founded_year, page_text),
        "certifications": _safe(extract_certifications, soup, page_text, default=[]) or [],
    }

    return {
        "url": url,
        "final_url": final_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "laadtijd_ms": laadtijd_ms,
        "sector": sector,
        "visual": visual,
        "team": team,
        "content": content,
        "contact": contact,
        "extra": extra,
    }


def save_data(slug: str, data: dict[str, Any]) -> Path:
    target_dir = DATA_ROOT / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "data.json"
    target_file.write_text(
        json.dumps({"slug": slug, **data}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target_file


def git_commit_push(slug: str, target_file: Path) -> None:
    rel = target_file.relative_to(REPO_ROOT)
    subprocess.run(["git", "-C", str(REPO_ROOT), "add", str(rel)], check=True)
    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet"]
    )
    if diff.returncode == 0:
        print(f"⏭ geen wijzigingen voor {slug} — commit overgeslagen.")
        return
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "commit", "-m", f"data: {slug}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(REPO_ROOT), "push"], check=True)


def verify_deploy(slug: str, timeout_seconds: int = DEPLOY_TIMEOUT_S) -> bool:
    """Poll de Vercel-deploy van een slug tot HTTP 200 of timeout."""
    url = f"{DEPLOY_BASE_URL}/demo/{slug}/"
    print(f"🔎 verify_deploy: {url}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        elapsed = int(timeout_seconds - (deadline - time.monotonic()))
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
            if resp.status_code == 200:
                print(f"✅ Demo live op {url}")
                return True
            print(f"  wachten op deploy... ({elapsed}s)  status={resp.status_code}")
        except requests.RequestException as exc:
            print(f"  wachten op deploy... ({elapsed}s)  err={type(exc).__name__}")
        time.sleep(DEPLOY_POLL_INTERVAL_S)
    print(f"⚠ verify_deploy: timeout na {timeout_seconds}s — {url}")
    return False


def _summary(data: dict[str, Any]) -> None:
    visual = data["visual"]
    team = data["team"]
    content = data["content"]
    contact = data["contact"]
    extra = data["extra"]
    print(f"  logo={visual['logo'] or '—'}")
    print(f"  colors: primary={visual['colors']['primary']}  secondary={visual['colors']['secondary']}  bg={visual['colors']['background']}")
    print(f"  fonts={visual['fonts']['families']}")
    print(f"  team_hero={team['hero_photo'] or '—'}  members={len(team['members'])}")
    print(f"  tagline={content['tagline'] or '—'}")
    print(f"  services={len(content['services'])}  blog={len(content['blog_posts'])}  reviews={len(content['reviews'])}  rating={content['rating']}")
    print(f"  nav_items={content['nav_items']}")
    print(f"  phone={contact['phone'] or '—'}  email={contact['email'] or '—'}")
    print(f"  address={contact['address'] or '—'}  hours={len(contact['opening_hours'])}")
    socials_set = [k for k, v in contact["socials"].items() if v]
    print(f"  socials={socials_set or '—'}")
    print(f"  founded={extra['founded_year']}  certs={len(extra['certifications'])}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Demo data scraper")
    parser.add_argument("--url", required=True, help="Lead-URL (volledige URL)")
    parser.add_argument("--slug", help="Slug onder data/ (default: domeinnaam)")
    parser.add_argument(
        "--sector",
        required=True,
        help="Sector ('makelaardij', 'tandartsen', …) — voedt de nav fallback.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Sla git add/commit/push over (alleen JSON wegschrijven).",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Sla de verify_deploy poll over (handig zolang templates nog niet renderen).",
    )
    args = parser.parse_args(argv)

    slug = slugify(args.slug) if args.slug else slug_from_url(args.url)
    print(f"🔎 scrape {args.url}  slug={slug}")
    try:
        data = scrape_site_data(args.url, sector=args.sector)
    except requests.RequestException as exc:
        print(f"⚠ request mislukt: {exc}", file=sys.stderr)
        return 1
    _summary(data)

    target = save_data(slug, data)
    print(f"💾 data opgeslagen → {target.relative_to(REPO_ROOT)}")

    if args.no_push:
        return 0
    try:
        git_commit_push(slug, target)
    except subprocess.CalledProcessError as exc:
        print(f"⚠ git stap mislukt: {exc}", file=sys.stderr)
        return 1

    if args.skip_verify:
        return 0
    index_file = PUBLIC_ROOT / "demo" / slug / "index.html"
    if not index_file.exists():
        print(
            f"⏭ verify_deploy overgeslagen — geen {index_file.relative_to(REPO_ROOT)} "
            f"(template fase nog niet gestart)."
        )
        return 0
    verify_deploy(slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
