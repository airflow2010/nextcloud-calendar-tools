import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from icalendar import Calendar, Event

# --- Konfiguration ---
# Aus der HAR-Analyse für den Heurigenkalender extrahiert.
API_ENDPOINT_PATH = "events"
API_QUERY_PARAMS = {
    "event-period": "upcoming",
    "scope": "page:66c703b250d3917f19d8fae0",
    "pagination": "limit:50",
}

ICS_FILENAME = "heurigen.ics"
BASE_PAGE_URL = "https://bad-fischau-brunn.at/wirtschaft/heurigenkalender"

# Standardfarbe für die neuen Termine
DEFAULT_EVENT_COLOR = "darkgoldenrod" # Passende Farbe für Heurigen
# --- Ende Konfiguration ---

def get_dynamic_build_version(url):
    """Fragt die Header der Webseite ab und extrahiert die build-version."""
    print(f"Versuche, die aktuelle build-version von {url} abzurufen...")
    try:
        response = requests.head(url, timeout=10)
        response.raise_for_status()
        
        build_version = response.headers.get('build-version')

        if build_version:
            print(f"Aktuelle build-version gefunden: {build_version}")
            return build_version
        else:
            print("WARNUNG: 'build-version' Header nicht in der Antwort gefunden!")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Fehler beim Abrufen der build-version: {e}")
        return None

# 1. Dynamisch die build-version holen
dynamic_build_version = get_dynamic_build_version(BASE_PAGE_URL)

if not dynamic_build_version:
    print("Konnte die build-version nicht ermitteln. Breche Skript ab.")
    sys.exit(1)

# 2. API-Aufruf mit der dynamischen Version
api_base_url = "https://api.v2.citiesapps.com/"
calendar_url = f"{api_base_url}{API_ENDPOINT_PATH}"

headers = {
    'Accept': 'application/json',
    'Origin': 'https://bad-fischau-brunn.at',
    'Referer': 'https://bad-fischau-brunn.at/',
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    'requesting-app': 'website-builder',
    'build-version': dynamic_build_version
}

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def extract_plain_description(event):
    if event.get("plainDescription"):
        return event["plainDescription"]

    doc = event.get("description") or {}
    parts = []

    def walk(node):
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            walk(child)
        if node_type == "paragraph":
            parts.append("\n")

    walk(doc)
    return "".join(parts).strip()


def fetch_all_events():
    events = []
    next_path = None

    while True:
        if next_path:
            request_url = urljoin(api_base_url, next_path.lstrip('/'))
            params = None
            print(f"Folge nextUrl: {next_path}")
        else:
            request_url = calendar_url
            params = API_QUERY_PARAMS
            print(f"\nRufe API auf: {request_url}")
            print(f"Verwende Parameter: {params}")

        try:
            response = requests.get(request_url, headers=headers, params=params, timeout=10)
            print(f"Status Code: {response.status_code}")
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Abrufen der API: {e}")
            if 'response' in locals():
                print(f"Antwort-Header: {response.headers}")
                print(f"Antwort-Text: {response.text}")
            return []

        try:
            payload = response.json()
        except json.JSONDecodeError:
            print("Fehler beim Verarbeiten der JSON-Antwort.")
            print(f"Empfangener Text: {response.text[:500]}")
            return []

        batch = payload.get("data", [])
        events.extend(batch)
        print(f"{len(batch)} Events in diesem Durchlauf, insgesamt {len(events)}.")

        next_path = payload.get("nextUrl")
        if not next_path:
            break

    return events


events_to_process = fetch_all_events()

# --- Verarbeitung der Daten ---
if events_to_process:
    events_to_process.sort(key=lambda x: x.get('startsAt') or x.get('startsAtDate') or "")
else:
    print("Keine Termine im Array 'data' der Antwort gefunden. Bitte Skript prüfen.")

try:
    from zoneinfo import ZoneInfo

    EVENT_TIMEZONE = ZoneInfo("Europe/Vienna")
