#!/usr/bin/env python3
"""Gratis DOM-kwaliteitsgate + deterministische fix-loop voor Harv-demo's.

Geen AI-kosten. Alles draait via headless Chromium (Playwright). Dit is laag 1
van de kwaliteitstrechter (zie ~/Developer/harv-dakdekker/v2/demo-generator/):
objectief, meetbaar, en zelf-fixend voor de simpele dingen.

Wat de gate meet (per viewport 390 en 1280):
  - horizontale overflow van de pagina (scrollWidth > clientWidth)
  - elementen die buiten hun vakje vallen (een te lange regel, een te brede knop)
  - gebroken afbeeldingen (naturalWidth == 0)
  - console-errors
  - achtergebleven placeholder-tekst (Van den Berg, Utrecht, VEBIDAK, ...)

Wat de gate zelf fixt (deterministisch, nul tokens):
  - tekst die niet breekt -> overflow-wrap/word-break/hyphens
  - flex/grid-kinderen die niet kunnen krimpen -> min-width:0
  - een te lange regel die buiten zijn vakje valt -> wrappen + max-width:100%
  - laatste vangnet: horizontale paginascroll afknijpen (en flaggen)

Gebruik:
    from qa_gate import run_gate
    result = run_gate(html)            # html-string in, gefixte html eruit
    if result["ok"]:
        html = result["html"]
    else:
        # niet op te lossen binnen de gate -> door naar laag 2 / mens
        ...

CLI:
    python3 qa_gate.py path/to/index.html
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Viewports waarop elke demo objectief goed moet zijn.
VIEWPORTS: dict[str, tuple[int, int]] = {
    "mobile": (390, 844),
    "desktop": (1280, 900),
}

# Placeholder-tekst uit de basis-template die NOOIT in een echte demo mag blijven
# staan (RULES.md deel 1 / deel 8a). Hoofdletterongevoelig.
#
# Bron van waarheid = RULES.md (deel 8a), ingelezen via rules_loader. De tuple
# hieronder is alleen de FALLBACK als RULES.md (of het blok) ontbreekt, zodat de
# gate nooit zonder leklijst draait.
_FALLBACK_PLACEHOLDER_LEAKS: tuple[str, ...] = (
    "Van den Berg",
    "VEBIDAK",
    "210+",
    "sinds 2003",
    "Dakwerken Van den Berg",
    "Lorem ipsum",
    "voorbeeldbedrijf",
)
# "Utrecht" alleen als lek tellen wanneer het de template-default is; de echte
# klant kan in Utrecht zitten. Daarom apart en standaard uit (de generator weet
# de echte plaats en kan dit aanzetten).
_FALLBACK_PLACEHOLDER_CITY_DEFAULT = "Utrecht"

try:  # RULES.md wint; valt veilig terug op de constanten hierboven.
    import rules_loader as _rules
    PLACEHOLDER_LEAKS: tuple[str, ...] = (
        _rules.get_placeholder_leaks() or _FALLBACK_PLACEHOLDER_LEAKS
    )
    PLACEHOLDER_CITY_DEFAULT: str = (
        _rules.get_placeholder_city_default() or _FALLBACK_PLACEHOLDER_CITY_DEFAULT
    )
except Exception:  # noqa: BLE001 - nooit de gate laten breken op een loader-fout
    PLACEHOLDER_LEAKS = _FALLBACK_PLACEHOLDER_LEAKS
    PLACEHOLDER_CITY_DEFAULT = _FALLBACK_PLACEHOLDER_CITY_DEFAULT

# Generieke reparatie-stylesheet: lost de overgrote meerderheid van
# "net-niet-goed" gevallen op zonder iets aan de inhoud te veranderen.
_REPAIR_CSS_GLOBAL = (
    "/* harv-qa-repair */"
    "*{min-width:0}"
    "h1,h2,h3,h4,p,span,a,li,td,th,div{overflow-wrap:break-word}"
    "h1,h2,h3{hyphens:auto}"
    "img{max-width:100%;height:auto}"
)

_QA_MARKER = "/* harv-qa-repair */"

# JS dat in de pagina draait en een rapport teruggeeft.
_INSPECT_JS = r"""
() => {
  const rep = {overflowX:false, overflowPx:0, elements:[], brokenImages:0, leaks:[], logoClipped:[]};
  const de = document.documentElement;
  rep.overflowPx = Math.max(0, de.scrollWidth - de.clientWidth);
  rep.overflowX = rep.overflowPx > 1;

  // RULES 5: het logo moet VOLLEDIG in beeld zijn — een logo dat groter
  // rendert dan zijn vak (header-balk of footer-chip) wordt afgeknipt of
  // hangt over de content heen. Hard issue.
  for (const img of document.querySelectorAll('.brand-logo-img, .logo-img')) {
    const cs = getComputedStyle(img);
    if (cs.display === 'none' || !img.getClientRects().length) continue;
    const p = img.parentElement;
    if (!p) continue;
    const ir = img.getBoundingClientRect(), pr = p.getBoundingClientRect();
    const clipY = Math.round(ir.height - pr.height);
    const clipX = Math.round(ir.width - pr.width);
    if (clipY > 3 || clipX > 3) {
      rep.logoClipped.push({clipY: clipY, clipX: clipX});
    }
  }

  function selectorFor(el){
    if(!el || el.nodeType!==1) return '';
    if(el.id) return '#'+CSS.escape(el.id);
    const parts=[];
    let node=el, depth=0;
    while(node && node.nodeType===1 && depth<5){
      let part=node.tagName.toLowerCase();
      const cls=(node.className&&typeof node.className==='string')
        ? node.className.trim().split(/\s+/).slice(0,2).map(c=>'.'+CSS.escape(c)).join('') : '';
      part+=cls;
      const par=node.parentElement;
      if(par){
        const sib=Array.from(par.children).filter(c=>c.tagName===node.tagName);
        if(sib.length>1) part+=':nth-of-type('+(sib.indexOf(node)+1)+')';
      }
      parts.unshift(part);
      node=node.parentElement; depth++;
    }
    return parts.join('>');
  }

  // Elementen die horizontaal buiten hun eigen vakje vallen.
  const vw = de.clientWidth;
  const all = document.body ? document.body.querySelectorAll('*') : [];
  for(const el of all){
    const cs = getComputedStyle(el);
    if(cs.display==='none' || cs.visibility==='hidden') continue;
    const selfOverflow = el.scrollWidth - el.clientWidth;
    const r = el.getBoundingClientRect();
    const pastViewport = r.right - vw;
    if(selfOverflow > 2 || pastViewport > 2){
      const txt=(el.textContent||'').trim().slice(0,60);
      rep.elements.push({
        sel: selectorFor(el),
        tag: el.tagName.toLowerCase(),
        selfOverflow: Math.round(selfOverflow),
        pastViewport: Math.round(pastViewport),
        nowrap: cs.whiteSpace==='nowrap' || cs.whiteSpace==='pre',
        text: txt
      });
    }
  }
  // Te veel: cap op de 25 ergste zodat het rapport klein blijft.
  rep.elements.sort((a,b)=>(b.selfOverflow+b.pastViewport)-(a.selfOverflow+a.pastViewport));
  rep.elements = rep.elements.slice(0,25);

  for(const img of document.images){
    if(img.complete && img.naturalWidth===0) rep.brokenImages++;
  }
  return rep;
}
"""


def _read_html(src: str | Path) -> str:
    p = Path(src)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return str(src)


def _find_leaks(html: str, include_city: bool = False) -> list[str]:
    found: list[str] = []
    low = html.lower()
    # Door de generator geverifieerde termen (staan letterlijk op de eigen
    # site van de lead, bv. een echt VEBIDAK-lidmaatschap) zijn geen lek.
    verified: set[str] = set()
    m = re.search(r"<!--\s*harv-verified-terms:\s*([^>]*?)\s*-->", html, re.I)
    if m:
        verified = {t.strip().lower() for t in m.group(1).split("|") if t.strip()}
    for needle in PLACEHOLDER_LEAKS:
        if needle.lower() in low and needle.lower() not in verified:
            found.append(needle)
    if include_city and PLACEHOLDER_CITY_DEFAULT.lower() in low:
        found.append(PLACEHOLDER_CITY_DEFAULT)
    return found


def inspect_html(html: str, viewport: tuple[int, int]) -> dict[str, Any]:
    """Open de html in headless Chromium op één viewport en geef een DOM-rapport.

    Vereist Playwright + chromium. Bij ontbreken: geef een 'skipped'-rapport.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"skipped": "playwright niet geinstalleerd"}

    w, h = viewport
    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": w, "height": h})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        # set_content rendert zonder netwerk-base; relatieve assets laden niet,
        # maar overflow/structuur-checks werken. Absolute (CDN/scrape) assets wel.
        page.set_content(html, wait_until="load", timeout=15000)
        page.wait_for_timeout(250)
        try:
            report = page.evaluate(_INSPECT_JS)
        except Exception as exc:  # noqa: BLE001
            report = {"error": str(exc), "overflowX": False, "elements": [], "brokenImages": 0}
        browser.close()
    report["console_errors"] = console_errors[:20]
    report["viewport"] = f"{w}x{h}"
    return report


