# Harv Demo Kit — regels & integratie-contract

De slimme laag van de demo generator zit in `harv_kit.py`. Dit document legt vast
welke regels gelden en hoe je elke sector-template (bijv. de dakdekkers-template)
de AI-content, de booking-widget en de "presented by Harv" laat overnemen.

Kort: jouw template hoeft alleen een paar `{{...}}`-placeholders te bevatten. De
render-stap (`generate_demo.render_demo`) vult ze automatisch.

---

## 1. Het placeholder-contract

Zet deze placeholders in je template. Alles wat je niet gebruikt, laat je weg.

| Placeholder | Wat erin komt | Verplicht |
|---|---|---|
| `{{HARV_GUARD_CSS}}` | Overlap-guard CSS. Zet dit één keer net vóór `</head>`. | Ja |
| `{{HARV_BOOKING_WIDGET}}` | De volledige multi-step widget (form, bevestiging, Cal.com-embed). Eigen scoped CSS/JS, plak het waar de contact/offerte-sectie hoort. | Ja |
| `{{HARV_PRESENTED_BY}}` | Klikbare "Presented by Harv Agency" die naar de getrackte Cal.com opent. Zet het in een `position:fixed`-wrapper voor het floating effect. | Ja |
| `{{HARV_LOCAL_HEADLINE}}` | Gelokaliseerde kop, bijv. "Jouw dakdekker in Utrecht". Geef het element de class `harv-fit-head`. | Aanbevolen |
| `{{HARV_LOCAL_INTRO}}` | Eén intro-zin op basis van echte gescrapete feiten. | Aanbevolen |
| `{{HARV_DISPLAY_NAME}}` | Veilig ingekorte bedrijfsnaam (overlap). Gebruik dit in de nav/header i.p.v. de volle naam, met een `harv-namefit`-span. | Aanbevolen |
| `{{HARV_TEAM_CAPTION}}` | Anonieme team-caption (alleen relevant als je zelf team-markup bouwt). | Optioneel |
| `{{HARV_BOOKING_URL}}` | Kale getrackte Cal.com-link, voor een eigen knop. | Optioneel |

De bestaande placeholders blijven ook werken: `{{BEDRIJFSNAAM}}`, `{{PRIMAIRE_KLEUR}}`,
`{{LOGO_URL}}`, `{{NAV_ITEMS}}`, `{{TAGLINE}}`, `{{TEAM_HTML}}`, `{{BLOG_HTML}}`,
`{{WONINGAANBOD_HTML}}`, `{{TRACKING_PIXEL}}`.

### Overlap-regels (logo + naam)

Het probleem "naam/logo te groot en valt over iets heen" wordt op twee manieren
opgelost, allebei automatisch:

1. **Korte weergavenaam.** De AI levert een ingekorte `display_name` aan (max 22
   tekens, netjes afgekapt). Gebruik `{{HARV_DISPLAY_NAME}}` in de nav/header.
2. **CSS-guards** (uit `{{HARV_GUARD_CSS}}`):
   - `.harv-namefit` knipt te lange tekst af met `...` binnen zijn breedte.
   - `.harv-fit-head` schaalt de kop mee (`clamp`) zodat hij niet uit de hero loopt.
   - `img.harv-logo-safe` houdt het logo binnen veilige hoogte/breedte.

Praktisch in de nav:
```html
<a class="nav-logo">
  <img src="{{LOGO_URL}}" class="harv-logo-safe" onerror="this.style.display='none'">
  <span class="harv-namefit" style="max-width:200px;">{{HARV_DISPLAY_NAME}}</span>
</a>
```

---

## 2. Wat de AI bepaalt (en de regels eromheen)

`harv_kit.ai_demo_content()` doet één Claude Haiku-call per lead en beslist:

* **Gelokaliseerde kop**: "Jouw {sector} in {plaats}". Plaats komt uit (in volgorde)
  expliciete stad, het gescrapete adres, of de regio.
* **Intro-zin**: kort, lokaal, gebaseerd op echte feiten van de site. Verzint niets.
* **Weergavenaam**: ingekort als de echte naam te lang is (overlap).
* **Team-strategie**:
  - Zijn er teamleden met namen, dan tonen we die namen.
  - Zijn er wel foto's maar geen namen, dan krijgt elke kaart een anonieme caption
    (bijv. "Het team dat vandaag voor je klaarstaat"), nooit een lege naam.
* **Welke secties tonen**: een sectie wordt alleen getoond als er echt data voor is.
  De AI mag een sectie nooit "aanzetten" als de scrape niets opleverde.

Harde regels die in code zijn afgedwongen:

* **Nooit een gedachtestreepje** in de teksten. Wordt automatisch vervangen door een komma.
* **Geen verzonnen feiten** (geen valse jaartallen of cijfers). De prompt stuurt hierop,
  en lege secties worden hard uitgezet op basis van de echte data.

### AI is verplicht

