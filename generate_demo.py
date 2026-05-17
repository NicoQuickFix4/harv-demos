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

# Fase-2 path-scraping doelen
BLOG_PATHS = ("/blog", "/blog/", "/nieuws", "/nieuws/", "/artikelen", "/updates")
RSS_PATHS = ("/feed", "/feed/", "/rss.xml", "/rss")
WONING_PATHS = ("/aanbod", "/aanbod/", "/woningen", "/woningen/", "/te-koop", "/koopwoningen")
TEAM_PATHS = ("/team", "/team/", "/over-ons", "/over-ons/", "/over", "/medewerkers", "/mensen")

WONING_STATUS_PATTERNS = (
    (re.compile(r"\bverkocht\b", re.I), "Verkocht"),
    (re.compile(r"\bonder\s*bod\b", re.I), "Onder bod"),
    (re.compile(r"\bbeschikbaar\b", re.I), "Beschikbaar"),
    (re.compile(r"\bte\s*koop\b", re.I), "Te koop"),
    (re.compile(r"\bte\s*huur\b", re.I), "Te huur"),
    (re.compile(r"\bnieuw\b", re.I), "Nieuw"),
)
PRICE_RE = re.compile(r"€\s*[\d.,]+(?:\s*(?:k|kr|/mnd|p/?m))?", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(\d{1,2}\s+(?:jan(?:uari)?|feb(?:ruari)?|maart|apr(?:il)?|mei|juni?|juli?|aug(?:ustus)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{4})\b",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
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
        candidate = _normalize_hex(meta["content"])
        # Accept theme-color alleen als 'ie ook echt een brand-kleur is —
        # near-white / near-black / grayscale skippen we zodat de ranked search
        # alsnog een betere brandkleur kan vinden.
        if (
            candidate
            and not _is_grayscale(candidate)
            and 25 <= _brightness(candidate) <= 235
        ):
            palette["primary"] = candidate

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

# ─── Fase-2 path-scraping ───────────────────────────────────────────────────

def _fetch_path(base_url: str, path: str) -> Optional[BeautifulSoup]:
    """GET base_url + path en geef parsed soup terug, of None bij faal/404."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"}
    parsed = urlparse(base_url)
    full = f"{parsed.scheme}://{parsed.netloc}{path}"
    try:
        resp = requests.get(full, headers=headers, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    return BeautifulSoup(resp.text, "html.parser")


def extract_bedrijfsnaam(soup: BeautifulSoup, url: str) -> Optional[str]:
    """Naam van het bedrijf — og:site_name > <title> opgeschoond > hostname slug."""
    og = soup.find("meta", attrs={"property": re.compile(r"^og:site_name$", re.I)})
    if og and og.get("content"):
        return og["content"].strip()
    title = soup.find("title")
    if title and title.get_text(strip=True):
        text = title.get_text(strip=True)
        # Veel sites zetten "Naam | Tagline" of "Naam - Stad" — pak deel vóór separator.
        for sep in ("|", " - ", " — ", " · ", "::"):
            if sep in text:
                left = text.split(sep, 1)[0].strip()
                if 2 <= len(left) <= 80:
                    return left
        if 2 <= len(text) <= 80:
            return text
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if host:
        return host.split(".")[0].capitalize()
    return None


def extract_homepage_tagline(soup: BeautifulSoup) -> Optional[str]:
    """<h1> als <80 chars, anders meta description."""
    h1 = soup.find("h1")
    if h1:
        text = _text(h1)
        if text and len(text) < 80:
            return text
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta and meta.get("content"):
        return meta["content"].strip() or None
    return None


def _extract_blog_post_card(item: Tag, base_url: str) -> Optional[dict[str, Any]]:
    """Probeer een blog-card te parsen tot titel/datum/samenvatting/afbeelding."""
    # Titel: eerste heading of link met tekst
    title_tag = item.find(re.compile(r"^h[1-4]$")) or item.find("a")
    if not title_tag:
        return None
    title = _text(title_tag)
    if not title or len(title) < 5:
        return None
    # Datum: <time datetime="..."> of zichtbare tekst
    datum: Optional[str] = None
    time_tag = item.find("time")
    if time_tag:
        datum = (time_tag.get("datetime") or _text(time_tag) or "").strip() or None
    if not datum:
        item_text = item.get_text(" ", strip=True)
        m = ISO_DATE_RE.search(item_text) or DATE_RE.search(item_text)
        if m:
            datum = m.group(0)
    # Samenvatting: eerste <p> ≥ 30 chars, anders item-tekst
    samenvatting: Optional[str] = None
    for p in item.find_all("p"):
        text = _text(p)
        if text and len(text) >= 30:
            samenvatting = text
            break
    if not samenvatting:
        all_text = item.get_text(" ", strip=True)
        if title in all_text:
            all_text = all_text.replace(title, "", 1).strip()
        samenvatting = all_text.strip() or None
    if samenvatting:
        samenvatting = samenvatting[:150]
    # Afbeelding
    afbeelding: Optional[str] = None
    img = item.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            afbeelding = _abs(src, base_url)
    return {
        "titel": title[:160],
        "datum": datum,
        "samenvatting": samenvatting,
        "afbeelding": afbeelding,
    }


def _parse_rss_items(xml_text: str, base_url: str) -> list[dict[str, Any]]:
    """Pak titel/datum/samenvatting/afbeelding uit een RSS-feed (BS4 'xml')."""
    soup = BeautifulSoup(xml_text, "xml")
    posts: list[dict[str, Any]] = []
    for item in soup.find_all(re.compile(r"^(item|entry)$"))[:3]:
        title_tag = item.find(re.compile(r"^title$"))
        title = title_tag.get_text(strip=True) if title_tag else ""
        if not title:
            continue
        date_tag = item.find(re.compile(r"^(pubDate|published|updated|dc:date)$"))
        datum = date_tag.get_text(strip=True) if date_tag else None
        desc_tag = item.find(re.compile(r"^(description|summary|content)$"))
        samenvatting = None
        if desc_tag:
            raw = desc_tag.get_text(" ", strip=True)
            samenvatting = (BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))[:150]
        afbeelding = None
        for media in item.find_all(re.compile(r"^(media:content|enclosure|media:thumbnail)$")):
            url = media.get("url") or media.get("href")
            if url:
                afbeelding = _abs(url, base_url)
                break
        if not afbeelding and desc_tag:
            inner = BeautifulSoup(desc_tag.get_text(" ", strip=True), "html.parser")
            img = inner.find("img")
            if img and img.get("src"):
                afbeelding = _abs(img["src"], base_url)
        posts.append({
            "titel": title[:160],
            "datum": datum,
            "samenvatting": samenvatting,
            "afbeelding": afbeelding,
        })
    return posts


def scrape_blog_posts(base_url: str) -> list[dict[str, Any]]:
    """Probeer /blog, /nieuws, /artikelen, /updates en RSS — max 3 posts."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"}
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Eerste: HTML blog/nieuws paginas
    for path in BLOG_PATHS:
        soup = _fetch_path(base_url, path)
        if soup is None:
            continue
        items: list[Tag] = []
        for tag_name in ("article", "li"):
            items.extend(soup.find_all(tag_name))
        # Filter naar items die plausibel blog-cards zijn (hebben heading + img of <p>)
        plausible: list[Tag] = []
        for it in items:
            if it.find(re.compile(r"^h[1-4]$")) and (it.find("img") or it.find("p")):
                plausible.append(it)
            if len(plausible) >= 8:
                break
        posts: list[dict[str, Any]] = []
        for it in plausible:
            parsed_post = _extract_blog_post_card(it, base_url)
            if parsed_post:
                posts.append(parsed_post)
            if len(posts) >= 3:
                break
        if posts:
            return posts

    # Daarna: RSS
    for path in RSS_PATHS:
        try:
            resp = requests.get(origin + path, headers=headers, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException:
            continue
        if not resp.ok or "<rss" not in resp.text[:1024].lower() and "<feed" not in resp.text[:1024].lower():
            continue
        rss_posts = _parse_rss_items(resp.text, base_url)
        if rss_posts:
            return rss_posts[:3]
    return []


def _detect_status(text: str) -> str:
    for pattern, label in WONING_STATUS_PATTERNS:
        if pattern.search(text):
            return label
    return "Beschikbaar"


def _detect_plaats(text: str) -> Optional[str]:
    """Vind een waarschijnlijke plaatsnaam — laatste 'Naam, Stad' patroon."""
    # 'Straatnaam 12, 1234 AB Stad' — pak deel achter de postcode of komma.
    m = re.search(r",\s*(?:\d{4}\s?[A-Z]{2}\s+)?([A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)*)\b", text)
    if m:
        plaats = m.group(1).strip()
        if 2 <= len(plaats) <= 40:
            return plaats
    return None


def _extract_woning_card(item: Tag, base_url: str) -> Optional[dict[str, Any]]:
    img = item.find("img")
    if img is None:
        return None
    src = img.get("src") or img.get("data-src") or ""
    if not src or src.startswith("data:"):
        return None
    text = item.get_text(" ", strip=True)
    price_match = PRICE_RE.search(text)
    if not price_match:
        return None
    return {
        "foto": _abs(src, base_url),
        "prijs": re.sub(r"\s+", " ", price_match.group(0).strip()),
        "status": _detect_status(text),
        "plaats": _detect_plaats(text),
    }


def scrape_woningaanbod(base_url: str) -> list[dict[str, Any]]:
    """Probeer /aanbod, /woningen, /te-koop, /koopwoningen — max 6 woningen."""
    for path in WONING_PATHS:
        soup = _fetch_path(base_url, path)
        if soup is None:
            continue
        candidates: list[Tag] = []
        for sel in (
            {"class": re.compile(r"(woning|listing|property|huis|object)", re.I)},
            {"itemtype": re.compile(r"(Residence|Product|Offer)", re.I)},
        ):
            for el in soup.find_all(attrs=sel):
                if el.find("img"):
                    candidates.append(el)
            if candidates:
                break
        # Fallback: <article> met img + €
        if not candidates:
            for art in soup.find_all("article"):
                if art.find("img") and "€" in art.get_text(""):
                    candidates.append(art)
        # Fallback 2: alle <a> met img + € in tekst
        if not candidates:
            for a in soup.find_all("a"):
                if a.find("img") and "€" in a.get_text(""):
                    candidates.append(a)

        woningen: list[dict[str, Any]] = []
        seen_imgs: set[str] = set()
        for c in candidates:
            parsed = _extract_woning_card(c, base_url)
            if not parsed:
                continue
            if parsed["foto"] in seen_imgs:
                continue
            seen_imgs.add(parsed["foto"])
            woningen.append(parsed)
            if len(woningen) >= 6:
                break
        if woningen:
            return woningen
    return []


def _image_portrait_ratio(img: Tag) -> Optional[float]:
    """h/w ratio uit width/height attrs; None als onbekend."""
    try:
        w = int(img.get("width") or 0)
        h = int(img.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return h / w


def _looks_like_person_name(text: str) -> bool:
    if not text:
        return False
    words = text.split()
    if not (2 <= len(words) <= 4):
        return False
    if not all(len(w) >= 2 for w in words):
        return False
    low = text.lower()
    if any(kw in low for kw in (
        "logo", "banner", "icon", "background", "header", "placeholder",
        "we are", "wij zijn", "ons team", "our team",
        "real estate", "estate", "makelaar",
        "youtube", "video", "preview", "thumbnail", "screenshot",
    )):
        return False
    # Eerste woord moet een persoonsnaam-vorm hebben (geen lidwoord/voornaamwoord).
    first = words[0].lower()
    if first in ("we", "wij", "ons", "onze", "the", "a", "our"):
        return False
    return sum(1 for w in words if w[:1].isupper()) >= len(words) - 1


def scrape_team_members(base_url: str) -> list[dict[str, Any]]:
    """Probeer /team, /over-ons, /over, /medewerkers, /mensen — max 6 personen.

    Filter op portrait-ratio > 0.6 (height/width). Onbekende dims worden niet
    geweerd, maar landscape-banners wel.
    """
    for path in TEAM_PATHS:
        soup = _fetch_path(base_url, path)
        if soup is None:
            continue
        members: list[dict[str, Any]] = []
        seen: set[str] = set()
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or src.startswith("data:"):
                continue
            absolute = _abs(src, base_url)
            if absolute in seen:
                continue

            # Skip logo/favicon-images — die zijn nooit teamportret.
            low_src = absolute.lower()
            classes = " ".join(img.get("class") or []).lower()
            if any(kw in low_src for kw in ("logo", "favicon", "brand")):
                continue
            if any(kw in classes for kw in ("logo", "brand")):
                continue

            # Portrait filter — alleen weren als we de ratio weten en hij is te plat.
            ratio = _image_portrait_ratio(img)
            if ratio is not None and ratio < 0.6:
                continue

            alt = (img.get("alt") or "").strip()
            naam: Optional[str] = alt if _looks_like_person_name(alt) else None
            if not naam:
                # Probeer naam in container te vinden (figcaption / korte heading)
                container = img.find_parent(["figure", "article", "li", "div", "section"])
                if container:
                    for tag in container.find_all(["figcaption", "h3", "h4", "h5", "strong", "span"]):
                        candidate = _text(tag)
                        if _looks_like_person_name(candidate):
                            naam = candidate
                            break
            if not naam:
                continue

            functie: Optional[str] = None
            container = img.find_parent(["figure", "article", "li", "div", "section"])
            if container:
                for piece in container.stripped_strings:
                    if piece.strip().lower() == naam.lower():
                        continue
                    if 3 <= len(piece) <= 80 and not _looks_like_person_name(piece):
                        functie = piece.strip()
                        break

            members.append({"foto": absolute, "naam": naam, "functie": functie})
            seen.add(absolute)
            if len(members) >= 6:
                break
        if members:
            return members
    return []


# ─── HTML renderers ─────────────────────────────────────────────────────────

def _html_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _status_class(status: Optional[str]) -> str:
    if not status:
        return "t-koop"
    s = status.lower()
    if "verkocht" in s:
        return "t-vkcht"
    if "bod" in s:
        return "t-bod"
    if "huur" in s:
        return "t-huur"
    return "t-koop"


TRACKING_DOMAIN = __import__("os").environ.get("TRACKING_DOMAIN", "localhost:5050")


def build_tracking_pixel_url(lead_id: str, slug: str) -> str:
    """Bouw de tracking-pixel URL voor event='open'.

    Gebruikt `TRACKING_DOMAIN` uit `.env`. Voor `localhost` (en `127.0.0.1`)
    wordt http gebruikt, anders https.
    """
    from urllib.parse import quote_plus
    scheme = "http" if TRACKING_DOMAIN.startswith(("localhost", "127.0.0.1")) else "https"
    return (
        f"{scheme}://{TRACKING_DOMAIN}/track-demo?"
        f"lead_id={quote_plus(lead_id)}&event=open&slug={quote_plus(slug)}"
    )


def build_nav_html(nav_items: list[str]) -> str:
    """Genereer aaneengesloten `<a href="#">tekst</a>` tags voor de nav-bar."""
    if not nav_items:
        return ""
    return "".join(f'<a href="#">{_html_escape(item)}</a>' for item in nav_items)


def build_woningaanbod_html(
    woningen: list[dict[str, Any]],
    primaire_kleur: Optional[str] = None,
) -> str:
    """Genereer .prop-card markup voor het woningaanbod-grid.

    `primaire_kleur` wordt — als opgegeven — gebruikt voor de inline kleur van
    de prijs-regel zodat de kaartjes altijd 'gebrand' kleuren, óók als de
    template-CSS-variabele niet correct gevuld is.
    """
    if not woningen:
        return ""
    price_style = ""
    if primaire_kleur:
        price_style = f' style="color: {_html_escape(primaire_kleur)};"'
    cards: list[str] = []
    for w in woningen:
        foto = _html_escape(w.get("foto") or "")
        prijs = _html_escape(w.get("prijs") or "")
        status = w.get("status") or "Te koop"
        plaats = _html_escape(w.get("plaats") or "")
        img_tag = f'<img src="{foto}" alt="">' if foto else ""
        cards.append(
            '<div class="prop-card">'
            f'<div class="prop-img-wrap">{img_tag}'
            f'<span class="prop-tag {_status_class(status)}">{_html_escape(status)}</span></div>'
            '<div class="prop-body">'
            f'<div class="prop-price"{price_style}>{prijs}</div>'
            f'<div class="prop-addr">{plaats}</div>'
            '</div></div>'
        )
    return "\n        ".join(cards)


def build_team_html(team: list[dict[str, Any]]) -> str:
    """Genereer .team-card markup."""
    if not team:
        return ""
    cards: list[str] = []
    for p in team:
        foto = _html_escape(p.get("foto") or "")
        naam = _html_escape(p.get("naam") or "")
        functie = _html_escape(p.get("functie") or "")
        img_tag = f'<img src="{foto}" alt="{naam}">' if foto else ""
        cards.append(
            '<div class="team-card">'
            f'{img_tag}'
            '<div class="team-info">'
            f'<div class="team-name">{naam}</div>'
            f'<div class="team-role">{functie}</div>'
            '</div></div>'
        )
    return "\n        ".join(cards)


def build_blog_html(blog_posts: list[dict[str, Any]]) -> str:
    """Genereer .blog-card markup (matchend met de bestaande template-CSS)."""
    if not blog_posts:
        return ""
    cards: list[str] = []
    for post in blog_posts:
        afbeelding = _html_escape(post.get("afbeelding") or "")
        titel = _html_escape(post.get("titel") or "")
        datum = _html_escape(post.get("datum") or "")
        samenvatting = _html_escape(post.get("samenvatting") or "")
        cards.append(
            '<a href="#" class="blog-card">'
            + (f'<img src="{afbeelding}" alt="">' if afbeelding else "")
            + '<div class="blog-body">'
            + (f'<div class="blog-cat">{datum}</div>' if datum else "")
            + f'<div class="blog-title">{titel}</div>'
            + (f'<div class="blog-snip">{samenvatting}</div>' if samenvatting else "")
            + '</div></a>'
        )
    return "\n        ".join(cards)


# Backwards compat: oude `render_*` namen blijven werken als aliassen.
render_woningaanbod_html = build_woningaanbod_html
render_team_html = build_team_html
render_blog_html = build_blog_html


# ─── Fase-2 orchestrator ────────────────────────────────────────────────────

def scrape_full_site_data(url: str, sector: str) -> dict[str, Any]:
    """Fase-2 scraper: levert een vlakke JSON-structuur klaar voor templating.

    Volgorde:
      1. homepage ophalen + basisvelden (kleur, logo, tagline, nav, team-hero)
      2. path-scrapes voor blog/woningen/team
      3. samenstellen tot de gespecificeerde JSON-vorm
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    final_url = resp.url
    soup = BeautifulSoup(html, "html.parser")

    palette = _safe(extract_color_palette, html, soup, final_url, default={"primary": None})
    primaire_kleur = (palette or {}).get("primary")
    logo_url = _safe(extract_logo, soup, final_url)
    if not logo_url:
        # Header-first-image fallback per spec
        header = soup.find("header")
        if header:
            img = header.find("img")
            if img and (img.get("src") or img.get("data-src")):
                logo_url = _abs(img.get("src") or img.get("data-src"), final_url)
    if not logo_url:
        logo_url = _safe(extract_favicon, soup, final_url)

    teamfoto_url = _safe(extract_team_hero, soup, final_url)
    tagline = _safe(extract_homepage_tagline, soup)
    nav_items = _safe(extract_nav_items, soup, sector, final_url, default=[]) or []
    bedrijfsnaam = _safe(extract_bedrijfsnaam, soup, final_url)

    blog_posts = _safe(scrape_blog_posts, final_url, default=[]) or []
    woningaanbod = (
        _safe(scrape_woningaanbod, final_url, default=[]) or []
        if (sector or "").lower() == "makelaardij"
        else []
    )
    team = _safe(scrape_team_members, final_url, default=[]) or []

    return {
        "bedrijfsnaam": bedrijfsnaam,
        "primaire_kleur": primaire_kleur,
        "logo_url": logo_url,
        "teamfoto_url": teamfoto_url,
        "tagline": tagline,
        "nav_items": nav_items,
        "blog_posts": blog_posts,
        "woningaanbod": woningaanbod,
        "team": team,
    }


# ─── Notion update ──────────────────────────────────────────────────────────

_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def update_notion_after_demo(notion_page_id: Optional[str], demo_url: str) -> bool:
    """Werk de Notion-pagina van de lead bij na een succesvolle demo-deploy.

    Returns True bij HTTP 200, False bij faal of overgeslagen.
    """
    if not notion_page_id:
        print("⚠ update_notion_after_demo: geen notion_page_id meegegeven — skip.")
        return False
    import os
    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY", "")
    if not token:
        print("⚠ update_notion_after_demo: NOTION_TOKEN ontbreekt — skip.")
        return False
    payload = {
        "properties": {
            "Demo-link": {"url": demo_url},
            "Fase": {"select": {"name": "Demo build"}},
            "Demo Approved": {"select": {"name": "❌ Niet goedgekeurd"}},
        }
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": _NOTION_VERSION,
    }
    try:
        resp = requests.patch(
            f"{_NOTION_API_BASE}/pages/{notion_page_id}",
            headers=headers,
            json=payload,
            timeout=20,
        )
        if resp.status_code == 200:
            print(f"✅ Notion bijgewerkt: pagina {notion_page_id[:8]}…  Demo-link={demo_url}")
            return True
        print(
            f"⚠ Notion update faalde: HTTP {resp.status_code} — body: {resp.text[:300]}"
        )
    except requests.RequestException as exc:
        print(f"⚠ Notion update exception: {exc}")
    return False


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


def _summary_full(data: dict[str, Any]) -> None:
    print(f"  bedrijfsnaam={data.get('bedrijfsnaam')}")
    print(f"  primaire_kleur={data.get('primaire_kleur')}  logo_url={data.get('logo_url')}")
    print(f"  tagline={data.get('tagline')}")
    print(f"  nav_items={data.get('nav_items')}")
    print(f"  blog_posts={len(data.get('blog_posts') or [])}  "
          f"woningaanbod={len(data.get('woningaanbod') or [])}  "
          f"team={len(data.get('team') or [])}")


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
        help="Sector (gebruik 'makelaardij') — voedt de nav fallback.",
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
    parser.add_argument(
        "--notion-page-id",
        help="Notion page id van de lead — wordt na verify_deploy bijgewerkt.",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Gebruik de oude nested scrape_site_data ipv de Fase-2 scrape_full_site_data.",
    )
    args = parser.parse_args(argv)

    slug = slugify(args.slug) if args.slug else slug_from_url(args.url)
    print(f"🔎 scrape {args.url}  slug={slug}")
    try:
        if args.legacy:
            data = scrape_site_data(args.url, sector=args.sector)
            _summary(data)
        else:
            data = scrape_full_site_data(args.url, sector=args.sector)
            _summary_full(data)
    except requests.RequestException as exc:
        print(f"⚠ request mislukt: {exc}", file=sys.stderr)
        return 1

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
    if verify_deploy(slug):
        update_notion_after_demo(
            args.notion_page_id, f"{DEPLOY_BASE_URL}/demo/{slug}/"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
