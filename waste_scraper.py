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
from typing import Iterable, List, Dict, Any, Optional

from playwright.async_api import async_playwright, Response

DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", re.UNICODE)

DEFAULT_URL = "https://bad-fischau-brunn.at/waste-management/areas"

@dataclass
class Item:
    date: str          # ISO: YYYY-MM-DD
    fraction: str      # Restmüll | Papier | Gelber Sack | ...
    street: str        # z.B. Institutsgasse
    source: str        # "dom" | "json"
    raw_text: str      # Zeile / Node-Text zum Nachvollziehen

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
    # Falls „Gelber Sack“ usw. vorkommt, gib den normalisierten Fraktionsnamen zurück
    norm = text.strip().lower()
    for w in wanted:
        if w.lower() in norm:
            return w
    return None

async def scrape_dom(page, street: str, wanted_fractions: List[str]) -> List[Item]:
    """
    Klicke 'Institutsgasse' (oder Straße per arg), warte auf das Modal, parse Tabellen/Listen.
    Selektoren sind robust über Text-Targeting gehalten.
    """
    items: List[Item] = []

    # 1) Seite aufrufen
    await page.goto(DEFAULT_URL, wait_until="domcontentloaded")

    # 2) Straße anklicken (Text-basiert; first, falls mehrfach)
    #    Falls die Seite eine Karte hat, hat sie irgendwo eine Liste oder Buttons mit Straßennamen.
    #    Sonst bitte kurz im DevTools schauen und ggf. anpassen.
    street_locator = page.get_by_text(street, exact=True)
    if not await street_locator.count():
        # Fallback: enthält-Variante
        street_locator = page.get_by_text(street)
    await street_locator.first.click()

    # 3) Auf Modal warten (role=dialog) und Inhalt lesen
    #    Wir nehmen an, dass im Modal eine Tabelle oder Liste erscheint.
    #    Wir durchsuchen ALLE Textelemente nach Datum+Fraktion.
    await page.wait_for_timeout(800)  # kurzer JS-Render-Puffer
    # Suche innerhalb des Modals (falls role=dialog vorhanden)
    modal = page.get_by_role("dialog")
    scope = modal if await modal.count() else page

    # Kandidaten: Tabellenzeilen, Listeneinträge oder generische Container
    candidates = []
    # Tabellen
    candidates.extend(await scope.locator("table tr").all())
    # Listen
    candidates.extend(await scope.locator("ul li").all())
    # Generische Zeilen/Absätze
    candidates.extend(await scope.locator("div, p").all())

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
    """
    Versuch: JSON-Responses der Seite parsen. Wir kennen die API nicht im Voraus,
    daher heuristisch: Finde JSON mit Datum + Fraktion im Text.
    """
    items: List[Item] = []
    for resp in payloads:
        try:
            if "application/json" not in (resp.headers.get("content-type","")):
                continue
            data = await resp.json()
        except Exception:
            continue

        # Heuristik: beliebig tief durchsuchen nach Strings, die Datum+Fraktion enthalten
        def walk(x) -> Iterable[str]:
            if isinstance(x, dict):
                for v in x.values():
                    yield from walk(v)
            elif isinstance(x, list):
                for v in x:
                    yield from walk(v)
            elif isinstance(x, str):
                yield x

        for s in walk(data):
            date_iso = parse_date_ddmmyyyy(s)
            if not date_iso:
                continue
            frac = row_matches_any_fraction(s, wanted_fractions)
            if not frac:
                continue
            items.append(Item(
                date=date_iso,
                fraction=frac,
                street=street,
                source=f"json:{resp.url}",
                raw_text=" ".join(s.split())
            ))
    return items

async def run(street: str, fractions: List[str], out_csv: Path, debug_network: bool) -> List[Item]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        json_responses: List[Response] = []
        if debug_network:
            page.on("response", lambda r: json_responses.append(r))

        # Erst DOM scraping
        dom_items = await scrape_dom(page, street, fractions)

        # Falls gewünscht/aktiv: zusätzlich JSON sniffen und mergen
        json_items = []
        if debug_network:
            # leichte Wartezeit, damit Requests durchlaufen
            await page.wait_for_timeout(500)
            json_items = await try_collect_json(json_responses, street, fractions)

        await browser.close()

    # zusammenführen & deduplizieren (nach date+fraction)
    combined: Dict[tuple, Item] = {}
    for it in dom_items + json_items:
        key = (it.date, it.fraction.lower())
        combined[key] = it  # letztes gewinnt – hier egal

    items = list(combined.values())
    items.sort(key=lambda x: (x.date, x.fraction))

    # CSV ausgeben
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
    ap.add_argument("--street", default="Institutsgasse", help="Straßenname (z.B. Institutsgasse)")
    ap.add_argument("--fractions", default="Restmüll,Papier,Gelber Sack",
                    help="Kommagetrennt: z.B. 'Restmüll,Papier,Gelber Sack'")
    ap.add_argument("--out", default="waste_institutsgasse.csv", help="CSV-Ausgabe-Datei")
    ap.add_argument("--debug-network", action="store_true", help="JSON-Responses mitschneiden und auswerten")
    args = ap.parse_args()

    fractions = [s.strip() for s in args.fractions.split(",") if s.strip()]
    items = asyncio.run(run(args.street, fractions, Path(args.out), args.debug_network))

    print(f"Found {len(items)} items:")
    for it in items[:10]:
        print(f"  {it.date} | {it.fraction} | {it.street} | {it.source}")
    if len(items) > 10:
        print(f"  ... ({len(items)-10} more)")

if __name__ == "__main__":
    main()
