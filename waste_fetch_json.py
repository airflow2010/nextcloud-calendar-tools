#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, csv, json, sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
import requests

WANTED_DEFAULT = ["Restmüll", "Papier", "Gelber Sack", "Altpapier"]

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

def extract_items(data: Dict[str, Any], wanted_fractions: List[str]) -> List[Dict[str,str]]:
    street = str(data.get("street") or "").strip()
    out: List[Dict[str,str]] = []
    for day in data.get("garbageCollectionDays", []):
        raw_date = day.get("date")
        date_iso = raw_date[:10] if isinstance(raw_date, str) and len(raw_date) >= 10 else ""

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
                out.append({
                    "date": date_iso,
                    "fraction": frac,
                    "street": street,
                    "source": "json",
                    "raw": n.strip(),
                })
    # dedupe per (date, fraction)
    ded: Dict[tuple, Dict[str,str]] = {}
    for it in out:
        ded[(it["date"], it["fraction"].lower())] = it
    return list(ded.values())

def browsery_headers(extra: Optional[Dict[str,str]] = None) -> Dict[str,str]:
    h = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Origin": "https://bad-fischau-brunn.at",
        "Referer": "https://bad-fischau-brunn.at/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if extra:
        h.update(extra)
    return h

def fetch_json(url: str, params: Optional[Dict[str,str]] = None, headers: Optional[Dict[str,str]] = None) -> Dict[str,Any]:
    full_url = url
    if params:
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}{urlencode(params)}"
    h = browsery_headers(headers)
    r = requests.get(full_url, headers=h, timeout=30)
    if r.status_code == 403:
        # Retry mit leicht anderem Header-Profil (manche Gateways prüfen X-Requested-With/Upgrade-Insecure-Requests)
        h2 = browsery_headers({"X-Requested-With": "XMLHttpRequest", "Upgrade-Insecure-Requests": "1"})
        r = requests.get(full_url, headers=h2, timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    ap = argparse.ArgumentParser(description="Fetch waste calendar JSON and write CSV")
    ap.add_argument("--url", required=True, help="z.B. https://api.v2.citiesapps.com/waste-management/areas/<areaId>/calendar")
    ap.add_argument("--out", default="waste_institutsgasse.csv")
    ap.add_argument("--fractions", default="Restmüll,Papier,Gelber Sack")
    ap.add_argument("--date-from", dest="date_from", help="YYYY-MM-DD (optional)")
    ap.add_argument("--date-to",   dest="date_to",   help="YYYY-MM-DD (optional)")
    ap.add_argument("--print-json", action="store_true", help="Rohes JSON (gekürzt) auf stderr ausgeben")
    ap.add_argument("--debug", action="store_true", help="mehr Ausgabe (URL/Params)")
    args = ap.parse_args()

    wanted = [s.strip() for s in args.fractions.split(",") if s.strip()]

    # Zeitraum standardmäßig: heute .. +12 Monate
    if not args.date_from or not args.date_to:
        today = date.today()
        default_from = today.replace(day=1)  # ab Monatsanfang
        default_to = (today + timedelta(days=365))
        params = {
            "from": default_from.isoformat(),
            "to": default_to.isoformat(),
        }
    else:
        params = {"from": args.date_from, "to": args.date_to}

    if args.debug:
        print(f"[DBG] URL: {args.url}")
        print(f"[DBG] Params: {params}")

    data = fetch_json(args.url, params=params)

    if args.print_json:
        preview = {k: data.get(k) for k in ("street", "garbageCollectionDays", "publicHolidays") if k in data}
        sys.stderr.write(json.dumps(preview, ensure_ascii=False)[:1500] + "\n")

    if not looks_like_waste_json(data):
        sys.exit("Unerwartetes JSON – 'garbageCollectionDays'/'street' fehlen.")

    items = extract_items(data, wanted)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date","fraction","street","source","raw"], delimiter=";")
        w.writeheader()
        for it in sorted(items, key=lambda x: (x["date"], x["fraction"])):
            w.writerow(it)

    print(f"OK: {len(items)} Einträge → {out_path}")

if __name__ == "__main__":
    main()
