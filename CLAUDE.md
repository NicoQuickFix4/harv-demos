# harv-demos — Claude Code context

## Project doel
Genereer gepersonaliseerde demo-pagina's per lead. Doel: zodra de lead de
link opent, ziet hij/zij een demo-website die op de zijne lijkt — eigen logo,
eigen kleuren, eigen team, eigen aanbod — maar dan modern en conversie-gericht.

Live URL-patroon: `https://harv-demos.vercel.app/demo/<slug>/`
Repo: github.com/NicoQuickFix4/harv-demos (public, Vercel-deployed)
Projectmap: `~/Developer/harv-demos`

## Hoe te draaien
```bash
cd ~/Developer/harv-demos

# Standaard run — scrapet + commit + push + verify deploy
python3 generate_demo.py \
    --url https://example-makelaar.nl \
    --sector makelaardij \
    --slug example-makelaar

# Optioneel: meteen de Notion-pagina van de lead updaten na succesvolle deploy
python3 generate_demo.py \
    --url https://… --sector makelaardij --slug … \
    --notion-page-id 12345678-90ab-cdef-…

# Alleen data scrapen, niet committen (handig voor debug)
python3 generate_demo.py --url https://… --sector makelaardij --no-push

# verify_deploy poll overslaan (zolang nog geen template gerenderd is)
python3 generate_demo.py --url https://… --sector makelaardij --skip-verify

# Oude (nested) scrape_site_data gebruiken ipv de Fase-2 vlakke structuur
python3 generate_demo.py --url https://… --sector makelaardij --legacy
```

## Output
- `data/<slug>/data.json` — gescrapete data, zelf gecommit naar `origin/main`
- `public/demo/<slug>/index.html` — gerenderde demo (gebeurt nog niet
  automatisch — template-fill is een aparte stap)
- Live: `https://harv-demos.vercel.app/demo/<slug>/`

## Pipeline-positie
```
scraper.py (harv-scraper)
  → leads.db
  → notion_sync.py
  → fase=synced_to_notion
  → [voor elke ready-lead] generate_demo.py
  → public/demo/<slug>/ live
  → update_notion_after_demo() zet Demo-link + fase=Demo build + Demo Approved=❌
  → push_smartleads.py (harv-scraper) na demo-verstuurd
```

## Fase 1 ✅ (klaar)
- Primaire brand-kleur via theme-color meta + frequentie-analyse op CSS
  (filtert grijswaardes + near-white/black, geeft de meest-voorkomende
  brandkleur)
- Beste teamfoto via `<img>` heuristieken (team-keywords in alt/url/class,
  dimensies, fallback op `og:image`)

## Fase 2 ✅ (klaar — taak Fase 2)
Volledige `scrape_full_site_data(url, sector)` met flat JSON output:
- **bedrijfsnaam** — og:site_name → title → hostname slug
- **primaire_kleur** — `extract_color_palette(...).primary`
- **logo_url** — img met "logo" in class/id/alt/src → eerste img in `<header>` → favicon
- **teamfoto_url** — hero team-image
- **tagline** — `<h1>` als <80 chars → meta description
- **nav_items** — top-level `<ul>/<li>/<a>` structuur, sector-fallback uit `NAV_FALLBACKS`
- **blog_posts[]** — path scrape `/blog`, `/nieuws`, `/artikelen`, `/updates`, dan RSS (`/feed`, `/rss.xml`). Per post: `{titel, datum, samenvatting (≤150), afbeelding}`. Max 3.
- **woningaanbod[]** — alleen `sector="makelaardij"`. Paths: `/aanbod`, `/woningen`, `/te-koop`, `/koopwoningen`. Per item: `{foto, prijs, status, plaats}`. Max 6.
- **team[]** — paths: `/team`, `/over-ons`, `/over`, `/medewerkers`, `/mensen`. Portrait-ratio filter > 0.6. Logo/favicon/placeholder-srcs uitgesloten. Per persoon: `{foto, naam, functie}`. Max 6.

## Notion-update na deploy
`update_notion_after_demo(notion_page_id, demo_url)`:
- PATCH `pages/{id}` op de Notion Leads DB
- Velden:
  - **Demo-link** → url: `demo_url`
  - **Fase** → select: `Demo build`
  - **Demo Approved** → select: `❌ Niet goedgekeurd`
- Wordt aangeroepen direct na `verify_deploy() == True`
- Skip + log waarschuwing als `notion_page_id` leeg of None is, of als
  `NOTION_TOKEN` ontbreekt in `.env`

## Template variabelen overzicht
(Historisch: dit beschrijft de verwijderde makelaardij-templates.) Onderstaande
placeholders worden ingevuld door de bijbehorende `build_*` functies.

| Placeholder | Builder / bron | Aanwezig in a | Aanwezig in b |
|---|---|---:|---:|
| `{{PRIMAIRE_KLEUR}}` | `data["primaire_kleur"]` direct in `:root --primary` | ✅ | ✅ (×2: `--blue` én `--primary`) |
| `{{LOGO_URL}}` | `data["logo_url"]` in `<img src=…>` in nav | ✅ | ✅ |
| `{{TAGLINE}}` | `data["tagline"]` | ✅ (nieuwe `<p class="hero-tagline">`) | ✅ (vervangt `.hero-sub` inhoud) |
| `{{NAV_ITEMS}}` | `build_nav_html(data["nav_items"])` | ✅ (×2: desktop + mobile) | ✅ (×2) |
| `{{WONINGAANBOD_HTML}}` | `build_woningaanbod_html(data["woningaanbod"], data["primaire_kleur"])` | ✅ | ✅ |
| `{{TEAM_HTML}}` | `build_team_html(data["team"])` | ✅ | ✅ |
| `{{BLOG_HTML}}` | `build_blog_html(data["blog_posts"])` | ✅ | — (template-b heeft geen blog-sectie) |

Lege lijst → builder retourneert `""` → sectie verdwijnt na substitutie. Geen
broken `<img>` tags: builders laten `src` weg als URL leeg is.

## Bestanden
- **Script:** `generate_demo.py`
- **Templates:** `templates/Roofer/dakdekkers-{a,b,c}.html` (+ gedeelde assets in `templates/_b-shared/`)
- **Data (per lead):** `data/<slug>/data.json` (in git)
- **Demo output:** `public/demo/<slug>/index.html` (nog handmatig te renderen)

## Vereiste env-variabelen (.env)
```
NOTION_TOKEN=secret_…
```
Optioneel: `NOTION_API_KEY` werkt als fallback voor `NOTION_TOKEN`.

## Sectoren met klare templates
- `dakdekkers` ✅ (`templates/Roofer/dakdekkers-{a,b,c}.html`; c = default,
  b = "helder", a = uitgefaseerd)
- `makelaardij` — templates verwijderd; sector rendert niet meer.

Andere sectoren worden later bepaald en toegevoegd.