def inspect_url(url: str, viewport: tuple[int, int]) -> dict[str, Any]:
    """Zoals inspect_html maar navigeert naar een URL (file:// of http://).

    Hierdoor laden relatieve en root-relatieve assets echt, zodat de
    gebroken-afbeelding- en console-checks kloppen (in tegenstelling tot
    set_content, dat geen asset-base heeft).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"skipped": "playwright niet geinstalleerd"}

    w, h = viewport
    console_errors: list[str] = []
    failed_images: set[str] = set()

    def _on_console(m: Any) -> None:
        if m.type == "error" and "failed to load resource" not in m.text.lower():
            console_errors.append(m.text)

    def _on_response(resp: Any) -> None:
        try:
            if resp.status >= 400 and resp.request.resource_type == "image":
                failed_images.add(resp.url)
        except Exception:  # noqa: BLE001
            pass

    def _on_requestfailed(req: Any) -> None:
        try:
            if req.resource_type == "image":
                failed_images.add(req.url)
        except Exception:  # noqa: BLE001
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": w, "height": h})
        page.on("console", _on_console)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.on("response", _on_response)
        page.on("requestfailed", _on_requestfailed)
        page.goto(url, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(300)
        try:
            report = page.evaluate(_INSPECT_JS)
        except Exception as exc:  # noqa: BLE001
            report = {"error": str(exc), "overflowX": False, "elements": [], "brokenImages": 0}
        browser.close()
    # Netwerk-gebaseerde detectie is accuraat (SVG's met naturalWidth 0 vallen af).
    report["brokenImages"] = len(failed_images)
    report["broken_image_urls"] = sorted(failed_images)[:10]
    report["console_errors"] = console_errors[:20]
    report["viewport"] = f"{w}x{h}"
    return report


class _StaticServer:
    """Mini HTTP-server (achtergrond-thread) geworteld in een map.

    Zo resolven relatieve én root-relatieve (/...) assets exact zoals Vercel ze
    serveert, terwijl we lokaal op het bestand fixen vóór de deploy.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._httpd = None
        self._thread = None
        self.port = 0

    def __enter__(self) -> "_StaticServer":
        import functools
        import http.server
        import socketserver
        import threading

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.root))
        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def url(self, route: str) -> str:
        return f"http://127.0.0.1:{self.port}/{route.lstrip('/')}"

    def __exit__(self, *exc: Any) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def _targeted_repair_css(elements: list[dict[str, Any]]) -> str:
    """Bouw CSS-regels voor de specifieke elementen die buiten hun vakje vallen."""
    rules: list[str] = []
    seen: set[str] = set()
    for el in elements:
        sel = el.get("sel")
        if not sel or sel in seen:
            continue
        seen.add(sel)
        decls = ["max-width:100%", "overflow-wrap:break-word", "word-break:break-word"]
        # Een element dat niet mocht wrappen (nowrap) en daardoor te lang is:
        # sta wrappen toe zodat de regel binnen het vakje blijft.
        if el.get("nowrap"):
            decls.append("white-space:normal")
        rules.append(f"{sel}{{{';'.join(decls)}}}")
    return "".join(rules)


