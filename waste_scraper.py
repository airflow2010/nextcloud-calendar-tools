#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, asyncio, csv, re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Dict, Any
from playwright.async_api import async_playwright, Response, TimeoutError as PwTimeout

DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", re.UNICODE)
DEFAULT_URL = "https://bad-fischau-brunn.at/waste-management/areas"

@dataclass
class Item:
    date: str
    fraction: str
    street: str
    source: str
    raw_text: str

def parse_date_ddmmyyyy(text: str) -> Optional[str]:
    m = DATE_RE.search(text)
    if not m: return None
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

async def dismiss_cookies(page):
    # Häufige Varianten
    selectors = [
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Einverstanden')",
        "button[aria-label*='akzept']",
    ]
    for sel in selectors:
        try:
            await page.locator(sel).first.click(timeout=1500)
            break
        except Exception:
            pass

async def click_institutsgasse(page, street: str):
    # Mehrere robuste Strategien
    strategies = [
        lambda: page.get_by_text(street, exact=True).first,
        lambda: page.get_by_role("button", name=street).first,
        lambda: page.get_by_role("link", name=street).first,
        lambda: page.locator(f"text=/^\\s*{re.escape(street)}\\s*$/i").first,
        lambda: page.locator(f"[aria-label*='{street}'], [title*='{street}']").first,
        lambda: page.locator(f":has-text('{street}')").first,
    ]
    for i, fn in enumerate(strategies, 1):
        loc = fn()
        try:
            await loc.wait_for(timeout=3000)
            await loc.click(timeout=3000)
            return True
        except Exception:
            continue
    return False

async def open_finder(page):
    # Klicke den Einstiegsknopf "Deinen Kalender finden"
    # Wir probieren mehrere robuste Selektoren:
    candidates = [
        lambda: page.get_by_role("button", name=re.compile(r"deinen\s+kalender\s+finden", re.I)).first,
        lambda: page.get_by_role("link",   name=re.compile(r"deinen\s+kalender\s+finden", re.I)).first,
        lambda: page.locator("button:has-text('Deinen Kalender finden')").first,
        lambda: page.locator("a:has-text('Deinen Kalender finden')").first,
        lambda: page.locator("text=/Deinen\\s+Kalender\\s+finden/i").first,
    ]
    for fn in candidates:
        loc = fn()
        try:
            await loc.wait_for(timeout=3000)
            await loc.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


async def scrape_dom(page, street: str, wanted_fractions: List[str]) -> List[Item]:
    """
    1) Seite laden
    2) Cookie-Banner schließen
    3) 'Deinen Kalender finden' klicken
    4) (falls vorhanden) Suchfeld mit Straßenname befüllen
    5) 'Institutsgasse' anklicken (öffnet Modal oder listet Termine)
    6) Termine (Datum + Fraktion) im Modal/Seitenbereich auslesen
    """
    items: List[Item] = []

    # Seite vollständig (netzwerkidle) laden
    await page.goto(DEFAULT_URL, wait_until="networkidle")

    # Cookiebanner weg
    await dismiss_cookies(page)

    # Einstieg "Deinen Kalender finden"
    opened = await open_finder(page)
    if not opened:
        # einmal scrollen und nochmals versuchen (falls offscreen)
        await page.mouse.wheel(0, 1200)
        opened = await open_finder(page)
    # wenn weiterhin False, machen wir trotzdem weiter – evtl. ist die Liste bereits sichtbar

    # Optional: vorhandenes Suchfeld füllen (wenn die UI eines anbietet)
    search_boxes = [
        page.get_by_role("textbox", name=re.compile(r"suche|straße|strasse|search|street", re.I)),
        page.locator("input[placeholder*='uch'], input[placeholder*='Stra'], input[placeholder*='Stras']"),
    ]
    for sb in search_boxes:
        try:
            await sb.first.fill(street, timeout=2000)
            await page.wait_for_timeout(300)  # UI filtern lassen
            break
        except Exception:
            pass

    # Ziel-Straße anklicken
    clicked = await click_institutsgasse(page, street)
    if not clicked:
        # noch etwas scrollen und erneut probieren
        await page.mouse.wheel(0, 2000)
        clicked = await click_institutsgasse(page, street)

    # Bereich bestimmen, in dem die Termine stehen (Modal oder gesamte Seite)
    modal = page.get_by_role("dialog")
    try:
        await modal.wait_for(timeout=4000)
        scope = modal
    except PwTimeout:
        scope = page

    # Kandidaten-Container für Terminzeilen
    candidates = []
    # Tabellen
    candidates.extend(await scope.locator("table tr").all())
    # Listen
    candidates.extend(await scope.locator("ul li").all())
    candidates.extend(await scope.locator("[class*=list] li").all())
    # Generische Zeilen/Absätze
    candidates.extend(await scope.locator("p, .row div, .col div").all())

    # Deduplizieren nach Text
    seen = set()
    for el in candidates:
        try:
            t = (await el.inner_text()).strip()
        except Exception:
            continue
        if not t or len(t) > 2000:
            continue
        key = hash(t)
        if key in seen:
            continue
        seen.add(key)

        date_iso = parse_date_ddmmyyyy(t)
        if not date_iso:
            continue
        frac = row_matches_any_fraction(t, wanted_fractions)
        if not frac:
            continue

        items.append(Item(
            date=date_iso,
            fraction=frac,
            street=street,
            source="dom",
            raw_text=" ".join(t.split())
        ))

    return items


