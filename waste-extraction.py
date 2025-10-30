import requests
import json
from datetime import datetime, timedelta
from icalendar import Calendar, Event, Alarm
import sys # Importiert für sys.exit() bei Fehlern
import uuid # Für eindeutige IDs

# --- Konfiguration ---
area_id = "6761584e36764e06d7104231" # Institutsgasse
filter_types = [] # Leer für alle Typen, z.B. ["Biomüll", "Restmüll"]
#filter_types = ["Restmüll", "Papier", "Gelber Sack"] # FP needs
ics_filename = "muelltermine.ics"
base_page_url = "https://bad-fischau-brunn.at/waste-management/areas" # URL der Webseite

# Farben für die Müllarten (Hex-Codes oder Farbnamen)
WASTE_TYPE_COLORS = {
    "Restmüll": "dimgrey",          # Dunkelgrau
    "Papier": "floralwhite",        # Papierweiß
    "Gelber Sack": "gold",          # Gelb
    "Biomüll": "saddlebrown",       # Braun
    "DEFAULT": "black"              # Schwarz
}
# --- Ende Konfiguration ---

def get_dynamic_build_version(url):
    """Fragt die Header der Webseite ab und extrahiert die build-version."""
    print(f"Versuche, die aktuelle build-version von {url} abzurufen...")
    try:
        # HEAD-Anfrage, um nur Header zu bekommen
        response = requests.head(url, timeout=10) # Timeout hinzugefügt
        response.raise_for_status()
        
        # Header extrahieren (Groß-/Kleinschreibung ignorieren)
        build_version = None
        for key, value in response.headers.items():
             if key.lower() == 'build-version':
                  build_version = value
                  break # Sobald gefunden, Schleife verlassen

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
dynamic_build_version = get_dynamic_build_version(base_page_url)

if not dynamic_build_version:
    print("Konnte die build-version nicht ermitteln. Breche Skript ab.")
    # Fallback auf eine bekannte Version (optional, falls gewünscht)
    # print("Verwende Fallback build-version.")
    # dynamic_build_version = '20251027142014-91373950b27c9f7611b3b5a8289033960709cdac' 
    sys.exit(1) # Beendet das Skript

# 2. API-Aufruf mit der dynamischen Version
calendar_url = f"https://api.v2.citiesapps.com/waste-management/areas/{area_id}/calendar"
headers = {
    'Accept': 'application/json',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://bad-fischau-brunn.at',
    'Referer': 'https://bad-fischau-brunn.at/',
    'Sec-Ch-Ua': '"Not=A?Brand";v="24", "Chromium";v="140"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Linux"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'cross-site',
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    'requesting-app': 'website-builder',
    'build-version': dynamic_build_version # Dynamischen Wert verwenden!
}

response = None
calendar_data = None

try:
    print(f"\nRufe API auf: {calendar_url}")
    response = requests.get(calendar_url, headers=headers)
    print(f"Status Code: {response.status_code}")
    response.raise_for_status()
    calendar_data = response.json()

except requests.exceptions.RequestException as e:
    print(f"Fehler beim Abrufen der API: {e}")
    if response is not None:
        print(f"Antwort-Header: {response.headers}")
        print(f"Antwort-Text: {response.text}")
except json.JSONDecodeError:
    print("Fehler beim Verarbeiten der JSON-Antwort.")
    if response is not None:
        print(f"Empfangener Text: {response.text}")

# --- Verarbeitung der Daten (Rest des Skripts bleibt gleich) ---
if calendar_data:
    # Ort und Straße anzeigen
    street = calendar_data.get("street", "Unbekannte Straße")
    location = f"Bad Fischau-Brunn, {street}" # Annahme Ort
    print(f"\n--- Kalender für: {location} ---")

    # Termine filtern und aufbereiten
    events_to_process = []
    if "garbageCollectionDays" in calendar_data:
        all_events = calendar_data["garbageCollectionDays"]
        
        if filter_types:
             print(f"\nFilter aktiv für: {', '.join(filter_types)}")
             filtered_events = [
                 event for event in all_events
                 if event.get("garbageTypeSettings", {}).get("displayName") in filter_types
             ]
        else:
             print("\nKein Filter aktiv, zeige alle Müllarten.")
             filtered_events = all_events
        
        filtered_events.sort(key=lambda x: x.get('date', ''))
        events_to_process = filtered_events
    else:
        print("Keine Termine (garbageCollectionDays) in der Antwort gefunden.")

    # Termine übersichtlich anzeigen
    print("\n--- Termine zur Kontrolle ---")
    if events_to_process:
        for event in events_to_process:
            date_str = event.get('date')
            event_name = event.get("garbageTypeSettings", {}).get("displayName", "Unbekannter Typ")
            try:
                date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                formatted_date = date_obj.strftime('%d.%m.%Y (%A)')
                print(f"{formatted_date}: {event_name}")
            except (ValueError, TypeError):
                print(f"{date_str}: {event_name} (Datumsformat unbekannt)")
    else:
        print("Keine Termine zum Anzeigen (nach Filterung).")

    # ICS-Datei erstellen
    print(f"\n--- Erstelle ICS-Datei ({ics_filename}) ---")
    cal = Calendar()
    # Notwendige Standard-Properties für den Kalender selbst
    cal.add('prodid', '-//My Waste Calendar Script//EN')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')

    ics_event_count = 0
    if events_to_process:
        for event_data in events_to_process:
            date_str = event_data.get('date')
            summary = event_data.get("garbageTypeSettings", {}).get("displayName", "Müllabholung")
            try:
                start_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                event = Event()
                event.add('summary', summary)
                event.add('dtstart', start_dt.date())
                event.add('dtend', start_dt.date() + timedelta(days=1))
                # WICHTIG: Eindeutige ID (UID) und Zeitstempel (DTSTAMP) hinzufügen
                # UID wird aus Datum und Müllart generiert, um bei erneutem Lauf stabil zu sein
                event.add('uid', f"{start_dt.strftime('%Y%m%d')}-{summary.replace(' ', '-')}@waste.script")
                event.add('dtstamp', datetime.now())
                # Farbe basierend auf dem Typ (summary) hinzufügen
                color = WASTE_TYPE_COLORS.get(summary, WASTE_TYPE_COLORS["DEFAULT"])
                event.add('color', color)

                # NEU: Erinnerung hinzufügen (18:00 am Vortag)
                alarm = Alarm()
                alarm.add('action', 'DISPLAY')
                alarm.add('description', f"Müll rausstellen: {summary}")
                alarm.add('trigger', timedelta(hours=-6)) # 6 Stunden VOR dem Start (00:00) -> 18:00 am Vortag
                event.add_component(alarm)

                cal.add_component(event)
                ics_event_count += 1
            except (ValueError, TypeError) as err:
                 print(f"Fehler beim Erstellen des Kalendereintrags für {summary} am {date_str}: {err}")
        try:
            with open(ics_filename, 'wb') as f:
                f.write(cal.to_ical())
            print(f"ICS-Datei '{ics_filename}' mit {ics_event_count} Terminen erfolgreich erstellt.")
        except IOError as e:
            print(f"Fehler beim Schreiben der ICS-Datei '{ics_filename}': {e}")
    else:
        print("Keine Termine zum Exportieren in ICS-Datei vorhanden.")
else:
    print("\nKonnte keine Daten von der API abrufen, Verarbeitung abgebrochen.")