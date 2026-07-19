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
import os
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

DEPLOY_BASE_URL = "https://harvagency.com"
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


_NOINDEX_META = ('<meta name="robots" content="noindex,nofollow,noarchive">'
                 '<meta name="googlebot" content="noindex,nofollow">')


def _ensure_noindex(html: str) -> str:
    """Zorg dat elke demo-pagina `noindex,nofollow` heeft.

    Per-lead demo's zijn niet bedoeld voor Google: indexeren van honderden
    bijna-identieke pagina's schaadt de SEO van de hoofdsite en lekt klant-demo's
    naar de zoekresultaten. We injecteren de meta-tags direct na <head>.
    """
    if "noindex" in html.lower():
        return html
    import re
    m = re.search(r"<head[^>]*>", html, flags=re.IGNORECASE)
    if m:
        return html[: m.end()] + _NOINDEX_META + html[m.end():]
    # Geen <head>? Plak het vooraan zodat het in elk geval aanwezig is.
    return _NOINDEX_META + html


def build_tracking_html(lead_id: str, slug: str) -> str:
    """Volledige tracking-snippet voor een demo-pagina.

    Bevat: (1) open-pixel als <img> (werkt ook zonder JS), en (2) een klein
    script dat scroll_50/scroll_100, cta_click en duration (tijd-op-pagina)
    meet en via `navigator.sendBeacon` naar /track-demo stuurt. sendBeacon met
    URLSearchParams = een CORS-'simple request', dus geen preflight nodig.
    """
    from urllib.parse import quote_plus
    scheme = "http" if TRACKING_DOMAIN.startswith(("localhost", "127.0.0.1")) else "https"
    base = f"{scheme}://{TRACKING_DOMAIN}/track-demo"
    px = build_tracking_pixel_url(lead_id, slug)
    lid = _html_escape(lead_id)
    sg = _html_escape(slug)
    img = (f'<img src="{px}" width="1" height="1" alt="" '
           f'style="position:absolute;left:-9999px;top:auto" />')
    script = f"""<script>
(function(){{
  var URL="{base}",LID={lid!r},SG={sg!r},t0=Date.now(),sent={{}};
  function beacon(ev,val){{
    try{{
      var p=new URLSearchParams({{lead_id:LID,slug:SG,event:ev}});
      if(val!=null)p.set('value',val);
      navigator.sendBeacon(URL,p);
    }}catch(e){{}}
  }}
  function onScroll(){{
    var h=document.documentElement,b=document.body;
    var st=h.scrollTop||b.scrollTop,sh=(h.scrollHeight||b.scrollHeight)-h.clientHeight;
    var pct=sh>0?(st/sh*100):0;
    if(pct>=50&&!sent.s50){{sent.s50=1;beacon('scroll_50');}}
    if(pct>=90&&!sent.s100){{sent.s100=1;beacon('scroll_100');}}
  }}
  window.addEventListener('scroll',onScroll,{{passive:true}});
  document.addEventListener('click',function(e){{
    var el=e.target.closest&&e.target.closest('#quote-widget-mount,a[href*="cal.com"],[data-cta],.harv-cta,.qw-submit,.book,.cta');
    if(el&&!sent.cta){{sent.cta=1;beacon('cta_click');}}
  }},true);
  function flush(){{
    if(sent.dur)return;sent.dur=1;
    beacon('duration',Math.round((Date.now()-t0)/1000));
  }}
  document.addEventListener('visibilitychange',function(){{
    if(document.visibilityState==='hidden')flush();
  }});
  window.addEventListener('pagehide',flush);
}})();
</script>"""
    return img + script


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


# ─── Template-render-stap ────────────────────────────────────────────────────

TEMPLATES_ROOT = REPO_ROOT / "templates"
# Sector → template-bestand. Voeg hier een regel toe als een sector-template klaar is.
SECTOR_TEMPLATES: dict[str, str] = {
    # makelaardij-templates zijn verwijderd (scope = dakdekkers); voeg een
    # sector pas weer toe zodra zijn template in templates/ staat.
    "dakdekker": "Roofer/dakdekkers-c.html",
    "dakdekkers": "Roofer/dakdekkers-c.html",
}


DAKDEKKER_SECTORS = {"dakdekker", "dakdekkers"}

# Generieke scheidingstekens in bedrijfsnamen ("Dakdekker A'dam | Loomans").
_NAME_SPLIT_RE = re.compile(r"\s*[|·•]\s*|\s+[–—-]\s+")


def clean_company_name(raw: Optional[str]) -> str:
    """Maak een nette weergavenaam van een gescrapete bedrijfsnaam.

    Veel Google-titels zijn 'Dakdekker Amsterdam | Dakdekkersbedrijf Loomans'.
    We pakken het meest merk-achtige deel (meestal het laatste segment) en
    knippen geo/keyword-ruis weg, zonder ooit iets te verzinnen.
    """
    if not raw:
        return ""
    parts = [p.strip() for p in _NAME_SPLIT_RE.split(raw) if p.strip()]
    if not parts:
        return raw.strip()
    # Voorkeur: het langste segment dat niet puur een plaats/keyword is.
    generic = ("dakdekker amsterdam", "dakdekker", "dakdekkers", "dakwerken")
    candidates = [p for p in parts if p.lower() not in generic] or parts
    # Kies het segment met de meeste woorden (meestal de echte bedrijfsnaam).
    name = max(candidates, key=lambda p: (len(p.split()), len(p)))
    return re.sub(r"\s+", " ", name).strip()


def _stars(rating: Any) -> str:
    """Sterren-string op basis van een echte rating (1-5). Leeg bij geen rating."""
    try:
        r = float(str(rating).replace(",", "."))
    except (TypeError, ValueError):
        return ""
    if r <= 0:
        return ""
    filled = max(1, min(5, int(round(r))))
    return "★" * filled + "☆" * (5 - filled)


def _trim(text: Optional[str], limit: int = 240) -> str:
    """Kort een (echte) tekst netjes in op een woordgrens."""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", str(text)).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(",.;:")
    return cut + "…"


def lead_to_demo_data(lead: dict[str, Any]) -> dict[str, Any]:
    """Map één lead-record (uit leads.db / Notion) naar het `data`-contract dat
    `render_demo` voor dakdekkers verwacht.

    ALLES is echte brondata. `google_reviews` wordt geparsed naar een lijst.
    Niets wordt verzonnen; ontbrekende velden blijven leeg.
    """
    reviews: list[dict[str, Any]] = []
    raw_reviews = lead.get("google_reviews")
    if raw_reviews:
        try:
            parsed = json.loads(raw_reviews) if isinstance(raw_reviews, str) else raw_reviews
            if isinstance(parsed, list):
                reviews = [r for r in parsed if isinstance(r, dict) and (r.get("text") or "").strip()]
        except (ValueError, TypeError):
            reviews = []

    contact_naam = (lead.get("contact_naam") or "").strip()
    beschrijving = (lead.get("bedrijf_beschrijving") or "").strip()

    return {
        "bedrijfsnaam": lead.get("bedrijfsnaam") or "",
        "website": lead.get("website") or "",
        "id": lead.get("id") or "",
        "stad": lead.get("stad") or "",
        "regio": lead.get("regio") or "",
        "telefoonnummer": lead.get("telefoonnummer") or "",
        "adres": lead.get("adres") or "",
        "email": lead.get("email") or "",
        "google_rating": lead.get("google_rating") or "",
        "google_reviews": reviews,
        "bedrijf_beschrijving": beschrijving,
        "logo_url": lead.get("logo_url") or "",
        "contact_naam": contact_naam,
        "primaire_kleur": lead.get("primaire_kleur") or "",
        # Sub-structuur die harv_kit.ai_demo_content leest:
        "content": {
            "about": beschrijving,
            "services": [],
            "reviews": reviews,
            "rating": lead.get("google_rating") or None,
            "tagline": None,
        },
        "team": {"members": [{"naam": contact_naam}] if contact_naam else []},
    }


# WordPress/Gutenberg default palette — deze kleuren staan in bijna ELKE WP-site
# en zijn dus NOOIT de echte merkkleur. Uitsluiten bij kleurdetectie.
_WP_DEFAULT_HEXES = {
    "#cf2e2e", "#ff6900", "#fcb900", "#7bdcb5", "#00d084", "#8ed1fc", "#0693e3",
    "#9b51e0", "#abb8c3", "#eb144c", "#f78da7", "#9900ef", "#cc3366", "#ffffff",
    "#000000",
    # nieuwere Gutenberg/blok-thema presets (o.a. #49e670 won onterecht bij Van Steen)
    "#49e670", "#32373c", "#ff6f61", "#7adcb4", "#00a0d2",
    # vendor-/widgetkleuren die nooit een merkkleur zijn: Google (reviews),
    # Facebook, WhatsApp, Trustpilot
    "#fbbc05", "#4285f4", "#34a853", "#ea4335", "#1877f2", "#25d366", "#00b67a",
}
_CITY_AFTER_POSTCODE_RE = re.compile(r"\b\d{4}\s?[A-Z]{2}\b\s+([A-Za-zÀ-ÿ'’.\-\s]+)")


def _city_from_address(adres: Optional[str], fallback: str = "") -> str:
    """Haal de ECHTE vestigingsplaats uit het adres (niet de scrape-stad).

    'Nieuwezijds Voorburgwal 104, 1012 SG Amsterdam, Nederland' -> 'Amsterdam'.
    Valt terug op `fallback` (de scrape-stad) als het adres geen plaats prijsgeeft.
    """
    if adres:
        # 1) plaats direct na de postcode
        m = _CITY_AFTER_POSTCODE_RE.search(adres)
        if m:
            city = m.group(1).strip(" ,.")
            city = re.split(r",|\bNederland\b|\bThe Netherlands\b", city)[0].strip(" ,.")
            if city:
                return city
        # 2) anders: voorlaatste comma-segment (… , Plaats , Nederland)
        parts = [p.strip() for p in adres.split(",") if p.strip()]
        parts = [p for p in parts if p.lower() not in ("nederland", "the netherlands")]
        if parts:
            last = re.sub(r"\b\d{4}\s?[A-Z]{2}\b", "", parts[-1]).strip()
            if last and not re.search(r"\d", last):
                return last
    return fallback or ""


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _is_neutralish(h: str) -> bool:
    try:
        r, g, b = _hex_to_rgb(h)
    except (ValueError, IndexError):
        return True
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 28:          # grijstinten
        return True
    if mx > 244 and mn > 230:  # bijna wit
        return True
    if mx < 28:                # bijna zwart
        return True
    return False


def _darken(h: str, factor: float = 0.42) -> str:
    r, g, b = _hex_to_rgb(h)
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))


def detect_brand_colors_rendered(url: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Bepaal (accent, dark, site_bg, cta) door de site écht te renderen.

    Kleur-rollen volgen hoe de LEAD ze gebruikt (les 2026-06-11, Van Gelder):
      - cta    = de knop-achtergrondkleur van hun site (geel/oranje/lime…) —
                 die hoort bij ÓNZE knoppen, niet als algemene merkkleur;
      - accent = de bredere merkkleur uit oppervlakken (header/footer/nav) en
                 zichtbare pixels — tekstkleuren tellen niet mee (misleidend);
      - dark   = donkerste vlakkleur; site_bg voor de dark-variant-detectie.

    Meet wat een bezoeker ZIET in plaats van wat er in CSS-bestanden staat —
    daardoor wegen vendor-paletten (WP/Gutenberg-presets, Google-reviewwidget,
    social embeds) niet mee. Twee bronnen, gecombineerd:
      1. computed styles van knoppen/CTA's/links/headers (merk-rollen);
      2. het pixel-histogram van de bovenkant van de pagina (zichtbaarheid).
    Een knopkleur die ook zichtbaar in beeld is wint; anders de meest
    voorkomende verzadigde pixelkleur. (None, None) bij falen — caller valt
    terug op de statische detectie.
    """
    try:
        import io
        from collections import Counter as _Counter
        from PIL import Image
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            # domcontentloaded + vaste wachttijd: sites met pollende widgets
            # (chat/analytics) bereiken networkidle nooit en zouden timeouten.
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            css_colors = page.evaluate("""() => {
                const out = [];
                const push = (c, role) => { if (c) out.push([c, role]); };
                document.querySelectorAll(
                    'button,[class*="btn"],[class*="cta"],input[type=submit]'
                ).forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 40 || r.height < 18) return;
                    push(getComputedStyle(el).backgroundColor, 'btn');
                });
                document.querySelectorAll('header,nav,footer,[class*="hero"]').forEach(el => {
                    push(getComputedStyle(el).backgroundColor, 'surf');
                });
                return out;
            }""")
            body_bg = page.evaluate(
                "() => getComputedStyle(document.body).backgroundColor")
            shot = page.screenshot()
            browser.close()

        def _parse_css_rgb(c: str) -> Optional[str]:
            m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", c or "")
            if not m:
                return None
            if m.group(4) is not None and float(m.group(4)) < 0.5:
                return None  # (semi-)transparant
            return _hex_from_rgb(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        btn_counts: _Counter[str] = _Counter()
        surf_counts: _Counter[str] = _Counter()
        for c, role in css_colors:
            h = _parse_css_rgb(c)
            if not h or _is_neutralish(h):
                continue
            (btn_counts if role == "btn" else surf_counts)[h] += 1

        img = Image.open(io.BytesIO(shot)).convert("RGB").resize((320, 225))
        pix_counts: _Counter[tuple] = _Counter()
        dark_counts: _Counter[tuple] = _Counter()
        total = 320 * 225
        for r, g, b in img.getdata():
            q = (r // 24 * 24, g // 24 * 24, b // 24 * 24)
            if max(r, g, b) - min(r, g, b) >= 28:
                pix_counts[q] += 1
            if (r + g + b) / 3 < 70 and max(r, g, b) - min(r, g, b) >= 12:
                dark_counts[q] += 1

        # Zichtbare paginakleur: opake body-achtergrond, anders de kleur van de
        # bovenste UI-strook (headerbalk) — foto-hero's vertekenen het beeld,
        # maar de headerbalk verraadt betrouwbaar een donkere site.
        site_bg = _parse_css_rgb(body_bg)
        if not site_bg or _parse_css_rgb(body_bg) is None:
            strip = list(img.crop((0, 0, 320, 14)).getdata())
            sr = sum(p[0] for p in strip) // len(strip)
            sg = sum(p[1] for p in strip) // len(strip)
            sb = sum(p[2] for p in strip) // len(strip)
            site_bg = _hex_from_rgb(sr, sg, sb)

        def _visible(h: str, min_frac: float = 0.002) -> bool:
            r, g, b = _hex_to_rgb(h)
            seen = sum(n for (qr, qg, qb), n in pix_counts.items()
                       if abs(qr - r) <= 36 and abs(qg - g) <= 36 and abs(qb - b) <= 36)
            return seen / total >= min_frac

        # CTA = de meest gebruikte heldere knop-achtergrond van hun site.
        cta = None
        for h, _n in btn_counts.most_common(8):
            if 60 <= _brightness(h) <= 240 and _visible(h, 0.0008):
                cta = h
                break

        # Accent = bredere merkkleur uit oppervlakken/pixels; donkere
        # vlakkleuren zijn juist de 'dark'. Niet (bijna) dezelfde als de CTA.
        accent, dark = None, None
        for h, _n in surf_counts.most_common(10):
            br = _brightness(h)
            if br < 60:
                dark = dark or h
                continue
            if br <= 235 and _visible(h) and not (cta and _color_distance(h, cta) < 60):
                accent = h
                break
        if not accent and pix_counts:
            for (qr, qg, qb), n in pix_counts.most_common(8):
                h = _hex_from_rgb(qr, qg, qb)
                if (n / total >= 0.01 and 60 <= _brightness(h) <= 235
                        and not (cta and _color_distance(h, cta) < 60)):
                    accent = h
                    break
        if not accent:
            accent = cta  # site leunt volledig op één kleur
        if not accent:
            return None, None, site_bg, None

        if not dark and dark_counts:
            (qr, qg, qb), n = dark_counts.most_common(1)[0]
            if n / total >= 0.03:
                dark = _hex_from_rgb(qr, qg, qb)
        if not dark:
            dark = _darken(accent, 0.42)
        return accent, dark, site_bg, cta
    except Exception:  # noqa: BLE001 — rendering is best effort
        return None, None, None, None


def detect_brand_colors(html: str, extra_css: str = "") -> tuple[Optional[str], Optional[str]]:
    """Bepaal (accent, dark) merkkleur op basis van frequentie in de site-CSS,
    met uitsluiting van WP-default-palet en neutrale tinten.

    Retourneert (None, None) als er geen duidelijke merkkleur te vinden is.
    """
    from collections import Counter
    blob = (html or "") + "\n" + (extra_css or "")
    counts: Counter[str] = Counter()
    for m in re.findall(r"#[0-9a-fA-F]{6}\b", blob):
        h = m.lower()
        if h in _WP_DEFAULT_HEXES or _is_neutralish(h):
            continue
        counts[h] += 1
    if not counts:
        return None, None
    accent = counts.most_common(1)[0][0]
    # Dark: donkerste voldoende-frequente niet-neutrale kleur, anders accent verdonkeren
    dark = None
    for h, _n in counts.most_common(12):
        r, g, b = _hex_to_rgb(h)
        if (r + g + b) / 3 < 70:
            dark = h
            break
    if not dark:
        dark = _darken(accent, 0.42)
    return accent, dark


def _clean_visible_text(soup: "BeautifulSoup", limit: int = 7000) -> str:
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return txt[:limit]


def fetch_site_bundle(url: str) -> dict[str, Any]:
    """Haal homepage + een paar voor de hand liggende subpagina's op en lever
    de ruwe bouwstenen voor personalisatie: tekst, css, kleur-html, projectkaarten."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"}
    bundle: dict[str, Any] = {"html": "", "text": "", "css": "", "project_cards": [], "service_cards": [], "image_pool": []}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException:
        return bundle
    home_html = resp.text
    final_url = resp.url
    home_soup = BeautifulSoup(home_html, "html.parser")
    bundle["html"] = home_html

    # Eerste linked stylesheet meenemen voor kleurdetectie (kort).
    try:
        link = home_soup.find("link", rel=lambda v: v and "stylesheet" in v)
        if link and link.get("href"):
            css_url = urljoin(final_url, link["href"])
            css = requests.get(css_url, headers=headers, timeout=8).text
            bundle["css"] = css[:120000]
    except requests.RequestException:
        pass

    # Projectkaarten deterministisch uit de homepage (img + kop + tekst).
    bundle["project_cards"] = _extract_project_cards(home_soup, final_url)
    # Beeld-pool van de homepage: echte foto's met alt, om slots te vullen.
    bundle["image_pool"] = _imgs_with_alt(home_soup, final_url)

    # Tekst van homepage + subpagina's voor de AI-extractie.
    texts = [_clean_visible_text(BeautifulSoup(home_html, "html.parser"), 6000)]
    for path in ("/over-ons/", "/diensten/", "/werkwijze/", "/aanpak/", "/projecten/", "/referenties/"):
        try:
            sub = requests.get(urljoin(final_url, path), headers=headers, timeout=8, allow_redirects=True)
            if sub.status_code == 200:
                sub_soup = BeautifulSoup(sub.text, "html.parser")
                texts.append(_clean_visible_text(BeautifulSoup(sub.text, "html.parser"), 3500))
                if not bundle["project_cards"] and ("project" in path or "referen" in path):
                    bundle["project_cards"] = _extract_project_cards(sub_soup, final_url)
                if "dienst" in path:
                    # De /diensten-pagina heeft vaak de échte dienstfoto's met
                    # sprekende alts ("Dakreparatie", …) — voeg ze toe aan de pool.
                    pool_urls = {it["url"] for it in bundle["image_pool"]}
                    bundle["image_pool"] += [it for it in _imgs_with_alt(sub_soup, final_url)
                                             if it["url"] not in pool_urls]
                if not bundle["service_cards"] and "dienst" in path:
                    bundle["service_cards"] = _service_cards_from_diensten(sub_soup, final_url, headers)
        except requests.RequestException:
            continue
    if not bundle["service_cards"]:
        bundle["service_cards"] = _service_cards_from_diensten(home_soup, final_url, headers)
    bundle["text"] = "\n\n".join(t for t in texts if t)[:14000]
    return bundle


