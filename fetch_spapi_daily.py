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
    r = requests.post(
        f"{BASE_URL}/reports/2021-06-30/reports",
        headers={"x-amz-access-token": access_token, "content-type": "application/json"},
        json=body,
        timeout=30,
    )
    if r.status_code != 202:
        raise SystemExit(f"Rapport aanvragen mislukt ({day}): HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["reportId"]


def poll_report(access_token: str, report_id: str, timeout_s: int = 300) -> str:
    """Wacht tot het rapport klaar is. Geeft reportDocumentId terug."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(
            f"{BASE_URL}/reports/2021-06-30/reports/{report_id}",
            headers={"x-amz-access-token": access_token},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("processingStatus")
        if status == "DONE":
            return data["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            raise SystemExit(f"Rapport genereren mislukt: status={status}")
        time.sleep(10)
    raise SystemExit("Timeout: rapport was na 5 minuten nog niet klaar.")


def download_report(access_token: str, report_document_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/reports/2021-06-30/documents/{report_document_id}",
        headers={"x-amz-access-token": access_token},
        timeout=30,
    )
    r.raise_for_status()
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="YYYY-MM-DD, default = gisteren (UTC)")
    ap.add_argument("--end", help="YYYY-MM-DD, default = zelfde als --start")
    args = ap.parse_args()

    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    start = dt.date.fromisoformat(args.start) if args.start else yesterday
    end = dt.date.fromisoformat(args.end) if args.end else start

    access_token = get_access_token()
    print(f"OK: access token opgehaald. Periode: {start} t/m {end}.")

    collected = []
    for day in daterange(start, end):
        print(f"-> Rapport aanvragen voor {day} ...")
        report_id = request_report(access_token, day)
        doc_id = poll_report(access_token, report_id)
        report_json = download_report(access_token, doc_id)
        row = extract_row(report_json, day)
        if row is None:
            print(f"   Geen data voor ASIN {ASIN} op {day} (mogelijk geen sales die dag).")
            continue
        print(f"   OK: units={row['unitsOrdered']} sales={row['orderedProductSales']} "
              f"cvr={row['unitSessionPercentage']}")
        collected.append(row)
        time.sleep(2)  # lichte pauze tussen rapportaanvragen i.v.m. rate limits

    if not collected:
        print("Geen nieuwe rijen opgehaald.")
        return

    upsert_history(collected)
    print(f"OK: {len(collected)} rij(en) weggeschreven naar {HISTORY_CSV}")


if __name__ == "__main__":
    main()
