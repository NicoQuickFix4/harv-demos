#!/usr/bin/env python3
"""audit_images.py — READ-ONLY visuele audit van de beeld-keuring.

DOEL (onthouden): dit is gereedschap om de demo-generator SLIMMER te maken in
het selecteren van foto's (en ze daarna goed in de juiste template-slots te
plaatsen). Puur read-only: het verandert de selectie-logica niet, het maakt
zichtbaar wat de generator doet zodat we de regels/drempels kunnen bijstellen.

Per foto toont de audit:
  • een ZICHTBAAR ID (site-fotonummer) zodat je kunt zeggen "foto 3-07 had ik
    in slot X gewild".
  • score + categorie + de korte model-reden.
  • een DUIDELIJKE afwijs-reden (pre-filter: exact waarom; keuring: categorie/
    score/drempel die niet gehaald is).
  • waar de foto zou LANDEN per template: C (split-hero) én B (géén hero — die
    foto's schuiven door naar dienst/why).

ALLE foto's worden beoordeeld: de 14-cap van de generator-pre-filter is voor de
audit UIT, zodat ook foto's voorbij #14 een oordeel krijgen.

Gebruik:
    python3 audit_images.py                 # 12 random dakdekker-leads
    python3 audit_images.py 15              # 15 leads
    python3 audit_images.py 12 https://site-a.nl  https://site-b.nl   # forceer sites
    python3 audit_images.py 8 --no-serve
"""
from __future__ import annotations

import html as _html
import os
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEADS_DB = HERE.parent / "harv-scraper" / "leads.db"
AUDIT_DIR = HERE / "audit"
SERVE_PORT = 8799

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(HERE))
import generate_demo as gd  # noqa: E402

# Generator-drempels 1-op-1 hergebruiken zodat de audit niet uit de pas loopt.
ACCEPT_CATS = gd._IMG_ACCEPT_CATS
ACCEPT_MIN = gd._IMG_ACCEPT_MIN
HERO_MIN = gd._IMG_HERO_MIN
HERO_SIM = gd._IMG_HERO_SIM_DIST

FALLBACK_SERVICES = ["Dakbedekking", "Dakreparatie", "Dakonderhoud", "Daklekkage"]