def _extract_project_cards(soup: "BeautifulSoup", base_url: str) -> list[dict[str, str]]:
    """Vind echte project/referentie-kaarten: een blok met een ECHTE foto plus
    een kop en omschrijving. Alleen tonen als er minimaal beschrijvende tekst is
    (geen verzonnen content)."""
    cards: list[dict[str, str]] = []
    for fig in soup.find_all(["figure", "article", "div"]):
        img = fig.find("img")
        if not img:
            continue
        src = img.get("src") or img.get("data-src") or ""
        if not re.search(r"\.(jpe?g|png|webp)(\?|$)", src, re.I):
            continue
        if any(k in src.lower() for k in ("logo", "icon", "favicon", "avatar", "placeholder")):
            continue
        head = fig.find(["h2", "h3", "h4"])
        cap = fig.find("figcaption") or fig.find("p")
        title = head.get_text(" ", strip=True) if head else ""
        desc = cap.get_text(" ", strip=True) if cap else ""
        if title and len(desc) >= 25:
            cards.append({
                "image": urljoin(base_url, src),
                "title": title[:80],
                "desc": re.sub(r"\s+", " ", desc)[:220],
            })
        if len(cards) >= 4:
            break
    # dedupe op image
    seen, out = set(), []
    for c in cards:
        if c["image"] not in seen:
            seen.add(c["image"]); out.append(c)
    return out


def _salvage_json(s: str) -> Optional[dict[str, Any]]:
    """Probeer afgekapte JSON te repareren: sluit open haken, of trim naar het
    laatste complete object. Retourneert None als het echt niet lukt."""
    def _close(frag: str) -> str:
        frag = frag.rstrip().rstrip(",")
        ob = frag.count("{") - frag.count("}")
        obk = frag.count("[") - frag.count("]")
        return frag + "]" * max(0, obk) + "}" * max(0, ob)

    try:
        return json.loads(_close(s))
    except json.JSONDecodeError:
        pass
    for m in reversed([mm.start() for mm in re.finditer(r"\}", s)]):
        try:
            return json.loads(_close(s[: m + 1]))
        except json.JSONDecodeError:
            continue
    return None


_SERVICE_SLUG_SKIP = {
    "", "diensten", "over-ons", "faq", "blog", "blogs", "contact", "home",
    "offerte", "privacy", "privacybeleid", "algemene-voorwaarden", "sitemap",
    "cookiebeleid", "spoedservice", "werkgebied", "vacatures",
}
_SERVICE_PRIORITY = [
    "dakreparatie", "daklekkage", "platte-daken", "dakinspectie", "dakonderhoud",
    "epdm-dakbedekking", "dakbedekking", "dakgoten", "bitumen-dak", "schoorstenen",
    "nokvorsten", "stormschade", "dakisolatie", "dakkapel",
]

# Junk we never want to treat as content imagery (logos, badges, sprites, …).
# RULES 7: ook keurmerken/certificaat-badges zijn nooit content-beeld.
_IMG_SKIP = ("logo", "icon", "favicon", "avatar", "placeholder", "sprite",
             "trustoo", "google", "whatsapp", "banner", "pixel",
             "keurmerk", "certificaat", "certificate", "vca", "vebidak",
             "tectum", "kiwa", "badge", "award", "kvk", "review", "sticker",
             "wordmark", "embleem")


