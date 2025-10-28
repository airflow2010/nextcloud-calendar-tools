import requests
import json
from datetime import datetime, timedelta
from ics import Calendar, Event # Importiere die ICS-Bibliothek

# --- Konfiguration ---
# Die AREA_ID für "Institutsgasse" (aus dem Trace entnommen)
area_id = "6761584e36764e06d7104231"

# Filtere Müllarten (Liste der anzuzeigenden/exportierenden Typen)
# Lasse die Liste leer [], um alle Typen anzuzeigen/exportieren.
# Beispiele: filter_types = ["Biomüll", "Restmüll"]
#            filter_types = ["Gelber Sack"]
# filter_types = [] # Zeige/Exportiere alle Typen
filter_types = ["Restmüll", "Papier", "Gelber Sack"] # Zeige/Exportiere alle Typen

# Name der ICS-Datei
ics_filename = "muelltermine.ics"
# --- Ende Konfiguration ---


# Die URL für den Kalender-Endpunkt
calendar_url = f"https://api.v2.citiesapps.com/waste-management/areas/{area_id}/calendar"

# Notwendige Header (aus dem Trace für den Kalender-API-Aufruf)
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
    'build-version': '20251027142014-91373950b27c9f7611b3b5a8289033960709cdac'
}

response = None # Initialisieren
calendar_data = None

try:
    # Sende die GET-Anfrage
    print(f"Rufe API auf: {calendar_url}")
    response = requests.get(calendar_url, headers=headers)
    print(f"Status Code: {response.status_code}")
    response.raise_for_status()

    # Lade die JSON-Daten
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

# --- Verarbeitung der Daten (nur wenn erfolgreich) ---
if calendar_data:
    # 1) Ort und Straße anzeigen
    street = calendar_data.get("street", "Unbekannte Straße")
    # Ort ist nicht direkt in dieser Antwort, nehmen wir Bad Fischau-Brunn an
    location = f"Bad Fischau-Brunn, {street}"
    print(f"\n--- Kalender für: {location} ---")

    # Termine filtern und aufbereiten
    events_to_process = []
    if "garbageCollectionDays" in calendar_data:
        all_events = calendar_data["garbageCollectionDays"]
        
        # Filtern
        if filter_types:
             print(f"\nFilter aktiv für: {', '.join(filter_types)}")
             filtered_events = [
                 event for event in all_events
                 if event.get("garbageTypeSettings", {}).get("displayName") in filter_types
             ]
        else:
             print("\nKein Filter aktiv, zeige alle Müllarten.")
             filtered_events = all_events
        
        # Sortieren nach Datum
        filtered_events.sort(key=lambda x: x.get('date', ''))
        
        events_to_process = filtered_events
    else:
        print("Keine Termine (garbageCollectionDays) in der Antwort gefunden.")

    # 2) Termine übersichtlich anzeigen
    print("\n--- Termine zur Kontrolle ---")
    if events_to_process:
        for event in events_to_process:
            date_str = event.get('date')
            event_name = event.get("garbageTypeSettings", {}).get("displayName", "Unbekannter Typ")
            
            # Datum lesbarer formatieren
            try:
                date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                # Annahme: Termine sind ganztägig, nur Datum anzeigen
                formatted_date = date_obj.strftime('%d.%m.%Y (%A)')
                print(f"{formatted_date}: {event_name}")
            except (ValueError, TypeError):
                print(f"{date_str}: {event_name} (Datumsformat unbekannt)")
    else:
        print("Keine Termine zum Anzeigen (nach Filterung).")

    # 4) ICS-Datei erstellen
    print(f"\n--- Erstelle ICS-Datei ({ics_filename}) ---")
    cal = Calendar()
    ics_event_count = 0
    if events_to_process:
        for event_data in events_to_process:
            date_str = event_data.get('date')
            summary = event_data.get("garbageTypeSettings", {}).get("displayName", "Müllabholung")

            try:
                # Datumsobjekt erstellen (UTC)
                start_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))

                # ICS-Event erstellen (als ganztägiges Ereignis)
                e = Event()
                e.name = summary
                e.begin = start_dt.date() # Nur das Datum verwenden für ganztägig
                e.make_all_day() # Als ganztägiges Event markieren
                cal.events.add(e)
                ics_event_count += 1

            except (ValueError, TypeError) as err:
                 print(f"Fehler beim Erstellen des Kalendereintrags für {summary} am {date_str}: {err}")

        # ICS-Datei speichern
        try:
            with open(ics_filename, 'w', encoding='utf-8') as f:
                f.writelines(cal.serialize_iter())
            print(f"ICS-Datei '{ics_filename}' mit {ics_event_count} Terminen erfolgreich erstellt.")
        except IOError as e:
            print(f"Fehler beim Schreiben der ICS-Datei '{ics_filename}': {e}")
    else:
        print("Keine Termine zum Exportieren in ICS-Datei vorhanden.")

else:
    print("\nKonnte keine Daten von der API abrufen, Verarbeitung abgebrochen.")