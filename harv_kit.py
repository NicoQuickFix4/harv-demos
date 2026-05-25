#!/usr/bin/env python3
"""Harv Demo Kit — de slimme, herbruikbare laag bovenop de demo generator.

Dit module bundelt drie dingen die elke sector-template kan overnemen:

  1. AI-contentlaag  (Claude Haiku)
     `ai_demo_content()` doet **één** gecombineerde call op de gescrapete data en
     beslist slim wat er waar komt te staan:
       - gelokaliseerde kop  ("Jouw dakdekker in Utrecht")
       - gelokaliseerde intro-zin
       - team-strategie       (namen tonen vs. anonieme fallback-caption)
       - veilige weergavenaam (kort als de bedrijfsnaam te lang is → overlap)
       - per sectie of-tonen   (team / reviews / blog / aanbod)
       - de kleine CTA-zin in de bevestiging

  2. Booking-engine (Cal.com)
     `build_booking_url()` bouwt de **getrackte** Cal.com-link (zelfde event als
     harvagency.com/boek), met `notion_id` + prefill van naam/e-mail.

  3. Self-contained widget
     `build_booking_widget()` levert één HTML-blok (eigen scoped CSS + JS, alle
     klassen `.harv-*`) dat in ELKE template past via de placeholder
     `{{HARV_BOOKING_WIDGET}}`. Multi-step: contactinfo → probleem → bevestiging
     ("we nemen binnen 24u contact op") → kleine CTA → Cal.com embed.
     De ingevulde data wordt naar de Flask-backend (`/demo-lead`) gepost.

Kostenregel (Harv): AI-content moet ruim binnen €0,20/lead all-in blijven.
Haiku-call hier ≈ €0,004/lead. `ai_demo_content()` logt de kosten per lead en
zet ze in het resultaat onder `_cost_eur`.

Geen ANTHROPIC_API_KEY? Dan valt alles terug op deterministische regels — de
demo rendert nog steeds compleet, alleen zonder AI-finesse.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import quote_plus, urlencode


class AIContentError(RuntimeError):
    """AI-content kon niet worden gegenereerd (geen key, of call faalde na retries).

    Wordt opgegooid wanneer `require=True` (de standaard). De pipeline behandelt
    dit als 'lead overslaan / opnieuw proberen' — we sturen NOOIT een demo zonder
    AI-content de deur uit.
    """

try:  # .env laden als python-dotenv beschikbaar is (optioneel)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Haiku pricing (Feb 2025) — voor kostenlogging per lead
HAIKU_INPUT_PER_TOKEN = 0.80 / 1_000_000  # $0.80 / 1M input
HAIKU_OUTPUT_PER_TOKEN = 4.00 / 1_000_000  # $4.00 / 1M output
EUR_PER_USD = 0.93

# Cal.com — zelfde event als harvagency.com/boek
CALCOM_EVENT = os.environ.get("CALCOM_EVENT", "harv-agency/20min.nicolas")
CALCOM_BOOKING_BASE = os.environ.get(
    "CALCOM_BOOKING_BASE", f"https://cal.com/{CALCOM_EVENT}"
)
# Origin voor de inline embed-API (app.cal.com is de embed-host)
CALCOM_EMBED_ORIGIN = os.environ.get("CALCOM_EMBED_ORIGIN", "https://app.cal.com")

# Backend die form-submissions ontvangt (Flask api_server.py → /demo-lead)
TRACKING_DOMAIN = os.environ.get("TRACKING_DOMAIN", "localhost:5050")

# Harv-branding (fallback als de lead geen eigen kleur heeft)
HARV_VIOLET = "#4928FD"

# Een bedrijfsnaam langer dan dit → AI levert een kortere weergavenaam aan
# zodat hij niet over het logo/de nav heen valt.
NAME_OVERLAP_THRESHOLD = 22

# Per sector: de "probleem"-keuzes die een eindklant van dat bedrijf zou kiezen.
# Dit demonstreert precies de lead-capture die Harv voor ze zou bouwen.
SECTOR_PROBLEMS: dict[str, list[str]] = {
    "dakdekker": [
        "Lekkage / spoedreparatie",
        "Compleet nieuw dak",
        "Dakgoot of zinkwerk",
        "Dakisolatie",
        "Onderhoud / inspectie",
    ],
    "dakdekkers": [
        "Lekkage / spoedreparatie",
        "Compleet nieuw dak",
        "Dakgoot of zinkwerk",
        "Dakisolatie",
        "Onderhoud / inspectie",
    ],
    "makelaardij": [
        "Woning verkopen",
        "Woning kopen",
        "Gratis waardebepaling",
        "Verhuur / beheer",
    ],
}
SECTOR_PROBLEMS_DEFAULT = [
    "Vrijblijvende offerte",
    "Een vraag stellen",
    "Afspraak inplannen",
]

# Sector → enkelvoud label voor de gelokaliseerde kop ("jouw {label} in {plaats}")
SECTOR_NOUN: dict[str, str] = {
    "dakdekker": "dakdekker",
    "dakdekkers": "dakdekker",
    "makelaardij": "makelaar",
}


def _esc(value: Any) -> str:
    return _html.escape(str(value or ""), quote=True)


def _first_name(full_name: Optional[str]) -> str:
    if not full_name:
        return ""
    return str(full_name).strip().split()[0]


def _sector_noun(sector: Optional[str]) -> str:
    return SECTOR_NOUN.get((sector or "").strip().lower(), (sector or "vakman").strip().lower())


def _sector_problems(sector: Optional[str]) -> list[str]:
    return SECTOR_PROBLEMS.get((sector or "").strip().lower(), SECTOR_PROBLEMS_DEFAULT)


def _scheme_for(domain: str) -> str:
    return "http" if domain.startswith(("localhost", "127.0.0.1")) else "https"


# ─────────────────────────────────────────────────────────────────────────────
# 1. AI-contentlaag
# ─────────────────────────────────────────────────────────────────────────────


def _plaats_from_data(data: dict[str, Any], stad: Optional[str], regio: Optional[str]) -> Optional[str]:
    """Bepaal de beste 'plaats' voor lokale copy: expliciet > adres > regio."""
    if stad:
        return stad.strip()
    # Probeer plaats uit een adres-string te vissen (… 1234 AB Plaatsnaam)
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    adres = data.get("adres") or contact.get("address") or ""
    if adres:
        m = re.search(r"\d{4}\s?[A-Z]{2}\s+([A-Za-zÀ-ÿ' \-]+)$", adres.strip())
        if m:
            return m.group(1).strip()
    if regio:
        return regio.strip()
    return None


def _team_has_names(data: dict[str, Any]) -> bool:
    team = data.get("team")
    members = team.get("members") if isinstance(team, dict) else team
    if not members:
        return False
    return any((m or {}).get("naam") for m in members if isinstance(m, dict))


def _fallback_content(
    data: dict[str, Any], sector: str, regio: Optional[str], stad: Optional[str]
) -> dict[str, Any]:
    """Deterministische content zonder AI — demo blijft compleet renderen."""
    naam = data.get("bedrijfsnaam") or "Uw bedrijf"
    plaats = _plaats_from_data(data, stad, regio)
    noun = _sector_noun(sector)

    if plaats:
        headline = f"Jouw {noun} in {plaats}"
        intro = f"{naam}, vakwerk en snelle service in {plaats} en omstreken."
    else:
        headline = f"Jouw betrouwbare {noun}"
        intro = f"{naam}, vakwerk en snelle service waar je op kunt rekenen."

    display_name = naam if len(naam) <= NAME_OVERLAP_THRESHOLD else (naam[: NAME_OVERLAP_THRESHOLD - 1].rstrip() + "…")

    team = data.get("team") if isinstance(data.get("team"), dict) else {}
    members = team.get("members") or []
    content = data.get("content") if isinstance(data.get("content"), dict) else {}

    return {
        "display_name": display_name,
        "local_headline": headline,
        "local_intro": intro,
        "team_mode": "named" if _team_has_names(data) else "unnamed",
        "team_caption": "Het team dat vandaag voor je klaarstaat",
        "cta_line": "Werkt dit goed voor je? Laten we even verder praten.",
        "show": {
            "team": bool(members),
            "reviews": bool(content.get("reviews")),
            "blog": bool(content.get("blog_posts")),
            "woningaanbod": bool(data.get("woningaanbod")) or bool((sector or "").lower() == "makelaardij"),
        },
        "_cost_eur": 0.0,
        "_source": "fallback",
    }


def _build_ai_prompt(
    data: dict[str, Any], sector: str, plaats: Optional[str]
) -> str:
    naam = data.get("bedrijfsnaam") or "onbekend"
    noun = _sector_noun(sector)
    team = data.get("team") if isinstance(data.get("team"), dict) else {}
    members = team.get("members") or []
    content = data.get("content") if isinstance(data.get("content"), dict) else {}

    # Compacte samenvatting van wat we hebben — houdt de call goedkoop.
    summary = {
        "bedrijfsnaam": naam,
        "sector": sector,
        "plaats": plaats,
        "tagline": content.get("tagline") or data.get("tagline"),
        "over_ons": (content.get("about") or "")[:400],
        "diensten": (content.get("services") or [])[:8],
        "aantal_teamleden": len(members),
        "teamleden_met_naam": sum(1 for m in members if isinstance(m, dict) and m.get("naam")),
        "aantal_reviews": len(content.get("reviews") or []),
        "aantal_blogposts": len(content.get("blog_posts") or []),
        "rating": content.get("rating"),
    }

    return f"""Je bent de content-strateeg van Harv Agency. We bouwen een gepersonaliseerde
