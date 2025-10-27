#!/usr/bin/env python3
import re, sys, requests
from urllib.parse import urljoin
from icalendar import Calendar, Event

# ==== KONFIG ====
BASE_URL   = "https://share.4fp.at/remote.php/dav/calendars/airflow/"
CAL_NAME   = "outlook-1"  # dein Kalender-Ordnername
USER       = "airflow"
APP_PWD    = "**** app password ****"

# Regeln: Liste von (regex, farbe_hex, transparent_bool)
RULES = [
    (r"^Mittagspause$",             "#88cc88", True),
    (r"^Focus( |$)|^Deep\s*Work$",  "#5b9bd5", True),
    (r"^Heartbeat$",                "#ff9900", True),
    # weitere…
]

SESSION = requests.Session()
SESSION.auth = (USER, APP_PWD)

def propfind_calendar():
    """Listet ICS-Objekte im Kalenderverzeichnis (Tiefe 1)."""
    url = urljoin(BASE_URL, CAL_NAME + "/")
    body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:getetag/>
  </d:prop>
</d:propfind>"""
    r = SESSION.request("PROPFIND", url, data=body, headers={
        "Depth": "1", "Content-Type": "application/xml"
    })
    r.raise_for_status()
    # naive Extraktion aller .ics Pfade + ETags
    hrefs = re.findall(r"<d:href>(.*?)</d:href>", r.text)
    etags = re.findall(r"<d:getetag>(.*?)</d:getetag>", r.text)
    items = []
    for h, et in zip(hrefs, etags):
        if h.endswith(".ics"):
            items.append((requests.utils.unquote(h), et.strip('"')))
    return items

def load_ics(abs_href):
    r = SESSION.get(abs_href, headers={"Accept": "text/calendar"})
    r.raise_for_status()
    return r.text, r.headers.get("ETag", "").strip('"')

def save_ics(abs_href, ics_text, etag):
    r = SESSION.put(abs_href, data=ics_text.encode("utf-8"),
                    headers={"If-Match": f"\"{etag}\"",
                             "Content-Type": "text/calendar; charset=utf-8"})
    if r.status_code in (200, 201, 204):
        return True
    print("WARN PUT", r.status_code, r.text[:300])
    return False

def normalize_summary(text):
    # Entfernt die typischen angehängten Steuerstrings (wie wir sie in deiner Datei gesehen haben)
    text = re.sub(r"(TRANSP|X-MICROSOFT-CDO-BUSYSTATUS|STATUS|SEQUENCE|LOCATION|CATEGORIES|CLASS|PRIORITY)[:;=].*$", "", text, flags=re.I)
    text = re.sub(r"\s*(TRANSPARENT|OPAQUE|BUSY|FREE)\s*$", "", text, flags=re.I)
    return text.strip()

def apply_rules_to_event(vevent):
    changed = False
    summary = vevent.get("SUMMARY")
    if not summary:
        return False
    title = normalize_summary(str(summary))

    for pattern, color, make_free in RULES:
        if re.search(pattern, title, flags=re.I):
            # TRANSP setzen
            if make_free:
                if vevent.get("TRANSP") != "TRANSPARENT":
                    vevent["TRANSP"] = "TRANSPARENT"  # frei
                    changed = True
            # COLOR setzen (RFC 7986)
            if vevent.get("COLOR") != color:
                vevent["COLOR"] = color
                changed = True
            break
    return changed

def patch_calendar_object(abs_href, etag):
    raw, server_etag = load_ics(abs_href)
    # ETag prüfen (Race vermeiden)
    etag = etag or server_etag
    cal = Calendar.from_ical(raw)
    changed = False
    for comp in cal.walk():
        if comp.name == "VEVENT":
            if apply_rules_to_event(comp):
                changed = True
    if not changed:
        return False
    new_payload = cal.to_ical().decode("utf-8")
    return save_ics(abs_href, new_payload, etag)

def main():
    items = propfind_calendar()
    touched, skipped = 0, 0
    for href, etag in items:
        abs_href = href if href.startswith("http") else urljoin(BASE_URL, href.lstrip("/"))
        try:
            if patch_calendar_object(abs_href, etag):
                touched += 1
            else:
                skipped += 1
        except requests.HTTPError as e:
            print("HTTPError on", abs_href, e)
        except Exception as e:
            print("Error on", abs_href, e)
    print(f"Done. Updated {touched} events, left {skipped} unchanged.")

if __name__ == "__main__":
    try:
        import icalendar  # noqa: F401
    except ImportError:
        sys.exit("Bitte vorher installieren: pip install icalendar requests")
    main()