async def try_collect_json(payloads: List[Response], street: str, wanted_fractions: List[str]) -> List[Item]:
    items: List[Item] = []
    def walk(x) -> Iterable[str]:
        if isinstance(x, dict):
            for v in x.values(): yield from walk(v)
        elif isinstance(x, list):
            for v in x: yield from walk(v)
        elif isinstance(x, str):
            yield x
    for resp in payloads:
        try:
            if "application/json" not in (resp.headers.get("content-type","")): continue
            data = await resp.json()
        except Exception:
            continue
        for s in walk(data):
            date_iso = parse_date_ddmmyyyy(s)
            if not date_iso: continue
            frac = row_matches_any_fraction(s, wanted_fractions)
            if not frac: continue
            items.append(Item(date_iso, frac, street, f"json:{resp.url}", " ".join(s.split())))
    # dedupe by (date, fraction)
    ded: Dict[tuple, Item] = {}
    for it in items:
        ded[(it.date, it.fraction.lower())] = it
    return list(ded.values())

async def run(street: str, fractions: List[str], out_csv: Path, debug_network: bool, headful: bool, trace: bool) -> List[Item]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        context = await browser.new_context()
        if trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()

        json_responses: List[Response] = []
        if debug_network:
            page.on("response", lambda r: json_responses.append(r))

        try:
            dom_items = await scrape_dom(page, street, fractions)
            json_items: List[Item] = []
            if debug_network:
                json_items = await try_collect_json(json_responses, street, fractions)
        except Exception as e:
            # Debug-Artefakte sichern
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(path=str(out_csv.parent / "waste_debug.png"), full_page=True)
            except Exception:
                pass
            try:
                html = await page.content()
                (out_csv.parent / "waste_debug.html").write_text(html, encoding="utf-8")
            except Exception:
                pass
            raise e
        finally:
            if trace:
                await context.tracing.stop(path=str(out_csv.parent / "trace.zip"))
            await browser.close()

    # Merge & sort
    combined: Dict[tuple, Item] = {}
    for it in dom_items + json_items:
        combined[(it.date, it.fraction.lower())] = it
    items = list(combined.values())
    items.sort(key=lambda x: (x.date, x.fraction))

    # CSV schreiben
    if out_csv:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["date", "fraction", "street", "source", "raw"])
            for it in items:
                w.writerow([it.date, it.fraction, it.street, it.source, it.raw_text])
    return items

def main():
    ap = argparse.ArgumentParser(description="Waste calendar scraper (Bad Fischau-Brunn)")
    ap.add_argument("--street", default="Institutsgasse")
    ap.add_argument("--fractions", default="Restmüll,Papier,Gelber Sack")
    ap.add_argument("--out", default="waste_institutsgasse.csv")
    ap.add_argument("--debug-network", action="store_true")
    ap.add_argument("--headful", action="store_true", help="Browser sichtbar starten")
    ap.add_argument("--trace", action="store_true", help="Playwright Trace aufnehmen (trace.zip)")
    args = ap.parse_args()

    fractions = [s.strip() for s in args.fractions.split(",") if s.strip()]
    items = asyncio.run(run(args.street, fractions, Path(args.out), args.debug_network, args.headful, args.trace))
    print(f"Found {len(items)} items:")
    for it in items[:12]:
        print(f"  {it.date} | {it.fraction} | {it.street} | {it.source}")
    if len(items) > 12:
        print(f"  ... ({len(items)-12} more)")

if __name__ == "__main__":
    main()
