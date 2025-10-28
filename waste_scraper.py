#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Dict, Any

from playwright.async_api import async_playwright, Response, TimeoutError as PwTimeout

# ----------------------------------------------------------------------
# Einstellungen
# ----------------------------------------------------------------------

DEFAULT_URL = "https://bad-fischau-brunn.at/waste-management/areas"
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", re.UNICODE)

WANTED_DEFAULT = ["Restmüll", "Papier", "Gelber Sack"]  # CLI überschreibt

# ----------------------------------------------------------------------
# Modelle & Utils
# ----------------------------------------------------------------------

@dataclass
class Item:
    date: str
    fraction: str
    street: str
    source: str
    raw_text: str


def parse_date_ddmmyyyy(text: str) -> Optional[str]:
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def row_matches_any_fraction(text: str, wanted: Iterable[str]) -> Optional[str]:
    norm = text.strip().lower()
    for w in wanted:
        if w.lower() in norm:
            return w
    return None


def normalize_fraction(name: str) -> str:
    s = (name or "").strip()
    low = s.lower()
    if low in ("altpapier", "papier"):
        return "Papier"
    if low.startswith("restmüll"):
        return "Restmüll"
    if low.startswith("gelber sack") or low.startswith("gelbsack"):
        return "Gelber Sack"
    return s


def looks_like_waste_json(data: Any) -> bool:
    return isinstance(data, dict) and "garbageCollectionDays" in data and "street" in data


def extract_items_from_json(data: Dict[str, Any],
                            wanted_fractions: List[str]) -> List[Item]:
    """Zieht (date, fraction, street, raw) aus dem offiziellen JSON-Payload."""
    items: List[Item] = []
    street = str(data.get("street") or "").strip()

    for day in data.get("garbageCollectionDays", []):
        date_raw = day.get("date")
        date_iso = (str(date_raw)[:10] if isinstance(date_raw, str) and len(str(date_raw)) >= 10 else "")

        names: List[str] = []
        gts = day.get("garbageTypeSettings", [])
        if isinstance(gts, dict):
            names.append(gts.get("displayName") or gts.get("name") or gts.get("garbageType") or "")
        elif isinstance(gts, list):
            for s in gts:
                if isinstance(s, dict):
                    names.append(s.get("displayName") or s.get("name") or s.get("garbageType") or "")
                else:
                    names.append(str(s))
        if not any(n for n in names) and day.get("name"):
            names.append(str(day["name"]))

        for n in names:
            if not n:
                continue
            frac = normalize_fraction(n)
            if frac in wanted_fractions:
                items.append(Item(date=date_iso, fraction=frac, street=street, source="json", raw_text=n.strip()))

    # dedupe: (date, fraction)
    seen: Dict[tuple, Item] = {}
    for it in items:
        seen[(it.date, it.fraction.lower())] = it
    return list(seen.values())

# ----------------------------------------------------------------------
# DOM-Interaktion
# ----------------------------------------------------------------------

async def dismiss_cookies(page):
    """Klickt häufige Varianten von Cookie-Bannern weg."""
    selectors = [
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Einverstanden')",
        "button[aria-label*='akzept']",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ]
    for sel in selectors:
        try:
            await page.locator(sel).first.click(timeout=1200)
            break
        except Exception:
            pass


async def open_finder(page) -> bool:
    """Klickt den Einstiegsknopf 'Deinen Kalender finden' (oder engl. Fallback)."""
    candidates = [
        lambda: page.get_by_role("button", name=re.compile(r"deinen\s+kalender\s+finden", re.I)).first,
        lambda: page.get_by_role("link",   name=re.compile(r"deinen\s+kalender\s+finden", re.I)).first,
        lambda: page.get_by_role("button", name=re.compile(r"find\s+your\s+calendar", re.I)).first,
        lambda: page.locator("button:has-text('Deinen Kalender finden')").first,
        lambda: page.locator("a:has-text('Deinen Kalender finden')").first,
        lambda: page.locator("text=/Deinen\\s+Kalender\\s+finden/i").first,
    ]
    for fn in candidates:
        loc = fn()
        try:
            await loc.wait_for(timeout=2500)
            await loc.click(timeout=2500)
            return True
        except Exception:
            continue
    return False


async def click_institutsgasse(page, street: str) -> bool:
    """Klickt den Eintrag 'Institutsgasse' in der darauf folgenden Liste."""
    strategies = [
        lambda: page.get_by_text(street, exact=True).first,
        lambda: page.get_by_role("button", name=street).first,
        lambda: page.get_by_role("link", name=street).first,
        lambda: page.locator(f"text=/^\\s*{re.escape(street)}\\s*$/i").first,
        lambda: page.locator(f"[aria-label*='{street}'], [title*='{street}']").first,
        lambda: page.locator(f":has-text('{street}')").first,
    ]
    for fn in strategies:
        loc = fn()
        try:
            await loc.wait_for(timeout=2500)
            await loc.click(timeout=2500)
            return True
        except Exception:
            continue
    return False


