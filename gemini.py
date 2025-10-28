import requests
import json

# Die AREA_ID für "Institutsgasse" (aus dem Trace entnommen)
area_id = "6761584e36764e06d7104231"

# Die URL für den Kalender-Endpunkt
calendar_url = f"https://api.v2.citiesapps.com/waste-management/areas/{area_id}/calendar"

# Notwendige Header (aus dem Trace für den Kalender-API-Aufruf)
# Wichtig: User-Agent, Referer, Origin, requesting-app, build-version
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
    'requesting-app': 'website-builder', # Hinzugefügt (oder sicherstellen, dass es nicht auskommentiert ist)
    'build-version': '20251027142014-91373950b27c9f7611b3b5a8289033960709cdac' # NEU hinzugefügt
}

response = None # Initialisieren für den Fall, dass die Anfrage fehlschlägt
try:
    # Sende die GET-Anfrage
    print(f"Rufe API auf: {calendar_url}")
    response = requests.get(calendar_url, headers=headers)
    print(f"Status Code: {response.status_code}")
    response.raise_for_status() # Löst einen Fehler aus, wenn der Statuscode nicht 2xx ist

    # Lade die JSON-Daten aus der Antwort
    calendar_data = response.json()

    # Gib die Daten aus (oder verarbeite sie weiter)
    print("--- API Antwort (JSON) ---")
    print(json.dumps(calendar_data, indent=2, ensure_ascii=False))

except requests.exceptions.RequestException as e:
    print(f"Fehler beim Abrufen der API: {e}")
    # Bei Fehlern detailliertere Info ausgeben, falls vorhanden
    if response is not None:
        print(f"Antwort-Header: {response.headers}")
        print(f"Antwort-Text: {response.text}")
except json.JSONDecodeError:
    print("Fehler beim Verarbeiten der JSON-Antwort.")
    if response is not None:
        print(f"Empfangener Text: {response.text}")