def _inject_css(html: str, css: str) -> str:
    """Voeg een <style> met de reparatie-CSS toe (vlak voor </head> of bovenaan)."""
    block = f"<style>{css}</style>"
    if "</head>" in html:
        return html.replace("</head>", block + "</head>", 1)
    if "<body" in html:
        return re.sub(r"(<body[^>]*>)", r"\1" + block, html, count=1)
    return block + html


def check_html(html: str, *, include_city_leak: bool = False) -> dict[str, Any]:
    """Inspecteer de html op alle viewports en aggregeer tot pass/fail."""
    reports: dict[str, Any] = {}
    issues: list[str] = []
    worst_elements: list[dict[str, Any]] = []

    for name, vp in VIEWPORTS.items():
        rep = inspect_html(html, vp)
        reports[name] = rep
        if rep.get("skipped"):
            continue
        if rep.get("overflowX"):
            issues.append(f"{name}: horizontale overflow ({rep.get('overflowPx')}px)")
        if rep.get("brokenImages"):
            issues.append(f"{name}: {rep['brokenImages']} gebroken afbeelding(en)")
        if rep.get("logoClipped"):
            c = rep["logoClipped"][0]
            issues.append(f"{name}: logo afgeknipt/buiten zijn vak "
                          f"({c.get('clipY', 0)}px te hoog, {c.get('clipX', 0)}px te breed)")
        if rep.get("console_errors"):
            issues.append(f"{name}: {len(rep['console_errors'])} console-error(s)")
        for el in rep.get("elements", []):
            worst_elements.append(el)

    leaks = _find_leaks(html, include_city=include_city_leak)
    if leaks:
        issues.append("placeholder-lek: " + ", ".join(leaks))

    return {
        "ok": not issues,
        "issues": issues,
        "leaks": leaks,
        "overflow_elements": worst_elements,
        "reports": reports,
    }


