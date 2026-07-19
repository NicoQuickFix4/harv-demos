#!/usr/bin/env python3
"""Laag 2 van de kwaliteitstrechter: visuele sign-off door Sonnet.

De gratis DOM-gate (qa_gate.py) bewijst dat de demo technisch niet stuk is.
Deze module bewijst dat hij OBJECTIEF GOED is: kloppen de kleuren met het merk,
oogt de indeling bewust, is de copy on-voice, is het beeld relevant. Dat kan een
DOM-check niet zien, dus we laten een visie-model (Sonnet) de screenshots
beoordelen tegen de regels uit ~/Developer/harv-dakdekker/v2/demo-generator/.

Kostenbewaking: elke call wordt vooraf tegen het demo-budget (€0,08) gehouden via
cost_tracker.would_exceed_budget en achteraf gelogd via db.add_ai_cost. Geen
compromis op kwaliteit; wel een harde cap. Komt het model er niet uit binnen
budget, dan gaat de demo naar menselijke review.

De enige functie die de API echt aanroept is `_call_sonnet`; de rest is pure,
testbare logica.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

SONNET_MODEL = os.environ.get("HARV_SIGNOFF_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _ensure_scraper_path() -> None:
    """Zorg dat harv-scraper (cost_tracker, db) importeerbaar is."""
    import sys
    scraper = Path(__file__).resolve().parent.parent / "harv-scraper"
    if scraper.exists() and str(scraper) not in sys.path:
        sys.path.insert(0, str(scraper))

# De rubric die Sonnet toepast.
#
# Bron van waarheid = RULES.md (deel 8b), ingelezen via rules_loader. De literal
# hieronder is alleen de FALLBACK als RULES.md (of het blok) ontbreekt, zodat de
# sign-off nooit zonder rubric draait.
_FALLBACK_RUBRIC = """Je bent de strenge kwaliteitskeurder van Harv. Je krijgt screenshots
(mobiel 390px en desktop 1280px) van een gepersonaliseerde demo-website voor een
dakdekker. Beoordeel ALLEEN op wat je ziet, tegen deze regels:

1. Kleuren matchen het merk: de merkkleur zit in de juiste rol, de "Offerte"/CTA
   steekt duidelijk af (kobalt + amber is de referentie). Geen blendende of
   laag-contrast kleuren.
2. Indeling oogt bewust en af: geen scheve of half-lege grids, geen eenzaam
   off-center element, geen leeg vak, niets dat buiten zijn kader valt.
3. Copy is on-voice en bevat geen achtergebleven placeholder ("Van den Berg",
   "Utrecht" als dat niet de echte plaats is, "210+", "VEBIDAK", "sinds 2003").
4. Beeld is relevant en niet gebroken. Geen generieke of misplaatste foto's.
5. Algemeen: ziet dit eruit als een objectief goede, moderne, professionele
   dakdekker-website die je met een gerust hart naar een prospect stuurt?

Context die je NIET mag afkeuren:
- Dit is een Harv-DEMO voor de eigenaar van het bedrijf. De volgende elementen
  zijn bewuste demo-tooling en GEEN fout: de desktop/mobiel-switcher bovenaan,
  de chip "Hé eigenaar, test mij ;)", de Harv-badge, de "Plan een gesprek"-
  bubbel/kaart van Nicolas (Harv Agency) en het "Voorproefje"-venster.
- Specifieke jaartallen, aantallen en keurmerken (bv. "sinds 1962", "TECTUM",
  "VCA") neemt de generator ALLEEN over als ze letterlijk op de eigen site van
  het bedrijf staan. Markeer ze niet als placeholder of verzinsel puur omdat
  ze specifiek zijn; alleen als ze visueel/tekstueel met elkaar in tegenspraak
  zijn (bv. "sinds 2024" naast "40+ jaar ervaring").
- Een donkere demo-variant (donkere achtergrond, lichte tekst) is een bewuste
  stijlkeuze die de eigen site van de lead volgt — geen fout.

Geef UITSLUITEND JSON terug, exact dit schema:
{"pass": true/false, "score": 0-100, "colors_match": true/false,
 "layout_intentional": true/false, "copy_on_voice": true/false,
 "image_ok": true/false,
 "issues": [{"desc": "...", "severity": "low|med|high", "fix": "css|content|none"}]}

