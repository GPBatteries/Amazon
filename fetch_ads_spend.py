"""
Haalt de dagelijkse advertentiekosten (spend) en advertentie-omzet op via de
Amazon Ads API, en zet ze in output/ads_spend_history.csv.

Waar de data vandaan komt:
  Amazon Ads Reporting API v3 (POST/GET /reporting/reports), rapporttype
  "spAdvertisedProduct" (Sponsored Products, per geadverteerd ASIN). Dit
  rapport geeft per dag per ASIN: cost (spend), clicks, impressions,
  sales14d (omzet toe te schrijven aan de advertentie, 14-dagen venster)
  en purchases14d (aantal orders daaruit).

  Dit is bewust ANDERS dan de sales/traffic-cijfers uit fetch_spapi_daily.py
  (die gaan over ALLE verkopen, organisch + advertentie samen). Deze data
  hier is puur het advertentie-deel, en is precies wat je nodig hebt om
  ACOS (=spend/sales14d) of TACOS (=spend/totale omzet) te berekenen.

Hoe het werkt (rapporten zijn ook hier asynchroon, maar sneller dan SP-API):
  1. POST /reporting/reports  -> vraag een rapport aan voor 1 dag
  2. GET  /reporting/reports/{reportId}  -> pollen tot status COMPLETED
  3. De respons bevat dan direct een download-url (gzip-JSON)

Verwachte GitHub secrets:
  ADS_CLIENT_ID, ADS_CLIENT_SECRET, ADS_REFRESH_TOKEN, ADS_PROFILE_ID

Gebruik:
  python fetch_ads_spend.py                          -> haalt gisteren op
  python fetch_ads_spend.py --start 2026-04-01 --end 2026-07-15  -> periode

Let op -- net als bij fetch_spapi_daily.py kon ik dit hier niet live testen
(geen toegang tot Amazon's servers vanuit deze omgeving). De opbouw van het
verzoek (kolomnamen, reportTypeId) is gebaseerd op de officiële v3-documentatie,
maar kan in de praktijk een klein detail afwijken -- zie extract_row() als
er 0 resultaten verschijnen terwijl je weet dat er wel spend was die dag.
"""
import os
import csv
import time
import gzip
import argparse
import datetime as dt

import requests

ASIN = "B00CO00Y32"
BASE_URL = "https://advertising-api-eu.amazon.com"  # EU-regio, bevestigd via ads_api_test.py

CAMPAIGN_MAPPING_FILE = "campaign_mapping.json"


def load_campaign_mapping() -> dict:
    """
    Leest campaign_mapping.json (indien aanwezig): {"campaigns": {"<id>": "<asin>"}}.
    Campagnes die hierin staan, worden ALTIJD aan het genoemde ASIN toegewezen,
    Campagnes die hierin staan, worden ALTIJD aan het genoemde ASIN toegewezen.
    Ontbreekt het bestand of een campagne-ID erin, dan valt terug op de
    standaardmethode: ASIN herkennen in de campagnenaam zelf.
    """
    if not os.path.exists(CAMPAIGN_MAPPING_FILE):
        return {}
    import json
    with open(CAMPAIGN_MAPPING_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    mapping = data.get("campaigns", {})
    # Placeholder-waarde uit het voorbeeldbestand nooit als echte koppeling gebruiken
    return {k: v for k, v in mapping.items() if k != "VUL_CAMPAGNE_ID_HIER_IN"}

HISTORY_CSV = os.path.join("output", "ads_spend_history.csv")
FIELDNAMES = ["date", "childAsin", "adSpend", "adClicks", "adImpressions", "adSales14d", "adOrders14d"]

CID = os.environ["ADS_CLIENT_ID"].strip()
CS = os.environ["ADS_CLIENT_SECRET"].strip()
RT = os.environ["ADS_REFRESH_TOKEN"].strip()
PROFILE_ID = os.environ["ADS_PROFILE_ID"].strip()


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
    if r.status_code != 200:
        raise SystemExit(f"MISLUKT (HTTP {r.status_code}) bij ophalen access token: {r.text[:400]}")
    return r.json()["access_token"]


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": CID,
        "Amazon-Advertising-API-Scope": PROFILE_ID,
        "Content-Type": "application/vnd.createasyncreportrequest.v3+json",
    }


