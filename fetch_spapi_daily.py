"""
Haalt de dagelijkse sales/traffic-cijfers op via de SP-API en zet ze in
output/spapi_history.csv, in hetzelfde format als de Google Sheet-route
(kolommen: date, childAsin, unitsOrdered, orderedProductSales, unitSessionPercentage).

Multi-ASIN: leest de lijst met ASIN's uit products.json (root van de repo) en
haalt PER DAG in 1 rapportaanvraag de cijfers op voor AL die ASIN's tegelijk --
dat rapport bevat namelijk sowieso alle producten van het account, we
filteren 'm alleen naar de ASIN's die in products.json staan.

Waar de data vandaan komt:
  Amazon Reports API (2021-06-30), reportType GET_SALES_AND_TRAFFIC_REPORT.
  Dit is het rapport dat units, sales EN sessions/CVR per ASIN per dag bevat --
  dezelfde cijfers die tot nu toe uit de Google Sheet "Daily IMP" kwamen.

Hoe het werkt (SP-API rapporten zijn altijd asynchroon):
  1. POST /reports/2021-06-30/reports  -> vraag een rapport aan voor 1 dag
  2. GET  /reports/2021-06-30/reports/{reportId}  -> pollen tot status DONE
  3. GET  /reports/2021-06-30/documents/{reportDocumentId}  -> download-url ophalen
  4. Download + (indien nodig) gunzip + JSON parsen

Dit script haalt standaard de data van GISTEREN op (UTC) en voegt die rijen
(1 per ASIN in products.json) toe aan de historie-CSV. Draai het dagelijks,
zodat de historie stap voor stap opgebouwd wordt.

Voor een eenmalige achterstand ophalen: geef --start en --end mee (YYYY-MM-DD),
dan wordt er per dag in die periode een los rapport opgevraagd (let op: dit
kan traag zijn en tegen rate limits aanlopen bij een lange periode).
"""
import os
import io
import csv
import sys
import json
import time
import gzip
import argparse
import datetime as dt

import requests

import crypto_utils

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
PRODUCTS_FILE = "products.json"
MARKETPLACE_ID = "A1F83G8C2ARO7P"  # UK
BASE_URL = "https://sellingpartnerapi-eu.amazon.com"  # regio bevestigd via sp_api_test.py

HISTORY_CSV_LEGACY = os.path.join("output", "spapi_history.csv")  # oude, leesbare naam -- wordt opgeruimd
HISTORY_ENC = os.path.join("output", "spapi_history.csv.enc")
FIELDNAMES = ["date", "childAsin", "unitsOrdered", "orderedProductSales", "unitSessionPercentage"]

CID = os.environ["LWA_CLIENT_ID"].strip()
CS = os.environ["LWA_CLIENT_SECRET"].strip()
RT = os.environ["SPAPI_REFRESH_TOKEN"].strip()