demo-website voor een lokaal {noun}bedrijf om de eigenaar te overtuigen. Op basis
van de gescrapete data hieronder bepaal jij slim wat waar komt te staan.

Gescrapete data (JSON):
{json.dumps(summary, ensure_ascii=False, indent=2)}

Geef EXACT dit JSON-object terug (alleen JSON, geen uitleg):

{{
  "display_name": "korte, nette weergavenaam van het bedrijf voor in de header (max {NAME_OVERLAP_THRESHOLD} tekens, anders inkorten zonder rare afkappingen)",
  "local_headline": "pakkende kop met lokale haak, bijv. 'Jouw {noun} in {plaats or 'de regio'}' (max 48 tekens)",
  "local_intro": "1 vloeiende zin, lokaal en concreet, max 22 woorden",
  "team_mode": "named als er teamleden met namen zijn, anders unnamed",
  "team_caption": "korte anonieme caption voor als er geen namen zijn, bijv. 'Het team dat vandaag voor je klaarstaat'",
  "cta_line": "kleine, vriendelijke CTA-zin richting de eigenaar om verder te praten, max 14 woorden",
  "show": {{
    "team": <true alleen als er teamleden zijn>,
    "reviews": <true alleen als er reviews zijn>,
    "blog": <true alleen als er blogposts zijn>,
    "woningaanbod": <true alleen als sector makelaardij is>
  }}
}}