async def extract_items_from_scope(scope, street: str, wanted_fractions: List[str]) -> List[Item]:
    """DOM-Parser (Fallback), falls kein JSON-Hook greift."""
    items: List[Item] = []
    seen = set()

    # Datum-Blöcke (so in deinem HTML-Dump gesehen)
    date_blocks = await scope.locator("div.align-items-center.d-flex.gap-3.text-3.text-shade-2").all()

    for block in date_blocks:
        try:
            date_text = (await block.inner_text()).strip()
        except Exception:
            continue

        # z. B. "21. Nov. • Freitag"
        m = re.search(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\.?", date_text)
        if not m:
            continue
        day, monthname = m.groups()
        months = {
            "Jan": 1, "Feb": 2, "Mär": 3, "Mrz": 3, "Apr": 4, "Mai": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Okt": 10, "Nov": 11, "Dez": 12,
        }
        mo = next((v for k, v in months.items() if monthname.lower().startswith(k.lower())), None)
        if not mo:
            continue

        today = datetime.today()
        year = today.year
        if mo < today.month - 1:
            year += 1
        try:
            date_iso = datetime(year, mo, int(day)).date().isoformat()
        except Exception:
            continue

        # Die direkt folgende <ul class="list"> enthält die Müllarten
        ul = block.locator("xpath=following-sibling::ul[1]")
        try:
            li_texts = await ul.locator("p.text-ellipsis").all_inner_texts()
        except Exception:
            continue

        for t in li_texts:
            t_clean = t.strip()
            frac = row_matches_any_fraction(t_clean, wanted_fractions)
            if not frac:
                continue
            key = (date_iso, frac.lower())
            if key in seen:
                continue
            seen.add(key)
            items.append(Item(date=date_iso, fraction=frac, street=street, source="dom", raw_text=t_clean))

    return items

# ----------------------------------------------------------------------
# Hauptlogik
# ----------------------------------------------------------------------

async def scrape_dom(page, street: str, wanted_fractions: List[str]) -> List[Item]:
    """Steuert die UI und ruft anschließend den DOM-Parser (Fallback) auf."""
    await page.goto(DEFAULT_URL, wait_until="networkidle")
    await dismiss_cookies(page)

    opened = await open_finder(page)
    if not opened:
        await page.mouse.wheel(0, 1200)
        opened = await open_finder(page)

    # optional Suchfeld füllen
    search_boxes = [
        page.get_by_role("textbox", name=re.compile(r"suche|straße|strasse|search|street", re.I)),
        page.locator("input[placeholder*='uch'], input[placeholder*='Stra'], input[placeholder*='Stras']"),
    ]
    for sb in search_boxes:
        try:
            await sb.first.fill(street, timeout=2000)
            await page.wait_for_timeout(300)
            break
        except Exception:
            pass

    clicked = await click_institutsgasse(page, street)
    if not clicked:
        await page.mouse.wheel(0, 2000)
        clicked = await click_institutsgasse(page, street)

    # Modal oder Seite als Scope wählen
    modal = page.get_by_role("dialog")
    try:
        await modal.wait_for(timeout=4000)
        scope = modal
    except PwTimeout:
        scope = page

    return await extract_items_from_scope(scope, street, wanted_fractions)

# ----------------------------------------------------------------------
# Laufsteuerung mit Trace und JSON-Hook
# ----------------------------------------------------------------------

async def run(street: str,
              fractions: List[str],
              out_csv: Path,
              headful: bool,
              debug_network: bool,
              trace: bool) -> List[Item]:

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        context = await browser.new_context()
        if trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = await context.new_page()

        # JSON-Hook: schreibe API-URL + Rohpayload, wenn gefunden
        json_items: List[Item] = []
        json_url_captured: Optional[str] = None

        async def on_response(resp: Response):
            nonlocal json_items, json_url_captured
            try:
                ct = resp.headers.get("content-type", "") or ""
            except Exception:
                ct = ""
            if "application/json" not in ct:
                return
            try:
                data = await resp.json()
            except Exception:
                return
            if looks_like_waste_json(data):
                # API-URL & Payload sichern
                json_url_captured = resp.url
                out_dir = out_csv.parent
                (out_dir / "waste_endpoint.txt").write_text(json_url_captured, encoding="utf-8")
                (out_dir / "waste_payload.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                # Items extrahieren
                json_items = extract_items_from_json(data, fractions)

        page.on("response", on_response)

        # DOM-Navigation (öffnet „Institutsgasse“)
        dom_items = await scrape_dom(page, street, fractions)

        # Trace sichern
        if trace:
            await context.tracing.stop(path=str(out_csv.parent / "trace.zip"))
        await browser.close()

    # Bevorzugt JSON-Ergebnis, falls verfügbar; sonst DOM-Fallback
    items = json_items if json_items else dom_items

    # CSV schreiben
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", "fraction", "street", "source", "raw"])
        for it in sorted(items, key=lambda x: (x.date, x.fraction)):
            w.writerow([it.date, it.fraction, it.street, it.source, it.raw_text])

    return items


def main():
    ap = argparse.ArgumentParser(description="Waste calendar scraper (Bad Fischau-Brunn)")
    ap.add_argument("--street", default="Institutsgasse", help="Straßenname (z.B. Institutsgasse)")
    ap.add_argument("--fractions", default="Restmüll,Papier,Gelber Sack",
                    help="Kommagetrennt: z. B. 'Restmüll,Papier,Gelber Sack'")
    ap.add_argument("--out", default="waste_institutsgasse.csv", help="CSV-Ausgabedatei")
    ap.add_argument("--headful", action="store_true", help="Browser sichtbar starten")
    ap.add_argument("--debug-network", action="store_true", help="Netzwerkanalyse aktivieren (nur Logging)")
    ap.add_argument("--trace", action="store_true", help="Playwright trace aufnehmen (trace.zip)")
    args = ap.parse_args()

    fractions = [s.strip() for s in args.fractions.split(",") if s.strip()]
    items = asyncio.run(
        run(args.street, fractions, Path(args.out),
            args.headful, args.debug_network, getattr(args, "trace", False))
    )

    print(f"✅ Found {len(items)} items:")
    for it in items[:10]:
        print(f"  {it.date} | {it.fraction} | {it.street}")
    if len(items) > 10:
        print(f"  ... ({len(items)-10} more)")


if __name__ == "__main__":
    main()
