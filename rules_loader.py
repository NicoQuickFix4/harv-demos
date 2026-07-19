#!/usr/bin/env python3
"""Leest de canonieke regels uit RULES.md zodat de code en de markdown nooit
uit de pas lopen.

De single source of truth is:
    ~/Developer/harv-dakdekker/v2/demo-generator/RULES.md

Dat bestand bevat (deel 8) machine-leesbare blokken tussen vaste markers. Deze
loader pakt daar drie dingen uit:
  - de placeholder-leklijst       -> qa_gate.py
  - de plaats-default (Utrecht)   -> qa_gate.py
  - de sign-off rubric            -> qa_signoff.py

Ontwerpprincipe: NOOIT crashen op een ontbrekend bestand/sectie. Elke getter
geeft `None` terug als hij de waarde niet vindt; de aanroeper houdt dan zijn
eigen ingebouwde fallback aan. Zo blijft alles werken, ook zonder RULES.md, maar
zodra het bestand er is wint het.

Padvolgorde voor RULES.md:
  1. env HARV_RULES_FILE (expliciet pad)
  2. ../harv-dakdekker/v2/demo-generator/RULES.md (naast de harv-demos repo)
  3. ~/Developer/harv-dakdekker/v2/demo-generator/RULES.md (absoluut)
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_MARKERS = {
    "placeholder_leaks": ("<!-- harv:placeholder_leaks:start -->",
                          "<!-- harv:placeholder_leaks:end -->"),
    "placeholder_city_default": ("<!-- harv:placeholder_city_default:start -->",
                                 "<!-- harv:placeholder_city_default:end -->"),
    "signoff_rubric": ("<!-- harv:signoff_rubric:start -->",
                       "<!-- harv:signoff_rubric:end -->"),
}


def rules_path() -> Optional[Path]:
    """Vind RULES.md via env -> repo-buur -> absoluut. None als niets bestaat."""
    candidates = []
    env = os.environ.get("HARV_RULES_FILE", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve().parent  # .../harv-demos
    candidates.append(here.parent / "harv-dakdekker" / "v2" / "demo-generator" / "RULES.md")
    candidates.append(Path("~/Developer/harv-dakdekker/v2/demo-generator/RULES.md").expanduser())
    for c in candidates:
        if c.is_file():
            return c
    return None


@lru_cache(maxsize=1)
def _raw() -> str:
    p = rules_path()
    if not p:
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _block(name: str) -> Optional[str]:
    """De ruwe tekst tussen de start/end markers van een blok, of None."""
    start, end = _MARKERS[name]
    text = _raw()
    if not text:
        return None
    i = text.find(start)
    j = text.find(end)
    if i == -1 or j == -1 or j <= i:
        return None
    return text[i + len(start):j]


def get_placeholder_leaks() -> Optional[tuple[str, ...]]:
    """Tuple met placeholder-lekken, of None als het blok ontbreekt."""
    body = _block("placeholder_leaks")
    if body is None:
        return None
    items: list[str] = []
    for line in body.splitlines():
        m = re.match(r"\s*-\s+(.*\S)\s*$", line)
        if m:
            items.append(m.group(1).strip())
    return tuple(items) if items else None


def get_placeholder_city_default() -> Optional[str]:
    """De plaats-default (bv. 'Utrecht'), of None."""
    body = _block("placeholder_city_default")
    if body is None:
        return None
    val = body.strip()
    return val or None


def get_signoff_rubric() -> Optional[str]:
    """De volledige sign-off rubric als string, of None."""
    body = _block("signoff_rubric")
    if body is None:
        return None
    val = body.strip()
    return val or None


if __name__ == "__main__":  # snelle handmatige check
    import json
    print(json.dumps({
        "rules_path": str(rules_path()),
        "leaks": get_placeholder_leaks(),
        "city_default": get_placeholder_city_default(),
        "rubric_chars": len(get_signoff_rubric() or ""),
    }, ensure_ascii=False, indent=2))
