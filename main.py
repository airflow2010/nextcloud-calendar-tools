#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nextcloud CalDAV/WebDAV Event-Formatter
- Setzt TRANSP (frei/beschäftigt) und COLOR basierend auf Titel-Regeln
- Arbeitet per PROPFIND/GET/PUT mit ETag-Schutz
- Robust gegen relative HREFs (korrekte absolute URLs)
- Dry-Run & Verbose/Debug Ausgaben
- .env Support (BASE_URL, CAL_NAME, USER, APP_PWD)

Benutzung (Beispiel):
  python main.py --dry-run --verbose
  python main.py --force               # erzwingt Überschreiben auch wenn identisch
  python main.py --limit 50            # nur 50 Objekte testen

Konfig bevorzugt via .env:
  BASE_URL=https://share.4fp.at/remote.php/dav/calendars/airflow/
  CAL_NAME=outlook-1
  USER=airflow
  APP_PWD=xxxx_app_password_xxxx
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Tuple
from urllib.parse import urljoin, urlparse

import requests

try:
    from icalendar import Calendar
except ImportError:
    sys.exit("Bitte installieren: pip install icalendar requests python-dotenv")

# .env optional laden (falls vorhanden)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


# ========= Regeln anpassen =========
# Liste aus (regex_pattern, hex_farbe, make_free_bool)
RULES = [
    (r"^T8$",   "khaki", True),
    (r"^T7$",   "khaki", True),
    (r"^T6$",   "khaki", True),
    (r"^T5$",   "khaki", True),
    (r"^N$",    "khaki", True),
    (r"^Teambesprechung$",   "khaki", True),
    # Weitere Beispiele:
    # (r".*Urlaub.*",                  "#f39c12", False),
]

# ========= Defaults aus .env =========
ENV_BASE_URL = os.getenv("BASE_URL", "").rstrip("/") + "/"
ENV_CAL_NAME = os.getenv("CAL_NAME", "")
ENV_USER     = os.getenv("USER", "")
ENV_APP_PWD  = os.getenv("APP_PWD", "")


@dataclass
class Config:
    base_url: str
    cal_name: str
    user: str
    app_pwd: str
    dry_run: bool = False
    verbose: bool = False
    debug: bool = False
    force: bool = False
    limit: int = 0
    normalize_summary: bool = True


def log(msg: str, *, level: str = "INFO", cfg: Config = None):
    if cfg and not cfg.verbose and level in ("DBG", "VERBOSE"):
        return
    print(msg)


def dbg(msg: str, cfg: Config):
    if cfg.debug:
        print(f"[DBG] {msg}")


def normalize_summary(text: str) -> str:
    """
    Entfernt die typischen, angehängten Steuerstrings, die wir in manchen Dateien gesehen haben.
    """
    t = re.sub(r"(TRANSP|X-MICROSOFT-CDO-BUSYSTATUS|STATUS|SEQUENCE|LOCATION|CATEGORIES|CLASS|PRIORITY)[:;=].*$",
               "", text, flags=re.I)
    t = re.sub(r"\s*(TRANSPARENT|OPAQUE|BUSY|FREE)\s*$", "", t, flags=re.I)
    return t.strip()


def build_origin(base_url: str) -> str:
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def propfind_calendar(cfg: Config, session: requests.Session) -> List[Tuple[str, str]]:
    """Listet ICS Objekte im Kalender (Tiefe 1) und liefert (absolute_href, etag)."""
    url = urljoin(cfg.base_url, cfg.cal_name.strip("/") + "/")

    body = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:getetag/>
    <d:getcontenttype/>
  </d:prop>
