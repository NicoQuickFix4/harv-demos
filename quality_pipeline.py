#!/usr/bin/env python3
"""Kwaliteitstrechter voor Harv-demo's: gratis DOM-gate -> Sonnet sign-off.

Eén ingang die laag 1 (qa_gate, gratis) en laag 2 (qa_signoff, Sonnet) combineert
en de uitkomst tot een harde beslissing maakt: mag deze demo de deur uit zonder
mens, of moet een mens kijken. Budget (€0,08/demo) wordt over de hele keten
bewaakt; de Haiku-content-kosten die de demo al maakte tellen mee.

Beslissing (return["decision"]):
  - "ship"          -> demo is objectief goed, mag naar Smartleads/outreach.
  - "human_review"  -> niet zeker binnen budget; een mens moet kijken.

Gebruik (na render + lokaal schrijven van public/demo/<slug>/index.html):
    from quality_pipeline import run_quality
    res = run_quality(slug=slug, public_root=PUBLIC_ROOT, lead_id=lead_id)
    if res["decision"] != "ship":
        # niet pushen; markeer voor review
        ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# Harde kostencap per demo-RUN (besluit 2026-06-11): €0,15 — vijftien CENT,
# géén euro's. Voorlopig maximum van Nico ("kom ik later op terug"); het
# gemiddelde van een schone run moet ver daaronder blijven (~€0,05-0,08).
# De cap geldt per run (kosten sinds de start van deze keuring), niet over
# de hele levensduur van de slug.
DEMO_BUDGET_EUR = 0.15


def _demo_spent(slug: str) -> float:
    """Reeds gelogde AI-kosten voor deze demo (Haiku-content etc.)."""
    try:
        import importlib
        import sys
        scraper = Path(__file__).resolve().parent.parent / "harv-scraper"
        if scraper.exists() and str(scraper) not in sys.path:
            sys.path.insert(0, str(scraper))
        db = importlib.import_module("db")
        return float(db.get_demo_cost(slug))
    except Exception:  # noqa: BLE001
        return 0.0


def run_quality(
    *,
    slug: str,
    public_root: str | Path,
    lead_id: str = "",
    real_city: Optional[str] = None,
    do_signoff: bool = True,
    budget_eur: float = DEMO_BUDGET_EUR,
    max_signoff_rounds: int = 4,
) -> dict[str, Any]:
    """Draai de volledige kwaliteitstrechter op één gerenderde demo.

    Returns:
        {
          "decision": "ship" | "human_review",
          "reason": str,
          "gate": {...},               # qa_gate-resultaat
          "signoff": {...} | None,     # laatste qa_signoff-resultaat
          "spent_eur": float,          # totale AI-kosten van deze demo
          "rounds": int,
        }

    `real_city`: de echte plaats van de klant. Staat die NIET in de demo terwijl
    "Utrecht" (de template-default) er wel staat, dan is dat een placeholder-lek.
    """
    import qa_gate

    public_root = Path(public_root)
    index = public_root / "demo" / slug / "index.html"
    route = f"demo/{slug}/index.html"
    if not index.exists():
        return {"decision": "human_review", "reason": f"index niet gevonden: {index}",
                "gate": None, "signoff": None, "spent_eur": _demo_spent(slug), "rounds": 0}

    # ── Laag 1: gratis DOM-gate + deterministische fix-loop ──
    include_city = bool(real_city) and real_city.strip().lower() != "utrecht"
    gate = qa_gate.run_gate_file(index, doc_root=public_root, include_city_leak=include_city)

    # Placeholder-lek = datafout die de gate niet mag/kan fixen -> mens.
    if gate.get("leaks"):
        return {"decision": "human_review",
                "reason": "placeholder-lek: " + ", ".join(gate["leaks"]),
                "gate": gate, "signoff": None, "spent_eur": _demo_spent(slug), "rounds": 0}

    # Resterende harde mechanische issues (gebroken beeld, console,
    # afgeknipt logo) -> mens.
    hard = [i for i in gate.get("issues", [])
            if "gebroken" in i or "console" in i or "logo afgeknipt" in i]
    if hard:
        return {"decision": "human_review", "reason": "mechanisch: " + "; ".join(hard),
                "gate": gate, "signoff": None, "spent_eur": _demo_spent(slug), "rounds": 0}

    if not do_signoff:
        return {"decision": "ship", "reason": "gate groen (sign-off uit)",
                "gate": gate, "signoff": None, "spent_eur": _demo_spent(slug), "rounds": 0}

    # ── Laag 2+3: Sonnet sign-off met verbeterloop (besluit 2026-06-11) ──
    # Afkeuren is geen eindstation meer: content-/css-punten gaan door een
    # automatische verbeterronde (qa_improve) en daarna opnieuw de keuring in,
    # tot "ship" of tot de harde kostencap per run (default €0,15) bereikt is.
    import qa_improve
    import qa_signoff

    facts, company = _facts_from_demo(index)
    last_signoff: Optional[dict[str, Any]] = None
    rounds = 0
    # Cap geldt per RUN: meet vanaf hier (cumulatieve slug-kosten van eerdere
    # runs tellen niet mee, anders blokkeert elke herkeuring voorgoed).
    _run_baseline = _demo_spent(slug)
    for rnd in range(1, max_signoff_rounds + 1):
        rounds = rnd
        spent = max(0.0, _demo_spent(slug) - _run_baseline)
        if spent >= budget_eur:
            return {"decision": "human_review",
                    "reason": f"harde kostencap bereikt (€{spent:.2f} >= €{budget_eur:.2f} per run)",
                    "gate": gate, "signoff": last_signoff,
                    "spent_eur": spent, "rounds": rounds}
        last_signoff = qa_signoff.signoff(
            slug=slug, doc_root=public_root, route=route,
            spent_eur=spent, lead_id=lead_id, budget_eur=budget_eur,
            facts=facts,
        )
        status = last_signoff["status"]

        if status == "pass":
            return {"decision": "ship", "reason": "sign-off geslaagd",
                    "gate": gate, "signoff": last_signoff,
                    "spent_eur": max(0.0, _demo_spent(slug) - _run_baseline), "rounds": rounds}

        if status == "skipped":
            return {"decision": "human_review",
                    "reason": f"sign-off: skipped ({last_signoff.get('reason')})",
                    "gate": gate, "signoff": last_signoff,
                    "spent_eur": max(0.0, _demo_spent(slug) - _run_baseline), "rounds": rounds}

        if rnd >= max_signoff_rounds:
            break  # geen verbeterronde meer na de laatste keuring

        if status == "needs_css":
            # Door de gate-repair halen en opnieuw beoordelen (binnen budget).
            qa_gate.run_gate_file(index, doc_root=public_root, include_city_leak=include_city)
            continue  # volgende ronde sign-off

        # needs_content / human -> automatische verbeterronde, dan herkeuren.
        verdict = last_signoff.get("verdict") or {}
        imp = qa_improve.improve(
            slug=slug, index_path=index, issues=verdict.get("issues") or [],
            company=company, spent_eur=max(0.0, _demo_spent(slug) - _run_baseline),
            budget_eur=budget_eur, lead_id=lead_id,
        )
        print(f"  🔧 verbeterronde {rnd}: {imp['reason']}")
        if not imp.get("applied"):
            return {"decision": "human_review",
                    "reason": f"sign-off: {status}; verbeteraar kon niets toepassen ({imp['reason']})",
                    "gate": gate, "signoff": last_signoff,
                    "spent_eur": max(0.0, _demo_spent(slug) - _run_baseline), "rounds": rounds}
        # Gratis mechanische hercheck na de wijziging (vangt eigen schade af).
        qa_gate.run_gate_file(index, doc_root=public_root, include_city_leak=include_city)

    # Rondes op zonder pass -> mens.
    return {"decision": "human_review", "reason": "sign-off niet geslaagd binnen rondes/budget",
            "gate": gate, "signoff": last_signoff,
            "spent_eur": max(0.0, _demo_spent(slug) - _run_baseline), "rounds": rounds}


def _facts_from_demo(index: Path) -> tuple[str, str]:
    """Bouw het geverifieerde-feiten-blok voor de keurder uit de embedded
    template-data van de demo zelf (die is al fallback-ladder-gevalideerd:
    alles daarin komt van de eigen site of geverifieerde leaddata).

    Returns (facts_text, company_name); lege strings bij parse-falen.
    """
    import re as _re
    try:
        html = index.read_text(encoding="utf-8")
        m = _re.search(r"__tplData\s*=\s*(\{.*?\})\s*[;<]", html, _re.S)
        if not m:
            m = _re.search(r'"template-data"[^{]*(\{.*?\})\s*</script>', html, _re.S)
        if not m:
            return "", ""
        td = json.loads(m.group(1))
        lines = []
        company = str(td.get("COMPANY_NAME") or "").strip()
        if company:
            lines.append(f"- Bedrijfsnaam: {company}")
        for key, label in (("CITY", "Plaats"), ("FOOT_PHONE", "Telefoon"),
                           ("FOOT_EMAIL", "E-mail"), ("FOOT_ADDRESS", "Adres"),
                           ("HERO_RATING", "Google-rating")):
            v = str(td.get(key) or "").strip()
            if v:
                lines.append(f"- {label}: {v}")
        for i in range(1, 5):
            v = str(td.get(f"STAT_{i}_VALUE") or "").strip()
            l = str(td.get(f"STAT_{i}_LABEL") or "").strip()
            if v and l:
                lines.append(f"- Cijfer: {v} {l}")
        return "\n".join(lines), company
    except Exception:  # noqa: BLE001 — feiten zijn nice-to-have
        return "", ""


def _main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Harv kwaliteitstrechter (gate + sign-off)")
    ap.add_argument("slug", help="demo-slug onder public/demo/<slug>/")
    ap.add_argument("--public-root", default=str(Path(__file__).resolve().parent / "public"))
    ap.add_argument("--lead-id", default="")
    ap.add_argument("--city", default=None, help="echte plaats van de klant")
    ap.add_argument("--no-signoff", action="store_true", help="alleen de gratis DOM-gate")
    args = ap.parse_args()

    res = run_quality(slug=args.slug, public_root=args.public_root, lead_id=args.lead_id,
                      real_city=args.city, do_signoff=not args.no_signoff)
    out = {k: v for k, v in res.items() if k not in ("gate", "signoff")}
    out["gate_ok"] = bool(res.get("gate", {}) and res["gate"].get("ok"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if res["decision"] == "ship" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
