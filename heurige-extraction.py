import argparse
import json
import os
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from icalendar import Calendar, Event

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# --- Konfiguration ---
# Aus der HAR-Analyse fuer den Heurigenkalender extrahiert.
API_ENDPOINT_PATH = "events"
API_QUERY_PARAMS = {
    "event-period": "upcoming",
    "scope": "page:66c703b250d3917f19d8fae0",
    "pagination": "limit:50",
}

ICS_FILENAME = "heurigen.ics"
BASE_PAGE_URL = "https://bad-fischau-brunn.at/wirtschaft/heurigenkalender"
BUILD_VERSION_REQUEST_TIMEOUT = (5, 20)
BUILD_VERSION_MAX_ATTEMPTS = 3
API_BASE_URL = "https://api.v2.citiesapps.com/"

ENV_BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
if ENV_BASE_URL:
    ENV_BASE_URL += "/"
ENV_CAL_WASTE = os.getenv("CAL_WASTE", "")
ENV_USER = os.getenv("USER", "")
ENV_APP_PWD = os.getenv("APP_PWD", "")

try:
    from zoneinfo import ZoneInfo

    EVENT_TIMEZONE = ZoneInfo("Europe/Vienna")
except Exception:
    EVENT_TIMEZONE = None

# --- Ende Konfiguration ---


@dataclass
class CalDavConfig:
    base_url: str
    cal_name: str
    user: str
    app_pwd: str
    verbose: bool = False


@dataclass(frozen=True)
class EventKey:
    summary: str
    dtstart: str
    dtend: str


@dataclass
class ComparableEvent:
    key: EventKey
    summary: str
    period: str
    start_value: object
    end_value: object
    location: str
    uid: str
    source: str


def get_header_case_insensitive(headers, header_name):
    for key, value in headers.items():
        if key.lower() == header_name.lower():
            return value
    return None


def get_dynamic_build_version(url):
    """Ermittelt die build-version robust per HEAD und GET mit Retries."""
    print(f"Versuche, die aktuelle build-version von {url} abzurufen...")
    last_error = None

    for attempt in range(1, BUILD_VERSION_MAX_ATTEMPTS + 1):
        for method in ("HEAD", "GET"):
            print(f"Versuch {attempt}/{BUILD_VERSION_MAX_ATTEMPTS} via {method}...")
            try:
                response = requests.request(
                    method,
                    url,
                    timeout=BUILD_VERSION_REQUEST_TIMEOUT,
                    allow_redirects=True,
                )
                response.raise_for_status()

                build_version = get_header_case_insensitive(response.headers, "build-version")
                if build_version:
                    print(f"Aktuelle build-version gefunden: {build_version}")
                    return build_version

                print(f"WARNUNG: 'build-version' Header bei {method} nicht gefunden.")
            except requests.exceptions.RequestException as e:
                last_error = e
                print(f"Fehler bei {method} fuer build-version: {e}")

        if attempt < BUILD_VERSION_MAX_ATTEMPTS:
            time.sleep(attempt)

    if last_error:
        print(f"Letzter Fehler beim Abrufen der build-version: {last_error}")
    return None


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def build_heurigen_headers(dynamic_build_version):
    return {
        "Accept": "application/json",
        "Origin": "https://bad-fischau-brunn.at",
        "Referer": "https://bad-fischau-brunn.at/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "requesting-app": "website-builder",
        "build-version": dynamic_build_version,
    }


def fetch_all_events(headers):
    events = []
    next_path = None
    calendar_url = f"{API_BASE_URL}{API_ENDPOINT_PATH}"

    while True:
        if next_path:
            request_url = urljoin(API_BASE_URL, next_path.lstrip("/"))
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
            if "response" in locals():
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


def print_events_for_control(events_to_process):
    print("\n--- Termine zur Kontrolle ---")
    if not events_to_process:
        print("Keine Termine zum Anzeigen.")
        return

    for event in events_to_process:
        event_name = event.get("name", "Unbenannter Termin").removeprefix("Ausgsteckt is: ").strip()
        start_str = event.get("startsAt") or event.get("startsAtDate")
        start_dt = parse_iso_datetime(start_str)
        if start_dt and EVENT_TIMEZONE:
            start_dt = start_dt.astimezone(EVENT_TIMEZONE)

        if start_dt:
            if event.get("hasStartTime", True):
                formatted_date = start_dt.strftime("%d.%m.%Y %H:%M")
            else:
                formatted_date = start_dt.strftime("%d.%m.%Y")
        else:
            formatted_date = start_str or "Unbekanntes Datum"

        print(f"{formatted_date}: {event_name}")