def run_gate(
    html: str | Path,
    *,
    max_iters: int = 3,
    include_city_leak: bool = False,
) -> dict[str, Any]:
    """Inspecteer -> repareer -> herinspecteer tot stabiel of max_iters.

    Returns:
        {
          "ok": bool,                # gate gehaald (geen overflow/broken/console)
          "html": str,               # (eventueel) gefixte html
          "iterations": int,
          "issues": [...],           # resterende issues
          "leaks": [...],            # placeholder-lekken (NIET auto-fixbaar)
          "repaired": bool,          # is er CSS geinjecteerd
        }

    Placeholder-lekken worden NIET door de gate gefixt: dat is een datafout die
    de generator of de fallback moet oplossen. De gate meldt ze wel.
    """
    html = _read_html(html)
    repaired = False
    last: dict[str, Any] = {}

    for i in range(1, max_iters + 1):
        last = check_html(html, include_city_leak=include_city_leak)
        # Niets op te lossen of niets meer dat de CSS-repair raakt -> stop.
        if last["ok"] or (not last["overflow_elements"] and not last["reports"].get("mobile", {}).get("overflowX") and not last["reports"].get("desktop", {}).get("overflowX")):
            break
        # Deterministische reparatie injecteren.
        css = _REPAIR_CSS_GLOBAL if not repaired else ""
        css += _targeted_repair_css(last["overflow_elements"])
        # Laatste iteratie en nog overflow: vangnet (afknijpen + flaggen).
        if i == max_iters and any("overflow" in s for s in last["issues"]):
            css += "html,body{overflow-x:hidden}"
        if css:
            html = _inject_css(html, css)
            repaired = True

    # Slotmeting na de laatste reparatie.
    final = check_html(html, include_city_leak=include_city_leak)
    return {
        "ok": final["ok"],
        "html": html,
        "iterations": i,
        "issues": final["issues"],
        "leaks": final["leaks"],
        "repaired": repaired,
    }