def _retry_wait(response: requests.Response, attempt: int) -> int:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(int(float(retry_after)), 5)
        except ValueError:
            pass
    return min(15 * attempt, 90)


# ----------------------------------------------------------------------------
def request_report(access_token: str, day: dt.date) -> str:
    """Vraagt het spAdvertisedProduct-rapport aan voor 1 dag. Geeft reportId terug."""
    body = {
        "name": f"SP advertised product report {day}",
        "startDate": str(day),
        "endDate": str(day),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["advertiser"],
            "columns": [
                "date",
                "campaignId",
                "campaignName",
                "cost",
                "clicks",
                "impressions",
                "sales14d",
                "purchases14d",
            ],
            "reportTypeId": "spAdvertisedProduct",
            "timeUnit": "DAILY",
            "format": "GZIP_JSON",
        },
    }
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            f"{BASE_URL}/reporting/reports",
            headers=_headers(access_token),
            json=body,
            timeout=30,
        )
        if r.status_code in (200, 202):
            return r.json()["reportId"]
        if r.status_code == 429 and attempt < max_attempts:
            wait = _retry_wait(r, attempt)
            print(f"   Rate limit (429) bij aanvragen rapport {day}. "
                  f"Poging {attempt}/{max_attempts}, {wait}s wachten...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Rapport aanvragen mislukt ({day}): HTTP {r.status_code}: {r.text[:300]}")
    raise RuntimeError(f"Rapport aanvragen bleef 429 geven voor {day} na {max_attempts} pogingen.")