def write_ics(events_to_process, ics_path):
    print(f"\n--- Erstelle ICS-Datei ({ics_path}) ---")
    cal = Calendar()
    cal.add("prodid", "-//https://github.com/airflow2010/nextcloud-calendar-tools//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    ics_event_count = 0

    if not events_to_process:
        print("Keine Termine zum Exportieren in ICS-Datei vorhanden.")
        return

    for event_data in events_to_process:
        summary = event_data.get("name", "Unbenannter Termin").removeprefix("Ausgsteckt is: ").strip()
        start_str = event_data.get("startsAt") or event_data.get("startsAtDate")
        end_str = event_data.get("endsAt") or event_data.get("endsAtDate")

        if not start_str:
            print(f"Ueberspringe '{summary}', kein Startdatum gefunden.")
            continue

        start_dt = parse_iso_datetime(start_str)
        end_dt = parse_iso_datetime(end_str) if end_str else None

        if not start_dt:
            print(f"Ueberspringe '{summary}', Startdatum '{start_str}' konnte nicht interpretiert werden.")
            continue

        event = Event()
        event.add("summary", summary)
        event.add("dtstart", start_dt.date())

        # Fuer Ganztagstermine ist das Enddatum exklusiv.
        # Wenn ein Event am 15. endet, muss dtend der 16. sein.
        if end_dt:
            event.add("dtend", end_dt.date() + timedelta(days=1))
        else:
            # Wenn kein Enddatum da ist, dauert es genau einen Tag.
            event.add("dtend", start_dt.date() + timedelta(days=1))

        description_text = extract_plain_description(event_data)
        location_details = event_data.get("locationDetails")
        meetup_url = event_data.get("meetupUrl")
        description_parts = [part.strip() for part in [description_text, location_details] if part]
        if meetup_url:
            description_parts.append(f"Weitere Infos: {meetup_url}")
        if description_parts:
            event.add("description", "\n\n".join(description_parts))

        location = (event_data.get("location") or {}).get("label")
        if not location:
            location = (event_data.get("page", {}).get("address") or {}).get("label")
        if location and location.strip(", "):
            event.add("location", location)

        event_id = event_data.get("_id", str(uuid.uuid4()))
        event.add("uid", f"{event_id}@heurigen.script")
        event.add("dtstamp", datetime.now(timezone.utc))

        cal.add_component(event)
        ics_event_count += 1

    try:
        with open(ics_path, "wb") as f:
            f.write(cal.to_ical())
        print(f"ICS-Datei '{ics_path}' mit {ics_event_count} Terminen erfolgreich erstellt.")
    except IOError as e:
        print(f"Fehler beim Schreiben der ICS-Datei '{ics_path}': {e}")


def generate_heurigen_ics(ics_path):
    dynamic_build_version = get_dynamic_build_version(BASE_PAGE_URL)

    if not dynamic_build_version:
        print("Konnte die build-version nicht ermitteln. Breche Skript ab.")
        sys.exit(1)

    events_to_process = fetch_all_events(build_heurigen_headers(dynamic_build_version))

    if events_to_process:
        events_to_process.sort(key=lambda x: x.get("startsAt") or x.get("startsAtDate") or "")
    else:
        print("Keine Termine im Array 'data' der Antwort gefunden. Bitte Skript pruefen.")

    print_events_for_control(events_to_process)
    write_ics(events_to_process, ics_path)


def build_origin(base_url):
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def propfind_calendar(cfg, session):
    """Listet ICS-Objekte im Kalender (Tiefe 1) und liefert (absolute_href, etag)."""
    url = urljoin(cfg.base_url, cfg.cal_name.strip("/") + "/")

    body = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:getetag/>
    <d:getcontenttype/>
  </d:prop>
</d:propfind>"""

    headers = {"Depth": "1", "Content-Type": "application/xml"}
    response = session.request("PROPFIND", url, data=body, headers=headers)
    response.raise_for_status()

    ns = {"d": "DAV:"}
    root = ET.fromstring(response.text)
    origin = build_origin(cfg.base_url)
    items = []

    for resp in root.findall("d:response", ns):
        href = resp.findtext("d:href", default="", namespaces=ns) or ""
        prop = resp.find("d:propstat/d:prop", ns)
        if prop is None:
            continue

        ctype = (prop.findtext("d:getcontenttype", default="", namespaces=ns) or "").lower()
        etag = (prop.findtext("d:getetag", default="", namespaces=ns) or "").strip('"')

        if href.endswith(".ics") or "text/calendar" in ctype:
            if href.startswith("http"):
                abs_href = href
            else:
                abs_href = origin + (href if href.startswith("/") else "/" + href)
            items.append((abs_href, etag))

    return items


def load_ics(session, abs_href):
    response = session.get(abs_href, headers={"Accept": "text/calendar"})
    response.raise_for_status()
    return response.text


def decoded_property(vevent, prop_name):
    try:
        return vevent.decoded(prop_name)
    except Exception:
        return None


def normalize_summary_for_key(summary):
    return " ".join(summary.split()).casefold()


def is_midnight(value):
    return isinstance(value, datetime) and value.timetz().replace(tzinfo=None) == datetime_time.min


def canonical_temporal(value):
    if value is None:
        return ""

    if isinstance(value, datetime):
        if value.tzinfo and EVENT_TIMEZONE:
            value = value.astimezone(EVENT_TIMEZONE)
        value = value.replace(microsecond=0)
        if is_midnight(value):
            return f"DATE:{value.date().isoformat()}"
        return f"DATE-TIME:{value.isoformat()}"

    if isinstance(value, date):
        return f"DATE:{value.isoformat()}"

    return str(value)


def format_date(value):
    return value.strftime("%d.%m.%Y")


def format_temporal(value):
    if isinstance(value, datetime):
        if value.tzinfo and EVENT_TIMEZONE:
            value = value.astimezone(EVENT_TIMEZONE)
        if is_midnight(value):
            return format_date(value.date())
        return value.strftime("%d.%m.%Y %H:%M")

    if isinstance(value, date):
        return format_date(value)

    return str(value) if value is not None else "ohne Datum"


def format_period(start_value, end_value):
    if isinstance(start_value, date) and not isinstance(start_value, datetime):
        if isinstance(end_value, date) and not isinstance(end_value, datetime):
            if end_value == start_value + timedelta(days=1):
                return format_temporal(start_value)
            return f"{format_temporal(start_value)} bis {format_temporal(end_value - timedelta(days=1))}"

    if end_value:
        return f"{format_temporal(start_value)} bis {format_temporal(end_value)}"

    return format_temporal(start_value)


def default_end_value(start_value, end_value, duration_value):
    if end_value is not None:
        return end_value

    if duration_value is not None and start_value is not None:
        try:
            return start_value + duration_value
        except TypeError:
            pass

    if isinstance(start_value, date) and not isinstance(start_value, datetime):
        return start_value + timedelta(days=1)

    return None


def comparable_event_from_vevent(vevent, source):
    summary = " ".join(str(vevent.get("SUMMARY", "(ohne Titel)")).split())
    start_value = decoded_property(vevent, "DTSTART")
    end_value = default_end_value(
        start_value,
        decoded_property(vevent, "DTEND"),
        decoded_property(vevent, "DURATION"),
    )
    location = " ".join(str(vevent.get("LOCATION", "")).split())
    uid = str(vevent.get("UID", ""))
    key = EventKey(
        summary=normalize_summary_for_key(summary),
        dtstart=canonical_temporal(start_value),
        dtend=canonical_temporal(end_value),
    )

    return ComparableEvent(
        key=key,
        summary=summary,
        period=format_period(start_value, end_value),
        start_value=start_value,
        end_value=end_value,
        location=location,
        uid=uid,
        source=source,
    )


def events_from_calendar(cal, source):
    events = []
    for component in cal.walk():
        if component.name == "VEVENT":
            events.append(comparable_event_from_vevent(component, source))
    return events


def load_local_events(ics_path):
    path = Path(ics_path)
    if not path.exists():
        sys.exit(f"Lokale ICS-Datei nicht gefunden: {path}")

    try:
        cal = Calendar.from_ical(path.read_bytes())
    except Exception as e:
        sys.exit(f"Lokale ICS-Datei konnte nicht gelesen werden: {e}")

    return events_from_calendar(cal, str(path))


def load_online_events(cfg):
    session = requests.Session()
    session.auth = (cfg.user, cfg.app_pwd)

    try:
        items = propfind_calendar(cfg, session)
    except requests.HTTPError as e:
        sys.exit(f"PROPFIND fehlgeschlagen: {e}")
    except ET.ParseError as e:
        sys.exit(f"PROPFIND-Antwort konnte nicht gelesen werden: {e}")

    if cfg.verbose:
        print(f"Gefundene Kalenderobjekte online: {len(items)}")

    events = []
    for index, (abs_href, _etag) in enumerate(items, start=1):
        if cfg.verbose:
            print(f"Lade Online-ICS {index}/{len(items)}: {abs_href}")
        try:
            raw_ics = load_ics(session, abs_href)
            cal = Calendar.from_ical(raw_ics)
            events.extend(events_from_calendar(cal, abs_href))
        except requests.HTTPError as e:
            print(f"WARNUNG: Online-ICS konnte nicht geladen werden ({abs_href}): {e}")
        except Exception as e:
            print(f"WARNUNG: Online-ICS konnte nicht gelesen werden ({abs_href}): {e}")

    return events


def group_by_key(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event.key].append(event)
    return grouped


def diff_events(left_events, right_events):
    right_counter = Counter(event.key for event in right_events)
    left_only = []

    for key, events in group_by_key(left_events).items():
        missing_count = len(events) - right_counter[key]
        if missing_count > 0:
            left_only.extend(events[:missing_count])

    return sorted(left_only, key=lambda event: (event.key.dtstart, event.summary, event.key.dtend))


def event_years(event):
    if event.start_value is None:
        return set()

    start_date = event.start_value.date() if isinstance(event.start_value, datetime) else event.start_value
    if not isinstance(start_date, date):
        return set()

    end_value = event.end_value
    if isinstance(end_value, datetime):
        end_date = end_value.date()
    elif isinstance(end_value, date):
        end_date = end_value
        if end_date > start_date:
            end_date -= timedelta(days=1)
    else:
        end_date = start_date

    if end_date < start_date:
        end_date = start_date

    return set(range(start_date.year, end_date.year + 1))


def event_start_year(event):
    if isinstance(event.start_value, datetime):
        return event.start_value.year

    if isinstance(event.start_value, date):
        return event.start_value.year

    return None


def relevant_years_from_events(events):
    years = set()
    for event in events:
        year = event_start_year(event)
        if year:
            years.add(year)
    return years


def filter_events_for_years(events, years):
    return [event for event in events if event_years(event) & years]


def format_years(years):
    return ", ".join(str(year) for year in sorted(years))


def print_event_list(title, events):
    print(f"\n--- {title} ({len(events)}) ---")
    if not events:
        print("Keine.")
        return

    for event in events:
        print(f"- {event.period}: {event.summary}")


def compare_with_online_calendar(ics_path, cfg, year=None):
    print(f"Lade lokale ICS-Datei: {ics_path}")
    local_events = load_local_events(ics_path)
    print(f"Lade CalDAV-Kalender: {cfg.cal_name}")
    online_events = load_online_events(cfg)

    relevant_years = {year} if year else relevant_years_from_events(local_events)
    if not relevant_years:
        sys.exit("Kein Vergleichsjahr gefunden: lokale ICS-Datei enthaelt keine datierten Termine.")

    local_events = filter_events_for_years(local_events, relevant_years)
    online_events = filter_events_for_years(online_events, relevant_years)

    only_local = diff_events(local_events, online_events)
    only_online = diff_events(online_events, local_events)

    print("\n--- Vergleich heurigen.ics <-> CalDAV ---")
    print(f"Vergleichsjahr: {format_years(relevant_years)}")
    print(f"Termine im Letztstand: {len(local_events)}")
    print(f"Termine im Online-Altstand: {len(online_events)}")
    print_event_list("Neu hinzugekommene Termine", only_local)
    print_event_list("Abgesagte Termine", only_online)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Heurigenkalender exportieren oder mit CalDAV vergleichen")
    parser.add_argument("--compare", action="store_true", help="lokale ICS-Datei mit dem CAL_WASTE-CalDAV-Kalender vergleichen")
    parser.add_argument("--ics-file", default=ICS_FILENAME, help=f"lokale ICS-Datei (Default: {ICS_FILENAME})")
    parser.add_argument("--base-url", default=ENV_BASE_URL, help="z.B. https://host/remote.php/dav/calendars/<user>/")
    parser.add_argument("--calendar", default=ENV_CAL_WASTE, help="Kalenderordner fuer den Vergleich (Default: CAL_WASTE)")
    parser.add_argument("--user", default=ENV_USER, help="Nextcloud Username")
    parser.add_argument("--app-pwd", default=ENV_APP_PWD, help="App-Passwort (Geraetepasswort) fuer API")
    parser.add_argument("--year", type=int, help="Vergleich auf dieses Kalenderjahr einschraenken")
    parser.add_argument("--verbose", action="store_true", help="ausfuehrlichere Logs beim Vergleich")
    return parser


def main():
    args = build_arg_parser().parse_args()
    ics_path = Path(args.ics_file)

    if args.compare:
        if not args.base_url or not args.calendar or not args.user or not args.app_pwd:
            sys.exit("Fehlende Konfiguration (BASE_URL, CAL_WASTE, USER, APP_PWD). Per .env oder CLI uebergeben.")

        cfg = CalDavConfig(
            base_url=args.base_url.rstrip("/") + "/",
            cal_name=args.calendar,
            user=args.user,
            app_pwd=args.app_pwd,
            verbose=args.verbose,
        )
        compare_with_online_calendar(ics_path, cfg, args.year)
        return

    generate_heurigen_ics(ics_path)


if __name__ == "__main__":
    main()