"pass" alleen true als score >= 85 en er geen "high"-issue is. "fix" = "css" als
het met een simpele CSS-aanpassing op te lossen is (te lange regel, marge,
uitlijning), "content" als de tekst/keuze moet veranderen, "none" als het niet
binnen budget te repareren is."""

try:  # RULES.md wint; valt veilig terug op de literal hierboven.
    import rules_loader as _rules
    _RUBRIC = _rules.get_signoff_rubric() or _FALLBACK_RUBRIC
except Exception:  # noqa: BLE001 - nooit de sign-off laten breken op een loader-fout
    _RUBRIC = _FALLBACK_RUBRIC


# Max hoogte per tile. De API weigert afbeeldingen > 8000px in één dimensie,
# maar schaalt bovendien alles terug tot lange zijde <= 1568px — een full-page
# screenshot van 9000px wordt dus onleesbaar smal. ~2800px per tile houdt de
# pagina na die downscale beoordeelbaar én blijft ruim onder de harde limiet.
TILE_MAX_PX = 2800


def _tile_png(png: bytes, max_px: int = TILE_MAX_PX) -> list[bytes]:
    """Splits een hoge full-page screenshot verticaal in tiles van <= max_px.

    Screenshots die al passen komen ongewijzigd (als 1-element-lijst) terug.
    Zonder PIL geen tiling: dan gedraagt dit zich als voorheen.
    """
    w, h = _png_size(png)
    if h <= max_px:
        return [png]
    try:
        import io
        from PIL import Image
    except Exception:  # noqa: BLE001
        return [png]
    img = Image.open(io.BytesIO(png))
    tiles: list[bytes] = []
    for top in range(0, img.height, max_px):
        buf = io.BytesIO()
        img.crop((0, top, img.width, min(top + max_px, img.height))).save(buf, format="PNG")
        tiles.append(buf.getvalue())
    return tiles


def screenshot_pages(doc_root: str | Path, route: str) -> dict[str, list[bytes]]:
    """Maak PNG-screenshots op 390 en 1280 van de geserveerde demo.

    Hergebruikt de statische server uit qa_gate zodat assets echt laden.
    Hoge pagina's worden verticaal getiled (zie _tile_png).
    Returns {"mobile": [png_bytes, …], "desktop": [png_bytes, …]}.
    """
    from qa_gate import _StaticServer, VIEWPORTS
    from playwright.sync_api import sync_playwright

    shots: dict[str, list[bytes]] = {}
    with _StaticServer(doc_root) as srv:
        url = srv.url(route)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            for name, (w, h) in VIEWPORTS.items():
                page = browser.new_page(viewport={"width": w, "height": h})
                page.goto(url, wait_until="networkidle", timeout=20000)
                # De Harv-intro (schaar knipt lint) ligt als overlay over de
                # pagina; wacht tot hij klaar is, anders keurt Sonnet de overlay.
                if page.query_selector(".harv-intro"):
                    try:
                        page.wait_for_selector(".harv-intro.is-done",
                                               state="attached", timeout=10000)
                    except Exception:  # noqa: BLE001 — intro zonder done-class
                        pass
                # Scroll de hele pagina door zodat lazy assets en stat-tellers
                # geladen/gestart zijn, en keer terug naar boven.
                page.evaluate("""async () => {
                    const step = window.innerHeight;
                    for (let y = 0; y < document.body.scrollHeight; y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 120));
                    }
                    window.scrollTo(0, 0);
                }""")
                # Dwing de eindstand af vóór de capture: scroll-reveals hebben
                # 0.7s transities + stagger-delays en zouden anders half-onzichtbaar
                # op de full-page screenshot komen; de "Plan een gesprek"-popup
                # (opent zodra de footer in beeld komt) hoort niet over de pagina.
                page.evaluate("""async () => {
                    document.querySelectorAll('.reveal, .reveal-stagger, .reveal-stagger > *')
                        .forEach(el => {
                            el.classList.add('in');
                            el.style.transition = 'none';
                            el.style.transitionDelay = '0ms';
                            el.style.opacity = '1';
                            el.style.transform = 'none';
                        });
                    const pop = document.getElementById('harvPop');
                    if (pop) { pop.classList.remove('is-open'); pop.hidden = true; }
                    // Harv-tooling (mini-bubbel/chip) is geen site-design: niet mee keuren.
                    const mini = document.getElementById('harvMini');
                    if (mini) mini.style.display = 'none';
                    // Lazy images eager maken en op decodering wachten — anders
                    // keurt Sonnet lege beeldvakken die een echte bezoeker wél ziet.
                    document.querySelectorAll('img[loading="lazy"]').forEach(i => {
                        i.loading = 'eager';
                    });
                    await Promise.all(Array.from(document.images)
                        .filter(i => !i.complete)
                        .map(i => new Promise(r => { i.onload = i.onerror = r;
                                                     setTimeout(r, 4000); })));
                }""")
                page.wait_for_timeout(800)
                shots[name] = _tile_png(page.screenshot(full_page=True))
                page.close()
            browser.close()
    return shots


def estimate_signoff_cost(shots: dict[str, list[bytes]], tok_out: int = 600) -> float:
    """Schat de kosten van één sign-off-call in euro (vóór we 'm doen).

    Gebruikt de echte afbeeldingsafmetingen voor de token-schatting.
    """
    _ensure_scraper_path()
    import cost_tracker as ct

    img_tokens = 0
    for tiles in shots.values():
        for png in tiles:
            w, h = _png_size(png)
            img_tokens += ct.image_tokens(w, h)
    # ~1500 tokens rubric/systeem.
    return ct.ai_call_cost_eur(SONNET_MODEL, 1500, tok_out, img_tokens)


def _png_size(png: bytes) -> tuple[int, int]:
    """Lees breedte/hoogte uit de PNG-header (geen PIL nodig)."""
    if len(png) >= 24 and png[12:16] == b"IHDR":
        w = int.from_bytes(png[16:20], "big")
        h = int.from_bytes(png[20:24], "big")
        return w, h
    return 1280, 2000  # veilige bovengrens


def _call_sonnet(shots: dict[str, list[bytes]], model: str = SONNET_MODEL,
                 facts: Optional[str] = None) -> dict[str, Any]:
    """De enige functie die de API echt aanroept. Returns (verdict, usage).

    `facts`: geverifieerde leadfeiten (naam, plaats, telefoon, cijfers) zodat de
    keurder die niet "ter verificatie" hoeft door te schuiven naar een mens.

    Raises RuntimeError bij ontbrekende key/pakket; de caller vangt dat af.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY ontbreekt")
    import anthropic

    rubric = _RUBRIC
    if facts:
        rubric += ("\n\nGEVERIFIEERDE FEITEN van deze klant (rechtstreeks uit de "
                   "eigen site/leaddata — beschouw deze als kloppend, keur ze "
                   "niet af als placeholder):\n" + facts)
    content: list[dict[str, Any]] = [{"type": "text", "text": rubric}]
    for name in ("desktop", "mobile"):
        tiles = shots.get(name) or []
        for i, png in enumerate(tiles, 1):
            label = name if len(tiles) == 1 else f"{name} deel {i}/{len(tiles)} (boven → onder)"
            content.append({"type": "text", "text": f"\n[{label}]"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                },
            })

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    # 900 was te krap: bij meerdere issues kapt het JSON-verdict halverwege af
    # en faalt parse_verdict. 2000 geeft ruimte; het model stopt zelf eerder.
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    raw = msg.content[0].text.strip()
    verdict = parse_verdict(raw)
    verdict["_tokens"] = {"in": int(msg.usage.input_tokens), "out": int(msg.usage.output_tokens)}
    verdict["_model"] = model
    return verdict


