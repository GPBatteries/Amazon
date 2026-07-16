"""
Haalt de dagelijkse sales/traffic-cijfers op via de SP-API en zet ze in
output/spapi_history.csv, in hetzelfde format als de Google Sheet-route
(kolommen: date, childAsin, unitsOrdered, orderedProductSales, unitSessionPercentage).

Waar de data vandaan komt:
  Amazon Reports API (2021-06-30), reportType GET_SALES_AND_TRAFFIC_REPORT.
  Dit is het rapport dat units, sales EN sessions/CVR per ASIN per dag bevat --
  dezelfde cijfers die tot nu toe uit de Google Sheet "Daily IMP" kwamen.

Hoe het werkt (SP-API rapporten zijn altijd asynchroon):
  1. POST /reports/2021-06-30/reports  -> vraag een rapport aan voor 1 dag
  2. GET  /reports/2021-06-30/reports/{reportId}  -> pollen tot status DONE
  3. GET  /reports/2021-06-30/documents/{reportDocumentId}  -> download-url ophalen
  4. Download + (indien nodig) gunzip + JSON parsen

Dit script haalt standaard de data van GISTEREN op (UTC) en voegt die ene
rij toe aan de historie-CSV. Draai het dagelijks (net als de Google Sheet-route
dat deed), zodat de historie stap voor stap opgebouwd wordt.

Voor een eenmalige achterstand ophalen: geef --start en --end mee (YYYY-MM-DD),
dan wordt er per dag in die periode een los rapport opgevraagd (let op: dit
kan traag zijn en tegen rate limits aanlopen bij een lange periode).
"""
import os
import io
import csv
import sys
import time
import gzip
import argparse
import datetime as dt

import requests

# ----------------------------------------------------------------------------
# Config -- zelfde ASIN/marketplace als update_amazon.py
# ----------------------------------------------------------------------------
ASIN = "B00CO00Y32"
MARKETPLACE_ID = "A1F83G8C2ARO7P"  # UK
BASE_URL = "https://sellingpartnerapi-eu.amazon.com"  # regio bevestigd via sp_api_test.py

HISTORY_CSV = os.path.join("output", "spapi_history.csv")
FIELDNAMES = ["date", "childAsin", "unitsOrdered", "orderedProductSales", "unitSessionPercentage"]

CID = os.environ["LWA_CLIENT_ID"].strip()
CS = os.environ["LWA_CLIENT_SECRET"].strip()
RT = os.environ["SPAPI_REFRESH_TOKEN"].strip()


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------
def get_access_token() -> str:
    r = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": RT,
            "client_id": CID,
            "client_secret": CS,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ----------------------------------------------------------------------------
# Rapport aanvragen, pollen, downloaden
# ----------------------------------------------------------------------------
def request_report(access_token: str, day: dt.date) -> str:
    """Vraagt het Sales & Traffic rapport aan voor 1 kalenderdag (UTC). Geeft reportId terug."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)

    body = {
        "reportType": "GET_SALES_AND_TRAFFIC_REPORT",
        "marketplaceIds": [MARKETPLACE_ID],
        "dataStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataEndTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reportOptions": {
            "dateGranularity": "DAY",
            "asinGranularity": "CHILD",
        },
    }
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            f"{BASE_URL}/reports/2021-06-30/reports",
            headers={"x-amz-access-token": access_token, "content-type": "application/json"},
            json=body,
            timeout=30,
        )
        if r.status_code == 202:
            return r.json()["reportId"]
        if r.status_code == 429 and attempt < max_attempts:
            wait = _retry_wait(r, attempt)
            print(f"   Rate limit (429) bij aanvragen rapport {day}. "
                  f"Poging {attempt}/{max_attempts}, {wait}s wachten en opnieuw proberen...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Rapport aanvragen mislukt ({day}): HTTP {r.status_code}: {r.text[:300]}")
    raise RuntimeError(f"Rapport aanvragen bleef 429 geven voor {day} na {max_attempts} pogingen.")


def _retry_wait(response: requests.Response, attempt: int) -> int:
    """Retry-After header respecteren indien aanwezig, anders exponentiële backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(int(float(retry_after)), 5)
        except ValueError:
            pass
    return min(30 * attempt, 180)  # 30s, 60s, 90s, ... max 180s