Regels:
- Schrijf in vlot, natuurlijk Nederlands. Geen Engelse marketingtaal.
- GEBRUIK NOOIT een gedachtestreepje (—) in de teksten; gebruik een komma of punt.
- Verzin geen feiten die niet uit de data blijken (geen valse cijfers/jaartallen).
- Toon een sectie alleen als er echt data voor is."""


def ai_demo_content(
    data: dict[str, Any],
    sector: str = "dakdekker",
    regio: Optional[str] = None,
    stad: Optional[str] = None,
    *,
    require: bool = True,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Bepaal slimme contentplaatsing voor één demo. Eén Haiku-call (met retries).

    Beleid (Harv): AI is VERPLICHT. Met `require=True` (de standaard) proberen we
    de call tot `max_retries` keer; lukt het dan nog niet, of ontbreekt de API-key,
    dan gooien we `AIContentError` zodat er NOOIT een demo zonder AI-content de deur
    uit gaat. Zet `require=False` alleen voor previews/tests: dan valt het terug op
    de deterministische `_fallback_content`.
    """
    plaats = _plaats_from_data(data, stad, regio)
    fallback = _fallback_content(data, sector, regio, stad)

    if not ANTHROPIC_API_KEY:
        note = "ANTHROPIC_API_KEY ontbreekt — zet de key in harv-demos/.env"
        if require:
            raise AIContentError(f"harv_kit: {note}")
        print(f"  ⚠ harv_kit: {note} — fallback")
        return fallback

    try:
        import anthropic
    except ImportError as exc:
        if require:
            raise AIContentError("harv_kit: anthropic-pakket niet geïnstalleerd "
                                 "(pip install anthropic)") from exc
        print("  ⚠ harv_kit: anthropic niet geïnstalleerd — fallback content")
        return fallback

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": _build_ai_prompt(data, sector, plaats)}],
            )
            raw = msg.content[0].text.strip()

            inp, out = msg.usage.input_tokens, msg.usage.output_tokens
            cost = (inp * HAIKU_INPUT_PER_TOKEN + out * HAIKU_OUTPUT_PER_TOKEN) * EUR_PER_USD
            print(f"  🤖 harv_kit AI-content: {inp}+{out} tokens = €{cost:.4f}")

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                raise ValueError("geen JSON-object in AI-respons")
            ai = json.loads(match.group())

            # Merge: AI-waarden over de fallback heen, met sanity-checks.
            merged = dict(fallback)
            for key in ("display_name", "local_headline", "local_intro", "team_mode", "team_caption", "cta_line"):
                val = ai.get(key)
                if isinstance(val, str) and val.strip():
                    merged[key] = val.strip()

            # Nooit een gedachtestreepje (projectregel) — vervang door komma.
            for key in ("local_headline", "local_intro", "team_caption", "cta_line"):
                merged[key] = re.sub(r"\s*[—–]\s*", ", ", merged[key])

            # Weergavenaam mag nooit te lang blijven (overlap-garantie).
            if len(merged["display_name"]) > NAME_OVERLAP_THRESHOLD:
                merged["display_name"] = merged["display_name"][: NAME_OVERLAP_THRESHOLD - 1].rstrip() + "…"

            if isinstance(ai.get("show"), dict):
                merged_show = dict(fallback["show"])
                for k, v in ai["show"].items():
                    if isinstance(v, bool):
                        merged_show[k] = v
                # Sectie alleen tonen als er ECHT data is — AI mag niets verzinnen.
                merged["show"] = {
                    "team": merged_show.get("team", False) and fallback["show"]["team"],
                    "reviews": merged_show.get("reviews", False) and fallback["show"]["reviews"],
                    "blog": merged_show.get("blog", False) and fallback["show"]["blog"],
                    "woningaanbod": merged_show.get("woningaanbod", False) and fallback["show"]["woningaanbod"],
                }

            merged["_cost_eur"] = round(cost, 5)
            merged["_source"] = "haiku"
            return merged

        except Exception as exc:  # transient API-fout, JSON-fout, etc.
            last_err = exc
            print(f"  ⚠ harv_kit AI-content poging {attempt}/{max_retries} faalde: {exc}")
            if attempt < max_retries:
                time.sleep(1.5 * attempt)  # eenvoudige backoff

    # Alle pogingen mislukt.
    if require:
        raise AIContentError(
            f"harv_kit: AI-content faalde na {max_retries} pogingen: {last_err}"
        )
    print("  ⚠ harv_kit: AI-content faalde — fallback content")
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# 2. Booking-engine (Cal.com)
# ─────────────────────────────────────────────────────────────────────────────