# ── leads ────────────────────────────────────────────────────────────────────
def pick_leads(n: int) -> list[dict]:
    con = sqlite3.connect(str(LEADS_DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, bedrijfsnaam, website, stad, logo_url
             FROM leads
            WHERE sector = 'dakdekkers' AND has_website = 1
              AND website IS NOT NULL AND website != ''
            ORDER BY RANDOM() LIMIT ?""",
        (n * 5,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def lead_for_website(url: str) -> dict:
    con = sqlite3.connect(str(LEADS_DB))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, bedrijfsnaam, website, stad, logo_url FROM leads WHERE website = ? LIMIT 1",
        (url,),
    ).fetchone()
    con.close()
    if row:
        return dict(row)
    return {"id": "", "bedrijfsnaam": url, "website": url, "stad": "", "logo_url": ""}


def service_names_from_bundle(bundle: dict) -> list[str]:
    names = [c.get("title", "").strip() for c in (bundle.get("service_cards") or [])]
    names = [x for x in names if x][:6]
    return names or FALLBACK_SERVICES


# ── pre-filter MET reden, ZONDER de 14-cap (audit ziet álle foto's) ───────────
def prefilter_analyze(pool: list[dict], logo_url: str) -> list[dict]:
    """Replica van gd._filter_image_pool, maar zonder cap en met per-foto reden,
    zodat we exact zien waarom een foto wel/niet door de pre-filter komt."""
    logo_l = (logo_url or "").strip().lower()
    hashes: list[int] = []
    out: list[dict] = []
    for it in pool:  # GEEN [:14] cap — alles beoordelen
        url = it.get("url") or ""
        rec = {"url": url, "alt": it.get("alt") or "", "kept": False,
               "reason": "", "b64": None, "hash": None, "w": 0, "h": 0}
        if logo_l and url.lower() == logo_l:
            rec["reason"] = "dit is het logo (overgeslagen)"
            out.append(rec); continue
        meta = gd._image_meta(url, with_preview=True)
        if meta is None:
            rec.update(kept=True, reason="niet te downloaden — behouden (kan niet gekeurd)")
            out.append(rec); continue
        w, h = meta["w"], meta["h"]
        ratio = w / max(1.0, float(h))
        rec["w"], rec["h"] = w, h
        if w < 350 or h < 230:
            rec["reason"] = f"te klein ({w}×{h}, min 350×230) → badge/icoon/thumbnail"
        elif ratio > 2.6:
            rec["reason"] = f"te breed ({w}×{h}, ratio {ratio:.1f} > 2.6) → banner/wordmark"
        elif meta["alpha"] > 0.02:
            rec["reason"] = f"transparant ({meta['alpha'] * 100:.0f}% > 2%) → logo/badge/uitsnede"
        elif any(gd._hash_dist(meta["hash"], hsh) <= 6 for hsh in hashes):
            rec["reason"] = "bijna-duplicaat van een eerdere foto (aHash ≤ 6)"
        else:
            hashes.append(meta["hash"])
            rec.update(kept=True, reason="door de pre-filter", b64=meta.get("b64"),
                       hash=meta["hash"])
        out.append(rec)
    return out


# ── keuring (scores) voor ALLE doorgekomen foto's, gebatcht tegen truncatie ───
def score_all(items: list[dict], services: list[str], company: str,
              logo_url: str, slug: str, lead_id: str, chunk: int = 10):
    """Scoor elke doorgekomen foto via de ECHTE keuring (gd.ai_select_images),
    in batches van ≤chunk zodat de JSON-output nooit afkapt. De per-batch
    hero/dienst/why-keuze negeren we; de plaatsing rekenen we hieronder zelf
    per template uit over de volledige set."""
    details: dict[str, dict] = {}
    logo_name = None
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        sel = gd.ai_select_images(
            part, services, company,
            logo_url=(logo_url if i == 0 else ""),  # logo maar 1× meesturen
            slug=slug, lead_id=lead_id,
        ) or {}
        for d in (sel.get("details") or []):
            details[d["url"]] = d
        if logo_name is None and "logo_contains_name" in sel:
            logo_name = sel["logo_contains_name"]
    return details, logo_name


def _landscape(d: dict) -> bool:
    w, h = d.get("w", 0), d.get("h", 0)
    return (not (w and h)) or w >= h * 1.15


def placement(details_list: list[dict], services: list[str], *, with_hero: bool) -> dict:
    """Bereken per goedgekeurde foto in welk slot die landt, voor één template.
    with_hero=True → Template C (split-hero, 2 foto's). with_hero=False →
    Template B (GEEN hero-foto: die foto's schuiven door naar dienst/why)."""
    accepted = [d for d in details_list
                if d.get("cat") in ACCEPT_CATS and d.get("score", 0) >= ACCEPT_MIN
                and d.get("text") != "heavy"]
    accepted.sort(key=lambda d: d.get("score", 0), reverse=True)
    used: set = set()
    slot: dict[str, str] = {}

    if with_hero:
        hc = [d for d in accepted if d.get("hero_safe")
              and d.get("score", 0) >= HERO_MIN and _landscape(d)]

        def too_sim(a, b):
            ha, hb = a.get("_hash"), b.get("_hash")
            return ha is not None and hb is not None and gd._hash_dist(ha, hb) <= HERO_SIM
        picks: list[dict] = []
        for d in hc:
            if len(picks) >= 2:
                break
            if any(too_sim(d, h) for h in picks):
                continue
            picks.append(d)
        for i, d in enumerate(picks):
            slot[d["url"]] = f"HERO {i + 1}"
            used.add(d["url"])

    for name in services[:6]:
        for d in accepted:
            if d["url"] in used:
                continue
            if (d.get("service") or "").strip().lower() == name.strip().lower():
                slot[d["url"]] = f"DIENST · {name}"
                used.add(d["url"])
                break
    for d in [x for x in accepted if x["url"] not in used][:3]:
        slot[d["url"]] = "WHY"
        used.add(d["url"])
    for d in accepted:
        slot.setdefault(d["url"], "reserve")
    return slot


def reject_reason(d: dict) -> str:
    cat, score, text = d.get("cat"), d.get("score", 0), d.get("text")
    if cat not in ACCEPT_CATS:
        return f"categorie '{cat}' → deze categorie wordt NOOIT geplaatst"
    if score < ACCEPT_MIN:
        return f"score {score} < {ACCEPT_MIN} → kwaliteitsdrempel niet gehaald"
    if text == "heavy":
        return "te veel tekst/overlay in beeld (text=heavy)"
    return "goedgekeurd, maar geen slot meer vrij"


def _slot_cls(label: str) -> str:
    if label.startswith("HERO"):
        return "hero"
    if label.startswith("DIENST"):
        return "dienst"
    if label == "WHY":
        return "why"
    if label == "reserve":
        return "reserve"
    return "afgekeurd"


# ── één site auditen ─────────────────────────────────────────────────────────
def audit_one(lead: dict, site_no: int) -> dict:
    website = (lead.get("website") or "").strip()
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    logo_url = (lead.get("logo_url") or "").strip()
    company = lead.get("bedrijfsnaam") or website

    bundle = gd.fetch_site_bundle(website)
    image_pool = list(bundle.get("image_pool") or [])
    services = service_names_from_bundle(bundle)

    pf = prefilter_analyze(image_pool, logo_url)
    kept = [r for r in pf if r["kept"] and r["b64"]]
    items = [{"url": r["url"], "_b64": r["b64"], "w": r["w"], "h": r["h"],
              "_hash": r["hash"], "alt": r["alt"]} for r in kept]

    details, logo_name = ({}, None)
    if items:
        details, logo_name = score_all(items, services, company, logo_url,
                                       lead.get("id") or "", lead.get("id") or "")
    det_list = list(details.values())
    place_c = placement(det_list, services, with_hero=True)
    place_b = placement(det_list, services, with_hero=False)

    photos = []
    for idx, r in enumerate(pf, 1):
        pid = f"{site_no}-{idx:02d}"
        url = r["url"]
        det = details.get(url)
        if not r["kept"]:
            cls, c_lbl, b_lbl = "prefilter", "—", "—"
            reason = "PRE-FILTER eruit: " + r["reason"]
        elif det is None:
            cls, c_lbl, b_lbl = "prefilter", "—", "—"
            reason = "niet beoordeeld door de keuring (geen preview/timeout)"
        elif url in place_c or url in place_b:
            c_lbl = place_c.get(url, "—")
            b_lbl = place_b.get(url, "—")
            cls = _slot_cls(c_lbl if c_lbl != "—" else b_lbl)
            reason = det.get("reason") or ""
        else:
            cls, c_lbl, b_lbl = "afgekeurd", "afgekeurd", "afgekeurd"
            reason = reject_reason(det)
        photos.append({
            "id": pid, "url": url, "alt": r["alt"], "b64": r["b64"],
            "kept": r["kept"],
            "score": (det or {}).get("score"), "cat": (det or {}).get("cat"),
            "text": (det or {}).get("text"), "hero_safe": (det or {}).get("hero_safe"),
            "service": (det or {}).get("service"),
            "w": r["w"], "h": r["h"],
            "c": c_lbl, "b": b_lbl, "cls": cls, "reason": reason,
            "model_reason": (det or {}).get("reason") or "",
        })

    hero_ids = [p for p in photos if str(p["c"]).startswith("HERO")]
    approved = sum(1 for p in photos if p["cls"] in ("hero", "dienst", "why", "reserve"))
    return {
        "lead": lead, "website": website, "company": company, "services": services,
        "site_no": site_no, "n_scraped": len(image_pool),
        "n_kept": len(kept), "n_approved": approved,
        "photos": photos, "hero_ids": hero_ids, "logo_name": logo_name,
    }


# ── HTML ─────────────────────────────────────────────────────────────────────
def esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def img_src(p: dict) -> str:
    if p.get("b64"):
        return f"data:image/jpeg;base64,{p['b64']}"
    return esc(p.get("url") or "")


def badge_html(label: str, kind: str) -> str:
    cls = "none" if label == "—" else _slot_cls(label)
    return f'<span class="pl pl-{cls}">{kind}: {esc(label)}</span>'


def render_thumb(p: dict, big: bool = False) -> str:
    score = p.get("score")
    sc = f'<span class="sc">{score}</span>' if score is not None else ""
    cat = f'<span class="cat">{esc(p.get("cat"))}</span>' if p.get("cat") else ""
    idb = f'<span class="idb">{esc(p["id"])}</span>'
    metab = []
    if p.get("text") and p["text"] != "none":
        metab.append(f"tekst:{esc(p['text'])}")
    if p.get("hero_safe"):
        metab.append("hero_safe")
    if p.get("service"):
        metab.append(f"→{esc(p['service'])}")
    if p.get("w") and p.get("h"):
        metab.append(f"{p['w']}×{p['h']}")
    meta = " · ".join(metab)
    cls = "card big" if big else "card"
    alt = esc(p.get("alt") or "")
    return f"""
      <figure class="{cls} st-{p['cls']}">
        <a href="{esc(p.get('url'))}" target="_blank" rel="noopener">
          <img loading="lazy" src="{img_src(p)}" alt="{alt}">
          {idb}{sc}{cat}
        </a>
        <figcaption>
          <div class="pls">{badge_html(p['c'], 'C')}{badge_html(p['b'], 'B')}</div>
          <span class="reason">{esc(p['reason'])}</span>
          {f'<span class="mr">model: {esc(p["model_reason"])}</span>' if p.get('model_reason') and p['model_reason'] != p['reason'] else ''}
          {f'<span class="meta">{meta}</span>' if meta else ''}
          {f'<span class="alt">alt: {alt}</span>' if alt else ''}
        </figcaption>
      </figure>"""


def render_site(rec: dict) -> str:
    lead = rec["lead"]
    heroes = rec["hero_ids"]
    if heroes:
        hero_html = "".join(render_thumb(p, big=True) for p in heroes)
        b_note = " / ".join(f'foto {esc(p["id"])} → B: {esc(p["b"])}' for p in heroes)
        hero_block = f"""
    <h3 class="lbl">★ Template C hero-paar — in Template B (géén hero) schuiven deze door</h3>
    <div class="grid best">{hero_html}</div>
    <p class="shift">In B worden dit: {b_note}</p>"""
    else:
        hero_block = '<h3 class="lbl">★ Geen foto haalde de C-hero (hero_safe + score ≥ 70)</h3>'
    all_html = "".join(render_thumb(p) for p in rec["photos"]) or \
        '<p class="none">— geen foto\'s gescrapet —</p>'
    logo_note = ""
    if rec.get("logo_name") is not None:
        logo_note = f' · logo bevat naam: <b>{esc(rec["logo_name"])}</b>'
    return f"""
  <section class="site">
    <header>
      <h2>#{rec['site_no']} · {esc(rec['company'])}</h2>
      <div class="sub">
        <a href="{esc(rec['website'])}" target="_blank" rel="noopener">{esc(rec['website'])}</a>
        <span class="loc">{esc(lead.get('stad') or '')}</span>
      </div>
      <div class="stats">
        gescrapet: <b>{rec['n_scraped']}</b> ·
        door pre-filter: <b>{rec['n_kept']}</b> ·
        goedgekeurd: <b>{rec['n_approved']}</b> ·
        ID = <code>{rec['site_no']}-NN</code>{logo_note}
      </div>
      <div class="svc">diensten aangeboden aan keuring: {esc(', '.join(rec['services']))}</div>
    </header>
    {hero_block}
    <h3 class="lbl">Alle gescrapete foto's ({rec['n_scraped']}) — elk met ID, C- en B-plaatsing</h3>
    <div class="grid">{all_html}</div>
  </section>"""


CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--ink:#e8eaed;--mut:#9aa0a8;--line:#2a2e37}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.top{padding:24px 28px;border-bottom:1px solid var(--line)}
.top h1{margin:0 0 4px;font-size:20px}
.top p{margin:2px 0;color:var(--mut)}
code{background:#000;padding:1px 5px;border-radius:4px;color:#cfd3da}
.site{padding:24px 28px;border-bottom:8px solid #000}
.site header h2{margin:0;font-size:22px}
.sub{margin:2px 0 8px}
.sub a{color:#77fb38;text-decoration:none}.sub a:hover{text-decoration:underline}
.sub .loc{color:var(--mut);margin-left:10px}
.stats,.svc{color:var(--mut);font-size:13px}
.svc{margin-top:4px;font-style:italic}
.shift{color:#cdd2da;font-size:13px;margin:8px 0 0}
.lbl{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:22px 0 10px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.grid.best{grid-template-columns:repeat(auto-fill,minmax(290px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:0;border-left:5px solid var(--line)}
.card a{display:block;position:relative;line-height:0}
.card img{width:100%;height:155px;object-fit:cover;background:#000}
.card.big img{height:235px}
.idb{position:absolute;top:6px;left:6px;background:#77fb38;color:#0f1115;font-weight:800;
  padding:2px 8px;border-radius:6px;font-size:14px;letter-spacing:.02em}
.sc{position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,.8);color:#fff;font-weight:700;padding:2px 7px;border-radius:6px;font-size:13px}
.cat{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.72);color:#cfd3da;padding:2px 7px;border-radius:6px;font-size:11px}
figcaption{padding:8px 10px;display:flex;flex-direction:column;gap:4px}
.pls{display:flex;gap:5px;flex-wrap:wrap}
.pl{font-size:11px;font-weight:700;padding:2px 6px;border-radius:5px;border:1px solid var(--line)}
.pl-hero{background:#163d12;color:#77fb38}.pl-dienst{background:#10283f;color:#7cc4ff}
.pl-why{background:#0e3330;color:#5eead4}.pl-reserve{background:#2a3210;color:#c4e335}
.pl-afgekeurd{background:#3a1414;color:#fca5a5}.pl-none{background:#23262d;color:#8b919b}
.reason{color:#dfe3ea;font-size:12px}
.mr{color:#8b919b;font-size:11px;font-style:italic}
.meta{color:var(--mut);font-size:11px}
.alt{color:#6b7280;font-size:11px;font-style:italic}
.none{color:var(--mut)}
.st-hero{border-left-color:#77fb38}.st-dienst{border-left-color:#4aa8ff}
.st-why{border-left-color:#2dd4bf}.st-reserve{border-left-color:#a3e635}
.st-afgekeurd{border-left-color:#ef4444}
.st-prefilter{border-left-color:#6b7280;opacity:.74}
"""


def render_page(records: list[dict], skipped: list[tuple[str, str]]) -> str:
    sites = "".join(render_site(r) for r in records)
    skip_html = ""
    if skipped:
        items = "".join(f"<li>{esc(n)} — {esc(e)}</li>" for n, e in skipped)
        skip_html = f'<div class="top"><p><b>Overgeslagen ({len(skipped)}):</b></p><ul>{items}</ul></div>'
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beeld-audit — dakdekkers</title><style>{CSS}</style></head><body>
<div class="top"><h1>Beeld-audit · per foto een ID, reden en template-plaatsing</h1>
<p>Doel: de generator slimmer maken in fotoselectie. Elke foto heeft een <b>ID</b>
(site-fotonummer) — noem het ID om te zeggen welke je waar had gewild.</p>
<p>Per foto: <b>C</b> = plaats in Template C (split-hero) · <b>B</b> = plaats in
Template B (géén hero → hero-foto's schuiven door). Klik een foto voor de volledige afbeelding.</p>
<p>Kleur/label: <span class="pl pl-hero">HERO</span> <span class="pl pl-dienst">DIENST</span>
<span class="pl pl-why">WHY</span> <span class="pl pl-reserve">reserve</span>
<span class="pl pl-afgekeurd">afgekeurd</span> · grijs = pre-filter eruit.
Álle gescrapete foto's worden getoond én beoordeeld (geen 14-cap).</p></div>
{skip_html}{sites}
</body></html>"""


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    n = 12
    serve = True
    forced_urls: list[str] = []
    for a in sys.argv[1:]:
        if a == "--no-serve":
            serve = False
        elif a.startswith("http"):
            forced_urls.append(a)
        elif a.isdigit():
            n = int(a)
    n = max(n, len(forced_urls))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠  ANTHROPIC_API_KEY ontbreekt — keuring geeft lege resultaten.")

    forced_leads = [lead_for_website(u) for u in forced_urls]
    candidates = forced_leads + pick_leads(n)
    forced_set = {u for u in forced_urls}
    print(f"▶ doel: {n} sites (ALLE foto's, geen cap) — {len(forced_leads)} geforceerd "
          f"+ random aanvulling, uit {LEADS_DB}")
    records, skipped = [], []
    site_no = 0
    for lead in candidates:
        if len(records) >= n:
            break
        name = lead.get("bedrijfsnaam") or lead.get("website")
        is_forced = lead.get("website") in forced_set
        site_no += 1
        print(f"\n[{len(records) + 1}/{n}] {name} — {lead.get('website')}"
              + ("  (geforceerd)" if is_forced else ""))
        try:
            rec = audit_one(lead, site_no)
            if rec["n_scraped"] == 0 and not is_forced:
                skipped.append((name, "0 foto's gescrapet (diepe URL of fetch mislukt)"))
                print("    ⤼ 0 foto's — overgeslagen, volgende kandidaat")
                site_no -= 1
                continue
            records.append(rec)
            heroes = ", ".join(p["id"] for p in rec["hero_ids"]) or "—"
            print(f"    ✓ gescrapet={rec['n_scraped']} pre-filter={rec['n_kept']} "
                  f"goedgekeurd={rec['n_approved']} · C-hero={heroes}")
        except Exception as exc:  # noqa: BLE001
            skipped.append((name, str(exc)))
            print(f"    ✗ overgeslagen: {exc}")
            traceback.print_exc()
            site_no -= 1

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIT_DIR / "index.html"
    out.write_text(render_page(records, skipped), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"📄 audit geschreven: {out}")
    print("\nSamenvatting per site (ID-reeks · gescrapet → goedgekeurd · C-hero):")
    for r in records:
        heroes = ", ".join(p["id"] for p in r["hero_ids"]) or "geen"
        print(f"  • #{r['site_no']} {r['company']}: foto's {r['site_no']}-01..{r['site_no']}-"
              f"{len(r['photos']):02d} · gescrapet={r['n_scraped']} "
              f"goedgekeurd={r['n_approved']} · C-hero: {heroes}")
    if skipped:
        print(f"\n  overgeslagen: {len(skipped)}")
        for nme, e in skipped:
            print(f"    - {nme}: {e}")

    if serve:
        url = f"http://localhost:{SERVE_PORT}/index.html"
        print(f"\n🌐 server: {url}  (cwd={AUDIT_DIR}) — stop met Ctrl-C")
        try:
            subprocess.run([sys.executable, "-m", "http.server", str(SERVE_PORT)],
                           cwd=str(AUDIT_DIR))
        except KeyboardInterrupt:
            print("\nserver gestopt.")
    else:
        print(f"\nServe overgeslagen. Server draait al? Anders: "
              f"cd {AUDIT_DIR} && python3 -m http.server {SERVE_PORT}")


if __name__ == "__main__":
    main()