def poll_report(access_token: str, report_id: str, timeout_s: int = 300) -> str:
    """Wacht tot het rapport klaar is. Geeft reportDocumentId terug."""
    deadline = time.time() + timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        r = requests.get(
            f"{BASE_URL}/reports/2021-06-30/reports/{report_id}",
            headers={"x-amz-access-token": access_token},
            timeout=30,
        )
        if r.status_code == 429:
            wait = _retry_wait(r, attempt)
            print(f"   Rate limit (429) bij pollen rapport. {wait}s wachten...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        status = data.get("processingStatus")
        if status == "DONE":
            return data["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"Rapport genereren mislukt: status={status}")
        time.sleep(10)
    raise RuntimeError("Timeout: rapport was na 5 minuten nog niet klaar.")


def download_report(access_token: str, report_document_id: str) -> dict:
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        r = requests.get(
            f"{BASE_URL}/reports/2021-06-30/documents/{report_document_id}",
            headers={"x-amz-access-token": access_token},
            timeout=30,
        )
        if r.status_code == 429 and attempt < max_attempts:
            wait = _retry_wait(r, attempt)
            print(f"   Rate limit (429) bij ophalen document-url. {wait}s wachten...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        break
    doc = r.json()
    url = doc["url"]
    compression = doc.get("compressionAlgorithm")

    file_r = requests.get(url, timeout=60)
    file_r.raise_for_status()
    raw = file_r.content
    if compression == "GZIP":
        raw = gzip.decompress(raw)

    import json
    return json.loads(raw.decode("utf-8"))


# ----------------------------------------------------------------------------
# Rapport-JSON omzetten naar 1 rij (date, childAsin, unitsOrdered, orderedProductSales, unitSessionPercentage)
# ----------------------------------------------------------------------------
def extract_row(report_json: dict, day: dt.date) -> dict | None:
    """
    Let op: de exacte structuur van dit rapport (veldnamen binnen salesAndTrafficByAsin)
    kan per Amazon API-versie licht afwijken. Print bij twijfel report_json eenmalig
    volledig (bv. via de --debug-json vlag) en vergelijk met de officiële SP-API docs
    voor GET_SALES_AND_TRAFFIC_REPORT, en pas de keys hieronder aan indien nodig.
    """
    rows = report_json.get("salesAndTrafficByAsin", [])
    for entry in rows:
        if entry.get("childAsin") != ASIN:
            continue
        sales = entry.get("salesByAsin", {})
        traffic = entry.get("trafficByAsin", {})
        units = sales.get("unitsOrdered", 0)
        ordered_sales = sales.get("orderedProductSales", {})
        amount = ordered_sales.get("amount", 0) if isinstance(ordered_sales, dict) else ordered_sales
        session_pct = traffic.get("unitSessionPercentage", 0)
        return {
            "date": str(day),
            "childAsin": ASIN,
            "unitsOrdered": units,
            "orderedProductSales": amount,
            "unitSessionPercentage": session_pct,
        }
    return None


# ----------------------------------------------------------------------------
# Historie-CSV bijwerken (dedup op datum: nieuwste run wint)
# ----------------------------------------------------------------------------
def upsert_history(new_rows: list[dict]):
    existing = {}
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                existing[row["date"]] = row

    for row in new_rows:
        existing[row["date"]] = row

    os.makedirs(os.path.dirname(HISTORY_CSV), exist_ok=True)
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for date_key in sorted(existing.keys()):
            writer.writerow(existing[date_key])


# ----------------------------------------------------------------------------
def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def parse_date(value: str) -> dt.date:
    """Parseert YYYY-M-D of YYYY-MM-DD (met of zonder voorloopnullen)."""
    parts = value.strip().split("-")
    if len(parts) != 3:
        raise SystemExit(f"Ongeldige datum '{value}', verwacht formaat YYYY-MM-DD.")
    y, m, d = parts
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError as e:
        raise SystemExit(f"Ongeldige datum '{value}': {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="YYYY-MM-DD, default = gisteren (UTC)")
    ap.add_argument("--end", help="YYYY-MM-DD, default = zelfde als --start")
    args = ap.parse_args()

    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    start = parse_date(args.start) if args.start else yesterday
    end = parse_date(args.end) if args.end else start

    access_token = get_access_token()
    print(f"OK: access token opgehaald. Periode: {start} t/m {end}.")

    ok_count = 0
    failed_days = []
    for day in daterange(start, end):
        print(f"-> Rapport aanvragen voor {day} ...")
        try:
            report_id = request_report(access_token, day)
            doc_id = poll_report(access_token, report_id)
            report_json = download_report(access_token, doc_id)
            row = extract_row(report_json, day)
        except Exception as e:
            # Niet meteen de hele run laten crashen: deze dag overslaan, wel
            # doorgaan met de rest, en de tot nu toe opgehaalde dagen blijven
            # zo behouden (zie upsert_history hieronder, per dag).
            print(f"   FOUT bij {day}: {e}. Deze dag wordt overgeslagen, ga door met de rest.")
            failed_days.append(str(day))
            continue

        if row is None:
            print(f"   Geen data voor ASIN {ASIN} op {day} (mogelijk geen sales die dag).")
        else:
            print(f"   OK: units={row['unitsOrdered']} sales={row['orderedProductSales']} "
                  f"cvr={row['unitSessionPercentage']}")
            upsert_history([row])  # direct wegschrijven, niet pas aan het eind
            ok_count += 1

        time.sleep(8)  # pauze tussen rapportaanvragen i.v.m. rate limits (naast de retry-logica hierboven)

    print(f"\nKlaar: {ok_count} dag(en) succesvol weggeschreven naar {HISTORY_CSV}.")
    if failed_days:
        print(f"LET OP: {len(failed_days)} dag(en) mislukt en overgeslagen: {', '.join(failed_days)}")
        print("Draai het script later opnieuw met --start/--end over (een deel van) deze dagen om ze alsnog op te halen.")
        print("De wel-gelukte dagen hierboven zijn al veilig weggeschreven; het script stopt NIET met een")
        print("foutcode, zodat de workflow gewoon doorgaat met Excel bouwen en committen.")


if __name__ == "__main__":
    main()