def build_booking_url(lead: dict[str, Any], notion_id: Optional[str] = None) -> str:
    """Getrackte Cal.com-link voor één lead (zelfde event als /boek).

    Draagt `notion_id` mee (mét streepjes) zodat Make.com de juiste Notion-pagina
    kan updaten bij een boeking. `name`/`email` zijn prefill voor het boekformulier.
    Werkt ook zonder notion_id (dan puur prefill + branding).
    """
    notion_id = notion_id or lead.get("notion_page_id") or lead.get("notion_id")
    params: dict[str, str] = {}
    if notion_id:
        params["notion_id"] = str(notion_id)

    naam = (lead.get("contact_naam") or lead.get("bedrijfsnaam") or "").strip()
    if naam:
        params["name"] = naam
    email = (lead.get("email") or "").strip()
    if email:
        params["email"] = email

    if not params:
        return CALCOM_BOOKING_BASE
    return f"{CALCOM_BOOKING_BASE}?{urlencode(params)}"


def build_demo_lead_endpoint() -> str:
    """De URL waar de widget de form-submissie naartoe post (/demo-lead)."""
    return f"{_scheme_for(TRACKING_DOMAIN)}://{TRACKING_DOMAIN}/demo-lead"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Self-contained UI-fragmenten (alle klassen .harv-*)
# ─────────────────────────────────────────────────────────────────────────────


def build_overlap_guard_css() -> str:
    """Globale CSS die logo- en naam-overlap voorkomt in ELKE template.

    Plaats dit één keer in de <head> (placeholder {{HARV_GUARD_CSS}}). Het is
    bewust generiek: het knipt te lange namen netjes af en houdt het logo binnen
    veilige grenzen, ongeacht de template-specifieke styling.
    """
    return """<style id="harv-guard-css">
/* Harv overlap-guards — voorkomt dat lange namen/logo's over elkaar vallen */
.harv-namefit{
  display:inline-block; max-width:100%;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  vertical-align:bottom;
}
.harv-namefit--wrap{ white-space:normal; overflow-wrap:anywhere; text-wrap:balance; }
img.harv-logo-safe{
  max-height:40px; width:auto; max-width:180px;
  object-fit:contain; object-position:left center;
}
@media(max-width:600px){ img.harv-logo-safe{ max-height:32px; max-width:140px; } }
/* Auto-fit kop: schaalt mee zodat hij niet uit de hero loopt */
.harv-fit-head{ font-size:clamp(22px, 5vw, 46px); line-height:1.1; overflow-wrap:anywhere; }
</style>"""


def build_presented_by_html(
    booking_url: str, lead_id: Optional[str] = None, slug: Optional[str] = None
) -> str:
    """Klikbare 'Presented by Harv Agency'-footer → opent de booking (nieuw tab).

    Vervangt de placeholder {{HARV_PRESENTED_BY}}. Bewust onopvallend maar klikbaar.
    """
    track = ""
    if lead_id:
        track = ' data-harv-lead="' + _esc(lead_id) + '" data-harv-slug="' + _esc(slug or "") + '"'
    anchor = (
        '<a class="harv-presented" href="' + _esc(booking_url) + '" target="_blank" rel="noopener"' + track + '>'
        '<span class="harv-presented-star">&#42;</span>'
        '<span>Presented by <strong>Harv Agency</strong></span>'
        '<span class="harv-presented-cta">Plan een kennismaking &#8599;</span>'
        '</a>'
    )
    css = """
<style id="harv-presented-css">
.harv-presented{
  display:inline-flex; align-items:center; gap:8px;
  font-family:'Inter Tight',system-ui,sans-serif; font-size:13px;
  text-decoration:none; color:#555; padding:8px 14px; border-radius:999px;
  background:#fff; border:1px solid #eee; box-shadow:0 2px 10px rgba(0,0,0,.06);
  transition:transform .15s ease, box-shadow .15s ease;
}
.harv-presented:hover{ transform:translateY(-1px); box-shadow:0 6px 18px rgba(0,0,0,.10); }
.harv-presented strong{ color:#111; }
.harv-presented-star{ color:var(--harv-brand,#4928FD); font-weight:800; }
.harv-presented-cta{ color:var(--harv-brand,#4928FD); font-weight:700; }
@media(max-width:600px){ .harv-presented-cta{ display:none; } }
</style>"""
    return anchor + css