def parse_verdict(raw: str) -> dict[str, Any]:
    """Trek het JSON-verdict uit de modelrespons; robuust tegen omringende tekst."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("geen JSON in sign-off-respons")
    data = json.loads(match.group())
    data.setdefault("pass", False)
    data.setdefault("score", 0)
    data.setdefault("issues", [])
    return data


def classify_issues(verdict: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Splits issues naar afhandeling: css-fixbaar, content, niet-fixbaar."""
    buckets: dict[str, list[dict[str, Any]]] = {"css": [], "content": [], "none": []}
    for iss in verdict.get("issues", []):
        fix = (iss.get("fix") or "none").lower()
        buckets.get(fix, buckets["none"]).append(iss)
    return buckets


def signoff(
    *,
    slug: str,
    doc_root: str | Path,
    route: str,
    spent_eur: float = 0.0,
    lead_id: str = "",
    budget_eur: float = 0.08,
    call_fn: Optional[Callable[[dict[str, list[bytes]]], dict[str, Any]]] = None,
    log: bool = True,
    facts: Optional[str] = None,
) -> dict[str, Any]:
    """Voer de visuele sign-off uit met budgetbewaking en kostenlogging.

    Returns:
        {
          "status": "pass" | "needs_css" | "needs_content" | "human" | "skipped",
          "verdict": {...} | None,
          "cost_eur": float,
          "issues": {...},          # geclassificeerd
          "reason": str,
        }

    - "pass": demo is objectief goed, mag de deur uit zonder mens.
    - "needs_css": alleen css-fixbare issues -> qa_gate-repair, daarna opnieuw.
    - "needs_content": tekst/keuze moet veranderen -> generator/fallback.
    - "human": niet binnen budget op te lossen -> menselijke review.
    - "skipped": geen key/budget op -> menselijke review.

    `call_fn` injecteert de API-call (default `_call_sonnet`); zo testbaar.
    """
    call = call_fn or (lambda s: _call_sonnet(s, facts=facts))

    shots = screenshot_pages(doc_root, route)
    est = estimate_signoff_cost(shots)

    # Budgetguard vóór de betaalde call.
    if budget_exceeded(spent_eur, est, budget_eur):
        return {"status": "skipped", "verdict": None, "cost_eur": 0.0,
                "issues": {}, "reason": f"budget op (besteed €{spent_eur:.4f}, "
                                        f"call ~€{est:.4f}, cap €{budget_eur:.2f})"}

    try:
        verdict = call(shots)
    except Exception as exc:  # noqa: BLE001 — geen key, API-fout, parse-fout
        return {"status": "skipped", "verdict": None, "cost_eur": 0.0,
                "issues": {}, "reason": f"sign-off kon niet draaien: {exc}"}

    # Werkelijke kosten loggen.
    cost = 0.0
    toks = verdict.get("_tokens") or {}
    if toks:
        _ensure_scraper_path()
        import cost_tracker as ct
        img_tokens = sum(ct.image_tokens(*_png_size(png)) for tiles in shots.values() for png in tiles)
        cost = ct.ai_call_cost_eur(verdict.get("_model") or SONNET_MODEL,
                                   int(toks.get("in") or 0), int(toks.get("out") or 0), img_tokens)
        if log:
            _log_cost(slug, cost, verdict, img_tokens, lead_id)

    buckets = classify_issues(verdict)
    status = _decide_status(verdict, buckets)
    return {"status": status, "verdict": verdict, "cost_eur": cost,
            "issues": buckets, "reason": "ok"}