def _aggregate(reports: dict[str, Any], html: str, include_city_leak: bool) -> dict[str, Any]:
    issues: list[str] = []
    worst: list[dict[str, Any]] = []
    for name, rep in reports.items():
        if rep.get("skipped"):
            continue
        if rep.get("overflowX"):
            issues.append(f"{name}: horizontale overflow ({rep.get('overflowPx')}px)")
        if rep.get("brokenImages"):
            issues.append(f"{name}: {rep['brokenImages']} gebroken afbeelding(en)")
        if rep.get("logoClipped"):
            c = rep["logoClipped"][0]
            issues.append(f"{name}: logo afgeknipt/buiten zijn vak "
                          f"({c.get('clipY', 0)}px te hoog, {c.get('clipX', 0)}px te breed)")
        if rep.get("console_errors"):
            issues.append(f"{name}: {len(rep['console_errors'])} console-error(s)")
        worst.extend(rep.get("elements", []))
    leaks = _find_leaks(html, include_city=include_city_leak)
    if leaks:
        issues.append("placeholder-lek: " + ", ".join(leaks))
    return {"ok": not issues, "issues": issues, "leaks": leaks, "overflow_elements": worst}


def run_gate_file(
    path: str | Path,
    *,
    doc_root: str | Path,
    max_iters: int = 3,
    include_city_leak: bool = False,
) -> dict[str, Any]:
    """Asset-getrouwe gate: serveert doc_root, inspecteert het bestand zoals het
    geserveerd wordt, fixt deterministisch in het bestand, en herhaalt.

    `path` is het index.html-bestand; `doc_root` is de webroot (bijv. PUBLIC_ROOT)
    zodat root-relatieve assets (/...) kloppen. Dit is de productie-ingang.
    """
    path = Path(path)
    doc_root = Path(doc_root)
    route = str(path.resolve().relative_to(doc_root.resolve()))
    repaired = False
    last_iter = 0

    with _StaticServer(doc_root) as srv:
        url = srv.url(route)
        for i in range(1, max_iters + 1):
            last_iter = i
            reports = {name: inspect_url(url, vp) for name, vp in VIEWPORTS.items()}
            html = path.read_text(encoding="utf-8")
            agg = _aggregate(reports, html, include_city_leak)
            has_overflow = any("overflow" in s for s in agg["issues"])
            if agg["ok"] or (not agg["overflow_elements"] and not has_overflow):
                break
            css = _REPAIR_CSS_GLOBAL if not repaired else ""
            css += _targeted_repair_css(agg["overflow_elements"])
            if i == max_iters and has_overflow:
                css += "html,body{overflow-x:hidden}"
            if css:
                path.write_text(_inject_css(html, css), encoding="utf-8")
                repaired = True
        # Slotmeting.
        reports = {name: inspect_url(url, vp) for name, vp in VIEWPORTS.items()}
        html = path.read_text(encoding="utf-8")
        final = _aggregate(reports, html, include_city_leak)

    return {
        "ok": final["ok"],
        "iterations": last_iter,
        "issues": final["issues"],
        "leaks": final["leaks"],
        "repaired": repaired,
        "reports": reports,
    }


def _main() -> int:
    if len(sys.argv) < 2:
        print("gebruik: python3 qa_gate.py path/to/index.html [--city-leak]")
        return 2
    path = sys.argv[1]
    include_city = "--city-leak" in sys.argv
    result = run_gate(path, include_city_leak=include_city)
    summary = {k: v for k, v in result.items() if k != "html"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