except Exception:
    EVENT_TIMEZONE = None

print("\n--- Termine zur Kontrolle ---")
if events_to_process:
    for event in events_to_process:
        event_name = event.get("name", "Unbenannter Termin")
        start_str = event.get('startsAt') or event.get('startsAtDate')
        start_dt = parse_iso_datetime(start_str)
        if start_dt and EVENT_TIMEZONE:
            start_dt = start_dt.astimezone(EVENT_TIMEZONE)

        if start_dt:
            if event.get("hasStartTime", True):
                formatted_date = start_dt.strftime('%d.%m.%Y %H:%M')
            else:
                formatted_date = start_dt.strftime('%d.%m.%Y')
        else:
            formatted_date = start_str or "Unbekanntes Datum"

        print(f"{formatted_date}: {event_name}")
else:
    print("Keine Termine zum Anzeigen.")

# ICS-Datei erstellen
print(f"\n--- Erstelle ICS-Datei ({ICS_FILENAME}) ---")
cal = Calendar()
cal.add('prodid', '-//https://github.com/airflow2010/nextcloud-calendar-tools//EN')
cal.add('version', '2.0')
cal.add('calscale', 'GREGORIAN')

ics_event_count = 0

if events_to_process:
    for event_data in events_to_process:
        summary = event_data.get("name", "Termin")
        has_start_time = event_data.get("hasStartTime", True)
        start_str = event_data.get("startsAt" if has_start_time else "startsAtDate") or event_data.get("startsAt") or event_data.get("startsAtDate")
        end_str = event_data.get("endsAt" if event_data.get("hasEndTime") else "endsAtDate") or event_data.get("endsAt") or event_data.get("endsAtDate")

        if not start_str:
            print(f"Überspringe '{summary}', kein Startdatum gefunden.")
            continue

        start_dt = parse_iso_datetime(start_str)
        if not start_dt:
            print(f"Überspringe '{summary}', Startdatum '{start_str}' konnte nicht interpretiert werden.")
            continue

        event = Event()
        event.add('summary', summary)

        if has_start_time:
            event.add('dtstart', start_dt)
            if end_str:
                end_dt = parse_iso_datetime(end_str)
                if end_dt:
                    event.add('dtend', end_dt)
        else:
            event.add('dtstart', start_dt.date())
            end_dt = parse_iso_datetime(end_str) if end_str else None
            if end_dt:
                event.add('dtend', end_dt.date())
            else:
                event.add('dtend', start_dt.date() + timedelta(days=1))

        description_text = extract_plain_description(event_data)
        location_details = event_data.get("locationDetails")
        meetup_url = event_data.get("meetupUrl")
        description_parts = [part.strip() for part in [description_text, location_details] if part]
        if meetup_url:
            description_parts.append(f"Weitere Infos: {meetup_url}")
        if description_parts:
            event.add('description', "\n\n".join(description_parts))

        location = (event_data.get("location") or {}).get("label")
        if not location:
            location = (event_data.get("page", {}).get("address") or {}).get("label")
        if location and location.strip(", "):
            event.add('location', location)

        event_id = event_data.get("_id", str(uuid.uuid4()))
        event.add('uid', f"{event_id}@heurigen.script")
        event.add('dtstamp', datetime.now(timezone.utc))
        event.add('color', DEFAULT_EVENT_COLOR)

        cal.add_component(event)
        ics_event_count += 1

    try:
        with open(ICS_FILENAME, 'wb') as f:
            f.write(cal.to_ical())
        print(f"ICS-Datei '{ICS_FILENAME}' mit {ics_event_count} Terminen erfolgreich erstellt.")
    except IOError as e:
        print(f"Fehler beim Schreiben der ICS-Datei '{ICS_FILENAME}': {e}")
else:
    print("Keine Termine zum Exportieren in ICS-Datei vorhanden.")

if __name__ == "__main__":
    # Das Skript kann direkt ausgeführt werden.
    pass