def budget_exceeded(spent_eur: float, next_call_eur: float, budget_eur: float = 0.08) -> bool:
    """Lokale budgetcheck (spiegelt cost_tracker.would_exceed_budget)."""
    try:
        _ensure_scraper_path()
        import cost_tracker as ct
        return ct.would_exceed_budget(spent_eur, next_call_eur, budget_eur)
    except Exception:  # noqa: BLE001
        return (spent_eur + next_call_eur) > budget_eur


def _decide_status(verdict: dict[str, Any], buckets: dict[str, list]) -> str:
    # Rubric-conform én richting auto-ship (besluit 2026-06-11): score >= 85
    # zonder high-issue = geslaagd. Resterende med/low-punten zijn
    # verbeterpunten (staan in het verdict-log), geen blokkade — het model
    # zet anders pass=false op smaakpunten en dan blijft alles op een mens
    # wachten.
    issues = verdict.get("issues") or []
    has_high = any(str(i.get("severity") or "").lower() == "high" for i in issues)
    score = int(verdict.get("score") or 0)
    if (verdict.get("pass") or score >= 85) and not has_high:
        return "pass"
    if buckets["none"]:
        return "human"
    if buckets["content"]:
        return "needs_content"
    if buckets["css"]:
        return "needs_css"
    # Niet geslaagd maar zonder bruikbare issue-tags -> mens.
    return "human"


def _log_cost(slug: str, cost: float, verdict: dict[str, Any], img_tokens: int, lead_id: str) -> None:
    try:
        import importlib
        import sys
        scraper = Path(__file__).resolve().parent.parent / "harv-scraper"
        if scraper.exists() and str(scraper) not in sys.path:
            sys.path.insert(0, str(scraper))
        db = importlib.import_module("db")
        toks = verdict.get("_tokens") or {}
        db.add_ai_cost(slug=slug, step="sonnet_signoff",
                       model=verdict.get("_model") or SONNET_MODEL, cost_eur=cost,
                       tok_in=int(toks.get("in") or 0), tok_out=int(toks.get("out") or 0),
                       img_tokens=img_tokens, lead_id=lead_id or None)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ sign-off kostenlog overgeslagen: {exc}")