def load_target_asins() -> list[str]:
    with open(PRODUCTS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    asins = [p["asin"] for p in data.get("products", [])]
    if not asins:
        raise SystemExit(f"Geen producten gevonden in {PRODUCTS_FILE}.")
    return asins


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
# Rapport-JSON omzetten naar rijen (date, childAsin, unitsOrdered, orderedProductSales, unitSessionPercentage)
# ----------------------------------------------------------------------------
def extract_rows(report_json: dict, day: dt.date, target_asins: list[str]) -> list[dict]:
    """
    Haalt voor ALLE asins in target_asins de cijfers uit 1 rapport (dat toch al
    alle producten van het account bevat). ASIN's die die dag niet in het
    rapport voorkomen (bv. geen sales) krijgen een rij met nullen -- dat
    voorkomt dat we die dag/asin-combinatie bij een volgende run steeds
    opnieuw blijven proberen op te halen (zie 'already_have' in main()).

    Let op: de exacte structuur van dit rapport (veldnamen binnen salesAndTrafficByAsin)
    kan per Amazon API-versie licht afwijken. Print bij twijfel report_json eenmalig
    volledig en vergelijk met de officiële SP-API docs voor
    GET_SALES_AND_TRAFFIC_REPORT, en pas de keys hieronder aan indien nodig.
    """
    by_asin = {}
    for entry in report_json.get("salesAndTrafficByAsin", []):
        asin = entry.get("childAsin")
        if asin not in target_asins:
            continue
        sales = entry.get("salesByAsin", {})
        traffic = entry.get("trafficByAsin", {})
        units = sales.get("unitsOrdered", 0)
        ordered_sales = sales.get("orderedProductSales", {})
        amount = ordered_sales.get("amount", 0) if isinstance(ordered_sales, dict) else ordered_sales
        session_pct = traffic.get("unitSessionPercentage", 0)
        by_asin[asin] = {
            "date": str(day),
            "childAsin": asin,
            "unitsOrdered": units,
            "orderedProductSales": amount,
            "unitSessionPercentage": session_pct,
        }

    rows = []
    for asin in target_asins:
        if asin in by_asin:
            rows.append(by_asin[asin])
        else:
            # Geen data voor dit ASIN die dag -- rij met nullen, zodat deze
            # dag/asin-combinatie als "afgehandeld" geldt (geen sales, geen fout).
            rows.append({
                "date": str(day), "childAsin": asin,
                "unitsOrdered": 0, "orderedProductSales": 0, "unitSessionPercentage": 0,
            })
    return rows


# ----------------------------------------------------------------------------
# Historie-CSV bijwerken (dedup op (datum, childAsin): nieuwste run wint)
# ----------------------------------------------------------------------------
def read_history_rows() -> list[dict]:
    """
    Ontsleutelt spapi_history.csv.enc en geeft de rijen terug. Bestaat dat
    bestand nog niet (bv. de eerste run na deze encryptie-update), dan wordt
    automatisch de OUDE leesbare CSV ingelezen als die er nog staat -- zodat
    de bestaande geschiedenis niet verloren gaat en er geen nieuwe backfill
    nodig is. Die oude data wordt bij de volgende write() gewoon versleuteld
    weggeschreven en de plaintext-versie verwijderd (zie write_history_rows).
    """
    if os.path.exists(HISTORY_ENC):
        password = crypto_utils.get_site_password()
        with open(HISTORY_ENC, "rb") as fh:
            blob = fh.read()
        plaintext = crypto_utils.decrypt_bytes(blob, password).decode("utf-8")
        return list(csv.DictReader(io.StringIO(plaintext)))
    if os.path.exists(HISTORY_CSV_LEGACY):
        print(f"   (Eerste run na de encryptie-update: bestaande {HISTORY_CSV_LEGACY} wordt "
              f"overgenomen en voortaan versleuteld opgeslagen.)")
        with open(HISTORY_CSV_LEGACY, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    return []


def write_history_rows(rows_by_key: dict):
    """Schrijft alle rijen versleuteld terug naar spapi_history.csv.enc."""
    password = crypto_utils.get_site_password()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    writer.writeheader()
    for key in sorted(rows_by_key.keys()):
        writer.writerow(rows_by_key[key])
    plaintext = buf.getvalue().encode("utf-8")

    os.makedirs(os.path.dirname(HISTORY_ENC), exist_ok=True)
    with open(HISTORY_ENC, "wb") as fh:
        fh.write(crypto_utils.encrypt_bytes(plaintext, password))

    # Oude leesbare CSV (van vóór deze fix) opruimen als die nog bestaat --
    # anders blijft de data alsnog onversleuteld in de repo staan.
    if os.path.exists(HISTORY_CSV_LEGACY):
        os.remove(HISTORY_CSV_LEGACY)


def upsert_history(new_rows: list[dict]):
    existing = {(r["date"], r["childAsin"]): r for r in read_history_rows()}
    for row in new_rows:
        existing[(row["date"], row["childAsin"])] = row
    write_history_rows(existing)


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

    target_asins = load_target_asins()
    print(f"Doel-ASIN's uit {PRODUCTS_FILE}: {', '.join(target_asins)}")

    access_token = get_access_token()
    token_obtained_at = time.time()
    print(f"OK: access token opgehaald. Periode: {start} t/m {end}.")

    existing_rows = read_history_rows()
    already_have = {(r["date"], r["childAsin"]) for r in existing_rows}  # set van (datum, asin) tuples

    # Migratie forceren: als het versleutelde bestand nog niet bestaat maar er
    # WEL al data is (uit de oude leesbare CSV), meteen wegschrijven -- ook als
    # er deze run toevallig geen nieuwe dagen zijn om op te halen. Anders wordt
    # de migratie ten onrechte overgeslagen (zie: "bestaat nog niet"-foutmelding
    # bij update_amazon.py, ook al stond de data er via de oude CSV wel degelijk).
    if not os.path.exists(HISTORY_ENC) and existing_rows:
        write_history_rows({(r["date"], r["childAsin"]): r for r in existing_rows})
        print(f"   Migratie voltooid: {len(existing_rows)} bestaande rij(en) direct versleuteld weggeschreven.")

    ok_count = 0
    skip_count = 0
    failed_days = []
    TOKEN_MAX_AGE_S = 50 * 60  # LWA-tokens zijn ~1 uur geldig; ruim voor verloop verversen
    for day in daterange(start, end):
        # Alleen overslaan als ALLE doel-ASIN's voor deze dag al aanwezig zijn.
        if all((str(day), asin) in already_have for asin in target_asins):
            skip_count += 1
            continue
        # Bij lange terugvul-runs verloopt het token halverwege (na ~1 uur) --
        # hier proactief verversen zodat we nooit "access token expired" tegenkomen.
        if time.time() - token_obtained_at > TOKEN_MAX_AGE_S:
            print("   Access token wordt oud, nieuw token ophalen...")
            access_token = get_access_token()
            token_obtained_at = time.time()
        print(f"-> Rapport aanvragen voor {day} ...")
        try:
            report_id = request_report(access_token, day)
            doc_id = poll_report(access_token, report_id)
            report_json = download_report(access_token, doc_id)
            rows = extract_rows(report_json, day, target_asins)
        except Exception as e:
            if "expired" in str(e).lower() or "unauthorized" in str(e).lower():
                print("   Token blijkt toch verlopen te zijn, eenmalig verversen en deze dag opnieuw proberen...")
                access_token = get_access_token()
                token_obtained_at = time.time()
                try:
                    report_id = request_report(access_token, day)
                    doc_id = poll_report(access_token, report_id)
                    report_json = download_report(access_token, doc_id)
                    rows = extract_rows(report_json, day, target_asins)
                except Exception as e2:
                    print(f"   FOUT bij {day} (ook na verversen token): {e2}. Deze dag wordt overgeslagen.")
                    failed_days.append(str(day))
                    continue
            else:
                # Niet meteen de hele run laten crashen: deze dag overslaan, wel
                # doorgaan met de rest, en de tot nu toe opgehaalde dagen blijven
                # zo behouden (zie upsert_history hieronder, per dag).
                print(f"   FOUT bij {day}: {e}. Deze dag wordt overgeslagen, ga door met de rest.")
                failed_days.append(str(day))
                continue

        for row in rows:
            print(f"   {row['childAsin']}: units={row['unitsOrdered']} "
                  f"sales={row['orderedProductSales']} cvr={row['unitSessionPercentage']}")
        upsert_history(rows)  # direct wegschrijven, niet pas aan het eind
        ok_count += 1

        time.sleep(8)  # pauze tussen rapportaanvragen i.v.m. rate limits (naast de retry-logica hierboven)

    print(f"\nKlaar: {ok_count} dag(en) succesvol opgehaald, {skip_count} dag(en) al aanwezig (overgeslagen).")
    if failed_days:
        print(f"LET OP: {len(failed_days)} dag(en) mislukt en overgeslagen: {', '.join(failed_days)}")
        print("Draai het script later opnieuw met --start/--end over (een deel van) deze dagen om ze alsnog op te halen.")
        print("De wel-gelukte dagen hierboven zijn al veilig weggeschreven; het script stopt NIET met een")
        print("foutcode, zodat de workflow gewoon doorgaat met Excel bouwen en committen.")


if __name__ == "__main__":
    main()
