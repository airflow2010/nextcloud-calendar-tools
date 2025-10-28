# nextcloud-calendar-tools

Basic tools for pimping my typical tasks in NC calendar

## Funktionen
- Verbindet sich zum NextCloud Kalender
- Nutzt WebDAV/CalDAV API
- Setzt Farben und Verfügbarkeit nach Regeln

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Anwendung

### main.py (Kalendereinträge umfärben)

Das Umfärben der Kalendereinträge erfolgt gemäss den Regeln, die im Script selbst definiert sind (selbsterklärend). Welcher Kalender konkret bearbeitet wird, wird in .env festgelegt.

```bash
(.venv) airflow@AQUARIUS:~/Documents/python-projekte/nextcloud-calendar-tools$ python main.py --debug --verbose
Gefundene Kalenderobjekte: 365
[DBG] HREF: https://<redacted>/remote.php/dav/calendars/airflow/privat/002ca65a-cd72-4895-83a5-4888470d484f.ics (ETag=d5262cf041cd9f9983e45a7bdaf31948)
[DBG] HREF: https://<redacted>/remote.php/dav/calendars/airflow/privat/0123D740-D0A1-4CB1-8778-29CCF601B35B.ics (ETag=d0222d13286aad3d40c8761d3b6f70d4)
Done. Checked=299 matched_files=66 updated=0 already_ok=66 failed_put=0
```

### waste-extraction.py (Termine abrufen)

Dieses separate Script dient ausschließlich der Extrahierung der Termine für die Müllabholung in meiner Straße in meiner Gemeinde. Das Script könnte aber auch leicht an andere Lokationen angepasst werden, solange die Gemeinde ihre Dienste via der citiesapp anbietet.

```bash
(.venv) airflow@AQUARIUS:~/Documents/python-projekte/nextcloud-calendar-tools$ python gemini.py
Rufe API auf: https://api.v2.citiesapps.com/waste-management/areas/6761584e36764e06d7104231/calendar
Status Code: 200

--- Kalender für: Bad Fischau-Brunn, Institutsgasse ---

Filter aktiv für: Restmüll, Papier, Gelber Sack

--- Termine zur Kontrolle ---
05.11.2025 (Wednesday): Papier
06.11.2025 (Thursday): Papier
12.11.2025 (Wednesday): Gelber Sack
21.11.2025 (Friday): Restmüll
10.12.2025 (Wednesday): Gelber Sack
19.12.2025 (Friday): Restmüll
29.12.2025 (Monday): Papier
30.12.2025 (Tuesday): Papier

--- Erstelle ICS-Datei (muelltermine.ics) ---
ICS-Datei 'muelltermine.ics' mit 8 Terminen erfolgreich erstellt.
```