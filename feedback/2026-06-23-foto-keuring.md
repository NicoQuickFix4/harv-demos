# Foto-keuring feedback — 2026-06-23 (Nicolas)

Bron: audit-run van 12 sites (`audit_images.py 12`), beoordeeld door Nicolas in de
localhost-audit. Doel: de demo-generator slimmer maken in fotoselectie + plaatsing.
Foto-ID = `site-fotonummer` (zoals in de audit). Notatie `6-02` == `6,02` == dezelfde foto.

**Legenda van Nicolas:**
- "mag gecombineerd worden met …" = waarschijnlijk met één van de huidige C-back-ups (reserve-pool).
- Geen commentaar op een nummer = waarschijnlijk akkoord met het oordeel van de generator.

---

## Per-site oordeel

### #1 — Dakhelden BV Middelharnis · http://www.dakheldenbv-middelharnis.nl/
- **AI-GENERATED → onbruikbaar, afkeuren:** `1-13`, `1-14`, `1-15`, `1-16`, `1-18`.
- Generator koos C-hero = `1-04` + `1-18`. **`1-18` mag niet** (AI-generated).
- **Correctie C-hero: `1-04` + `1-12`.** Reden: het zijn twee verschillende foto's
  ondanks dat er vrijwel hetzelfde gebeurt → prima als paar. **Voorwaarde:** bij
  `1-12` moet het HELE logo zichtbaar zijn (onderwerp/logo niet wegvallen na crop).

### #2 — Dakdekker Sneek · https://dakdekkersneek.net/
- **`2-01` + `2-02` mogen samen op de C-hero** — ze verschillen qua beeld genoeg.
  (Generator koos alleen `2-01` als hero, `2-02` als dienst.)

### #3 — GV Dakwerken · https://www.gvdakwerken.nl/
- Generator keurde ALLES af (0 goedgekeurd) → **te streng**.
- **`3-04` is een mooie foto en kan op de C-hero**, maar moet gecombineerd worden
  met een foto waarop iemand wérkt.

### #4 — KampDak B.V. · https://kampdak.nl/
- **Goede calls** (akkoord).

### #5 — KEDA-Dakwerken · http://keda-dakwerken.nl/
- **`5-08` mag op de hero** — goed beeld van iemand aan het werk — en mag
  gecombineerd worden met de al geselecteerde (`5-12`).

### #6 — Flexdakwerk · https://www.flexdakwerk.nl/
- **`6-02` mag ook op de hero.** (Generator had geen C-hero voor deze site.)

### #7 — Delft Dakdekker · (geen commentaar → akkoord)
### #8 — Dak advies groep · (geen commentaar → akkoord)

### #9 — NH Dak & Lekkages · https://www.nhdakenlekkages.nl/
- **`9-13` mag op de hero**, i.c.m. ook een dak met pannen erbij.

### #10 — Dakdekker Eindhoven · (geen commentaar → akkoord)
### #11 — Uniek Dakdekkers · (geen commentaar → akkoord)

### #12 — Total Roof Care · http://www.totalroofcare.nl/
- **Goeie call** (`12-09`), maar mag gecombineerd worden met een persoon uit de reserve.

---

## Generaliseerbare lessen (input voor de selectie-logica)

1. **AI-GENERATED foto's zijn onbruikbaar → detecteren en afkeuren.** Meerdere
   Dakhelden-foto's waren AI-gegenereerd; één werd zelfs als hero gekozen. De
   keuring heeft hier nu GEEN detectie voor. Toevoegen: visuele tell-tales
   (uncanny textures, onmogelijke geometrie, "te perfecte" render-look) als
   afkeur-categorie, plus bestandsnaam-hints (`chatgpt`, `midjourney`, `dall-e`,
   `-ai`, `generated`). NB: ook Loodgieter Dordrecht had `ChatGPT_Image_…`-bestanden.

2. **Hero-PAAR moet COMPLEMENTAIR zijn, niet redundant.** Ideaal C-hero = één mooi
   resultaat/dak/detail + één persoon aan het werk. Voorbeelden: `3-04` (dak) vraagt
   een werkende-persoon-partner; `9-13` vraagt een dak-met-pannen erbij; `12-09` goed
   maar combineer met een persoon uit de reserve. De huidige logica pakt top-2
   hero_safe op score + hash-diversiteit — die borgt GEEN complementaire inhoud.

3. **Diversiteit-nuance:** twee foto's van DEZELFDE handeling maar visueel
   onderscheidend zijn prima als paar (`1-04`+`1-12`, `2-01`+`2-02`). De
   hash-diversiteit (`_IMG_HERO_SIM_DIST`) klopt ongeveer, maar "ander beeld"
   weegt zwaarder dan "andere pixels" — reject geen goed complementair paar puur
   omdat de handeling lijkt.

4. **Hero-RECALL is te laag (keuring te streng).** `3-04`, `5-08`, `6-02`, `9-13`
   waren goede hero-kandidaten die het systeem miste/afkeurde; GV Dakwerken kreeg
   0 goedgekeurd terwijl `3-04` duidelijk bruikbaar is. Accept-/hero-drempels of de
   categorie-poort keuren echte, bruikbare werkfoto's te vaak af.

5. **Onderwerp/logo zichtbaar na crop** (`1-12`: heel logo zichtbaar) — bevestigt de
   bestaande hero_safe-regel (onderwerp mag niet achter titel/formulier wegvallen).

6. **Totale bruikbaarheid over templates.** Een foto die bij C op de hero kan, hoort
   bij B (géén hero) meteen heel hoog op de pagina te staan. "Hero-waardig" = "beste
   overall" → bovenaan gebruiken, ongeacht template. We meten dus totale
   bruikbaarheid, niet alleen de hero-slot.