def build_problem_chips(sector: Optional[str]) -> str:
    chips = []
    for label in _sector_problems(sector):
        chips.append(
            f'<button type="button" class="harv-chip" data-val="{_esc(label)}">{_esc(label)}</button>'
        )
    return "".join(chips)


# Het widget-blok als template met %%TOKENS%% (zodat CSS/JS-accolades veilig zijn).
_WIDGET_TEMPLATE = r"""
<section class="harv-w" id="%%WID%%"
  data-endpoint="%%ENDPOINT%%"
  data-lead="%%LEAD_ID%%"
  data-slug="%%SLUG%%"
  data-notion="%%NOTION_ID%%"
  data-sector="%%SECTOR%%"
  data-callink="%%CALLINK%%"
  data-calorigin="%%CALORIGIN%%">
  <div class="harv-w-card">
    <div class="harv-w-head">
      <span class="harv-w-live"><i></i> Reageert meestal binnen een uur</span>
      <h3 class="harv-w-title">%%TITLE%%</h3>
      <p class="harv-w-sub">Laat je gegevens achter, dan nemen we snel contact op.</p>
      <div class="harv-w-prog"><b class="on"></b><b></b><b></b></div>
    </div>

    <div class="harv-step" data-step="1">
      <label class="harv-f">Naam
        <input type="text" name="naam" autocomplete="name" placeholder="Jouw naam" value="%%PRE_NAAM%%">
      </label>
      <label class="harv-f">E-mailadres
        <input type="email" name="email" autocomplete="email" placeholder="jij@voorbeeld.nl" value="%%PRE_EMAIL%%">
      </label>
      <label class="harv-f">Telefoon <span class="harv-opt">(optioneel)</span>
        <input type="tel" name="telefoon" autocomplete="tel" placeholder="06 - ..." value="%%PRE_TEL%%">
      </label>
      <div class="harv-err" data-err></div>
      <button type="button" class="harv-btn harv-next">Volgende &#8594;</button>
    </div>

    <div class="harv-step" data-step="2" hidden>
      <div class="harv-q">Waar kunnen we mee helpen?</div>
      <div class="harv-chips">%%CHIPS%%</div>
      <label class="harv-f">Korte toelichting <span class="harv-opt">(optioneel)</span>
        <textarea name="bericht" rows="3" placeholder="Vertel kort wat je zoekt..."></textarea>
      </label>
      <div class="harv-row">
        <button type="button" class="harv-btn harv-ghost harv-back">&#8592; Terug</button>
        <button type="button" class="harv-btn harv-submit">Versturen</button>
      </div>
    </div>

    <div class="harv-step harv-done" data-step="3" hidden>
      <div class="harv-check">&#10003;</div>
      <h3 class="harv-done-h">Gelukt<span data-fname></span>!</h3>
      <p class="harv-done-p">%%BEDRIJF%% neemt binnen %%HOURS%% uur contact met je op.</p>
      <div class="harv-meta">
        <span class="harv-meta-line">%%CTA_LINE%%</span>
        <button type="button" class="harv-btn harv-open-cal">Plan een kennismaking &#8594;</button>
      </div>
      <div class="harv-cal-wrap" hidden>
        <div class="harv-cal" id="%%WID%%-cal"></div>
        <a class="harv-cal-fb" href="%%BOOKING_URL%%" target="_blank" rel="noopener">Liever in een nieuw tabblad? Open de agenda &#8599;</a>
      </div>
    </div>
  </div>

<style id="harv-w-css">
.harv-w{ --harv-brand: %%BRAND%%; font-family:'Inter Tight',system-ui,-apple-system,sans-serif;
  max-width:480px; margin:0 auto; box-sizing:border-box; }
.harv-w *{ box-sizing:border-box; }
.harv-w-card{ background:#fff; border:1px solid #ececec; border-radius:20px; padding:26px;
  box-shadow:0 20px 60px rgba(0,0,0,.10); }
.harv-w-live{ display:inline-flex; align-items:center; gap:7px; font-size:12px; font-weight:600;
  color:#1a8f4a; background:#eafaf0; padding:5px 11px; border-radius:999px; }
.harv-w-live i{ width:7px; height:7px; border-radius:50%; background:#1fbf5d; display:inline-block;
  box-shadow:0 0 0 0 rgba(31,191,93,.6); animation:harvpulse 1.8s infinite; }
@keyframes harvpulse{ 0%{box-shadow:0 0 0 0 rgba(31,191,93,.5);} 70%{box-shadow:0 0 0 7px rgba(31,191,93,0);} 100%{box-shadow:0 0 0 0 rgba(31,191,93,0);} }
.harv-w-title{ font-size:23px; font-weight:800; letter-spacing:-.4px; margin:14px 0 4px; color:#111; }
.harv-w-sub{ font-size:14px; color:#777; margin:0 0 18px; line-height:1.5; }
.harv-w-prog{ display:flex; gap:6px; margin-bottom:18px; }
.harv-w-prog b{ height:4px; flex:1; border-radius:999px; background:#ececec; transition:background .25s; }
.harv-w-prog b.on{ background:var(--harv-brand); }
.harv-f{ display:block; font-size:13px; font-weight:600; color:#333; margin-bottom:13px; }
.harv-opt{ color:#aaa; font-weight:500; }
.harv-f input, .harv-f textarea{ width:100%; margin-top:6px; padding:12px 14px; font-size:15px;
  font-family:inherit; border:1.5px solid #e6e6e6; border-radius:11px; background:#fafafa; color:#111;
  outline:none; transition:border-color .15s, background .15s; }
.harv-f input:focus, .harv-f textarea:focus{ border-color:var(--harv-brand); background:#fff; }
.harv-f textarea{ resize:vertical; }
.harv-q{ font-size:13px; font-weight:700; color:#333; margin-bottom:10px; }
.harv-chips{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
.harv-chip{ font-family:inherit; font-size:13px; font-weight:600; color:#444; cursor:pointer;
  padding:9px 14px; border-radius:999px; border:1.5px solid #e6e6e6; background:#fff; transition:all .14s; }
.harv-chip:hover{ border-color:var(--harv-brand); }
.harv-chip.sel{ background:var(--harv-brand); border-color:var(--harv-brand); color:#fff; }
.harv-btn{ font-family:inherit; font-size:15px; font-weight:700; cursor:pointer; border:none;
  border-radius:11px; padding:13px 18px; background:var(--harv-brand); color:#fff; width:100%;
  transition:transform .12s, filter .15s; }
.harv-btn:hover{ transform:translateY(-1px); filter:brightness(1.06); }
.harv-btn.harv-ghost{ background:#f2f2f2; color:#333; }
.harv-row{ display:flex; gap:10px; }
.harv-row .harv-back{ flex:0 0 38%; }
.harv-err{ color:#d4351c; font-size:13px; font-weight:600; margin-bottom:10px; min-height:0; }
.harv-done{ text-align:center; padding:6px 0; }
.harv-check{ width:54px; height:54px; margin:6px auto 14px; border-radius:50%; background:#eafaf0;
  color:#1fbf5d; font-size:28px; line-height:54px; font-weight:800; }
.harv-done-h{ font-size:22px; font-weight:800; color:#111; margin:0 0 6px; }
.harv-done-p{ font-size:15px; color:#555; line-height:1.55; margin:0 0 22px; }
.harv-meta{ border-top:1px dashed #e6e6e6; padding-top:18px; }
.harv-meta-line{ display:block; font-size:12.5px; color:#999; margin-bottom:12px; }
.harv-cal-wrap{ margin-top:18px; }
.harv-cal{ width:100%; min-height:520px; overflow:auto; border-radius:12px; }
.harv-cal-fb{ display:inline-block; margin-top:10px; font-size:12.5px; color:var(--harv-brand); text-decoration:none; }
@media(max-width:600px){ .harv-w-card{ padding:20px; border-radius:16px; } .harv-w-title{ font-size:20px; } }
</style>

<script>
(function(){
  var root = document.getElementById("%%WID%%");
  if(!root || root.dataset.harvInit) return;
  root.dataset.harvInit = "1";

  var steps = root.querySelectorAll(".harv-step");
  var prog  = root.querySelectorAll(".harv-w-prog b");
  var errEl = root.querySelector("[data-err]");
  var selected = [];

  function show(n){
    steps.forEach(function(s){ s.hidden = (s.dataset.step !== String(n)); });
    prog.forEach(function(b,i){ b.classList.toggle("on", i < n); });
    if(errEl) errEl.textContent = "";
  }
  function val(name){ var el = root.querySelector('[name="'+name+'"]'); return el ? el.value.trim() : ""; }
  function validEmail(e){ return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e); }

  // Stap 1 → 2
  var nextBtn = root.querySelector(".harv-next");
  if(nextBtn) nextBtn.addEventListener("click", function(){
    if(!val("naam")){ errEl.textContent = "Vul je naam in."; return; }
    if(!validEmail(val("email"))){ errEl.textContent = "Vul een geldig e-mailadres in."; return; }
    show(2);
  });

  // Terug
  var backBtn = root.querySelector(".harv-back");
  if(backBtn) backBtn.addEventListener("click", function(){ show(1); });

  // Chips
  root.querySelectorAll(".harv-chip").forEach(function(c){
    c.addEventListener("click", function(){
      c.classList.toggle("sel");
      var v = c.dataset.val;
      var i = selected.indexOf(v);
      if(i>-1){ selected.splice(i,1); } else { selected.push(v); }
    });
  });

  // Versturen
  var submitBtn = root.querySelector(".harv-submit");
  if(submitBtn) submitBtn.addEventListener("click", function(){
    submitBtn.disabled = true; submitBtn.textContent = "Versturen...";
    var payload = {
      lead_id: root.dataset.lead || "",
      slug: root.dataset.slug || "",
      notion_id: root.dataset.notion || "",
      sector: root.dataset.sector || "",
      naam: val("naam"), email: val("email"), telefoon: val("telefoon"),
      problemen: selected, bericht: val("bericht"),
      url: location.href
    };
    var ep = root.dataset.endpoint;
    var done = function(){
      var fn = (payload.naam || "").split(" ")[0];
      var fEl = root.querySelector("[data-fname]");
      if(fEl && fn) fEl.textContent = ", " + fn;
      show(3);
    };
    if(ep){
      fetch(ep, { method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(payload), keepalive:true })
        .then(done).catch(function(e){ console.warn("harv demo-lead post faalde", e); done(); });
    } else { done(); }
  });

  // Cal.com embed lazy laden
  var calBtn = root.querySelector(".harv-open-cal");
  if(calBtn) calBtn.addEventListener("click", function(){
    var wrap = root.querySelector(".harv-cal-wrap");
    if(wrap) wrap.hidden = false;
    calBtn.style.display = "none";
    if(root.dataset.calInit) return;
    root.dataset.calInit = "1";
    (function (C, A, L) { var p = function (a, ar) { a.q.push(ar); }; var d = C.document;
      C.Cal = C.Cal || function () { var cal = C.Cal; var ar = arguments;
        if (!cal.loaded) { cal.ns = {}; cal.q = cal.q || []; d.head.appendChild(d.createElement("script")).src = A; cal.loaded = true; }
        if (ar[0] === L) { var api = function () { p(api, arguments); }; var ns = ar[1]; api.q = api.q || [];
          if(typeof ns === "string"){ cal.ns[ns] = cal.ns[ns] || api; p(cal.ns[ns], ar); p(cal, ["initNamespace", ns]); } else p(cal, ar); return; }
        p(cal, ar); }; })(window, "https://app.cal.com/embed/embed.js", "init");
    Cal("init", "harv", { origin: root.dataset.calorigin || "https://app.cal.com" });
    Cal.ns.harv("inline", {
      elementOrSelector: "#%%WID%%-cal",
      calLink: root.dataset.callink,
      config: {
        layout: "month_view",
        name: val("naam"),
        email: val("email"),
        metadata: { notion_id: root.dataset.notion || "", harv_lead: root.dataset.lead || "" }
      }
    });
  });
})();
</script>
</section>
"""