def _img_real_src(im) -> str:
    """Echte afbeeldings-URL achterhalen, óók bij lazy-loading. Veel bouwers
    (Elementor, WP-Rocket, NitroPack, Drupal) zetten `src` op leeg of een
    `data:`-placeholder en bewaren de echte URL in een lazy-/data-attribuut of
    in srcset. Zonder dit levert een lazy-loadende site (de meeste WordPress-
    daksites) NUL foto's op — terwijl de foto's er wél zijn (feedback 2026-06-23)."""
    src = (im.get("src") or "").strip()
    if src and not src.startswith("data:"):
        return src
    # src is leeg of een data:-placeholder -> echte URL staat in een lazy-attr
    for attr in ("data-src", "data-lazy-src", "data-original",
                 "nitro-lazy-src", "data-nitro-lazy-src"):
        v = (im.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return v
    # …of in een srcset-variant ("url1 480w, url2 900w") -> pak de laatste (grootste)
    for attr in ("nitro-lazy-srcset", "data-srcset", "srcset"):
        v = (im.get(attr) or "").strip()
        if v:
            cand = v.split(",")[-1].strip().split()
            if cand and not cand[0].startswith("data:"):
                return cand[0]
    return src  # niets bruikbaars -> (lege/data) src; wordt verderop weggefilterd


def _imgs_with_alt(soup: "BeautifulSoup", base_url: str) -> list[dict[str, str]]:
    """Collect real content images (with their alt text) so they can be matched
    to template slots. Skips logos/icons/badges (checked in BOTH the url and the
    alt text). De-duped on URL, in DOM order."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for im in soup.find_all("img"):
        src = _img_real_src(im)
        # avif erbij: moderne CMS'en (o.a. Drupal) serveren .jpg.avif?itok=… ;
        # Pillow 11+ decodeert avif, dus die foto's zijn gewoon bruikbaar.
        if not re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", src, re.I):
            continue
        alt = re.sub(r"\s+", " ", (im.get("alt") or "")).strip()
        hay = (src + " " + alt).lower()
        if any(k in hay for k in _IMG_SKIP):
            continue
        url = urljoin(base_url, src)
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "alt": alt})
    return out


def _image_meta(url: str, with_preview: bool = False) -> Optional[dict]:
    """Download een afbeelding en lees afmetingen, transparantie-aandeel en een
    8x8 perceptual hash (aHash). Met `with_preview` ook een verkleinde JPEG
    (base64, lange zijde <= 640px) voor de AI-beeldselectie — voorkomt een
    tweede download. None bij netwerk- of decode-fout."""
    try:
        import base64 as _b64mod
        import io
        from PIL import Image
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=8)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img.load()
        w, h = img.size
        alpha_frac = 0.0
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            a = img.convert("RGBA").getchannel("A")
            hist = a.histogram()
            alpha_frac = sum(hist[:128]) / float(max(1, w * h))
        g = img.convert("L").resize((8, 8))
        px = list(g.getdata())
        avg = sum(px) / 64.0
        bits = 0
        for i, p in enumerate(px):
            if p > avg:
                bits |= 1 << i
        # Gemiddelde luminantie van de NIET-transparante pixels: daarmee zien we
        # of een logo licht ("light"-variant, wit) of donker is — bepaalt of hij
        # een contrasterend chipje nodig heeft op zijn achtergrond.
        try:
            rgba = img.convert("RGBA")
            px = list(rgba.resize((24, 24)).getdata())
            vis = [(r0 + g0 + b0) / 3 for r0, g0, b0, a0 in px if a0 > 120]
            lum = sum(vis) / len(vis) if vis else 128.0
        except Exception:  # noqa: BLE001
            lum = 128.0
        meta = {"w": w, "h": h, "alpha": alpha_frac, "hash": bits, "lum": lum}
        if with_preview:
            prev = img.convert("RGB")
            if max(prev.size) > 640:
                sc = 640.0 / max(prev.size)
                prev = prev.resize((int(prev.width * sc), int(prev.height * sc)), Image.LANCZOS)
            buf = io.BytesIO()
            prev.save(buf, format="JPEG", quality=78)
            meta["b64"] = _b64mod.standard_b64encode(buf.getvalue()).decode("ascii")
        return meta
    except Exception:  # noqa: BLE001 — verificatie is best effort
        return None


def _hash_dist(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _svg_aspect(url: str) -> Optional[float]:
    """Breedte/hoogte-verhouding van een SVG via de viewBox (PIL leest geen
    SVG; voor wordmark-detectie is de verhouding genoeg)."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=8)
        r.raise_for_status()
        m = re.search(r'viewBox\s*=\s*["\']\s*[\d.\-]+[\s,]+[\d.\-]+[\s,]+([\d.]+)[\s,]+([\d.]+)',
                      r.text, re.I)
        if not m:
            m = re.search(r'width\s*=\s*["\']?([\d.]+)[^>]*height\s*=\s*["\']?([\d.]+)', r.text, re.I)
        if m and float(m.group(2)) > 0:
            return float(m.group(1)) / float(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    return None


def _filter_image_pool(pool: list[dict[str, str]], logo_url: str = "") -> list[dict[str, str]]:
    """RULES 7: weer merk-assets en near-duplicates uit de beeldpool op basis
    van échte beeldkenmerken — klein, (deels) transparant of extreem breed is
    een logo/keurmerk/badge, geen content-foto. Twee bijna-identieke foto's
    (aHash-afstand <= 6) komen nooit samen in de pool, zodat ze ook nooit
    naast elkaar in een grid belanden. Niet te downloaden -> behouden (de
    url/alt-filters vingen de evidente junk al)."""
    out: list[dict[str, str]] = []
    hashes: list[int] = []
    logo_l = (logo_url or "").strip().lower()
    for it in pool[:14]:
        url = it.get("url") or ""
        if logo_l and url.lower() == logo_l:
            continue
        meta = _image_meta(url, with_preview=True)
        if meta is None:
            out.append(it)
            continue
        if meta["w"] < 350 or meta["h"] < 230:
            continue  # badges/keurmerken zijn klein
        if meta["w"] / max(1.0, float(meta["h"])) > 2.6:
            continue  # extreem breed = wordmark/banner
        if meta["alpha"] > 0.02:
            continue  # transparante uitsnede = logo/badge
        if any(_hash_dist(meta["hash"], hsh) <= 6 for hsh in hashes):
            continue  # near-duplicate van een eerdere foto
        hashes.append(meta["hash"])
        # _hash meenemen: de hero-selectie gebruikt 'm later om twee té gelijkende
        # foto's nooit samen in de split-hero te zetten (RULES 7, feedback 2026-06-23).
        out.append({**it, "w": meta["w"], "h": meta["h"],
                    "_b64": meta.get("b64"), "_hash": meta["hash"]})
    return out


_IMG_SELECT_MODEL = os.environ.get("HARV_IMGSELECT_MODEL", "claude-sonnet-4-6")

# Kwaliteitsdrempels voor de strenge beeldkeuring. Beeld is HET dragende element
# van de demo ("valt of staat met de foto's") -> liever een backup-foto dan een
# matige eigen foto. Tunebaar via env. Een foto moet >= ACCEPT_MIN scoren om
# überhaupt gebruikt te worden; de hero is heiliger en vereist >= HERO_MIN +
# hero_safe (liggend, onderwerp niet pal in het midden, geen tekst in het
# centrum — want titel/formulier liggen daar overheen).
_IMG_ACCEPT_MIN = int(os.environ.get("HARV_IMG_ACCEPT_MIN", "62"))
_IMG_HERO_MIN = int(os.environ.get("HARV_IMG_HERO_MIN", "70"))
# Hamming-afstand waaronder twee hero-foto's "te veel op elkaar lijken" en dus
# nooit samen in de split-hero mogen (feedback 2026-06-23). Losser dan de
# pool-dedup (6): near-duplicates zijn al weg, dit vangt "zelfde tafereel".
_IMG_HERO_SIM_DIST = int(os.environ.get("HARV_IMG_HERO_SIM_DIST", "12"))
# Categorieën die het visie-model toekent. Alleen deze mogen de site in:
_IMG_ACCEPT_CATS = {"people_working", "beautiful_roof", "craft_detail",
                    "van_clean", "material"}
# … alle overige (mold/open_roof/damage/debris/logo_badge/portrait/interior/
#   text_heavy/stock_unrelated/other) worden NOOIT geplaatst.


def ai_select_images(
    pool: list[dict[str, Any]],
    services: list[str],
    company: str,
    *,
    logo_url: str = "",
    slug: str = "",
    lead_id: str = "",
) -> dict[str, Any]:
    """Laat een visie-model de site-foto's toewijzen aan de template-slots.

    Beeld is het belangrijkste personalisatie-element: de eerste foto's moeten
    van de eigen site komen ("wow, dit is ónze zaak") — bij voorkeur eigen
    werk/werkers, passend bij de sectie, professioneel. Het model mag een slot
    leeg laten (-> stock-fallback), nooit een foto twee keer geven.

    Returns {"services": {naam: url}, "why": [url, ...],
             "logo_contains_name": bool|None}. Leeg dict bij falen/geen key.
    Kostenbewaking: gelogd als step "sonnet_imgselect"; het hele element moet
    onder de €0,08 per demo blijven (downscaled previews, één call).
    """
    cands = [it for it in pool if it.get("_b64")]
    if not cands and not logo_url:
        return {}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {}
    return _ai_select_images_call(pool, services, company, logo_url=logo_url,
                                  slug=slug, lead_id=lead_id, _retry=True)


def _ai_select_images_call(
    pool: list[dict[str, Any]],
    services: list[str],
    company: str,
    *,
    logo_url: str = "",
    slug: str = "",
    lead_id: str = "",
    _retry: bool = False,
) -> dict[str, Any]:
    cands = [it for it in pool if it.get("_b64")]
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        import anthropic

        content: list[dict[str, Any]] = []
        svc_lines = "\n".join(f"- {s}" for s in services[:4]) or "- (geen diensten bekend)"
        # STRENGE KEURING: het model beoordeelt ELKE foto los (score + categorie +
        # tekst-hoeveelheid + hero-geschiktheid). De selectie zelf (hero-paar /
        # diensten / why) gebeurt daarna in Python met harde drempels — zo is de
        # kwaliteitslat deterministisch en niet afhankelijk van de bui van het model.
        prompt = (
            f"Je bent een strenge fotoredacteur voor een premium demo-website van "
            f"dakdekkersbedrijf {company}. De site VALT OF STAAT met de kwaliteit van "
            "de foto's, dus keur streng. Beoordeel hieronder ELKE genummerde foto los.\n\n"
            "Geef per foto een CATEGORIE (cat):\n"
            "  GOED (mogen geplaatst worden):\n"
            "  - people_working : dakdekker(s)/vakmensen duidelijk aan het werk op/aan een dak.\n"
            "  - beautiful_roof : een mooi, strak, NET AFGEWERKT dak (nieuwe/gerenoveerde "
            "pannen, bitumen, EPDM, leien) — representatief en aantrekkelijk.\n"
            "  - craft_detail   : net vakwerk-detail (nok, goot, lood/zink, schoorsteen) "
            "dat er verzorgd en professioneel uitziet.\n"
            "  - van_clean      : een verzorgde bedrijfsbus/-wagen (bedrijfslogo prima), "
            "MITS niet volgeplakt met tekst.\n"
            "  - material       : mooi/representatief materiaal of een net bouwtafereel.\n"
            "  AFKEUREN (NOOIT plaatsen — geef de juiste reden als cat):\n"
            "  - mold           : schimmel, algen, vocht-/lekkagevlekken, rot, vieze plekken.\n"
            "  - open_roof      : open/half-gesloopt dak, pannen eraf, kale dakconstructie, "
            "een dak 'dat openligt', sloop/afbraak, rommelige bouwplaats.\n"
            "  - damage         : schade-closeup, kapot dak, storm-/lekschade.\n"
            "  - debris         : bouwafval, rommel, troep.\n"
            "  - text_heavy     : poster/banner/flyer-achtig, prijslijst, veel tekst-overlay, "
            "'bel nu'-promo — beeld dat vooral uit tekst bestaat.\n"
            "  - logo_badge     : logo, keurmerk, certificaat, badge, sticker, wordmark.\n"
            "  - portrait       : portret/pasfoto/review-avatar (persoon ZONDER werkcontext).\n"
            "  - interior       : interieur, kantoor, vergaderzaal, showroom.\n"
            "  - stock_unrelated: duidelijk generieke stockfoto die niet bij dit bedrijf past.\n"
            "  - other          : iets anders dat niet thuishoort.\n\n"
            "Per foto ook:\n"
            "  score (0-100): hoe mooi/bruikbaar als blikvanger op een premium site. "
            "Wees streng: een matige of twijfelachtige foto < 60. Twijfel je of een dak "
            "écht 'mooi' is? Geef dan een LAGE score (liever een nette backup dan een matige "
            "eigen foto).\n"
            "  text (none|light|heavy): hoeveel tekst/overlay in het beeld zit. "
            "Een busje met alleen een logo = light; een banner vol tekst = heavy.\n"
            "  hero_safe (true/false): geschikt als grote HERO-foto. true alleen als: "
            "liggend (breder dan hoog), het hoofdonderwerp NIET pal in het midden zit "
            "(daar komt de titel + een formulier overheen) en er GEEN belangrijke tekst "
            "in het midden/onder staat. EXTRA: de hero wordt GECENTREERD bijgesneden, "
            "dus het onderwerp dat je wilt tonen (bv. de werkende persoon) moet óók ná "
            "die crop goed zichtbaar blijven — niet half buiten beeld, niet zó klein of "
            "aan de rand dat het wegvalt, en niet achter de titel/het formulier (midden) "
            "verdwijnen. Een HELE, duidelijk werkende persoon in beeld is sterker dan "
            "alleen handen of een los detail. Anders false.\n"
            f"  service (één van: {', '.join(services[:4]) or '—'} | null): bij welke dienst "
            "deze foto het best past, of null.\n"
            "  reason: heel kort waarom.\n\n"
            "Geef UITSLUITEND JSON:\n"
            "{\"photos\":[{\"n\":1,\"cat\":\"...\",\"score\":0-100,\"text\":\"none|light|heavy\","
            "\"hero_safe\":true,\"service\":\"<dienstnaam|null>\",\"reason\":\"...\"}, ...]"
        )
        if logo_url:
            prompt += (", \"logo_contains_name\": true/false}  — de LAATSTE afbeelding is hun logo; "
                       f"logo_contains_name = staat de bedrijfsnaam ('{company}') óf een kenmerkend "
                       "deel ervan (bv. de achternaam of merknaam) als LEESBARE TEKST in dat logo? "
                       "Bij twijfel: true.")
        else:
            prompt += "}"
        content.append({"type": "text", "text": prompt})
        for i, it in enumerate(cands, 1):
            content.append({"type": "text", "text": f"\n[foto {i}]"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": it["_b64"]}})
        if logo_url:
            lmeta = _image_meta(logo_url, with_preview=True)
            if lmeta and lmeta.get("b64"):
                content.append({"type": "text", "text": "\n[logo]"})
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": lmeta["b64"]}})
            else:
                logo_url = ""  # logo niet leesbaar -> vraag vervalt

        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=_IMG_SELECT_MODEL, max_tokens=1400,
            messages=[{"role": "user", "content": content}],
        )
        raw = msg.content[0].text
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            data = json.loads(m.group()) if m else {}
        except json.JSONDecodeError:
            # Model schreef kapotte JSON — telt als misser; retry vangt het op.
            data = {}

        try:  # kosten berekenen + loggen (best effort, mag nooit breken)
            import sys as _sys
            _scraper = Path(__file__).resolve().parent.parent / "harv-scraper"
            if _scraper.exists() and str(_scraper) not in _sys.path:
                _sys.path.insert(0, str(_scraper))
            import cost_tracker as _ct
            _eur = _ct.ai_call_cost_eur(
                _IMG_SELECT_MODEL, int(msg.usage.input_tokens), int(msg.usage.output_tokens))
            log_ai_cost(slug, "sonnet_imgselect",
                        {"_cost_eur": _eur, "_model": _IMG_SELECT_MODEL,
                         "_tokens": {"in": int(msg.usage.input_tokens),
                                     "out": int(msg.usage.output_tokens)}},
                        lead_id=lead_id)
        except Exception as _exc:  # noqa: BLE001
            print(f"  ⚠ kostenlog beeldselectie overgeslagen: {_exc}")

        # ── Scores per foto inlezen, koppelen aan de kandidaat-url + afmetingen ──
        photos = data.get("photos") or []
        scored: list[dict[str, Any]] = []
        for p in photos:
            try:
                i = int(p.get("n"))
            except (TypeError, ValueError):
                continue
            if not (1 <= i <= len(cands)):
                continue
            c = cands[i - 1]
            try:
                score = max(0, min(100, int(p.get("score", 0))))
            except (TypeError, ValueError):
                score = 0
            scored.append({
                "url": c["url"],
                "cat": str(p.get("cat") or "other"),
                "score": score,
                "text": str(p.get("text") or "none"),
                "hero_safe": bool(p.get("hero_safe")),
                "service": (str(p["service"]) if p.get("service") else None),
                "w": c.get("w") or 0, "h": c.get("h") or 0,
                "_hash": c.get("_hash"),
                "reason": str(p.get("reason") or ""),
            })

        # ── Harde kwaliteitspoort: alleen GOEDE categorieën, boven de drempel,
        #    zonder zware tekst-overlay halen de site. ──
        accepted = [s for s in scored
                    if s["cat"] in _IMG_ACCEPT_CATS
                    and s["score"] >= _IMG_ACCEPT_MIN
                    and s["text"] != "heavy"]
        accepted.sort(key=lambda s: s["score"], reverse=True)

        used: set = set()
        # HERO (2 foto's, voor de split-hero van Template C; A/B pakt er 1):
        # liggend + hero_safe + boven de strengere hero-drempel, beste eerst.
        def _landscape(s: dict) -> bool:
            return not (s["w"] and s["h"]) or s["w"] >= s["h"] * 1.15
        hero_cands = [s for s in accepted
                      if s["hero_safe"] and s["score"] >= _IMG_HERO_MIN and _landscape(s)]
        # Hero-PAAR moet visueel DUIDELIJK verschillen: twee bijna-gelijke opnames
        # naast elkaar in de split-hero oogt als een fout (RULES 7, feedback
        # 2026-06-23). Kies de sterkste hero, en als tweede de beste die niet té
        # veel op de eerste lijkt; de afgewezen bijna-twin blijft in `accepted` en
        # zakt zo naar de reserve (dienst/why). Drempel losser dan de pool-dedup
        # (6) want "lijkt te veel op elkaar" is ruimer dan "near-duplicate".
        def _too_similar(a: dict, b: dict) -> bool:
            ha, hb = a.get("_hash"), b.get("_hash")
            if ha is None or hb is None:
                return False  # geen hash -> niet te bepalen, blokkeer niet
            return _hash_dist(ha, hb) <= _IMG_HERO_SIM_DIST
        hero_picks: list[dict] = []
        for s in hero_cands:
            if len(hero_picks) >= 2:
                break
            if any(_too_similar(s, h) for h in hero_picks):
                continue  # te vergelijkbaar met een al gekozen hero -> naar reserve
            hero_picks.append(s)
        hero_urls: list[str] = []
        for s in hero_picks:
            hero_urls.append(s["url"])
            used.add(s["url"])

        remaining = [s for s in accepted if s["url"] not in used]
        # DIENSTEN: per dienstnaam de hoogst scorende foto die het model daaraan
        # koppelde; valt terug op de generieke alt-match in de bake.
        services_map: dict[str, str] = {}
        for name in services[:4]:
            best = None
            for s in remaining:
                if s["url"] in used:
                    continue
                if (s["service"] or "").strip().lower() == name.strip().lower():
                    best = s
                    break  # remaining is al op score gesorteerd
            if best:
                services_map[name] = best["url"]
                used.add(best["url"])
        # WHY: de overige goedgekeurde foto's (max 3), beste eerst.
        why_urls = [s["url"] for s in remaining if s["url"] not in used][:3]
        for u in why_urls:
            used.add(u)

        out: dict[str, Any] = {
            "hero": hero_urls,
            "services": services_map,
            "why": why_urls,
            # rest = ALLE goedgekeurde foto's op score (blind-fill mag hier veilig uit putten)
            "approved": [s["url"] for s in accepted],
            "scores": {s["url"]: s["score"] for s in scored},
            # volledig per-foto oordeel (cat/score/text/hero_safe/service/reason) —
            # voor de audit-/feedback-tool zodat je ziet WAAROM een foto koos/afviel.
            "details": scored,
        }
        if logo_url:
            out["logo_contains_name"] = bool(data.get("logo_contains_name"))

        # Vision is niet deterministisch: 0 scores bij >= 3 echte foto's is
        # vrijwel zeker een misser -> één keer opnieuw proberen.
        if not scored and len(cands) >= 3 and _retry:
            print("  🖼  AI-beeldkeuring gaf 0 scores — retry")
            return _ai_select_images_call(pool, services, company, logo_url=logo_url,
                                          slug=slug, lead_id=lead_id, _retry=False)
        rej = len(scored) - len(accepted)
        print(f"  🖼  AI-beeldkeuring: {len(accepted)}/{len(cands)} foto's goedgekeurd "
              f"({rej} afgekeurd) — hero={len(hero_urls)} diensten={len(services_map)} "
              f"why={len(why_urls)}"
              + (f", logo bevat naam: {out.get('logo_contains_name')}" if logo_url else ""))
        return out
    except Exception as exc:  # noqa: BLE001 — beeldselectie is best effort
        print(f"  ⚠ AI-beeldkeuring overgeslagen: {exc}")
        return {}


def _match_image(name: str, pool: list[dict[str, str]], used: set) -> Optional[str]:
    """Pick the pool image whose alt best matches a slot name (word overlap).
    Skips already-used URLs so each real photo lands in only one slot."""
    nset = set(re.findall(r"[a-z]{4,}", (name or "").lower()))
    best, score = None, 0
    for it in pool:
        url = it.get("url")
        if not url or url in used:
            continue
        aset = set(re.findall(r"[a-z]{4,}", (it.get("alt") or "").lower()))
        s = len(nset & aset)
        if s > score:
            best, score = url, s
    if best and score >= 1:
        used.add(best)
        return best
    return None


def _service_cards_from_diensten(soup: "BeautifulSoup", base_url: str, headers: dict, limit: int = 4) -> list[dict[str, str]]:
    """Bouw dienstkaarten uit de ECHTE detailpagina's per dienst: titel + foto +
    korte omschrijving, rechtstreeks van de site (zoals de gebruiker vroeg)."""
    from urllib.parse import urlparse
    host = urlparse(base_url).netloc
    cand: dict[str, tuple[str, str]] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        p = urlparse(href)
        if p.netloc != host:
            continue
        parts = [x for x in p.path.split("/") if x]
        if len(parts) != 1:  # alleen top-level dienstpagina's
            continue
        slug = parts[0].lower()
        if slug in _SERVICE_SLUG_SKIP:
            continue
        if not re.search(r"dak|epdm|nok|schoorsteen|goot|storm|bitumen|isolatie|lood|zink", slug):
            continue
        text = a.get_text(" ", strip=True)
        if text and slug not in cand:
            cand[slug] = (text[:40], href)

    # Volgorde: prioriteit eerst, dan paginavolgorde
    ordered = [s for s in _SERVICE_PRIORITY if s in cand] + [s for s in cand if s not in _SERVICE_PRIORITY]

    cards: list[dict[str, str]] = []
    for slug in ordered:
        if len(cards) >= limit:
            break
        title, url = cand[slug]
        try:
            d = requests.get(url, headers=headers, timeout=8)
            if d.status_code != 200:
                continue
            ds = BeautifulSoup(d.text, "html.parser")
        except requests.RequestException:
            continue
        # Zelfde strenge filter als de beeldpool (logo's heten lang niet altijd
        # "logo" — Van Steen's logo heette "Untitled-design-…"): neem de eerste
        # échte content-foto en bewaar de rest voor de beeldpool/AI-selectie.
        cand_imgs = _imgs_with_alt(ds, url)
        img = cand_imgs[0]["url"] if cand_imgs else ""
        desc = ""
        md = ds.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            desc = md["content"]
        if not desc:
            for para in ds.find_all("p"):
                t = para.get_text(" ", strip=True)
                if len(t) >= 50:
                    desc = t
                    break
        h1 = ds.find("h1")
        title = (h1.get_text(" ", strip=True) if h1 else title) or title
        if img:
            cards.append({"image": img, "title": _trim(title, 40), "desc": _trim(desc, 150),
                          "images": [{"url": c["url"], "alt": c["alt"] or title}
                                     for c in cand_imgs[:4]]})
    return cards


def ai_site_extract(site_text: str, *, company: str, city: str) -> dict[str, Any]:
    """Eén Claude-call die ECHTE, gepersonaliseerde content uit de sitetekst haalt.

    Verzint niets: lege lijst/false bij afwezigheid. Retourneert process-stappen,
    on-site testimonials, certificering/garantie-signalen, diensten en about.
    """
    empty = {"process": [], "testimonials": [], "services": [], "why": [], "stats": [], "about": "",
             "certified": False, "insured": False, "guarantee": False,
             "service_area": "", "years_experience": "", "emergency": False, "free_quote": False,
             "own_team": False, "fast_response": False, "projects_done": "", "customers": "",
             "_cost_eur": 0.0}
    import os as _os
    key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or not site_text.strip():
        return empty
    try:
        import anthropic
    except ImportError:
        return empty

    prompt = f"""Je bent de content-strateeg van Harv Agency. Hieronder staat de
ECHTE zichtbare tekst van de website van een dakdekkersbedrijf ({company}, {city}).
Haal hieruit feiten op om een gepersonaliseerde demo te vullen.

STRIKTE REGELS:
- Verzin NIETS. Gebruik alleen wat echt in de tekst staat.
- Onbekend? Lege lijst of false.
- Schrijf in natuurlijk Nederlands. NOOIT een gedachtestreepje, gebruik komma of punt.

Geef EXACT dit JSON-object terug (alleen JSON, geen uitleg, geen markdown):
{{
  "process": [{{"title": "korte staptitel zoals op de site", "body": "1 korte zin"}}],
  "testimonials": [{{"text": "echte review op de site, max 200 tekens", "author": "naam indien vermeld, anders leeg"}}],
  "services": [{{"name": "dienst zoals op de site", "desc": "1 korte zin"}}],
  "why": [{{"title": "KWALITATIEVE reden om te kiezen, GEEN puur cijfer (bv. VCA gecertificeerd, persoonlijke service, scherpe prijzen, veilig werken op hoogte, klantgericht), max 4 woorden", "body": "1 korte zin"}}],
  "stats": [{{"value": "kort cijfer zoals letterlijk op de site, bv. 22+, 15, 24/7", "label": "waar het cijfer over gaat, bv. Jaar ervaring, Jaar garantie, Geslaagde projecten, Certificaten"}}],
  "about": "1 a 2 zinnen over het bedrijf, alleen op basis van de tekst, zonder verzonnen cijfers of jaartallen",
  "certified": true/false,
  "insured": true/false,
  "guarantee": true/false,
  "service_area": "dekkingsgebied zoals LETTERLIJK op de site (bv. 'Den Haag en regio', 'Zuid-Holland', 'Den Haag en omstreken'); leeg indien onbekend",
  "years_experience": "jaren ervaring OF oprichtingsjaar als getal (bv. '20' of '2003'); leeg indien onbekend",
  "emergency": true/false,
  "free_quote": true/false,
  "own_team": true/false,
  "fast_response": true/false,
  "projects_done": "echt aantal opgeleverde daken/projecten als getal; leeg indien onbekend",
  "customers": "echt aantal tevreden klanten als getal; leeg indien onbekend"
}}

LIMIETEN (belangrijk, anders wordt het antwoord afgekapt):
- process: max 4 stappen
- testimonials: max 3, elk max 200 tekens
- services: max 4
- why: max 4 kwalitatieve pluspunten van de site, GEEN cijfers (die horen bij stats)
- stats: max 4, ALLEEN cijfers die echt op de site staan (geen verzonnen cijfers); leeg laten als er geen cijfers zijn
- houd alle teksten kort

WEBSITE-TEKST:
{site_text}"""

    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        inp, out = msg.usage.input_tokens, msg.usage.output_tokens
        cost = (inp * 0.80 / 1e6 + out * 4.00 / 1e6) * 0.93
        print(f"  🤖 ai_site_extract: {inp}+{out} tokens = €{cost:.4f}")
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return empty
        try:
            res = json.loads(match.group())
        except json.JSONDecodeError:
            # Bij afkapping: probeer het grootste geldige JSON-prefix te herstellen.
            res = _salvage_json(match.group())
            if res is None:
                return empty
        # sanitize
        def _clean(s: Any) -> str:
            return re.sub(r"\s*[—–]\s*", ", ", str(s or "")).strip()
        out_d: dict[str, Any] = {
            "process": [{"title": _clean(p.get("title")), "body": _clean(p.get("body"))}
                        for p in (res.get("process") or []) if isinstance(p, dict) and p.get("title")][:4],
            "testimonials": [{"text": _clean(t.get("text")), "author": _clean(t.get("author"))}
                             for t in (res.get("testimonials") or []) if isinstance(t, dict) and len(str(t.get("text") or "")) > 20][:6],
            "services": [{"name": _clean(s.get("name")), "desc": _clean(s.get("desc"))}
                         for s in (res.get("services") or []) if isinstance(s, dict) and s.get("name")][:4],
            "why": [{"title": _clean(w.get("title")), "body": _clean(w.get("body"))}
                    for w in (res.get("why") or []) if isinstance(w, dict) and w.get("title")][:4],
            "stats": [{"value": _clean(st.get("value"))[:8], "label": " ".join(_clean(st.get("label")).split()[:2])}
                      for st in (res.get("stats") or []) if isinstance(st, dict) and st.get("value") and st.get("label")][:4],
            "about": _clean(res.get("about"))[:400],
            "certified": bool(res.get("certified")),
            "insured": bool(res.get("insured")),
            "guarantee": bool(res.get("guarantee")),
            "service_area": _clean(res.get("service_area"))[:40],
            "years_experience": _clean(res.get("years_experience"))[:8],
            "emergency": bool(res.get("emergency")),
            "free_quote": bool(res.get("free_quote")),
            "own_team": bool(res.get("own_team")),
            "fast_response": bool(res.get("fast_response")),
            "projects_done": _clean(res.get("projects_done"))[:8],
            "customers": _clean(res.get("customers"))[:8],
            "_cost_eur": round(cost, 5),
        }
        return out_d
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ ai_site_extract faalde: {type(exc).__name__}: {exc}")
        return empty


def build_dakdekker_template_data(
    data: dict[str, Any],
    ai_content: dict[str, Any],
    extract: Optional[dict[str, Any]] = None,
    colors: Optional[tuple[Optional[str], Optional[str]]] = None,
) -> dict[str, Any]:
    """Bouw het volledige `template-data` JSON-object voor de dakdekker-template
    uit ECHTE leaddata. Secties zonder echte data worden via SHOW_*-vlaggen en
    lege waarden verborgen (geen verzonnen cijfers, reviews of projecten)."""
    extract = extract or {}
    accent, dark = colors or (None, None)

    naam = clean_company_name(data.get("bedrijfsnaam"))
    # ECHTE vestigingsplaats uit het adres (niet de scrape-stad — bv. Hoorn≠Amsterdam).
    city = _city_from_address(data.get("adres"), (data.get("stad") or "")).strip()
    # Werkgebied-check (besluit 2026-06-11): opereert het bedrijf realistisch
    # gezien breder dan één stad (site noemt meerdere plaatsen, een provincie,
    # "regio" of landelijk bereik), benoem dan de provincie/regio in de copy
    # i.p.v. één stad — "Zuid-Holland" dekt de lading beter dan "Den Haag".
    _area_raw = str((extract or {}).get("service_area") or "")
    _wide_area = bool(re.search(
        r",| en |provincie|regio\b|landelijk|heel\s+(nederland|zuid|noord|het\s+land)|omstreken|omgeving",
        _area_raw, re.I))
    _regio_naam = " ".join(w.title() for w in re.split(r"[\s-]+", str(data.get("regio") or "").strip()) if w)
    # Provincienamen krijgen hun officiële koppelteken terug (db is slordig).
    _regio_naam = re.sub(r"^(Noord|Zuid) (Holland|Brabant)$", r"\1-\2", _regio_naam)
    if _wide_area and _regio_naam:
        city = _regio_naam
    rating = data.get("google_rating")
    contact_naam = (data.get("contact_naam") or "").strip()
    year = datetime.now().year

    rating_str = ""
    if rating not in (None, "", 0):
        try:
            rating_str = f"{float(str(rating).replace(',', '.')):.1f}"
        except (TypeError, ValueError):
            rating_str = ""

    # About: AI-extractie van de site heeft voorkeur, anders de verrijkte DB-tekst.
    beschrijving = (extract.get("about") or "").strip() or (data.get("bedrijf_beschrijving") or "").strip()

    # ── Reviews: ALLEEN echte Google-reviews, ALLEEN goede (>=4 sterren) ──
    def _is_recent(t: str) -> bool:
        t = (t or "").lower()
        if any(w in t for w in ("uur", "dag", "dagen", "gisteren", "vandaag", "week", "weken")):
            return True
        return bool(re.search(r"\b(1|een|één)\s+maand", t))

    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for rv in (data.get("google_reviews") or []):
        try:
            r_i = int(round(float(str(rv.get("rating") or rating or 0).replace(",", "."))))
        except (TypeError, ValueError):
            r_i = 0
        if r_i < 4:  # commercieel: alleen goede reviews tonen
            continue
        text = _trim(rv.get("text"), 240)
        if not text:
            continue
        k = text.lower()[:40]
        if k in seen:
            continue
        seen.add(k)
        t = str(rv.get("time") or "").strip()
        label = "Via Google" + (f" · {t}" if _is_recent(t) else "")
        merged.append({
            "text": text,
            "author": (rv.get("author") or "Klant").strip(),
            "stars": _stars(r_i) or "★★★★★",
            "label": label,
        })
    merged = merged[:6]
    n_rev = len(merged)

    # Commerciële kadering op basis van aantal reviews.
    if n_rev <= 1:
        tm_eyebrow, tm_t1, tm_acc = "Klantervaring", "Onze meest", "recente review"
        tm_intro = "Rechtstreeks van Google."
    elif n_rev == 2:
        tm_eyebrow, tm_t1, tm_acc = "Klantervaringen", "Wat klanten", "zeggen"
        tm_intro = "Twee uitgelichte beoordelingen, rechtstreeks van Google."
    else:
        tm_eyebrow, tm_t1, tm_acc = "Klantervaringen", "Wat klanten", "zeggen"
        tm_intro = "Echte beoordelingen van klanten, rechtstreeks van Google."

    # ── Process: hun eigen stappen indien gevonden, anders generieke werkwijze ──
    # Saneer AI-output: "Stap N"-titels en "Inhoud niet beschikbaar"-bodies zijn
    # waardeloze vulling — die mogen nooit de goede generieke werkwijze verdringen.
    _AI_PH_RE = re.compile(r"niet beschikbaar|geen informatie|geen inhoud|onbekend|not available", re.I)
    site_steps = []
    for s in (extract.get("process") or []):
        _t = str(s.get("title") or "").strip()
        _b = str(s.get("body") or "").strip()
        if not _t or _AI_PH_RE.search(_t) or re.fullmatch(r"stap\s*\d+\.?", _t, re.I):
            continue
        if _AI_PH_RE.search(_b):
            _b = ""
        site_steps.append({"title": _t, "body": _b})
    if len(site_steps) >= 2:
        steps = [{"title": s["title"], "body": s.get("body", "")} for s in site_steps][:4]
        process_intro = "Zo pakken wij uw dakklus aan, helder en stap voor stap."
    else:
        steps = [
            {"title": "Adviesgesprek", "body": "We bekijken uw situatie en bespreken de mogelijkheden."},
            {"title": "Offerte op maat", "body": "U krijgt een heldere offerte met planning en materialen."},
            {"title": "Uitvoering", "body": "Het werk wordt netjes en zorgvuldig uitgevoerd."},
            {"title": "Oplevering", "body": "Een laatste controle en alles wordt opgeruimd achtergelaten."},
        ]
        process_intro = "Een helder proces met duidelijke communicatie in elke stap."

    # ── Diensten: het meest persoonlijke wint. ──
    #   1) echte dienstkaarten van de site (titel + tekst + FOTO)
    #   2) anders door AI uit de sitetekst gehaalde diensten (titel + tekst)
    #   3) anders nette standaardcategorieën
    site_service_cards = [c for c in (extract.get("service_cards") or []) if c.get("image") and c.get("title")]
    site_services = extract.get("services") or []
    services_generic = False
    if len(site_service_cards) >= 2:
        # Echte dienstnamen + teksten van de site; afbeeldingen blijven stock
        # (de detailpagina-foto's zijn niet betrouwbaar te vinden).
        services = [{"name": _trim(c["title"], 40), "desc": _trim(c.get("desc", ""), 160)}
                    for c in site_service_cards][:4]
    elif len(site_services) >= 2:
        services = [{"name": s["name"], "desc": s.get("desc", "")} for s in site_services][:4]
    else:
        services = [
            {"name": "Dak plaatsen", "desc": "Vakkundige aanleg van een nieuw dak met degelijke materialen."},
            {"name": "Dakreparatie", "desc": "Snel verhelpen van lekkages, schade en losse dakpannen."},
            {"name": "Dakvervanging", "desc": "Vervanging van een versleten dak, netjes en duurzaam uitgevoerd."},
            {"name": "Dakinspectie", "desc": "Controle van de staat van uw dak met een helder advies."},
        ]
        services_generic = True

    # ── Waarom ons: hun ECHTE pluspunten van de site, anders veilige generieke ──
    site_why = extract.get("why") or []
    if len(site_why) >= 2:
        whies = [{"title": _trim(w["title"], 34), "body": _trim(w.get("body", ""), 130)} for w in site_why][:4]
    else:
        whies = [
            {"title": "Vrijblijvende offerte", "body": "U ontvangt vooraf een duidelijke offerte, zonder verplichtingen."},
            {"title": "Persoonlijk contact", "body": "Korte lijnen en heldere communicatie van begin tot eind."},
            {"title": (f"Lokaal in {city}" if city else "Lokaal actief"),
             "body": (f"Actief in {city} en omgeving, dus snel ter plaatse." if city
                      else "Actief in de regio, dus snel bij u ter plaatse.")},
            {"title": "Vakkundig uitgevoerd", "body": "Net en zorgvuldig werk met oog voor een goede afwerking."},
        ]

    # ── Cijfers boven 'Waarom ons': vaste, geprioriteerde pool van 8 kandidaten.
    #    Vul de eerste 4 die we ECHT van de site kunnen afleiden (nooit verzinnen).
    #    Bewust GEEN sterren-rating (zit in de hero) en GEEN cert/verzekerd/garantie
    #    (zitten in de trust-marks). Werkgebied past zich aan de site aan. ──
    def _exp_value(raw):
        m = re.search(r"(19|20)\d{2}", raw or "")
        if m:
            yrs = year - int(m.group())
            if yrs < 3:
                return ""
            return f"{(yrs // 5) * 5}+" if yrs >= 10 else f"{yrs}+"
        m2 = re.search(r"\d{1,3}", raw or "")
        return f"{int(m2.group())}+" if m2 and int(m2.group()) >= 3 else ""

    def _count_value(raw):
        m = re.search(r"\d[\d.\s]{0,6}\d|\d", raw or "")
        return (m.group().replace(" ", "") + "+") if m else ""

    # RULES 7b: nooit een plaats/regio/werkgebied als stat — locatie hoort in
    # hero/over-ons, niet in de cijferbalk (past ook niet op 390px).
    _pool = []
    _ev = _exp_value(extract.get("years_experience"))
    if _ev:
        _pool.append({"value": _ev, "label": "Jaar ervaring"})
    if extract.get("emergency"):
        _pool.append({"value": "24/7", "label": "Bereikbaar bij spoed"})
    if extract.get("free_quote"):
        _pool.append({"value": "Gratis", "label": "Offerte & inspectie"})
    if extract.get("own_team"):
        _pool.append({"value": "100%", "label": "Eigen vaste ploeg"})
    if extract.get("fast_response"):
        _pool.append({"value": "<24u", "label": "Reactie op aanvraag"})
    _pd = _count_value(extract.get("projects_done"))
    if _pd:
        _pool.append({"value": _pd, "label": "Daken opgeleverd"})
    _cu = _count_value(extract.get("customers"))
    if _cu:
        _pool.append({"value": _cu, "label": "Tevreden klanten"})
    # dedup op waarde (geen 2x 'Gratis'/'24/7'), behoud prioriteitsvolgorde.
    # RULES 7b: max 3 stats — met 4 breekt de balk op mobiel (390px).
    _seen, stats = set(), []
    for _s in _pool:
        k = _s["value"].lower()
        if k in _seen:
            continue
        _seen.add(k)
        stats.append(_s)
        if len(stats) >= 3:
            break
    show_stats = len(stats) >= 2

    # Dedup: laat why-punten weg die hetzelfde cijfer/onderwerp als een stat tonen.
    _stat_words = set()
    for s in stats:
        for w in re.findall(r"[a-z0-9/]+", (s["label"] + " " + s["value"]).lower()):
            if len(w) >= 3 or "/" in w:
                _stat_words.add(w)
    _stop = {"jaar", "de", "het", "een", "van", "ons", "onze"}

    def _dups_stat(title: str) -> bool:
        words = [w for w in re.findall(r"[a-z0-9/]+", title.lower()) if w not in _stop]
        return any(w in _stat_words for w in words)

    # Why ontdubbelen t.o.v. de cijfers + aanvullen met veilige, niet-numerieke redenen.
    generic_why = [
        {"title": "Persoonlijk contact", "body": "Korte lijnen en heldere communicatie van begin tot eind."},
        {"title": (f"Lokaal in {city}" if city else "Lokaal actief"),
         "body": (f"Actief in {city} en omgeving, dus snel ter plaatse." if city
                  else "Actief in de regio, dus snel bij u ter plaatse.")},
        {"title": "Vrijblijvende offerte", "body": "U ontvangt vooraf een duidelijke offerte, zonder verplichtingen."},
        {"title": "Vakkundig uitgevoerd", "body": "Net en zorgvuldig werk met oog voor een goede afwerking."},
    ]
    whies = [w for w in whies if not _dups_stat(w["title"])]
    for g in generic_why:
        if len(whies) >= 4:
            break
        if any(g["title"].lower() == w["title"].lower() for w in whies) or _dups_stat(g["title"]):
            continue
        whies.append(g)
    whies = whies[:4]

    projects = [p for p in (extract.get("project_cards") or []) if p.get("image") and p.get("title")][:3]
    certified = bool(extract.get("certified"))
    insured = bool(extract.get("insured"))
    guarantee = bool(extract.get("guarantee"))

    td: dict[str, Any] = {
        "META_TITLE": f"{naam} — Dakdekker" + (f" in {city}" if city else ""),
        "COMPANY_NAME": naam,
        "COMPANY_TAGLINE": "",
        "NAV_HOME": "Home", "NAV_ABOUT": "Over ons", "NAV_SERVICES": "Diensten",
        "NAV_TEAM": "Team", "NAV_PROJECTS": "Projecten", "NAV_CONTACT": "Contact",
        "CTA_QUOTE": "Offerteaanvraag",
        # Hero-eyebrow badge: alleen "24/7 bereikbaar" als de partij dat echt
        # biedt, anders leeg → de template verbergt het badge (geen filler).
        "HERO_BADGE": ("24/7 bereikbaar" if extract.get("emergency") else ""),
        "HERO_TITLE_LINE_1": "Vakwerk voor elk",
        "HERO_TITLE_LINE_2": "dak boven uw hoofd",
        "CITY": city,
        "HERO_CTA_PRIMARY": "Neem contact op",
        "HERO_RATING": rating_str,
        "HERO_REVIEW_COUNT": ("Klantbeoordeling op Google" if rating_str else ""),
        "QUOTE_HEADING": "", "QUOTE_FIELD_NAME": "", "QUOTE_FIELD_PHONE": "",
        "QUOTE_FIELD_EMAIL": "", "QUOTE_FIELD_SERVICE": "", "QUOTE_FIELD_POSTCODE": "",
        "QUOTE_FIELD_ADDRESS": "", "QUOTE_FIELD_MESSAGE": "", "QUOTE_SUBMIT": "",

        "ABOUT_EYEBROW": "Over ons",
        "ABOUT_TITLE_1": "Uw vertrouwde",
        "ABOUT_TITLE_ACCENT": "dakdekker",
        "ABOUT_TITLE_2": (f"in {city}" if city else ""),
        "ABOUT_BODY": beschrijving or (ai_content.get("local_intro") or ""),
        "ABOUT_CTA_PRIMARY": "Meer over ons",
        "ABOUT_CALLBACK": "Vraag terugbelverzoek aan",

        "WHY_EYEBROW": "Waarom ons",
        "WHY_TITLE_1": "Waarom kiezen voor",
        "WHY_TITLE_ACCENT": "ons",
        "WHY_INTRO": "Betrouwbaar dakwerk met heldere afspraken en persoonlijk contact.",

        "SERVICES_EYEBROW": "Diensten",
        "SERVICES_TITLE_1": "Uw", "SERVICES_TITLE_ACCENT": "dakwerk",
        "SERVICES_TITLE_2": "volledig verzorgd",

        "PROCESS_EYEBROW": "Hoe wij werken",
        "PROCESS_TITLE_1": "Van offerte tot",
        "PROCESS_TITLE_ACCENT": "oplevering",
        "PROCESS_INTRO": process_intro,
        "PROCESS_CTA": "Vraag een offerte",

        "PROJECTS_EYEBROW": "Projecten",
        "PROJECTS_TITLE_1": "Onze uitgelichte",
        "PROJECTS_TITLE_ACCENT": "projecten",
        "PROJECTS_INTRO": "Een greep uit recent werk.",

        "TEAM_EYEBROW": "Ons team",
        "TEAM_TITLE_1": "Maak kennis met",
        "TEAM_TITLE_ACCENT": "het team",
        "TEAM_INTRO": "De mensen die uw dak verzorgen.",
        "TEAM_CTA": "Team",
        "TEAM_1_NAME": contact_naam, "TEAM_1_ROLE": ("Aanspreekpunt" if contact_naam else ""),
        "TEAM_2_NAME": "", "TEAM_2_ROLE": "",
        "TEAM_3_NAME": "", "TEAM_3_ROLE": "",
        "TEAM_4_NAME": "", "TEAM_4_ROLE": "",

        "TM_EYEBROW": tm_eyebrow,
        "TM_TITLE_1": tm_t1,
        "TM_TITLE_ACCENT": tm_acc,
        "TM_INTRO": tm_intro,

        "ART_EYEBROW": "", "ART_TITLE_1": "", "ART_TITLE_ACCENT": "", "ART_TITLE_2": "",
        "ART_1_TAG": "", "ART_1_DATE": "", "ART_1_TITLE": "",
        "ART_2_TAG": "", "ART_2_DATE": "", "ART_2_TITLE": "",
        "ART_3_TAG": "", "ART_3_DATE": "", "ART_3_TITLE": "",
        "ART_CTA": "",

        "FOOT_ABOUT": (f"Dakwerk in {city} en omgeving." if city else "Vakkundig dakwerk in de regio."),
        "FOOT_COL1_TITLE": "Diensten", "FOOT_COL2_TITLE": "Bedrijf", "FOOT_COL3_TITLE": "Contact",
        "FOOT_PHONE": data.get("telefoonnummer") or "",
        "FOOT_EMAIL": data.get("email") or "",
        "FOOT_ADDRESS": data.get("adres") or "",
        "FOOT_COPYRIGHT": f"© {year} {naam}." if naam else f"© {year}.",
        "LOGO_URL": data.get("logo_url") or "",

        # ── Zichtbaarheidsvlaggen ──
        "SHOW_STATS": show_stats,
        "SHOW_PROJECTS": len(projects) >= 2,
        "SHOW_ARTICLES": False,
        "SHOW_WHY": True,
        "SHOW_TEAM": True,

        # ── Interne data voor de baking-stap (underscore = niet in de embed-JSON) ──
        "_STEPS": steps,
        "_SERVICES": services,
        "_REVIEWS": merged,
        "_PROJECTS": projects,
        "_CERTIFIED": certified,
        "_INSURED": insured,
        "_GUARANTEE": guarantee,
        "_ACCENT": accent,
        "_DARK": dark,
        "_SERVICES_GENERIC": services_generic,
        "_N_REVIEWS": n_rev,
        "_WHY": whies,
        "_STATS": stats,
    }

    for i in range(1, 5):
        sd = stats[i - 1] if i - 1 < len(stats) else None
        td[f"STAT_{i}_VALUE"] = sd["value"] if sd else ""
        td[f"STAT_{i}_LABEL"] = sd["label"] if sd else ""

    # Steps/services ook als platte data-tpl waarden (voor de eerste N kaarten).
    for i in range(1, 5):
        st = steps[i - 1] if i - 1 < len(steps) else None
        td[f"STEP_{i}_TITLE"] = st["title"] if st else ""
        td[f"STEP_{i}_BODY"] = st.get("body", "") if st else ""
        wy = whies[i - 1] if i - 1 < len(whies) else None
        td[f"WHY_{i}_TITLE"] = wy["title"] if wy else ""
        td[f"WHY_{i}_BODY"] = wy.get("body", "") if wy else ""
    # Diensten tot 6 (Template C toont een langere diensten-lijst dan A/B).
    # Lege slots -> "" zodat de bake de overtollige dienst-rij verwijdert i.p.v.
    # er hardcoded template-tekst in te laten staan (heilige no-fabricatie-regel).
    for i in range(1, 7):
        sv = services[i - 1] if i - 1 < len(services) else None
        td[f"SERVICE_{i}_NAME"] = sv["name"] if sv else ""
        td[f"SERVICE_{i}_DESC"] = sv.get("desc", "") if sv else ""

    # Testimonials ook plat (eerste 4) voor de no-JS fallback; baking maakt ze dynamisch.
    for i in range(1, 5):
        rev = merged[i - 1] if i - 1 < len(merged) else None
        td[f"TM_{i}_QUOTE"] = rev["text"] if rev else ""
        td[f"TM_{i}_NAME"] = (rev["author"] or "Klant") if rev else ""
        td[f"TM_{i}_LABEL"] = rev["label"] if rev else ""
        td[f"TM_{i}_STARS"] = rev["stars"] if rev else ""

    # Echte site-foto's (alt + url) voor het vullen van dienst-/why-slots in de bake.
    # RULES 7: pool eerst verifiëren op echte beeldkenmerken (geen logo's,
    # keurmerken, badges of near-duplicates in content-slots).
    # De dienst-detailfoto's tellen mee: sites met lazy-loading (lege img-src
    # op de homepage) hebben hun beste foto's juist op de dienstpagina's.
    _raw_pool = list(extract.get("image_pool") or [])
    _pool_urls = {it.get("url") for it in _raw_pool}
    for c in (extract.get("service_cards") or []):
        for cit in (c.get("images") or
                    ([{"url": c["image"], "alt": c.get("title") or ""}] if c.get("image") else [])):
            if cit.get("url") and cit["url"] not in _pool_urls:
                _raw_pool.append({"url": cit["url"], "alt": cit.get("alt") or c.get("title") or ""})
                _pool_urls.add(cit["url"])
    td["_IMAGE_POOL"] = _filter_image_pool(_raw_pool, data.get("logo_url") or "")

    return td


def _bake_dakdekker_dom(html: str, td: dict[str, Any]) -> str:
    """Bak de echte waarden + sectie-zichtbaarheid server-side in de raw HTML.

    Hierdoor staat er NERGENS nep-default-data in de bron (ook niet voor
    no-JavaScript bezoekers of scrapers): lege/onbekende velden worden leeggezet
    en secties zonder echte data worden volledig verwijderd.
    """
    from urllib.parse import quote_plus
    from copy import copy as _copy

    soup = BeautifulSoup(html, "html.parser")

    def is_set(key: str) -> bool:
        return str(td.get(key) or "").strip() != ""

    def _strip_tpl(node) -> None:
        if node.has_attr("data-tpl"):
            del node["data-tpl"]
        for el in node.select("[data-tpl]"):
            del el["data-tpl"]

    # <title>
    if td.get("META_TITLE"):
        title_el = soup.find("title")
        if title_el:
            title_el.string = str(td["META_TITLE"])

    # ── Echte merkkleuren toepassen (override op de :root tokens) ──
    accent, dark = td.get("_ACCENT"), td.get("_DARK")
    _dark_theme = bool(td.get("_DARK_THEME"))
    if accent:
        def _mixw(h: str, amt: float) -> str:
            r, g, b = _hex_to_rgb(h)
            return "#%02x%02x%02x" % (
                int(r + (255 - r) * amt), int(g + (255 - g) * amt), int(b + (255 - b) * amt))

        def _mixd(h: str, amt: float, base: str = "#0c1422") -> str:
            r, g, b = _hex_to_rgb(h)
            br, bg_, bb = _hex_to_rgb(base)
            return "#%02x%02x%02x" % (
                int(r + (br - r) * amt), int(g + (bg_ - g) * amt), int(b + (bb - b) * amt))
        dark = dark or _darken(accent, 0.42)
        # In de dark-variant is "accent-soft" een dónkere tint van het accent
        # (mix richting de donkere achtergrond) i.p.v. een pasteltint.
        accent_soft = _mixd(accent, 0.78) if _dark_theme else _mixw(accent, 0.85)
        css = (
            ":root{"
            f"--c-accent:{accent} !important;"
            f"--c-accent-soft:{accent_soft} !important;"
            f"--c-dark:{dark} !important;"
            f"--c-dark-soft:{_mixw(dark, 0.12)} !important;"
            "}"
        )
        if _dark_theme:
            # RULES 3/3b: contrast is heilig — de tekst-accentrol (--blue-strong:
            # hero-highlight "in <plaats>", telefoonnummer, badges) bridgt naar
            # de donkere merkkleur en valt op donker volledig weg. Geef hem een
            # gegarandeerd leesbare toon: het accent zelf als dat licht genoeg
            # is, anders een opgelichte versie.
            _strong = accent if _brightness(accent) >= 130 else _mixw(accent, 0.55)
            css += f"body.harv-dark{{--blue-strong:{_strong};}}"
        # RULES 4 (les Van Gelder/Roofix): gebruikt de lead op de eigen site een
        # duidelijke knopkleur (geel, lime, oranje…), dan krijgen ÓNZE knoppen
        # die kleur — in licht én donker thema. Knopkleuren zijn knopkleuren.
        _cta_col = td.get("_CTA_COLOR")
        if _cta_col and _color_distance(_cta_col, accent) < 60 and not _dark_theme:
            # CTA mag de hoofdkleur niet erven (RULES 4): valt de knopkleur
            # samen met het accent, hou dan de eigen amber van de template.
            _cta_col = None
        if not _cta_col and _dark_theme and _brightness(accent) >= 110 and not _is_neutralish(accent):
            _cta_col = accent  # dark-fallback: fel accent = CTA (Roofix-lime)
        if _cta_col and not _is_neutralish(_cta_col) and 60 <= _brightness(_cta_col) <= 240:
            # Tekst op de CTA: zwart zodra de kleur licht is (wit op gifgroen/geel
            # leest niet — feedback 2026-06-11).
            _on_cta = "#0c1605" if _brightness(_cta_col) >= 120 else "#ffffff"
            _tint = _mixd(_cta_col, 0.80) if _dark_theme else _mixw(_cta_col, 0.85)
            _scope = "body.harv-dark" if _dark_theme else ":root"
            css += (
                _scope + "{"
                f"--amber:{_cta_col};"
                f"--amber-strong:{_darken(_cta_col, 0.86)};"
                f"--amber-deep:{_darken(_cta_col, 0.45)};"
                f"--amber-tint:{_tint};"
                f"--on-amber:{_on_cta};"
                f"--shadow-amber:0 6px 18px {_cta_col}55;"
                "}"
            )
        head = soup.find("head")
        if head:
            st = soup.new_tag("style", id="harv-brand")
            st.string = css
            head.append(st)

    # ── Dark-variant: token-overrides leven in de template (body.harv-dark) ──
    if _dark_theme:
        body_el = soup.find("body")
        if body_el is not None:
            body_el["class"] = (body_el.get("class") or []) + ["harv-dark"]

    # ── Esthetische + responsieve tweaks ──
    n_steps = len([s for s in (td.get("_STEPS") or []) if s.get("title")]) or 4
    n_rev = int(td.get("_N_REVIEWS") or 0)
    n_stats = len([s for s in (td.get("_STATS") or []) if s.get("value")]) or 4
    tm_cols = n_rev if 1 <= n_rev <= 4 else 3
    desktop = (
        "@media(min-width:760px){"
        "body:not(.preview-mobile) .process-grid{grid-template-columns:repeat(" + str(min(n_steps, 4)) + ",1fr)}"
        "body:not(.preview-mobile) .tm-grid{grid-template-columns:repeat(" + str(tm_cols) + ",1fr)}"
        # Cijfers passend op het echte aantal (niet de grootte van 4)
        "body:not(.preview-mobile) .stats{grid-template-columns:repeat(" + str(min(n_stats, 4)) + ",1fr)}"
        # USP's uitlijnen met de afbeeldingen (rechterkolom iets lager)
        "body:not(.preview-mobile) .why-right{padding-top:92px}}"
    )
    head = soup.find("head")
    if head:
        tw = soup.new_tag("style", id="harv-tweaks")
        tw.string = (
            "#about{padding-bottom:36px}"
            ".why-left h2{margin-bottom:24px}"
            ".why-list{gap:2px}"
            ".why-item{padding:14px 0}"
            ".process-head{margin-bottom:26px}"
            ".tm-head{align-items:center}"
            # Mobiele actiebalk (telefoon-icoon + offerteaanvraag), sticky onderin
            ".harv-mobilebar{display:none;position:sticky;bottom:0;z-index:9000;gap:10px;padding:10px 14px;"
            "background:rgba(255,255,255,.96);border-top:1px solid var(--c-line);box-shadow:0 -6px 20px rgba(0,0,0,.10)}"
            ".harv-mobilebar a{display:inline-flex;align-items:center;justify-content:center;gap:8px;"
            "border-radius:12px;font-weight:700;font-size:15px;text-decoration:none;height:50px}"
            ".harv-mobilebar .harv-mb-phone{width:50px;flex:0 0 50px;background:var(--c-accent);color:#fff}"
            ".harv-mobilebar .harv-mb-quote{flex:1;background:var(--c-dark);color:#fff}"
            "body.harv-dark .harv-mobilebar{background:rgba(13,20,36,.97);border-top:1px solid rgba(255,255,255,.08)}"
            "body.preview-mobile.harv-intro-done .harv-mobilebar{display:flex}"
            "body.preview-mobile .sticky-cta{display:none}"
            "@media(max-width:759px){body.harv-intro-done .harv-mobilebar{display:flex}.sticky-cta{display:none}}"
            # Telefoonframe: actiebalk volledig tot onderaan (geen gat)
            "body.preview-mobile .page{padding-bottom:0}"
            + desktop +
            "body.preview-mobile .process-grid{grid-template-columns:1fr}"
            "body.preview-mobile .tm-grid{grid-template-columns:1fr}"
            # RULES 7b: stats-balk op mobiel als compacte grid (display:grid is
            # nodig — template A's .stats is flex, waar kolommen niets doen en
            # de rij anders buiten beeld loopt op 390px), max 3 naast elkaar.
            # RULES 7b: mobiel gecentreerd, met een consequent scheidingslijntje
            # tussen ALLE cijfers (de template-eigen even/odd-regel sloeg er één
            # over) — currentColor-mix zodat het in licht én donker leesbaar is.
            # Stats-layout op mobiel komt UIT DE TEMPLATE (RULES 7b: vaste
            # 3-koloms gecentreerde rij). De generator injecteert hier bewust
            # NIETS meer overheen — eerdere injecties vochten met de template
            # en stapelden de cijfers onder elkaar (feedback 2026-06-11).
            "@media(max-width:759px){.process-grid{grid-template-columns:1fr}.tm-grid{grid-template-columns:1fr}}"
        )
        head.append(tw)

    # 'Waarom ons' iets hoger laten beginnen.
    whysec = soup.find(id="why")
    if whysec:
        whysec["style"] = "padding-top:28px"

    # ── Mobiele actiebalk: tel-icoon + offerteaanvraag, in het telefoonframe ──
    page = soup.select_one(".page")
    if page:
        phone = str(td.get("FOOT_PHONE") or "").strip()
        tel = "tel:" + re.sub(r"[^0-9+]", "", phone) if phone else ""
        bar = '<div class="harv-mobilebar">'
        if tel:
            bar += (f'<a class="harv-mb-phone" href="{tel}" aria-label="Bel ons">'
                    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
                    '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 '
                    '19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.37 1.9.7 2.81a2 2 0 0 1-.45 '
                    '2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.33 1.85.57 2.81.7A2 2 0 0 1 22 16.92Z"/>'
                    '</svg></a>')
        bar += '<a class="harv-mb-quote" href="#quote">Offerteaanvraag</a></div>'
        page.append(BeautifulSoup(bar, "html.parser"))
        # Mobiele balk pas tonen NA de welkomst-animatie (niet vooraf).
        reveal = (
            "(function(){function d(){document.body.classList.add('harv-intro-done');}"
            "var ov=document.getElementById('intro-overlay');"
            "if(!ov){d();return;}setTimeout(d,5400);"
            "var s=ov.querySelector('.intro-skip');if(s)s.addEventListener('click',d);})();"
        )
        sc = soup.new_tag("script")
        sc.string = reveal
        page.append(sc)

    # data-tpl text bindings (echte waarde, leeg toegestaan)
    for el in soup.select("[data-tpl]"):
        key = el.get("data-tpl")
        if key in td:
            el.string = str(td[key])

    # data-tpl-attr attribuut-bindings ("LOGO_URL:src")
    for el in soup.select("[data-tpl-attr]"):
        for pair in (el.get("data-tpl-attr") or "").split(","):
            if ":" in pair:
                k, attr = [s.strip() for s in pair.split(":", 1)]
                if k in td and attr:
                    el[attr] = str(td[k])

    # Logo vs naam-tekst: precies één tonen
    if is_set("LOGO_URL"):
        for e in soup.select(".logo-text"):
            e.decompose()
    else:
        for e in soup.select(".logo-img"):
            e.decompose()

    # ── RULES 5 (template B): het dakje-icoon is nooit een logo(vervanger) —
    #    ook niet in de footer of de mobiele nav. Mét logo: overal het echte
    #    logo; zonder: initialen-monogram in de merkkleur.
    brand_marks = soup.select(".brand-mark")
    if brand_marks:
        _logo = str(td.get("LOGO_URL") or "").strip()
        for bm in brand_marks:
            bsvg = bm.find("svg")
            bimg = bm.select_one(".brand-logo-img")
            if _logo:
                if bimg is None:
                    bimg = soup.new_tag("img", attrs={"class": "brand-logo-img", "alt": ""})
                    bimg["onerror"] = "this.removeAttribute('src')"
                    bm.insert(0, bimg)
                bimg["src"] = _logo
                if bsvg:
                    bsvg.decompose()
            else:
                # RULES 5 (besluit 2026-06-11): een merkteken verzinnen mag
                # NIET — ook geen monogram/initialen. Geen logo gevonden ->
                # alleen de bedrijfsnaam als tekst, het merk-vak verdwijnt.
                bm.decompose()
        head_b = soup.find("head")

        # RULES 5: bevat het logo de bedrijfsnaam al, zet de naam er dan niet
        # nóg eens naast — logo groter, tekst weg. Primair beslist de
        # vision-check (ai_select_images keek letterlijk naar het logo);
        # alleen zonder uitslag valt de brede-beeld-heuristiek in.
        if _logo:
            # Wordmark = vision-ja ÓF duidelijk breed beeld. OR-logica: het
            # vision-antwoord wisselt per run; een breed logo (aspect >= 2.6)
            # bevat vrijwel altijd de naam — dat signaal mag een AI-"false"
            # niet verliezen.
            _ai_word = (td.get("_AI_IMGS") or {}).get("logo_contains_name")
            lmeta = None
            if _logo.lower().split("?")[0].endswith(".svg"):
                _asp = _svg_aspect(_logo)
            else:
                lmeta = _image_meta(_logo)
                _asp = (lmeta["w"] / max(1.0, float(lmeta["h"]))) if lmeta else None
            _ai_word = bool(_ai_word) or bool(_asp and _asp >= 2.6)

            # RULES 5: logo altijd leesbaar op zijn achtergrond. Een "light"-
            # logo (wit/zeer licht) valt weg op het lichte thema -> donker
            # chipje erachter; een zeer donker logo op de dark-variant -> licht
            # chipje. Gemeten op de echte pixels, geen aannames.
            _lum = (lmeta or {}).get("lum")
            _chip = None
            if _lum is not None:
                if not _dark_theme and _lum > 195:
                    _chip = "var(--c-dark, #1a2334)"
                elif _dark_theme and _lum < 60:
                    _chip = "#ffffff"
            if _chip and head_b:
                cst = soup.new_tag("style", id="harv-logochip")
                cst.string = (
                    ".header .brand-mark:has(.brand-logo-img[src]:not([src=\"\"])){"
                    f"background:{_chip};border-radius:9px;padding:4px 10px}}")
                head_b.append(cst)
            if _ai_word:
                for bt in soup.select(".brand .brand-txt"):
                    bt.decompose()
                if head_b:
                    # Wordmark mag de balk nooit uitgroeien. EXPLICIETE hoogte
                    # op de img zelf: met height:100% + max-width won max-width
                    # en schaalde de hoogte mee (84px in een 34px-vak -> alleen
                    # het topje zichtbaar). object-fit:contain vangt brede
                    # logo's die tegen de max-width aanlopen.
                    wst = soup.new_tag("style", id="harv-wordmark")
                    wst.string = (
                        ".brand-mark{width:auto;min-width:34px;height:34px;max-width:184px;"
                        "background:transparent;border-radius:0}"
                        ".brand-logo-img{height:34px;width:auto;max-width:180px;"
                        "border-radius:0;object-fit:contain}"
                        ".footer .brand-mark{height:auto}")
                    head_b.append(wst)

    # Hero-rating/reviews alleen bij echte Google-rating
    if not is_set("HERO_RATING"):
        for e in soup.select(".reviews, .ms-reviews"):
            e.decompose()

    # ── Trust-marks: waarheidsgetrouw op basis van site-signalen ──
    # Markup-volgorde van de template: [0]=cert, [1]=verzekerd, [2]=garantie.
    # Belangrijk: NOOIT het icoon-vakje (.tm-ic) overschrijven (dan komt er tekst
    # in het icoon en overlapt het). Alleen .tm-num/.tm-sub vullen, en alleen met
    # GENERIEK-ware labels (nooit een specifiek keurmerk als "VEBIDAK" of een
    # termijn als "10 jaar" verzinnen). Niet-gedetecteerde marks worden weggelaten.
    def _set_mark(mark, num_txt, sub_txt):
        n = mark.select_one(".tm-num")
        s = mark.select_one(".tm-sub")
        if n:
            n.string = num_txt
        if s:
            s.string = sub_txt
        mark["title"] = num_txt
    marks = soup.select(".trust-marks .trust-mark")
    if len(marks) >= 3:
        m_cert, m_insured, m_guar = marks[0], marks[1], marks[2]
        if td.get("_CERTIFIED"):
            _set_mark(m_cert, "Gecertificeerd", "erkend dakdekkersbedrijf")
        else:
            m_cert.decompose()
        if td.get("_INSURED"):
            _set_mark(m_insured, "Verzekerd", "bedrijfs- & aansprakelijkheid")
        else:
            m_insured.decompose()
        if td.get("_GUARANTEE"):
            _set_mark(m_guar, "Garantie", "op materiaal & uitvoering")
        else:
            m_guar.decompose()

    # Stats (count-up): tonen bij echte cijfers; lege kaarten weg.
    # RULES 7b: de template-defaults in data-to (20 / 4,9 / 3.000) zouden anders
    # over de échte lead-waarde heen animeren ("3.000+ Bereikbaar bij spoed").
    # Patch data-to naar de lead-waarde; niet-numeriek (24/7, Gratis, <24u)
    # animeert niet maar staat er direct.
    if td.get("SHOW_STATS") is True:
        for i, st in enumerate(soup.select(".stats .stat"), 1):
            if not is_set(f"STAT_{i}_VALUE"):
                st.decompose()
                continue
            cnt = st.select_one(".count")
            if cnt is None:
                continue  # template A: statische waarde, geen count-up
            val = str(td.get(f"STAT_{i}_VALUE") or "").strip()
            suf = st.select_one(".suf")
            m_dec = re.fullmatch(r"\d+,\d", val)
            m_int = re.fullmatch(r"(\d[\d.]*)\s*(\+?)", val)
            if m_dec:
                cnt["data-to"] = val
                cnt["data-decimal"] = ""
                if cnt.has_attr("data-tpl"):
                    del cnt["data-tpl"]  # runtime-hydration niet óver de teller heen
                if suf:
                    suf.decompose()
            elif m_int:
                cnt["data-to"] = m_int.group(1)
                if cnt.has_attr("data-decimal"):
                    del cnt["data-decimal"]
                if cnt.has_attr("data-tpl"):
                    del cnt["data-tpl"]  # anders schrijft hydration "22+" naast .suf → "22++"
                cnt.string = m_int.group(1)
                if suf and not m_int.group(2):
                    suf.decompose()
                elif not suf and m_int.group(2):
                    nsuf = soup.new_tag("span", attrs={"class": "suf"})
                    nsuf.string = "+"
                    cnt.insert_after(nsuf)
            else:
                # Geen telbaar getal: count-up uitschakelen, waarde laten staan.
                cnt["class"] = [c for c in (cnt.get("class") or []) if c != "count"]
                if cnt.has_attr("data-to"):
                    del cnt["data-to"]
                if cnt.has_attr("data-decimal"):
                    del cnt["data-decimal"]
                if suf:
                    suf.decompose()
    else:
        for e in soup.select(".stats"):
            e.decompose()

    # ── Process: trim naar het echte aantal stappen ──
    for i, step in enumerate(soup.select(".process-grid .step"), 1):
        if not is_set(f"STEP_{i}_TITLE"):
            step.decompose()

    # ── Diensten: stock-afbeeldingen; de 'platte daken'-kaart krijgt de plat-dak foto ──
    _FLAT_IMG = "/demo-assets/dakdekkers/service-4-inspect.jpg"   # echte plat-dak foto
    _STOCK_POOL = [
        "/demo-assets/dakdekkers/service-1-install.jpg",
        "/demo-assets/dakdekkers/service-2-repair.jpg",
        "/demo-assets/dakdekkers/service-3-replace.jpg",
    ]
    _svc_rows = []
    for i, row in enumerate(soup.select(".services-list .service-row"), 1):
        if not is_set(f"SERVICE_{i}_NAME"):
            row.decompose()
        else:
            _svc_rows.append(row)
    _pool = td.get("_IMAGE_POOL") or []
    _ai_imgs = td.get("_AI_IMGS") or {}
    _ai_svc = _ai_imgs.get("services") or {}
    _ai_hero = list(_ai_imgs.get("hero") or [])      # hero-foto's: niet hergebruiken
    _ai_approved = list(_ai_imgs.get("approved") or [])  # ALLE door de keuring gekomen foto's
    _used: set = set(_ai_svc.values()) | set(_ai_imgs.get("why") or []) | set(_ai_hero)
    _pi = 0
    # De eerste 3 dienst-foto's komen van de eigen site. Volgorde: AI-keuze ->
    # alt-match -> nog ongebruikte GOEDGEKEURDE sitefoto -> pas daarna stock.
    # Cruciaal (strenge keuring): blind-fill put UITSLUITEND uit `approved` — nooit
    # uit de rauwe pool, want een niet-goedgekeurde restfoto is vermoedelijk
    # schimmel/open dak/keurmerk/tekst en mag de site niet in.
    _leftover = [u for u in _ai_approved if u not in _used]
    _blind_fill_ok = bool(_ai_approved)  # alleen blind-fill als er goedgekeurde foto's zijn
    for idx, row in enumerate(_svc_rows):
        h3 = row.select_one("h3")
        slot = row.select_one(".img-slot")
        if not slot:
            continue
        nm = h3.get_text(" ", strip=True) if h3 else ""
        real = _ai_svc.get(nm) or _match_image(nm, _pool, _used)
        if not real and idx < 3 and _blind_fill_ok:
            _leftover = [u for u in _leftover if u not in _used]
            if _leftover:
                real = _leftover.pop(0)
        if real:
            _used.add(real)
            slot["style"] = f"--bg-img: url('{real}')"
        elif "plat" in nm.lower():
            slot["style"] = f"--bg-img: url('{_FLAT_IMG}')"
        else:
            slot["style"] = f"--bg-img: url('{_STOCK_POOL[_pi % len(_STOCK_POOL)]}')"
            _pi += 1

    # Template B: de dienst-kaarten zijn .svc-artikelen met een <img> in
    # .svc-img — een ANDERE markup dan A's .service-row/.img-slot. Dit was de
    # reden dat B-demo's nooit eigen sitefoto's in de diensten kregen.
    _svc_cards_b = soup.select(".svc-grid .svc")
    if _svc_cards_b:
        _leftover = [u for u in _ai_approved if u not in _used]
        for idx, card in enumerate(_svc_cards_b):
            h3 = card.select_one("h3")
            img = card.select_one(".svc-img img")
            if img is None:
                continue
            nm = h3.get_text(" ", strip=True) if h3 else ""
            real = _ai_svc.get(nm) or _match_image(nm, _pool, _used)
            if not real and idx < 3 and _blind_fill_ok:
                _leftover = [u for u in _leftover if u not in _used]
                if _leftover:
                    real = _leftover.pop(0)
            if real:
                _used.add(real)
                img["src"] = real
                if img.get("loading"):
                    del img["loading"]

    # ── Template C: diensten = lange tekst-lijst (.svc-list .svc, max 6) + ÉÉN
    #    sticky dienst-foto (.svc-photo img). C gebruikt bewust 2 hero + 1 dienst
    #    eigen foto's; de rest blijft bundled stock (top-backups). ──
    _c_svc_rows = soup.select(".svc-list .svc")
    if _c_svc_rows:
        for idx, row in enumerate(_c_svc_rows, 1):
            # Rij verwijderen als zijn dienst-naam leeg is (geen Verhoeven-restjes).
            if not str(td.get(f"SERVICE_{idx}_NAME") or "").strip():
                row.decompose()
        # De sticky dienst-foto: beste goedgekeurde dienst-/site-foto, anders stock.
        c_svc_photo = soup.select_one(".svc-photo img")
        if c_svc_photo is not None:
            _svc_pick = next(iter(_ai_svc.values()), None) \
                or next((u for u in _ai_approved if u not in _used), None)
            _C_SVC_BACKUP = "/demo-assets/dakdekkers/service-1-install.jpg"
            c_svc_photo["src"] = _svc_pick or _C_SVC_BACKUP
            c_svc_photo["onerror"] = f"this.onerror=null;this.src='{_C_SVC_BACKUP}'"
            if _svc_pick:
                _used.add(_svc_pick)

    # ── Template C: feitenband + review-rating zijn echte-data-of-weg (heilige
    #    no-fabricatie-regel). Geen rating → verwijder het rating-feit én de
    #    review-score; leeg STAT_n → verwijder dat feit. ──
    _c_facts = soup.select(".facts .fact")
    if _c_facts:
        _has_rating = bool(str(td.get("HERO_RATING") or "").strip())
        for f in _c_facts:
            kind = f.get("data-fact") or ""
            if kind == "rating" and not _has_rating:
                f.decompose()
            elif kind.startswith("stat"):
                n = kind.replace("stat", "")
                if not str(td.get(f"STAT_{n}_VALUE") or "").strip():
                    f.decompose()
        if not _has_rating:
            sc = soup.select_one(".rev-score")
            if sc is not None:
                sc.decompose()

    # ── Why-mozaïek: vul met echte (nog ongebruikte) sitefoto's; anders de stock ──
    # RULES 7: alleen dak-relevante foto's (geen portretten/kantoor/interieur).
    # Keywords matchen op BESTANDSNAAM + alt — niet op de hele URL, want de
    # domeinnaam van een dakdekker bevat zelf al "dak". Portret/vierkant
    # (h >= w) is vrijwel nooit dakwerk -> overslaan.
    _DAK_KW = ("dak", "roof", "bitumen", "epdm", "pannen", "lood", "zink",
               "goot", "schoorsteen", "isolat", "renovat", "storm", "lekkage")

    def _dak_relevant(it: dict) -> bool:
        fname = (it.get("url") or "").rsplit("/", 1)[-1].lower()
        hay = fname + " " + (it.get("alt") or "").lower()
        if not any(k in hay for k in _DAK_KW):
            return False
        if it.get("w") and it.get("h") and int(it["h"]) >= int(it["w"]):
            return False
        return True

    # ALLEEN AI-goedgekeurde foto's (de keurfilters van het visie-model kennen
    # schimmel/keurmerken; de oude keyword-fallback liet die juist door).
    # Geen AI-keuzes -> template-defaults blijven staan (professionele stock).
    _wy_imgs = list(_ai_imgs.get("why") or [])
    if not _ai_imgs:  # AI draaide helemaal niet -> voorzichtige keyword-fallback
        _wy_imgs += [it["url"] for it in _pool
                     if it.get("url") and it["url"] not in _used
                     and it["url"] not in _wy_imgs and _dak_relevant(it)]
    for k, wslot in enumerate(soup.select('[data-img^="why_team_action"]')):
        if k < len(_wy_imgs):
            wslot["style"] = f"--bg-img: url('{_wy_imgs[k]}')"
            _used.add(_wy_imgs[k])
    # Template B: het why-mozaïek bestaat uit <figure><img> — vul de srcs met
    # de gekozen sitefoto's (defaults blijven staan waar niets gekozen is).
    for k, wimg in enumerate(soup.select(".why-mosaic figure img")):
        if k < len(_wy_imgs):
            wimg["src"] = _wy_imgs[k]
            if wimg.get("loading"):
                del wimg["loading"]
            _used.add(_wy_imgs[k])

    # ── Hero-foto: de eigen, GOEDGEKEURDE site-foto als die door de strenge
    #    keuring kwam (hero_safe + boven de hero-drempel); anders de vaste bundled
    #    backup-foto via het gegarandeerde /demo-assets-pad. onerror valt altijd
    #    terug op de backup. De hero draagt de demo -> liever niets dan een matige
    #    foto, dus alleen `_ai_hero` (al gefilterd op hero_safe) belandt hier. ──
    _HERO_BACKUP = "/demo-assets/dakdekkers/hero-houses.jpg"
    # Template A/B: één hero-foto in .hero-img img.
    hero_img = soup.select_one(".hero-img img")
    if hero_img is not None:
        hero_img["src"] = (_ai_hero[0] if _ai_hero else _HERO_BACKUP)
        hero_img["onerror"] = f"this.onerror=null;this.src='{_HERO_BACKUP}'"
        if _ai_hero:
            _used.add(_ai_hero[0])
    # Template C: split-hero met twee foto's naast elkaar. Vul de 2 beste
    # goedgekeurde hero-foto's; ontbreekt er één -> bundled backup per slot.
    _c_hero_slots = soup.select(".hero-imgs figure img, .hero-imgs img, "
                                ".hero-split .hero-photo, .hero-photos img, .hero-split img")
    if _c_hero_slots:
        _hero_backups = [_HERO_BACKUP, "/demo-assets/dakdekkers/why-1.jpg"]
        for hi, slot in enumerate(_c_hero_slots[:2]):
            src = _ai_hero[hi] if hi < len(_ai_hero) else _hero_backups[hi % len(_hero_backups)]
            if slot.name == "img":
                slot["src"] = src
                slot["onerror"] = f"this.onerror=null;this.src='{_hero_backups[hi % len(_hero_backups)]}'"
            else:
                slot["style"] = f"--bg-img: url('{src}')"
            if hi < len(_ai_hero):
                _used.add(_ai_hero[hi])

    # ── Dubbele CTA's weghalen (er staat er al een sticky/nav in beeld) ──
    for a in soup.select("#why a.btn, .process-head a.btn"):
        a.decompose()

    # ── Nav: de Projecten-link alleen tonen als er echte projecten zijn —
    #    anders wijst hij naar een (runtime) verborgen sectie. ──
    if td.get("SHOW_PROJECTS") is not True:
        for a in soup.select('a[href="#projecten"], a[href="#projects"]'):
            a.decompose()

    # RULES 6: sweet spot is 4-5 nav-items; drie of minder oogt kaal. Vul aan
    # met "Over ons" (label-categorie van hun bedrijfspagina, wijst naar de
    # waarom-sectie) en daarna Werkwijze. Hoge data-nav-prio zodat vul-items
    # bij ruimtegebrek als eerste weer wijken.
    _nav = soup.select_one(".header .nav")
    if _nav is not None:
        _fillers = [("#waarom", "Over ons", "3"), ("#werkwijze", "Werkwijze", "4")]
        for href, label, prio in _fillers:
            links = _nav.select("a.navlink")
            if len(links) >= 4:
                break
            if soup.select_one(href) is None:
                continue  # sectie bestaat niet (meer) in deze demo
            if _nav.select_one(f'a[href="{href}"]'):
                continue
            a = soup.new_tag("a", attrs={"class": "navlink", "href": href,
                                         "data-nav-prio": prio})
            a.string = label
            links[-1].insert_after(a)
        # Mobiele menu meeneemt: zelfde aanvulling vóór de Contact-link.
        _mm = soup.select_one(".mm-links")
        if _mm is not None and len(_nav.select("a.navlink")) >= 4:
            for href, label, _prio in _fillers:
                if _nav.select_one(f'a[href="{href}"]') and not _mm.select_one(f'a[href="{href}"]'):
                    a = soup.new_tag("a", attrs={"href": href})
                    a.string = label
                    contact = _mm.select_one('a[href="#offerte"]')
                    if contact:
                        contact.insert_before(a)

    # ── Diensten: eigenaar-gerichte hint als we geen echte diensten vonden ──
    if td.get("_SERVICES_GENERIC"):
        sh = soup.select_one("#services .services-head")
        if sh:
            note = soup.new_tag("div")
            note["style"] = (
                "margin-top:14px;display:inline-flex;align-items:center;gap:8px;font-size:12.5px;"
                "font-weight:600;color:#4928FD;background:#efeaff;border:1px solid #ddd2ff;"
                "padding:8px 13px;border-radius:999px")
            note.append("✱ Laten we hier je diensten toevoegen voor een compleet beeld voor je klanten.")
            sh.append(note)

    # ── Team-CTA: duidelijke label + link naar team ──
    tcta = soup.select_one(".team-cta a")
    if tcta:
        tcta["href"] = "#team"

    # ── Why-items: passende pictogrammen + trim naar het echte aantal ──
    _ICONS = {
        "shield": '<path d="M12 2 4 6v6c0 5 3.5 9 8 10 4.5-1 8-5 8-10V6l-8-4Z"/><path d="m9 12 2 2 4-4"/>',
        "badge": '<circle cx="12" cy="9" r="6"/><path d="m8.5 13.5-2 7 5.5-3 5.5 3-2-7"/>',
        "pin": '<path d="M12 22s-8-7.5-8-13a8 8 0 0 1 16 0c0 5.5-8 13-8 13z"/><circle cx="12" cy="9" r="3"/>',
        "chat": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
        "doc": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h6"/>',
        "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.4 2.4-2-2 2.4-2.4z"/>',
        "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        "check": '<path d="M20 6 9 17l-5-5"/>',
    }
    city_l = str(td.get("CITY") or "").lower()

    def _pick_icon(text: str) -> str:
        t = text.lower()
        def has(*kw): return any(k in t for k in kw)
        if has("garant", "waarborg"): return "shield"
        if has("gecertif", "certif", "erkend", "vca", "keurmerk", "diploma", "gediplom"): return "badge"
        if has("veilig"): return "shield"
        if has("lokaal", "local", "regio", "omgeving", "plaatse") or (city_l and city_l in t): return "pin"
        if has("contact", "persoonlijk", "communicat", "bereikbaar", "klantgericht"): return "chat"
        if has("offerte", "prijs", "kosten", "transparant", "vrijblijvend", "tarief", "gratis"): return "doc"
        if has("vakkundig", "vakmanschap", "kwaliteit", "ervaring", "afwerking", "netjes", "zorgvuldig", "expert"): return "wrench"
        if has("snel", "spoed", "direct", "binnen", "24"): return "clock"
        return "check"

    for i, item in enumerate(soup.select("#why .why-item"), 1):
        if not is_set(f"WHY_{i}_TITLE"):
            item.decompose()
            continue
        text = f"{td.get(f'WHY_{i}_TITLE', '')} {td.get(f'WHY_{i}_BODY', '')}"
        tile = item.select_one(".icon-tile")
        if tile:
            svg = soup.new_tag("svg", width="22", height="22", viewBox="0 0 24 24")
            svg["fill"] = "none"; svg["stroke"] = "currentColor"; svg["stroke-width"] = "2"
            svg["stroke-linecap"] = "round"; svg["stroke-linejoin"] = "round"
            svg.append(BeautifulSoup(_ICONS[_pick_icon(text)], "html.parser"))
            tile.clear()
            tile.append(svg)

    # ── Projecten: opbouwen uit gescrapete projectkaarten (alleen bij >=2) ──
    projects = td.get("_PROJECTS") or []
    psec = soup.find(id="projects")
    if td.get("SHOW_PROJECTS") is True and projects and psec:
        plist = psec.select_one(".projects-list")
        tmpl = plist.select_one(".project") if plist else None
        if plist and tmpl:
            built = []
            for p in projects[:3]:
                c = _copy(tmpl)
                _strip_tpl(c)
                slot = c.select_one(".img-slot")
                if slot:
                    slot["style"] = f"--bg-img: url('{p['image']}')"
                h = c.select_one("h3")
                if h:
                    h.string = p.get("title", "")
                para = c.select_one("p")
                if para:
                    para.string = p.get("desc", "")
                a = c.select_one("a")
                if a:
                    a.decompose()
                built.append(c)
            plist.clear()
            for c in built:
                plist.append(c)
    else:
        if psec:
            psec.decompose()
        for a in soup.select('.nav-pill a[href="#projects"]'):
            a.decompose()

    # Artikelen / why
    if td.get("SHOW_ARTICLES") is not True:
        a = soup.find(id="articles")
        if a:
            a.decompose()
    if td.get("SHOW_WHY") is not True:
        w = soup.find(id="why")
        if w:
            w.decompose()

    # ── Testimonials: dynamisch opbouwen uit echte reviews (Google + site) ──
    reviews = td.get("_REVIEWS") or []
    rsec = soup.find(id="reviews")
    grid = soup.select_one("#reviews .tm-grid")
    if not reviews or not grid:
        if rsec:
            rsec.decompose()
    else:
        tmpl_card = grid.select_one(".tm-card")
        built = []
        for rv in reviews:
            c = _copy(tmpl_card)
            _strip_tpl(c)
            body = c.select_one(".body")
            if body:
                body.string = rv.get("text", "")
            stars = c.select_one(".tm-stars")
            if stars:
                stars.string = rv.get("stars", "★★★★★")
            h5 = c.select_one(".who h5")
            if h5:
                h5.string = rv.get("author") or "Klant"
            lbl = c.select_one(".who span")
            if lbl:
                lbl.string = rv.get("label", "")
            built.append(c)
        grid.clear()
        for c in built:
            grid.append(c)

        # Swipe-pijlen weg (er is niks te swipen).
        for nav in rsec.select(".tm-nav"):
            nav.decompose()

        # Kleine groene 'live'-regel, net boven de reviews (onder kop + tekst).
        badge = soup.new_tag("span")
        badge["style"] = (
            "display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:600;"
            "color:#1a8f4a;margin:0 0 14px")
        dot = soup.new_tag("i")
        dot["style"] = ("width:7px;height:7px;border-radius:50%;background:#1fbf5d;display:inline-block;"
                        "box-shadow:0 0 0 0 rgba(31,191,93,.5)")
        badge.append(dot)
        badge.append(" Live in Google, automatisch bijgewerkt")
        grid.insert_before(badge)

    # Team: nooit verzonnen namen. Lege naam -> alleen de motiverende placeholder
    for i, member in enumerate(soup.select(".team-grid .member"), 1):
        if not is_set(f"TEAM_{i}_NAME"):
            body = member.select_one(".member-body")
            if body:
                body.decompose()
        else:
            hint = member.select_one(".placeholder-hint")
            if hint:
                hint.decompose()
    if td.get("SHOW_TEAM") is False:
        t = soup.find(id="team")
        if t:
            t.decompose()

    # Nav-telefoon-pill
    phone = str(td.get("FOOT_PHONE") or "").strip()
    nav_phone = soup.find(id="nav-phone")
    if nav_phone:
        if not phone:
            nav_phone.decompose()
        else:
            nav_phone["href"] = "tel:" + re.sub(r"[^0-9+]", "", phone)

    # Echte 'Open in Google Maps'-link
    maps = soup.select_one(".location-link")
    if maps and (is_set("FOOT_ADDRESS") or is_set("COMPANY_NAME")):
        q = f"{td.get('COMPANY_NAME', '')} {td.get('FOOT_ADDRESS', '')}".strip()
        maps["href"] = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)
        maps["target"] = "_blank"
        maps["rel"] = "noopener"

    return str(soup)


def _personalize_widget_done(widget: str) -> str:
    """Herontwerp de bevestigingsstap van de Harv-widget: paarse Harv-bubbel,
    duidelijke tekst en een heldere CTA naar de kennismaking."""
    bubble = (
        '<div style="width:56px;height:56px;margin:6px auto 16px;border-radius:50%;'
        'background:#4928FD;color:#fff;font-weight:800;font-size:16px;display:flex;'
        'align-items:center;justify-content:center;letter-spacing:-.3px">Harv</div>'
    )
    widget = widget.replace('<div class="harv-check">&#10003;</div>', bubble)
    widget = widget.replace("Gelukt<span data-fname></span>!", "Bevalt het je tot dusver?")
    widget = re.sub(
        r'(<p class="harv-done-p">).*?(</p>)',
        r"\1Laten we gewoon verder praten.\2",
        widget, count=1, flags=re.DOTALL,
    )
    # Kleine grijze meta-regel leegmaken (de bubbel + tekst dragen de boodschap).
    widget = re.sub(
        r'(<span class="harv-meta-line">).*?(</span>)',
        r"\1\2", widget, count=1, flags=re.DOTALL,
    )
    return widget


def _harv_float_bubble(booking_url: str) -> str:
    """Vaste bubbel rechtsonder: 'Presented by Harv Agency' (eigen regel) +
    duidelijke CTA naar de kennismaking. Blijft ook op mobiel zichtbaar (buiten
    het telefoonframe, want position:fixed t.o.v. het scherm)."""
    u = _html_escape(booking_url)
    return (
        '<div class="harv-float" style="position:fixed;right:18px;bottom:18px;z-index:99999;'
        "background:#fff;border:1px solid #ececec;border-radius:16px;"
        "box-shadow:0 12px 34px rgba(0,0,0,.16);padding:13px 16px;max-width:240px;"
        'font-family:\'Inter Tight\',system-ui,-apple-system,sans-serif">'
        '<div style="font-size:13px;color:#555;margin-bottom:10px">'
        'Presented by <strong style="color:#111">Harv Agency</strong></div>'
        f'<a href="{u}" target="_blank" rel="noopener" '
        'style="display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:700;color:#fff;'
        'background:#4928FD;border-radius:999px;padding:9px 15px;text-decoration:none;white-space:nowrap">'
        "Plan een kennismaking &#8599;</a></div>"
    )


def _wizardize_widget(widget: str) -> str:
    """Maak de intake uitgebreider ZONDER dat de widget langer/groter wordt:
    elke vraag wordt een eigen sub-stap (één vraag per scherm). De kaarthoogte
    blijft compact; navigeren gaat met Volgende/Terug binnen stap 2."""
    done_marker = '<div class="harv-step harv-done"'
    i_done = widget.find(done_marker)
    step2_open = '<div class="harv-step" data-step="2" hidden>'
    if i_done == -1 or step2_open not in widget:
        return widget
    head = widget[:i_done]
    tail = widget[i_done:]
    i2 = head.rfind(step2_open)
    pre2 = head[:i2]
    step2_block = head[i2:]

    m = re.search(r'<div class="harv-chips">(.*?)</div>', step2_block, re.DOTALL)
    service_chips = m.group(1) if m else ""

    def chips(opts: list[str]) -> str:
        return "".join(
            f'<button type="button" class="harv-chip" data-val="{_html_escape(o)}">{_html_escape(o)}</button>'
            for o in opts
        )

    def sub(n: int, question: str, body: str, *, first: bool = False, last: bool = False) -> str:
        hidden = "" if first else " hidden"
        back = ('<button type="button" class="harv-btn harv-ghost harv-back">&#8592; Terug</button>'
                if first else
                '<button type="button" class="harv-btn harv-ghost harv-subprev">&#8592; Terug</button>')
        fwd = ('<button type="button" class="harv-btn harv-submit">Versturen</button>' if last
               else '<button type="button" class="harv-btn harv-subnext">Volgende &#8594;</button>')
        return (
            f'<div class="harv-sub" data-sub="{n}"{hidden}>'
            f'<div class="harv-q">{_html_escape(question)}</div>'
            f'{body}'
            f'<div class="harv-row" style="margin-top:14px">{back}{fwd}</div>'
            f'</div>'
        )

    toelichting = (
        '<label class="harv-f">Korte toelichting <span class="harv-opt">(optioneel)</span>'
        '<textarea name="bericht" rows="3" placeholder="Vertel kort wat je zoekt..."></textarea></label>'
    )

    new_inner = (
        sub(1, "Waar kunnen we mee helpen?", f'<div class="harv-chips">{service_chips}</div>', first=True)
        + sub(2, "Wat voor dak?", f'<div class="harv-chips">{chips(["Plat dak", "Schuin dak", "Weet ik niet"])}</div>')
        + sub(3, "Geschat oppervlak?", f'<div class="harv-chips">{chips(["< 50 m²", "50 - 100 m²", "> 100 m²", "Onbekend"])}</div>')
        + sub(4, "Materiaal (indien bekend)?", f'<div class="harv-chips">{chips(["Bitumen", "EPDM", "Dakpannen", "Zink", "Anders"])}</div>')
        + sub(5, "Wanneer?", f'<div class="harv-chips">{chips(["Spoed", "Binnen een maand", "Oriënterend"])}</div>')
        + sub(6, "Nog iets dat we moeten weten?", toelichting, last=True)
    )

    new_step2 = step2_open + new_inner + "</div>\n\n    "

    nav_js = """
<script>
(function(){
  var w = document.querySelector('.harv-w'); if(!w) return;
  var step2 = w.querySelector('.harv-step[data-step="2"]'); if(!step2) return;
  var subs = step2.querySelectorAll('.harv-sub'); if(!subs.length) return;
  var cur = 0;
  function show(i){ cur = Math.max(0, Math.min(subs.length-1, i)); subs.forEach(function(s,idx){ s.hidden = idx !== cur; }); }
  step2.addEventListener('click', function(e){
    var nx = e.target.closest('.harv-subnext');
    var pv = e.target.closest('.harv-subprev');
    if(nx){ e.preventDefault(); show(cur+1); }
    else if(pv){ e.preventDefault(); show(cur-1); }
  });
  var nb = w.querySelector('.harv-next');
  if(nb) nb.addEventListener('click', function(){ setTimeout(function(){ show(0); }, 0); });
  show(0);
})();
</script>
"""
    out = pre2 + new_step2 + tail
    i_sec = out.rfind("</section>")
    if i_sec != -1:
        out = out[:i_sec] + nav_js + out[i_sec:]
    return out


def _widget_valid_affordance(widget: str, dark: str) -> str:
    """Maak de 'Volgende'-knop van stap 1 donker zodra naam + geldig e-mail zijn
    ingevuld, als duidelijke 'je kunt door'-affordance."""
    js = (
        "\n<script>\n(function(){\n"
        "  var w=document.querySelector('.harv-w'); if(!w) return;\n"
        "  var s1=w.querySelector('.harv-step[data-step=\"1\"]'); if(!s1) return;\n"
        "  var nb=s1.querySelector('.harv-next');\n"
        "  var nm=s1.querySelector('[name=\"naam\"]'), em=s1.querySelector('[name=\"email\"]');\n"
        "  function ok(){ return nm&&nm.value.trim()&&em&&/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(em.value.trim()); }\n"
        "  function upd(){ if(nb) nb.style.background = ok() ? '" + dark + "' : ''; }\n"
        "  s1.addEventListener('input', upd); upd();\n"
        "})();\n</script>\n"
    )
    i = widget.rfind("</section>")
    return widget[:i] + js + widget[i:] if i != -1 else widget + js


def _render_dakdekker_html(
    html: str,
    *,
    data: dict[str, Any],
    ai_content: dict[str, Any],
    slug: str,
    lead_id: str,
    notion_id: str,
    regio: Optional[str],
    stad: Optional[str],
    brand: str,
) -> str:
    """Vul de dakdekker-template: scrape de site voor echte personalisatie
    (kleuren, werkwijze, reviews, projecten, certificering), patch het
    template-data JSON-blok en vul de Harv-placeholders."""
    import harv_kit as hk

    # ── Scrape de live site voor echte, gepersonaliseerde content ──
    url = data.get("website") or data.get("id") or ""
    extract: dict[str, Any] = {}
    colors: tuple[Optional[str], Optional[str]] = (None, None)
    if url:
        bundle = fetch_site_bundle(url)
        # RULES 5: NOOIT een logo verzinnen. Staat er geen logo in de leaddata,
        # haal hem dan van de site; monogram is pas het állerlaatste vangnet.
        if not str(data.get("logo_url") or "").strip() and bundle.get("html"):
            _lsoup = BeautifulSoup(bundle["html"], "html.parser")
            _found_logo = _safe(extract_logo, _lsoup, url)
            if _found_logo:
                data["logo_url"] = _found_logo
                print(f"  🔎 logo van de site gehaald: {_found_logo[:80]}")
        # Merkkleur van de GERENDERDE site (wat de bezoeker ziet); statische
        # CSS-frequentie alleen als fallback — die trapt in vendor-paletten.
        _acc, _drk, _site_bg, _cta_col = detect_brand_colors_rendered(url)
        colors = (_acc, _drk)
        if not colors[0]:
            colors = detect_brand_colors(bundle.get("html", ""), bundle.get("css", ""))
        # Breed werkgebied ook herkennen aan meerdere stadsnamen in de sitetekst
        # ("dakdekker Den Haag … dakdekker Rijswijk" -> regio benoemen).
        _stedenkoppen = set(re.findall(r"[Dd]akdekkers?\s+(?:in\s+)?([A-Z][a-zA-Zé-]{3,})",
                                       bundle.get("text") or ""))
        if len(_stedenkoppen) >= 2:
            extract_area_hint = " regio " + ", ".join(sorted(_stedenkoppen)[:4])
        else:
            extract_area_hint = ""
        # Donkere variant: expliciet via --theme, of automatisch wanneer de
        # eigen site van de lead donker is (RULES 3b).
        _theme = str(data.get("_theme") or "auto").lower()
        dark_theme = _theme == "dark" or (
            _theme == "auto" and bool(_site_bg) and _brightness(_site_bg) < 80)
        if dark_theme:
            print(f"  🌙 donkere variant (site-achtergrond {_site_bg or 'geforceerd'})")
        city = _city_from_address(data.get("adres"), data.get("stad") or "")
        extract = ai_site_extract(
            bundle.get("text", ""),
            company=clean_company_name(data.get("bedrijfsnaam")),
            city=city,
        )
        extract["project_cards"] = bundle.get("project_cards", [])
        extract["service_cards"] = bundle.get("service_cards", [])
        extract["image_pool"] = bundle.get("image_pool", [])
        if extract_area_hint:
            extract["service_area"] = (str(extract.get("service_area") or "")
                                       + extract_area_hint)
        if colors[0]:
            print(f"  🎨 merkkleur: accent={colors[0]} dark={colors[1]}"
                  + (f" cta={_cta_col}" if _cta_col else ""))
        print(f"  🧩 site-extract: {len(extract.get('process') or [])} stappen, "
              f"{len(extract.get('services') or [])} diensten ({len(extract.get('service_cards') or [])} met foto), "
              f"{len(extract.get('project_cards') or [])} projecten, "
              f"cert={extract.get('certified')} garantie={extract.get('guarantee')}")

    td = build_dakdekker_template_data(data, ai_content, extract, colors)
    td["_DARK_THEME"] = bool(url) and dark_theme
    td["_CTA_COLOR"] = _cta_col if url else None

    # AI-beeldselectie: site-foto's slim toewijzen aan dienst-/why-slots, plus
    # de wordmark-vraag (staat de naam al in het logo?). Best effort.
    _svc_names = [str(td.get(f"SERVICE_{i}_NAME") or "").strip()
                  for i in range(1, 7) if str(td.get(f"SERVICE_{i}_NAME") or "").strip()]
    td["_AI_IMGS"] = ai_select_images(
        td.get("_IMAGE_POOL") or [], _svc_names,
        str(td.get("COMPANY_NAME") or ""),
        logo_url=str(td.get("LOGO_URL") or ""),
        slug=slug, lead_id=lead_id,
    )

    # Embed-JSON zonder interne (_-prefixed) sleutels.
    embed_td = {k: v for k, v in td.items() if not k.startswith("_")}
    new_json = json.dumps(embed_td, ensure_ascii=False, indent=2)
    html, n = re.subn(
        r'(<script id="template-data" type="application/json">)(.*?)(</script>)',
        lambda m: m.group(1) + "\n" + new_json + "\n" + m.group(3),
        html, count=1, flags=re.DOTALL,
    )
    if n != 1:
        raise RuntimeError("dakdekker: template-data JSON-blok niet gevonden in template")

    # Harv-placeholders (widget/presented-by/guard-css). De widget/CTA krijgt de
    # ECHTE merkkleur van de website (niet het Harv-violet).
    widget_brand = (colors[0] if colors and colors[0] else brand)
    kit = hk.kit_placeholders(
        data, lead_id=lead_id, slug=slug, notion_id=notion_id,
        sector="dakdekker", regio=regio, stad=stad, brand=widget_brand, ai_content=ai_content,
    )
    # Floating bubbel rechtsonder: presented-by + duidelijke CTA (eigen ontwerp).
    booking_url = hk.build_booking_url(data, notion_id)
    kit["{{HARV_PRESENTED_BY}}"] = _harv_float_bubble(booking_url)

    # Widget: speelse tagline, herontworpen bevestiging, uitgebreide vragenlijst.
    widget = kit.get("{{HARV_BOOKING_WIDGET}}", "")
    if widget:
        widget = widget.replace("Reageert meestal binnen een uur", "Test mij uit ;)")
        widget = _personalize_widget_done(widget)
        widget = _wizardize_widget(widget)
        widget = _widget_valid_affordance(widget, (colors[1] if colors and colors[1] else "#1a2334"))
        kit["{{HARV_BOOKING_WIDGET}}"] = widget
    # Tracking: open-pixel + scroll/cta/duration-beacons (build_tracking_html).
    if lead_id:
        kit["{{TRACKING_PIXEL}}"] = build_tracking_html(lead_id, slug)
    else:
        kit["{{TRACKING_PIXEL}}"] = ""

    for token, value in kit.items():
        html = html.replace(token, value or "")

    # Bak echte waarden + sectie-zichtbaarheid server-side in de raw HTML
    # (geen nep-defaults in de bron; werkt ook zonder JavaScript).
    html = _bake_dakdekker_dom(html, td)

    # Leklijst-termen die AANTOONBAAR op de eigen site staan (bv. een echt
    # VEBIDAK-lidmaatschap) zijn geen placeholder-lek. Markeer ze zodat de
    # gate ze overslaat — alleen termen die letterlijk in de sitetekst staan.
    try:
        import rules_loader as _rl
        _leak_terms = _rl.get_placeholder_leaks() or ()
    except Exception:  # noqa: BLE001
        _leak_terms = ("Van den Berg", "VEBIDAK", "210+", "sinds 2003", "Lorem ipsum")
    _site_text = (bundle.get("text") or "").lower() if url else ""
    _verified = [t for t in _leak_terms
                 if t.lower() in _site_text and t.lower() in html.lower()]
    if _verified:
        html += f"\n<!-- harv-verified-terms: {'|'.join(_verified)} -->"
        print(f"  ✅ geverifieerde site-termen (geen lek): {', '.join(_verified)}")
    return html


def _team_for_caption(team: list[dict[str, Any]], ai_content: dict[str, Any]) -> list[dict[str, Any]]:
    """Als er geen namen zijn, vul de anonieme AI-caption als 'naam' in zodat de
    team-kaarten niet leeg ogen (regel: nooit een lege naam tonen)."""
    if ai_content.get("team_mode") == "named":
        return team
    caption = ai_content.get("team_caption") or "Ons team"
    out = []
    for p in team:
        q = dict(p)
        if not q.get("naam"):
            q["naam"] = caption
        out.append(q)
    return out


def render_demo(
    *,
    slug: str,
    sector: str,
    data: dict[str, Any],
    lead_id: str = "",
    notion_id: str = "",
    regio: Optional[str] = None,
    stad: Optional[str] = None,
    template_path: Optional[Path] = None,
    write: bool = True,
    require_ai: bool = True,
) -> str:
    """Vul de sector-template met scrape-data + AI-content + de Harv-widget.

    Dit is de ontbrekende schakel: het leest de template, vervangt ALLE
    placeholders (de bestaande {{...}} én de nieuwe {{HARV_*}}) en schrijft
    `public/demo/<slug>/index.html`. Retourneert de gerenderde HTML.

    AI is verplicht: bij `require_ai=True` (standaard) gooit een mislukte
    AI-call `harv_kit.AIContentError` — de demo wordt dan NIET geschreven, zodat
    er nooit een demo zonder AI-content live gaat. Zet `require_ai=False` alleen
    voor previews/tests.
    """
    import harv_kit as hk

    sector_key = (sector or "").strip().lower()
    if template_path is None:
        tpl_name = SECTOR_TEMPLATES.get(sector_key)
        if not tpl_name:
            raise ValueError(f"Geen template bekend voor sector '{sector}'. "
                             f"Bekend: {sorted(SECTOR_TEMPLATES)}")
        template_path = TEMPLATES_ROOT / tpl_name
    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"Template niet gevonden: {template_path}")

    html = template_path.read_text(encoding="utf-8")

    # AI-content één keer; daarna hergebruikt voor zowel placeholders als team.
    ai_content = hk.ai_demo_content(
        data, sector=sector_key or "dakdekker", regio=regio, stad=stad, require=require_ai
    )
    # Kosten van de Haiku content-call live wegschrijven (best effort).
    log_ai_cost(slug, "haiku_content", ai_content, lead_id=lead_id)

    bedrijfsnaam = data.get("bedrijfsnaam") or ai_content.get("display_name") or ""
    primaire_kleur = data.get("primaire_kleur") or hk._detect_brand(data)

    # ── Dakdekker: eigen render-pad (patcht template-data JSON i.p.v. losse tokens) ──
    if sector_key in DAKDEKKER_SECTORS:
        html = _render_dakdekker_html(
            html, data=data, ai_content=ai_content, slug=slug,
            lead_id=lead_id, notion_id=notion_id, regio=regio, stad=stad,
            brand=primaire_kleur,
        )
        html = _ensure_noindex(html)
        if write:
            out_file = PUBLIC_ROOT / "demo" / slug / "index.html"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(html, encoding="utf-8")
            print(f"🖼  demo gerenderd → {out_file.relative_to(REPO_ROOT)}")
            maybe_quality_gate(slug, lead_id=lead_id, real_city=stad)
        return html

    team_in = data.get("team") or []
    team_cards = _team_for_caption(team_in, ai_content)

    # Bestaande placeholders
    base: dict[str, str] = {
        "{{PRIMAIRE_KLEUR}}": primaire_kleur,
        "{{LOGO_URL}}": data.get("logo_url") or "",
        "{{NAV_ITEMS}}": build_nav_html(data.get("nav_items") or []),
        "{{TAGLINE}}": data.get("tagline") or ai_content.get("local_intro") or "",
        "{{TEAM_HTML}}": build_team_html(team_cards),
        "{{BLOG_HTML}}": build_blog_html(data.get("blog_posts") or []),
        "{{WONINGAANBOD_HTML}}": build_woningaanbod_html(data.get("woningaanbod") or [], primaire_kleur),
        "{{TRACKING_PIXEL}}": build_tracking_html(lead_id, slug) if lead_id else "",
        "{{BEDRIJFSNAAM}}": _html_escape(bedrijfsnaam),
    }

    # Nieuwe Harv-kit placeholders (widget, presented-by, guard-css, lokale teksten)
    kit = hk.kit_placeholders(
        data,
        lead_id=lead_id,
        slug=slug,
        notion_id=notion_id,
        sector=sector_key or "dakdekker",
        regio=regio,
        stad=stad,
        brand=primaire_kleur,
        ai_content=ai_content,
    )

    for token, value in {**base, **kit}.items():
        html = html.replace(token, value or "")

    html = _ensure_noindex(html)
    if write:
        out_file = PUBLIC_ROOT / "demo" / slug / "index.html"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(html, encoding="utf-8")
        print(f"🖼  demo gerenderd → {out_file.relative_to(REPO_ROOT)}")
        maybe_quality_gate(slug, lead_id=lead_id, real_city=stad)

    return html


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


HARV_SCRAPER_DIR = Path.home() / "Developer" / "harv-scraper"


def _import_harv_db(scraper_dir: Path = HARV_SCRAPER_DIR):
    """Importeer het `db` module van harv-scraper via sys.path-injectie.

    Wordt lazy gedaan zodat generate_demo.py ook werkt als harv-scraper niet
    bestaat (bv. in een geïsoleerde demo-only-deploy of unit-test).
    """
    import importlib
    import sys
    scraper_dir = Path(scraper_dir)
    if not (scraper_dir / "db.py").exists():
        raise FileNotFoundError(f"harv-scraper db.py niet gevonden in {scraper_dir}")
    if str(scraper_dir) not in sys.path:
        sys.path.insert(0, str(scraper_dir))
    return importlib.import_module("db")


def log_ai_cost(
    slug: str,
    step: str,
    ai_content: dict[str, Any],
    *,
    lead_id: str = "",
    run_id: str = "",
) -> None:
    """Log de AI-kosten van één call naar de ai_costs-tabel (best effort).

    Leest `_cost_eur`, `_model` en `_tokens` uit het ai_content-dict dat
    harv_kit teruggeeft. Faalt dit (geen db, geen kosten), dan slaan we het
    stil over: kostenlogging mag de demo-generatie nooit breken.
    """
    cost = float(ai_content.get("_cost_eur") or 0.0)
    if cost <= 0:
        return
    toks = ai_content.get("_tokens") or {}
    try:
        db = _import_harv_db()
        db.add_ai_cost(
            slug=slug,
            step=step,
            model=str(ai_content.get("_model") or "haiku"),
            cost_eur=cost,
            tok_in=int(toks.get("in") or 0),
            tok_out=int(toks.get("out") or 0),
            img_tokens=int(ai_content.get("_img_tokens") or 0),
            lead_id=lead_id or None,
            run_id=run_id or None,
        )
    except Exception as exc:  # noqa: BLE001 — logging mag nooit breken
        print(f"  ⚠ kostenlog overgeslagen ({step}): {exc}")


def maybe_quality_gate(slug: str, *, lead_id: str = "", real_city: Optional[str] = None) -> Optional[dict]:
    """Draai de kwaliteitstrechter (gate + sign-off) als HARV_QUALITY_GATE=1.

    Default UIT zodat bestaand gedrag niet verandert tot Playwright + de key op
    de server staan. Bij 'human_review' wordt de lead geflagd zodat hij niet
    automatisch gepusht wordt. Best effort: faalt dit, dan alleen een waarschuwing.
    """
    if os.environ.get("HARV_QUALITY_GATE", "") not in ("1", "true", "yes"):
        return None
    try:
        import quality_pipeline as qp
        res = qp.run_quality(slug=slug, public_root=PUBLIC_ROOT, lead_id=lead_id, real_city=real_city)
        emoji = "✅" if res["decision"] == "ship" else "🛑"
        print(f"  {emoji} kwaliteit: {res['decision']} — {res['reason']} "
              f"(€{res.get('spent_eur', 0):.4f} besteed)")
        if res["decision"] != "ship" and lead_id:
            try:
                db = _import_harv_db()
                db.add_lead_flag(lead_id, "quality_review")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ kon lead niet flaggen voor review: {exc}")
        return res
    except Exception as exc:  # noqa: BLE001 — kwaliteitsgate mag generatie niet breken
        print(f"  ⚠ kwaliteitsgate overgeslagen: {exc}")
        return None


def _notion_patch_demo_link(
    notion_page_id: str,
    demo_url: str,
    notion_token: str,
) -> tuple[bool, Optional[str]]:
    """PATCH alleen het Demo-link veld. Returns (success, error_message)."""
    payload = {"properties": {"Demo-link": {"url": demo_url}}}
    headers = {
        "Authorization": f"Bearer {notion_token}",
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
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"


def update_lead_after_deploy(
    notion_page_id: Optional[str],
    demo_url: str,
    *,
    db_path: Optional[Path] = None,
    scraper_dir: Path = HARV_SCRAPER_DIR,
    notion_token: Optional[str] = None,
) -> dict[str, Any]:
    """Werk SQLite-fase + Notion Demo-link bij na een succesvolle demo-deploy.

    Stappen:
      1. Lookup lead in SQLite via `notion_page_id`.
      2. SQLite fase → 'demo_verstuurd' (alleen als de lead gevonden is).
      3. PATCH Notion Demo-link (alleen als token + notion_page_id beschikbaar).

    Beide stappen worden onafhankelijk geprobeerd zodat één faal de ander
    niet meeneemt. Returns een statusdict met booleans + foutmeldingen.
    """
    result: dict[str, Any] = {
        "notion_page_id": notion_page_id,
        "demo_url": demo_url,
        "lead_found": False,
        "sqlite_updated": False,
        "notion_updated": False,
        "lead_id": None,
        "errors": [],
    }

    if not notion_page_id:
        result["errors"].append("notion_page_id ontbreekt — niets bij te werken.")
        return result

    # 1+2: SQLite
    try:
        db = _import_harv_db(scraper_dir)
        kwargs = {"db_path": db_path} if db_path else {}
        lead = db.get_lead_by_notion_page_id(notion_page_id, **kwargs)
        if lead is None:
            result["errors"].append(f"geen lead in SQLite met notion_page_id={notion_page_id}")
        else:
            result["lead_found"] = True
            result["lead_id"] = lead["id"]
            if db.update_lead_fase(lead["id"], "demo_verstuurd", **kwargs):
                result["sqlite_updated"] = True
            else:
                result["errors"].append("update_lead_fase gaf rowcount=0")
    except FileNotFoundError as exc:
        result["errors"].append(f"SQLite-stap overgeslagen: {exc}")
    except Exception as exc:  # noqa: BLE001 — defensief, mag geen crash veroorzaken
        result["errors"].append(f"SQLite-stap fout: {type(exc).__name__}: {exc}")

    # 3: Notion
    if notion_token is None:
        import os
        notion_token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY", "")
    if notion_token:
        ok, err = _notion_patch_demo_link(notion_page_id, demo_url, notion_token)
        result["notion_updated"] = ok
        if err:
            result["errors"].append(f"Notion: {err}")
    else:
        result["errors"].append("NOTION_TOKEN ontbreekt — Notion-stap overgeslagen.")

    return result


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


def _ensure_b_shared() -> list[Path]:
    """Template B (helder) verwijst naar gedeelde assets (`../assets/…`) en de
    React-widget (`../quote-widget.jsx`), die gedeeld in `public/demo/` staan.
    Kopieer ze (idempotent) uit de gevendorde bron `templates/_b-shared/` zodat
    elke B-demo ze live heeft. Retourneert de paden om mee te committen."""
    import shutil
    src = TEMPLATES_ROOT / "_b-shared"
    demo_root = PUBLIC_ROOT / "demo"
    touched: list[Path] = []
    if (src / "quote-widget.jsx").exists():
        dst = demo_root / "quote-widget.jsx"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "quote-widget.jsx", dst)
        touched.append(dst)
    asset_src = src / "assets"
    if asset_src.is_dir():
        asset_dst = demo_root / "assets"
        asset_dst.mkdir(parents=True, exist_ok=True)
        for img in asset_src.iterdir():
            if img.is_file():
                shutil.copyfile(img, asset_dst / img.name)
        touched.append(asset_dst)
    return touched


def _main_dakdekker(args: argparse.Namespace, slug: str) -> int:
    """CLI-pad voor dakdekkers: render uit de VERRIJKTE lead in de DB (echte
    rating/reviews/adres/telefoon/beschrijving) i.p.v. een verse scrape, en
    push + verify. Dit is het pad dat batch_demo.py op volume aanroept."""
    try:
        db = _import_harv_db()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ kan harv-scraper db niet laden: {exc}", file=sys.stderr)
        return 1

    lead = None
    if args.notion_page_id:
        lead = db.get_lead_by_notion_page_id(args.notion_page_id)
    if lead is None:
        lead = db.get_lead_by_id(args.url)
    if lead is None:
        print(f"⚠ geen lead in DB voor url={args.url} / notion={args.notion_page_id}", file=sys.stderr)
        return 1

    notion_id = args.notion_page_id or lead.get("notion_page_id") or ""
    data = lead_to_demo_data(lead)
    data["_theme"] = getattr(args, "theme", "auto") or "auto"
    tpl_variant = getattr(args, "template", "c") or "c"
    tpl_path = TEMPLATES_ROOT / "Roofer" / f"dakdekkers-{tpl_variant}.html"
    print(f"🏗  render dakdekker-demo  slug={slug}  template={tpl_variant}  bedrijf={lead.get('bedrijfsnaam','')[:40]}")
    try:
        render_demo(
            slug=slug, sector="dakdekkers", data=data,
            lead_id=lead.get("id") or args.url, notion_id=notion_id,
            regio=lead.get("regio"), stad=lead.get("stad"),
            write=True, require_ai=not args.no_ai,
            template_path=tpl_path,
        )
    except Exception as exc:  # noqa: BLE001 — AIContentError of render-fout: lead overslaan
        print(f"⚠ render mislukt voor {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    target = PUBLIC_ROOT / "demo" / slug / "index.html"
    print(f"💾 demo gerenderd → {target.relative_to(REPO_ROOT)}")

    # Template B leunt op gedeelde assets + widget in public/demo/ — zorg dat ze er staan.
    shared_paths = _ensure_b_shared() if tpl_variant == "b" else []
    if shared_paths:
        print(f"📦 gedeelde B-assets/widget klaar ({len(shared_paths)} paden)")

    if args.no_push:
        return 0
    try:
        for pth in shared_paths:
            subprocess.run(["git", "-C", str(REPO_ROOT), "add", str(pth)], check=False)
        git_commit_push(slug, target)
    except subprocess.CalledProcessError as exc:
        print(f"⚠ git stap mislukt: {exc}", file=sys.stderr)
        return 1

    if args.skip_verify:
        return 0
    if verify_deploy(slug):
        demo_url = f"{DEPLOY_BASE_URL}/demo/{slug}/"
        status = update_lead_after_deploy(notion_id or None, demo_url)
        if status["sqlite_updated"]:
            print(f"✅ SQLite fase → demo_verstuurd  (lead_id={status['lead_id']})")
        if status["notion_updated"]:
            print(f"✅ Notion Demo-link bijgewerkt op pagina {notion_id[:8]}…")
        for err in status["errors"]:
            print(f"⚠ {err}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Demo data scraper")
    parser.add_argument("--url", required=True, help="Lead-URL (volledige URL)")
    parser.add_argument("--slug", help="Slug onder data/ (default: domeinnaam)")
    parser.add_argument(
        "--sector",
        required=True,
        help="Sector (gebruik 'dakdekkers') — bepaalt template + nav fallback.",
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
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Render zonder verplichte AI-content (alleen voor previews/tests).",
    )
    parser.add_argument(
        "--template",
        choices=["a", "b", "c"],
        default="c",
        help="Template-variant voor dakdekkers: 'c' (premium, DEFAULT — vervangt a) "
             "of 'b' (helder). 'a' (origineel) is uitgefaseerd maar blijft selecteerbaar.",
    )
    parser.add_argument(
        "--theme",
        choices=["auto", "light", "dark"],
        default="auto",
        help="Kleurthema: 'auto' volgt de site van de lead (donkere site → "
             "donkere variant), 'light'/'dark' forceren.",
    )
    args = parser.parse_args(argv)

    slug = slugify(args.slug) if args.slug else slug_from_url(args.url)

    # Dakdekkers renderen uit de verrijkte DB-lead (echte data, no-fake-data).
    if (args.sector or "").strip().lower() in DAKDEKKER_SECTORS:
        return _main_dakdekker(args, slug)

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
        demo_url = f"{DEPLOY_BASE_URL}/demo/{slug}/"
        status = update_lead_after_deploy(args.notion_page_id, demo_url)
        if status["sqlite_updated"]:
            print(f"✅ SQLite fase → demo_verstuurd  (lead_id={status['lead_id']})")
        if status["notion_updated"]:
            print(f"✅ Notion Demo-link bijgewerkt op pagina {(args.notion_page_id or '')[:8]}…")
        for err in status["errors"]:
            print(f"⚠ {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