Beleid: er gaat **nooit** een demo zonder AI-content de deur uit. `ai_demo_content`
en `render_demo` draaien met `require=True` als standaard:

* De call wordt tot 3 keer geprobeerd (met backoff) bij een tijdelijke storing.
* Lukt het dan nog niet, of ontbreekt de API-key, dan volgt `AIContentError` en
  wordt de demo NIET geschreven. De lead blijft dan staan voor een nieuwe poging,
  in plaats van een mindere demo te versturen.
* De deterministische fallback bestaat nog wel, maar wordt alleen gebruikt bij
  `require=False` (previews en tests).

---

## 3. De booking-widget

`{{HARV_BOOKING_WIDGET}}` is volledig zelfstandig (eigen `.harv-*` CSS en JS). Flow:

1. **Stap 1 — gegevens**: naam, e-mail, telefoon.
2. **Stap 2 — probleem**: sector-specifieke keuzes (chips) + vrije toelichting.
   Voor dakdekkers bijvoorbeeld: lekkage, nieuw dak, dakgoot, isolatie, onderhoud.
3. **Stap 3 — bevestiging**: "[bedrijf] neemt binnen 24 uur contact met je op",
   met daaronder een kleine CTA ("Werkt dit goed voor je? Laten we even verder praten").
4. **Cal.com**: bij klik op die CTA verschijnt de Cal.com-embed (zelfde event als
   harvagency.com/boek), met naam/e-mail vooraf ingevuld en de tracking meegestuurd.

Sector-specifieke chips en de widget-titel staan in `harv_kit.SECTOR_PROBLEMS` en
`_default_widget_title()`. Een nieuwe sector toevoegen is daar één regel.

### Opslag van wat wordt ingevuld

Elke submissie wordt opgeslagen. De widget post naar `POST /demo-lead` op de
Flask-backend (`api_server.py`):

* Bewaart de submissie altijd in de tabel `demo_form_submissions` (los van of we de
  lead kennen).
* Kennen we de lead, dan zetten we er een flag "Demo-formulier ingevuld" bij en
  voegen we een notitie toe aan de Notion-pagina.
* De pipeline-fase verandert hier bewust niet. Een formulier-invul is nog geen
  geboekte call. De echte boeking komt binnen via de bestaande `POST /calcom-booking`
  webhook (matcht op e-mail, zet de fase op "afspraak").

CORS staat aan zodat de demo (Vercel) cross-origin naar de backend kan posten.

---

## 4. Tracking & herleidbaarheid

Alles is terug te leiden tot de lead:

* De widget draagt `lead_id`, `slug` en `notion_id` mee in de POST.
* De Cal.com-link/embed draagt `notion_id` mee (plus naam/e-mail prefill) zodat
  Make.com de juiste Notion-pagina kan updaten bij een boeking.
* De "presented by Harv" opent dezelfde getrackte Cal.com-link.

---

## 5. Kosten

* AI-content: ongeveer **€0,0012 per lead** gemeten (Claude Haiku, ~700+170 tokens).
* Ruim binnen de harde grens van €0,08 per lead all-in.
* De kosten worden per lead gelogd (zie de `🤖`-regel in de output en `_cost_eur`
  in het resultaat).

---

## 6. Een sector-template aansluiten (stappenplan)

1. Zet `{{HARV_GUARD_CSS}}` net vóór `</head>`.
2. Gebruik `{{HARV_DISPLAY_NAME}}` (met `harv-namefit`) in de nav, en
   `{{HARV_LOCAL_HEADLINE}}` (met `harv-fit-head`) als hero-kop.
3. Vervang je contact/offerte-sectie door `{{HARV_BOOKING_WIDGET}}`.
4. Zet `{{HARV_PRESENTED_BY}}` in een `position:fixed`-wrapper onderaan.
5. Registreer de template in `generate_demo.SECTOR_TEMPLATES`
   (bijv. `"dakdekkers": "dakdekkers-a.html"`).
6. Render: `render_demo(slug=..., sector="dakdekkers", data=..., lead_id=..., notion_id=..., stad=...)`.

Zie `templates/makelaardij-a.html` als werkend voorbeeld van alle vijf stappen.

---

## 7. Omgevingsvariabelen (`harv-demos/.env`)

| Variabele | Waarvoor |
|---|---|
| `ANTHROPIC_API_KEY` | AI-content (verplicht, anders `AIContentError`). |
| `TRACKING_DOMAIN` | Host van de Flask-backend (tracking-pixel + `/demo-lead`). |
| `NOTION_TOKEN` | Notion-updates na deploy. |
| `CALCOM_EVENT` / `CALCOM_BOOKING_BASE` | Het Cal.com-event voor de booking. |

Zonder `ANTHROPIC_API_KEY` in deze map valt de generator niet stil terug op
generieke tekst, maar stopt hij met een duidelijke fout. Zet de key dus altijd.
