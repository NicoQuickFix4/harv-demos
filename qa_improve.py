#!/usr/bin/env python3
"""Laag 3 van de kwaliteitstrechter: automatische verbeterronde.

Keurt de sign-off een demo af op content-/css-punten, dan probeert deze laag
de demo gericht te VERBETEREN zodat hij bij herkeuring wél slaagt — in plaats
van hem direct naar menselijke review te sturen (besluit 2026-06-11: demo's
moeten op termijn zonder mens kunnen shippen).

Werkwijze: de issues van de keurder + een uitgeklede versie van de demo-HTML
(zonder script/style-bulk) gaan naar Sonnet, die UITSLUITEND chirurgische
operaties teruggeeft:
  - replace_text: exacte zichtbare tekst vervangen (feiten nooit verzinnen);
  - remove:       een element verwijderen (liever weglaten dan een claim);
  - css:          een css-regel toevoegen (komt in <style id="harv-improve">).

De operaties worden in Python toegepast; mislukte/niet-matchende ops worden
stil overgeslagen. Kosten worden gelogd (step "sonnet_improve") en bewaakt
tegen de harde demo-cap.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

IMPROVE_MODEL = os.environ.get("HARV_IMPROVE_MODEL", "claude-sonnet-4-6")


def _ensure_scraper_path() -> None:
    import sys
    scraper = Path(__file__).resolve().parent.parent / "harv-scraper"
    if scraper.exists() and str(scraper) not in sys.path:
        sys.path.insert(0, str(scraper))


def _trimmed_html(html: str, limit: int = 50000) -> str:
    """Structuur + zichtbare tekst, zonder de bulk: script-inhoud en
    style-inhoud eruit (de verbeteraar voegt alleen NIEUWE css toe)."""
    out = re.sub(r"(<script\b[^>]*>).*?(</script>)", r"\1\2", html, flags=re.S | re.I)
    out = re.sub(r"(<style\b[^>]*>).*?(</style>)", r"\1\2", out, flags=re.S | re.I)
    return out[:limit]


_PROMPT = """Je verbetert een demo-website van dakdekkersbedrijf {company}. Een \
kwaliteitskeurder vond onderstaande punten. Geef chirurgische operaties die de \
punten oplossen, als JSON. Harde regels:
- Verzin NOOIT feiten (geen jaartallen, keurmerken, aantallen, namen). Twijfel \
over een claim -> verwijder het element in plaats van de claim aan te passen.
- replace_text alleen met een EXACT voorkomende tekst uit de HTML.
- Hou het minimaal: alleen ops die een genoemd punt oplossen.
- VERWIJDER NOOIT structurele blokken: geen secties, grids, kolommen of \
foto-mozaïeken (section, .why-mosaic, .stats, .steps, .reviews-grid, header, \
footer, .hero). Alleen kleine elementen (één badge, één regel tekst, één kaart).
- VERWIJDER NOOIT afbeeldingen (img/figure/foto-containers) — foto's zijn het \
belangrijkste element van de demo. Een beeldprobleem los je niet op door het \
beeld weg te halen.
- Maskeer ontbrekende content NIET met css (geen grijze placeholder-vlakken, \
geen min-height-hacks). Kun je een beeldprobleem niet echt oplossen, sla het \
punt dan over.
- css-regels zijn volledige regels ("selector {{ ... }}") voor kleine correcties \
(afbreking, marge, contrast); ze worden achteraan toegevoegd en winnen op volgorde.

Punten van de keurder:
{issues}

Geef UITSLUITEND JSON, schema:
{{"ops": [
  {{"op": "replace_text", "find": "...", "replace": "..."}},
  {{"op": "remove", "selector": "css-selector"}},
  {{"op": "css", "rule": "selector {{ eigenschap: waarde; }}"}}
]}}

