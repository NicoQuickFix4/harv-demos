#!/usr/bin/env python3
"""Demo data scraper — fase 1.

Haalt voor een lead-URL:
  * de primaire brand-kleur (via theme-color meta of frequentie-analyse op CSS)
  * de beste team-foto (via heuristieken op `<img>` tags)

en slaat het resultaat op als JSON onder `data/<slug>/data.json`. Daarna doet
het script `git add + commit + push` zodat de data direct gesynct is met de
remote. Template-invulling gebeurt later in een aparte stap.

Gebruik:
    python3 generate_demo.py --url https://voorbeeld-bakkerij.nl
    python3 generate_demo.py --url https://… --slug voorbeeld-bakkerij
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
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = REPO_ROOT / "data"

REQUEST_TIMEOUT_S = 15
MAX_STYLESHEETS = 5  # max aantal externe stylesheets dat we ophalen
USER_AGENT = "HarvDemoGenerator/0.1 (+https://harvagency.com)"

HEX_COLOR_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
RGB_COLOR_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
TEAM_KEYWORDS = (
    "team",
    "ons-team",
    "onsteam",
    "personeel",
    "medewerkers",
    "collega",
    "about",
    "over-ons",
    "wie-zijn-wij",
    "staff",
    "crew",
)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9\-]+", "-", value.lower())
    return re.sub(r"-+", "-", value).strip("-") or "lead"


def slug_from_url(url: str) -> str:
    host = urlparse(url).hostname or url
    host = host.removeprefix("www.")
    return slugify(host.split(".")[0])


# ─── kleur-extractie ────────────────────────────────────────────────────────

def _normalize_hex(raw: str) -> Optional[str]:
    raw = raw.lower()
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) in (6, 8):
        return "#" + raw[:6]
    return None


def _hex_from_rgb(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _is_grayscale(hex_color: str, tolerance: int = 12) -> bool:
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return max(r, g, b) - min(r, g, b) <= tolerance


def _is_near_white_or_black(hex_color: str) -> bool:
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    brightness = (r + g + b) / 3
    return brightness > 235 or brightness < 25


def _collect_colors(text: str) -> list[str]:
    out: list[str] = []
    for match in HEX_COLOR_RE.findall(text):
        norm = _normalize_hex(match)
        if norm:
            out.append(norm)
    for r, g, b in RGB_COLOR_RE.findall(text):
        out.append(_hex_from_rgb(int(r), int(g), int(b)))
    return out


def _fetch_text(url: str, headers: dict[str, str]) -> str:
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        if resp.ok:
            return resp.text
    except requests.RequestException:
        pass
    return ""


def extract_primary_color(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    # 1) <meta name="theme-color"> — meest betrouwbaar als 'ie er staat.
    meta = soup.find("meta", attrs={"name": re.compile(r"^theme-color$", re.I)})
    if meta and meta.get("content"):
        norm = _normalize_hex(meta["content"].lstrip("#"))
        if norm:
            return norm

    # 2) Verzamel kleuren uit inline <style> + externe stylesheets, kies de
    #    meest voorkomende niet-grijswaarde kleur.
    headers = {"User-Agent": USER_AGENT}
    counter: Counter[str] = Counter()
    for tag in soup.find_all("style"):
        for color in _collect_colors(tag.get_text() or ""):
            counter[color] += 1

    stylesheet_links = [
        urljoin(base_url, link["href"])
        for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r)
        if link.get("href")
    ][:MAX_STYLESHEETS]

    for href in stylesheet_links:
        css = _fetch_text(href, headers)
        for color in _collect_colors(css):
            counter[color] += 1

    for color, _count in counter.most_common():
        if _is_grayscale(color) or _is_near_white_or_black(color):
            continue
        return color
    return None


# ─── team-foto extractie ────────────────────────────────────────────────────

def _img_score(img_tag: Any, base_url: str) -> tuple[int, str]:
    """Geeft (score, absolute_url) terug. Hogere score = waarschijnlijker team-foto."""
    src = img_tag.get("src") or img_tag.get("data-src") or ""
    if not src:
        return 0, ""
    absolute = urljoin(base_url, src)
    if absolute.startswith("data:"):
        return 0, ""

    haystack = " ".join(
        filter(None, [
            (img_tag.get("alt") or "").lower(),
            (img_tag.get("title") or "").lower(),
            (img_tag.get("class") and " ".join(img_tag.get("class")).lower()) or "",
            absolute.lower(),
        ])
    )

    score = 0
    for kw in TEAM_KEYWORDS:
        if kw in haystack:
            score += 5

    # Grotere bekende afmetingen scoren beter.
    try:
        w = int(img_tag.get("width") or 0)
        h = int(img_tag.get("height") or 0)
        if w >= 400 or h >= 400:
            score += 2
        if w >= 800 or h >= 600:
            score += 2
    except (TypeError, ValueError):
        pass

    # Penalty voor 'logo' / 'icon' / 'sprite' — geen team-foto.
    for negative in ("logo", "icon", "sprite", "avatar-default", "placeholder"):
        if negative in haystack:
            score -= 6

    return score, absolute


def extract_team_photo(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    best_score = 0
    best_url: Optional[str] = None
    for img in soup.find_all("img"):
        score, url = _img_score(img, base_url)
        if score > best_score and url:
            best_score = score
            best_url = url
    if best_score >= 3 and best_url:
        return best_url
    # Fallback: open graph image, vaak wel een sfeerbeeld van het bedrijf.
    og = soup.find("meta", attrs={"property": re.compile(r"^og:image$", re.I)})
    if og and og.get("content"):
        return urljoin(base_url, og["content"])
    return None


# ─── orchestratie ───────────────────────────────────────────────────────────

def scrape(url: str) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"}
    start = time.monotonic()
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)
    laadtijd_ms = int((time.monotonic() - start) * 1000)
    resp.raise_for_status()
    html = resp.text
    final_url = resp.url

    primary_color = extract_primary_color(html, final_url)
    team_photo = extract_team_photo(html, final_url)

    return {
        "url": url,
        "final_url": final_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "primary_color": primary_color,
        "team_photo": team_photo,
        "laadtijd_ms": laadtijd_ms,
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
    # Niets te committen? Skip.
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Demo data scraper")
    parser.add_argument("--url", required=True, help="Lead-URL (volledige URL)")
    parser.add_argument("--slug", help="Slug onder data/ (default: domeinnaam)")
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Sla git add/commit/push over (alleen JSON wegschrijven).",
    )
    args = parser.parse_args(argv)

    slug = slugify(args.slug) if args.slug else slug_from_url(args.url)
    print(f"🔎 scrape {args.url}  slug={slug}")
    data = scrape(args.url)
    print(f"  primary_color={data['primary_color']}")
    print(f"  team_photo={data['team_photo']}")

    target = save_data(slug, data)
    print(f"💾 data opgeslagen → {target.relative_to(REPO_ROOT)}")

    if args.no_push:
        return 0
    try:
        git_commit_push(slug, target)
    except subprocess.CalledProcessError as exc:
        print(f"⚠ git stap mislukt: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