def build_booking_widget(
    *,
    lead_id: str = "",
    slug: str = "",
    notion_id: str = "",
    sector: str = "dakdekker",
    bedrijf: str = "Dit bedrijf",
    title: Optional[str] = None,
    prefill_naam: str = "",
    prefill_email: str = "",
    prefill_telefoon: str = "",
    cta_line: str = "Werkt dit goed voor je? Laten we even verder praten.",
    confirm_hours: int = 24,
    brand: Optional[str] = None,
    booking_url: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> str:
    """Bouw het volledige, self-contained widget-blok (HTML + scoped CSS + JS).

    Plak het resultaat op de plek van placeholder {{HARV_BOOKING_WIDGET}} in
    eender welke template. Heeft geen externe afhankelijkheden behalve de
    Cal.com-embed (lazy geladen pas bij klik op 'Plan een kennismaking').
    """
    wid = f"harv-w-{slugify_id(slug or lead_id or 'demo')}"
    title = title or _default_widget_title(sector)
    brand = brand or HARV_VIOLET
    booking_url = booking_url or CALCOM_BOOKING_BASE
    endpoint = endpoint or build_demo_lead_endpoint()

    tokens = {
        "%%WID%%": wid,
        "%%ENDPOINT%%": _esc(endpoint),
        "%%LEAD_ID%%": _esc(lead_id),
        "%%SLUG%%": _esc(slug),
        "%%NOTION_ID%%": _esc(notion_id),
        "%%SECTOR%%": _esc(sector),
        "%%CALLINK%%": _esc(CALCOM_EVENT),
        "%%CALORIGIN%%": _esc(CALCOM_EMBED_ORIGIN),
        "%%TITLE%%": _esc(title),
        "%%PRE_NAAM%%": _esc(prefill_naam),
        "%%PRE_EMAIL%%": _esc(prefill_email),
        "%%PRE_TEL%%": _esc(prefill_telefoon),
        "%%CHIPS%%": build_problem_chips(sector),
        "%%BEDRIJF%%": _esc(bedrijf),
        "%%HOURS%%": str(int(confirm_hours)),
        "%%CTA_LINE%%": _esc(cta_line),
        "%%BRAND%%": brand,
        "%%BOOKING_URL%%": _esc(booking_url),
    }
    out = _WIDGET_TEMPLATE
    for k, v in tokens.items():
        out = out.replace(k, v)
    return out


def _default_widget_title(sector: Optional[str]) -> str:
    s = (sector or "").strip().lower()
    if s in ("dakdekker", "dakdekkers"):
        return "Vraag een vrijblijvende offerte aan"
    if s == "makelaardij":
        return "Plan een gratis waardebepaling"
    return "Neem vrijblijvend contact op"


def slugify_id(value: str) -> str:
    """HTML-id-veilige slug (letters/cijfers/koppelteken)."""
    s = re.sub(r"[^a-z0-9\-]+", "-", (value or "").lower())
    return re.sub(r"-+", "-", s).strip("-") or "demo"


def _detect_brand(data: dict[str, Any]) -> str:
    """Beste merkkleur: expliciete primaire_kleur > visual.colors.primary > Harv-violet."""
    if data.get("primaire_kleur"):
        return data["primaire_kleur"]
    visual = data.get("visual")
    if isinstance(visual, dict):
        colors = visual.get("colors")
        if isinstance(colors, dict) and colors.get("primary"):
            return colors["primary"]
    return HARV_VIOLET


# ─────────────────────────────────────────────────────────────────────────────
# 4. Convenience: alle nieuwe placeholders in één keer
# ─────────────────────────────────────────────────────────────────────────────


def kit_placeholders(
    data: dict[str, Any],
    *,
    lead_id: str = "",
    slug: str = "",
    notion_id: str = "",
    sector: str = "dakdekker",
    regio: Optional[str] = None,
    stad: Optional[str] = None,
    brand: Optional[str] = None,
    ai_content: Optional[dict[str, Any]] = None,
    require: bool = True,
) -> dict[str, str]:
    """Geef een dict {placeholder: html/tekst} terug voor de render-stap.

    Doet de AI-call (tenzij `ai_content` al meegegeven is), bouwt de widget,
    de presented-by-footer, de overlap-guard-CSS en levert de gelokaliseerde
    teksten. De render-stap hoeft alleen nog `.replace()` te doen.

    `require=True` (standaard) → AI is verplicht; faalt hij, dan `AIContentError`.
    """
    ai = ai_content or ai_demo_content(data, sector=sector, regio=regio, stad=stad, require=require)
    lead_like = {
        "bedrijfsnaam": data.get("bedrijfsnaam"),
        "contact_naam": (data.get("contact") or {}).get("contact_naam") if isinstance(data.get("contact"), dict) else data.get("contact_naam"),
        "email": (data.get("contact") or {}).get("email") if isinstance(data.get("contact"), dict) else data.get("email"),
        "notion_page_id": notion_id or data.get("notion_page_id"),
    }
    booking_url = build_booking_url(lead_like, notion_id=notion_id or None)
    brand = brand or _detect_brand(data)

    widget = build_booking_widget(
        lead_id=lead_id,
        slug=slug,
        notion_id=notion_id,
        sector=sector,
        bedrijf=ai.get("display_name") or data.get("bedrijfsnaam") or "Dit bedrijf",
        prefill_naam="",
        prefill_email="",
        prefill_telefoon="",
        cta_line=ai.get("cta_line", "Werkt dit goed voor je? Laten we even verder praten."),
        brand=brand,
        booking_url=booking_url,
    )

    return {
        "{{HARV_BOOKING_WIDGET}}": widget,
        "{{HARV_PRESENTED_BY}}": build_presented_by_html(booking_url, lead_id=lead_id, slug=slug),
        "{{HARV_GUARD_CSS}}": build_overlap_guard_css(),
        "{{HARV_LOCAL_HEADLINE}}": _esc(ai.get("local_headline", "")),
        "{{HARV_LOCAL_INTRO}}": _esc(ai.get("local_intro", "")),
        "{{HARV_DISPLAY_NAME}}": _esc(ai.get("display_name", data.get("bedrijfsnaam") or "")),
        "{{HARV_TEAM_CAPTION}}": _esc(ai.get("team_caption", "")),
        "{{HARV_BOOKING_URL}}": _esc(booking_url),
    }