De (uitgeklede) HTML van de demo:
"""


def improve(
    *,
    slug: str,
    index_path: str | Path,
    issues: list[dict[str, Any]],
    company: str = "",
    spent_eur: float = 0.0,
    budget_eur: float = 13.0,
    lead_id: str = "",
) -> dict[str, Any]:
    """Eén verbeterronde. Returns {"applied": int, "cost_eur": float, "reason": str}."""
    index_path = Path(index_path)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"applied": 0, "cost_eur": 0.0, "reason": "ANTHROPIC_API_KEY ontbreekt"}
    if not issues:
        return {"applied": 0, "cost_eur": 0.0, "reason": "geen issues om te verbeteren"}

    html = index_path.read_text(encoding="utf-8")
    trimmed = _trimmed_html(html)

    # Budgetguard vóór de betaalde call (ruwe schatting: 1 token ~ 4 tekens).
    _ensure_scraper_path()
    try:
        import cost_tracker as ct
        est = ct.ai_call_cost_eur(IMPROVE_MODEL, len(trimmed) // 4 + 800, 1200)
        if ct.would_exceed_budget(spent_eur, est, budget_eur):
            return {"applied": 0, "cost_eur": 0.0,
                    "reason": f"budget op (besteed €{spent_eur:.2f}, call ~€{est:.2f}, cap €{budget_eur:.2f})"}
    except Exception:  # noqa: BLE001
        ct = None

    issue_lines = "\n".join(
        f"- [{i.get('severity','?')}/{i.get('fix','?')}] {i.get('desc','')}" for i in issues)
    prompt = _PROMPT.format(company=company or "de klant", issues=issue_lines)

    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=IMPROVE_MODEL, max_tokens=1400,
        messages=[{"role": "user", "content": prompt + trimmed}],
    )
    raw = msg.content[0].text
    cost = 0.0
    try:
        if ct is not None:
            cost = ct.ai_call_cost_eur(IMPROVE_MODEL,
                                       int(msg.usage.input_tokens), int(msg.usage.output_tokens))
            import importlib
            db = importlib.import_module("db")
            db.add_ai_cost(slug=slug, step="sonnet_improve", model=IMPROVE_MODEL,
                           cost_eur=cost, tok_in=int(msg.usage.input_tokens),
                           tok_out=int(msg.usage.output_tokens), lead_id=lead_id or None)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ kostenlog verbeterronde overgeslagen: {exc}")

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"applied": 0, "cost_eur": cost, "reason": "geen JSON in verbeter-respons"}
    try:
        ops = (json.loads(m.group()) or {}).get("ops") or []
    except json.JSONDecodeError as exc:
        return {"applied": 0, "cost_eur": cost, "reason": f"JSON-fout: {exc}"}

    applied = 0
    css_rules: list[str] = []
    from bs4 import BeautifulSoup
    soup: Optional[BeautifulSoup] = None
    for op in ops[:12]:
        kind = (op.get("op") or "").strip()
        if kind == "replace_text":
            find, repl = str(op.get("find") or ""), str(op.get("replace") or "")
            if find and find in html:
                html = html.replace(find, repl)
                applied += 1
        elif kind == "remove":
            sel = str(op.get("selector") or "").strip()
            if not sel:
                continue
            # Vangrail: nooit structurele blokken slopen — dat maakt de demo
            # slechter dan het issue dat het moest oplossen.
            _PROTECTED = ("section", "header", "footer", "main", ".hero",
                          ".why-mosaic", ".stats", ".steps", ".reviews-grid",
                          ".process-grid", ".footer-top", ".services-list")
            if any(p in sel.lower() for p in _PROTECTED):
                continue
            if soup is None:
                soup = BeautifulSoup(html, "html.parser")
            try:
                hits = soup.select(sel)
            except Exception:  # noqa: BLE001 — ongeldige selector
                continue
            for el in hits[:4]:
                if len(el.find_all()) > 10:
                    continue  # te structureel om te verwijderen
                if el.name in ("img", "figure", "picture") or el.find("img") is not None:
                    continue  # RULES 7c: foto's verwijderen mag nooit
                el.decompose()
                applied += 1
        elif kind == "css":
            rule = str(op.get("rule") or "").strip()
            if rule and "{" in rule and "}" in rule:
                css_rules.append(rule)
                applied += 1

    if soup is not None:
        html = str(soup)
    if css_rules:
        block = '<style id="harv-improve">' + "\n".join(css_rules) + "</style>"
        if "</head>" in html:
            html = html.replace("</head>", block + "</head>", 1)
        else:
            html += block

    if applied:
        index_path.write_text(html, encoding="utf-8")
    return {"applied": applied, "cost_eur": cost,
            "reason": f"{applied} operatie(s) toegepast" if applied else "geen toepasbare ops"}