def poll_report(access_token: str, report_id: str, timeout_s: int = 900) -> str:
    """Wacht tot het rapport klaar is. Geeft de download-url terug."""
    deadline = time.time() + timeout_s
    attempt = 0
    headers = _headers(access_token)
    headers["Content-Type"] = "application/json"  # GET heeft geen create-content-type nodig
    last_status = None
    while time.time() < deadline:
        attempt += 1
        r = requests.get(f"{BASE_URL}/reporting/reports/{report_id}", headers=headers, timeout=30)
        if r.status_code == 429:
            wait = _retry_wait(r, attempt)
            print(f"   Rate limit (429) bij pollen rapport. {wait}s wachten...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status != last_status:
            print(f"   Status: {status} (na {int(time.time() - (deadline - timeout_s))}s wachten)")
            last_status = status
        if status == "COMPLETED":
            return data["url"]
        if status in ("FAILURE", "FAILED", "CANCELLED"):
            raise RuntimeError(f"Rapport genereren mislukt: {data}")
        time.sleep(15)
    raise RuntimeError(f"Timeout: rapport was na {timeout_s // 60} minuten nog niet klaar "
                        f"(laatste status: {last_status}).")


def download_report(url: str) -> list:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass  # was toch al niet gzip-gecomprimeerd
    import json
    return json.loads(raw.decode("utf-8"))


# ----------------------------------------------------------------------------
def extract_row(rows: list, day: dt.date, campaign_mapping: dict) -> dict:
    """
    Telt alle campagnes voor ASIN B00CO00Y32 die dag bij elkaar op (er kunnen
    meerdere Sponsored Products-campagnes tegelijk voor hetzelfde product lopen).

    Matching-logica per campagne, in deze volgorde:
      1. Staat het campaignId in campaign_mapping.json? -> die koppeling geldt
         altijd (override), voor uitzonderingsgevallen.
      2. Anders (de normale situatie): staat het ASIN letterlijk in de
         campagnenaam, bv. "SP - KW - Exact - ... - B00CO00Y32 - MAG (aa
         batteries)"? -> meetellen. Dit is de standaardmethode, want alle
         campagnes in dit account hebben het ASIN in de naam staan.

    Let op: veldnamen (cost, sales14d, purchases14d) zijn gebaseerd op de
    officiële v3-documentatie -- als deze functie altijd 0 teruggeeft terwijl
    je weet dat er spend was, print dan eenmalig de ruwe 'rows'-inhoud om de
    daadwerkelijke veldnamen te controleren.
    """
    spend = clicks = impressions = sales = orders = 0
    matched_via_override = matched_via_name = 0
    for row in rows:
        campaign_id = str(row.get("campaignId", ""))
        campaign_name = row.get("campaignName", "") or ""

        if campaign_id in campaign_mapping:
            if campaign_mapping[campaign_id] != ASIN:
                continue
            matched_via_override += 1
        elif ASIN in campaign_name:
            matched_via_name += 1
        else:
            continue

        spend += row.get("cost", 0) or 0
        clicks += row.get("clicks", 0) or 0
        impressions += row.get("impressions", 0) or 0
        sales += row.get("sales14d", 0) or 0
        orders += row.get("purchases14d", 0) or 0

    if matched_via_override:
        print(f"   ({matched_via_override} campagne(s) meegeteld via campaign_mapping.json-override)")
    if matched_via_name:
        print(f"   ({matched_via_name} campagne(s) meegeteld via ASIN in campagnenaam)")
    return {
        "date": str(day),
        "childAsin": ASIN,
        "adSpend": round(spend, 2),
        "adClicks": clicks,
        "adImpressions": impressions,
        "adSales14d": round(sales, 2),
        "adOrders14d": orders,
    }


# ----------------------------------------------------------------------------
def upsert_history(row: dict):
    existing = {}
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                existing[r["date"]] = r
    existing[row["date"]] = row

    os.makedirs(os.path.dirname(HISTORY_CSV), exist_ok=True)
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for date_key in sorted(existing.keys()):
            writer.writerow(existing[date_key])


def parse_date(value: str) -> dt.date:
    parts = value.strip().split("-")
    if len(parts) != 3:
        raise SystemExit(f"Ongeldige datum '{value}', verwacht formaat YYYY-MM-DD.")
    y, m, d = parts
    return dt.date(int(y), int(m), int(d))


def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="YYYY-MM-DD, default = gisteren (UTC)")
    ap.add_argument("--end", help="YYYY-MM-DD, default = zelfde als --start")
    args = ap.parse_args()

    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    start = parse_date(args.start) if args.start else yesterday
    end = parse_date(args.end) if args.end else start

    access_token = get_access_token()
    token_obtained_at = time.time()
    print(f"OK: access token opgehaald. Periode: {start} t/m {end}.")

    campaign_mapping = load_campaign_mapping()
    if campaign_mapping:
        print(f"OK: {len(campaign_mapping)} campagne-override(s) geladen uit {CAMPAIGN_MAPPING_FILE}.")

    already_have = set()
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="", encoding="utf-8") as fh:
            already_have = {r["date"] for r in csv.DictReader(fh)}

    ok_count = 0
    skip_count = 0
    failed_days = []
    TOKEN_MAX_AGE_S = 50 * 60
    for day in daterange(start, end):
        if str(day) in already_have:
            skip_count += 1
            continue
        if time.time() - token_obtained_at > TOKEN_MAX_AGE_S:
            print("   Access token wordt oud, nieuw token ophalen...")
            access_token = get_access_token()
            token_obtained_at = time.time()

        print(f"-> Rapport aanvragen voor {day} ...")
        try:
            report_id = request_report(access_token, day)
            download_url = poll_report(access_token, report_id)
            rows = download_report(download_url)
            row = extract_row(rows, day, campaign_mapping)
        except Exception as e:
            print(f"   FOUT bij {day}: {e}. Deze dag wordt overgeslagen, ga door met de rest.")
            failed_days.append(str(day))
            continue

        print(f"   OK: spend={row['adSpend']} clicks={row['adClicks']} "
              f"sales14d={row['adSales14d']} orders14d={row['adOrders14d']}")
        upsert_history(row)
        ok_count += 1
        time.sleep(3)  # lichte pauze tussen rapportaanvragen

    print(f"\nKlaar: {ok_count} dag(en) succesvol opgehaald, {skip_count} dag(en) al aanwezig (overgeslagen).")
    if failed_days:
        print(f"LET OP: {len(failed_days)} dag(en) mislukt en overgeslagen: {', '.join(failed_days)}")
        print("Draai het script later opnieuw met --start/--end over (een deel van) deze dagen.")


if __name__ == "__main__":
    main()