</d:propfind>"""

    headers = {"Depth": "1", "Content-Type": "application/xml"}
    r = session.request("PROPFIND", url, data=body, headers=headers)
    r.raise_for_status()

    ns = {"d": "DAV:"}
    # Manche Nextclouds liefern XML mit Namespace-Attributen – ET findet trotzdem via ns
    root = ET.fromstring(r.text)

    origin = build_origin(cfg.base_url)
    items: List[Tuple[str, str]] = []

    for resp in root.findall("d:response", ns):
        href = resp.findtext("d:href", default="", namespaces=ns) or ""
        prop = resp.find("d:propstat/d:prop", ns)
        if prop is None:
            continue
        ctype = (prop.findtext("d:getcontenttype", default="", namespaces=ns) or "").lower()
        etag  = (prop.findtext("d:getetag", default="", namespaces=ns) or "").strip('"')

        # Nur Event-Ressourcen
        if href.endswith(".ics") or "text/calendar" in ctype:
            # Absolute URL bauen: immer gegen Origin, NICHT base_url (um Dopplung zu vermeiden)
            if href.startswith("http"):
                abs_href = href
            else:
                abs_href = origin + (href if href.startswith("/") else "/" + href)
            items.append((abs_href, etag))

    return items


def load_ics(session: requests.Session, abs_href: str) -> Tuple[str, str]:
    r = session.get(abs_href, headers={"Accept": "text/calendar"})
    r.raise_for_status()
    return r.text, r.headers.get("ETag", "").strip('"')


def save_ics(session: requests.Session, abs_href: str, ics_text: str, etag: str, cfg: Config) -> bool:
    if cfg.dry_run:
        dbg(f"DRY-RUN: PUT {abs_href}", cfg)
        return True
    headers = {
        "If-Match": f"\"{etag}\"" if etag else "*",
        "Content-Type": "text/calendar; charset=utf-8",
    }
    r = session.put(abs_href, data=ics_text.encode("utf-8"), headers=headers)
    dbg(f"PUT {abs_href} -> {r.status_code}", cfg)
    if r.status_code in (200, 201, 204):
        return True
    if r.status_code == 412:
        log("   ETag mismatch (412) – wird mit frischem ETag erneut versucht.", level="VERBOSE", cfg=cfg)
    else:
        log(f"   WARN PUT {r.status_code} {r.text[:300]}", level="VERBOSE", cfg=cfg)
    return False


def apply_rules_to_event(vevent, cfg: Config) -> Tuple[bool, bool]:
    """
    Liefert (matched_any_rule, changed_something)
    """
    summary_val = vevent.get("SUMMARY")
    if summary_val is None:
        return (False, False)

    title = str(summary_val)
    if cfg.normalize_summary:
        title = normalize_summary(title)

    matched_any = False
    changed = False

    for pattern, color, make_free in RULES:
        if re.search(pattern, title, flags=re.I):
            matched_any = True
            before_t = vevent.get("TRANSP")
            before_c = vevent.get("COLOR")

            desired_t = "TRANSPARENT" if make_free else "OPAQUE"
            if before_t != desired_t:
                vevent["TRANSP"] = desired_t
                changed = True

            if color and vevent.get("COLOR") != color:
                vevent["COLOR"] = color
                changed = True

            break  # Erste passende Regel gewinnt

    return (matched_any, changed)


def patch_calendar_object(session: requests.Session, abs_href: str, etag_hint: str, cfg: Config,
                          stats: dict) -> None:
    try:
        raw, etag = load_ics(session, abs_href)
    except requests.HTTPError as e:
        log(f"HTTPError on GET {abs_href} {e}", level="VERBOSE", cfg=cfg)
        return

    etag = etag or etag_hint
    cal = Calendar.from_ical(raw)

    matched_total = 0
    changed_total = 0

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        matched, changed = apply_rules_to_event(comp, cfg)
        if matched:
            matched_total += 1
        if changed or cfg.force:
            changed_total += 1
            if cfg.force and matched:
                # force: setze nochmal explizit (auch wenn gleich)
                # (hier optional – eigentl. reicht das set im apply)
                pass

    if matched_total == 0:
        stats["checked"] += 1
        return

    stats["matched_files"] += 1

    if changed_total == 0 and not cfg.force:
        stats["already_ok_files"] += 1
        return

    # Speichern
    new_payload = cal.to_ical().decode("utf-8")
    if save_ics(session, abs_href, new_payload, etag, cfg):
        stats["updated_files"] += 1
        return

    # ETag mismatch → noch einmal frisch laden & erneut versuchen
    try:
        fresh_raw, fresh_etag = load_ics(session, abs_href)
    except requests.HTTPError as e:
        log(f"HTTPError on re-GET {abs_href} {e}", level="VERBOSE", cfg=cfg)
        return

    try:
        cal2 = Calendar.from_ical(fresh_raw)
        # Nochmals anwenden (idempotent)
        for comp in cal2.walk():
            if comp.name == "VEVENT":
                apply_rules_to_event(comp, cfg)
        payload2 = cal2.to_ical().decode("utf-8")
        if save_ics(session, abs_href, payload2, fresh_etag, cfg):
            stats["updated_files"] += 1
        else:
            stats["failed_put"] += 1
    except Exception as e:
        log(f"Error on second patch {abs_href} {e}", level="VERBOSE", cfg=cfg)


def main():
    ap = argparse.ArgumentParser(description="Nextcloud CalDAV Formatter")
    ap.add_argument("--base-url", default=ENV_BASE_URL, help="z.B. https://host/remote.php/dav/calendars/<user>/")
    ap.add_argument("--calendar", default=ENV_CAL_NAME, help="Kalenderordner, z.B. outlook-1")
    ap.add_argument("--user", default=ENV_USER, help="Nextcloud Username")
    ap.add_argument("--app-pwd", default=ENV_APP_PWD, help="App-Passwort (Gerätepasswort) für API")
    ap.add_argument("--dry-run", action="store_true", help="Nichts schreiben, nur simulieren")
    ap.add_argument("--verbose", action="store_true", help="ausführlichere Logs")
    ap.add_argument("--debug", action="store_true", help="sehr ausführliche Debug-Logs")
    ap.add_argument("--force", action="store_true", help="immer schreiben, auch wenn identisch")
    ap.add_argument("--limit", type=int, default=0, help="max. Anzahl Kalenderobjekte bearbeiten")
    ap.add_argument("--no-normalize-summary", action="store_true", help="Summary nicht bereinigen (roh matchen)")

    args = ap.parse_args()
    if not args.base_url or not args.calendar or not args.user or not args.app_pwd:
        sys.exit("Fehlende Konfiguration (BASE_URL, CAL_NAME, USER, APP_PWD). Per .env oder CLI übergeben.")

    cfg = Config(
        base_url=args.base_url,
        cal_name=args.calendar,
        user=args.user,
        app_pwd=args.app_pwd,
        dry_run=args.dry_run,
        verbose=args.verbose or args.debug,
        debug=args.debug,
        force=args.force,
        limit=args.limit,
        normalize_summary=(not args.no_normalize_summary),
    )

    # Session
    s = requests.Session()
    s.auth = (cfg.user, cfg.app_pwd)

    # PROPFIND
    try:
        items = propfind_calendar(cfg, s)
    except requests.HTTPError as e:
        sys.exit(f"PROPFIND fehlgeschlagen: {e}")

    log(f"Gefundene Kalenderobjekte: {len(items)}", cfg=cfg)
    if cfg.debug:
        for u, et in items[:5]:
            dbg(f"HREF: {u} (ETag={et})", cfg)

    if cfg.limit > 0:
        items = items[: cfg.limit]

    stats = {
        "checked": 0,
        "matched_files": 0,
        "already_ok_files": 0,
        "updated_files": 0,
        "failed_put": 0,
    }

    for abs_href, etag in items:
        patch_calendar_object(s, abs_href, etag, cfg, stats)

    log(
        f"Done. Checked={stats['checked']} matched_files={stats['matched_files']} "
        f"updated={stats['updated_files']} already_ok={stats['already_ok_files']} failed_put={stats['failed_put']}",
        cfg=cfg,
    )


if __name__ == "__main__":
    main()
