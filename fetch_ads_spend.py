"""
Haalt de dagelijkse advertentiekosten (spend) en advertentie-omzet op via de
Amazon Ads API, en zet ze in output/ads_spend_history.csv.

Multi-ASIN: leest de lijst met ASIN's uit products.json en verdeelt de spend
van elke campagne over de ASIN's die in de campagnenaam voorkomen (of via
campaign_mapping.json, voor uitzonderingen).

Waar de data vandaan komt:
  Amazon Ads Reporting API v3 (POST/GET /reporting/reports), voor ALLE DRIE
  de advertentietypes die Amazon aanbiedt (anders mis je spend!):
    - Sponsored Products (SP)  -> reportTypeId "spCampaigns"
    - Sponsored Brands (SB)    -> reportTypeId "sbCampaigns"
    - Sponsored Display (SD)   -> reportTypeId "sdCampaigns"
  Elk type heeft zijn eigen rapport en zijn eigen campagnelijst-endpoint (SB
  en SD delen namelijk geen campaign-ID-ruimte met SP). Per dag wordt voor
  elk van de 3 types een los rapport aangevraagd en bij elkaar opgeteld.

  Dit rapport geeft per dag per CAMPAGNE: cost (spend), clicks, impressions,
  sales14d (omzet toe te schrijven aan de advertentie, 14-dagen venster)
  en purchases14d (aantal orders daaruit). De koppeling naar het ASIN
  gebeurt door te kijken of het ASIN in de campagnenaam voorkomt (zie
  extract_row) -- niet via een apart ASIN-veld in het rapport zelf.

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

Let op -- de Sponsored Products-kant (rapport + campagnelijst) is inmiddels
live getest en bevestigd correct. De Sponsored Brands- en Sponsored
Display-kant zijn NIEUW en nog niet live getest (ik heb geen toegang tot
Amazon's servers vanuit deze omgeving) -- de endpoints/kolomnamen zijn
gebaseerd op de officiële documentatie maar kunnen in de praktijk een detail
afwijken. Als een van de twee faalt, print het script duidelijk welke en
waarom (zie get_campaign_names en request_report), zodat we dat gericht
kunnen bijstellen zonder dat de SP-cijfers geraakt worden.
"""
import os
import io
import csv
import json
import time
import gzip
import argparse
import datetime as dt

import requests

import crypto_utils

PRODUCTS_FILE = "products.json"
BASE_URL = "https://advertising-api-eu.amazon.com"  # EU-regio, bevestigd via ads_api_test.py

CAMPAIGN_MAPPING_FILE = "campaign_mapping.json"


def load_target_asins() -> list[str]:
    with open(PRODUCTS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    asins = [p["asin"] for p in data.get("products", [])]
    if not asins:
        raise SystemExit(f"Geen producten gevonden in {PRODUCTS_FILE}.")
    return asins

# De 3 advertentietypes die Amazon aanbiedt. Elk heeft een eigen reportTypeId,
# eigen toegestane kolomnamen, EN eigen veldnamen voor "omzet"/"orders" (SP
# gebruikt sales14d/purchases14d met 14-dagen attributie, SD gebruikt gewoon
# sales/purchasesClicks -- bevestigd via Amazon's eigen foutmeldingen die de
# toegestane kolommen exact opnoemen).
AD_PRODUCTS = [
    {
        "key": "SPONSORED_PRODUCTS", "reportTypeId": "spCampaigns", "label": "Sponsored Products",
        "columns": ["date", "campaignId", "cost", "clicks", "impressions", "sales14d", "purchases14d"],
        "salesField": "sales14d", "ordersField": "purchases14d",
    },
    {
        "key": "SPONSORED_BRANDS", "reportTypeId": "sbCampaigns", "label": "Sponsored Brands",
        # LET OP: Amazon's foutmelding met toegestane kolommen was afgekapt na
        # "newToBr..." -- "purchases" is bevestigd toegestaan, "sales" is een
        # aanname (nog niet 100% bevestigd, zie get_campaign_names_sb-comment).
        "columns": ["date", "campaignId", "cost", "clicks", "impressions", "sales", "purchases"],
        "salesField": "sales", "ordersField": "purchases",
    },
    {
        "key": "SPONSORED_DISPLAY", "reportTypeId": "sdCampaigns", "label": "Sponsored Display",
        # Bevestigd via Amazon's foutmelding: "sales" en "purchasesClicks" staan
        # expliciet in de toegestane kolommenlijst.
        "columns": ["date", "campaignId", "cost", "clicks", "impressions", "sales", "purchasesClicks"],
        "salesField": "sales", "ordersField": "purchasesClicks",
    },
]


def load_campaign_mapping() -> dict:
    """
    Leest campaign_mapping.json (indien aanwezig): {"campaigns": {"<id>": "<asin>"}}.
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


HISTORY_CSV_LEGACY = os.path.join("output", "ads_spend_history.csv")  # oude, leesbare naam -- wordt opgeruimd
HISTORY_ENC = os.path.join("output", "ads_spend_history.csv.enc")
FIELDNAMES = ["date", "childAsin", "adSpend", "adClicks", "adImpressions", "adSales14d", "adOrders14d"]

CID = os.environ["ADS_CLIENT_ID"].strip()
CS = os.environ["ADS_CLIENT_SECRET"].strip()
RT = os.environ["ADS_REFRESH_TOKEN"].strip()
PROFILE_ID = os.environ["ADS_PROFILE_ID"].strip()


def _base_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": CID,
        "Amazon-Advertising-API-Scope": PROFILE_ID,
    }


def get_campaign_names_sp(access_token: str) -> dict:
    """Sponsored Products campagnelijst (bevestigd werkend, live getest)."""
    headers = {
        **_base_headers(access_token),
        "Content-Type": "application/vnd.spCampaign.v3+json",
        "Accept": "application/vnd.spCampaign.v3+json",
    }
    mapping = {}
    next_token = None
    while True:
        body = {"maxResults": 200}
        if next_token:
            body["nextToken"] = next_token
        r = requests.post(f"{BASE_URL}/sp/campaigns/list", headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            print(f"   [SP] Kon campagnelijst niet ophalen (HTTP {r.status_code}): {r.text[:600]}")
            return mapping
        data = r.json()
        for c in data.get("campaigns", []):
            mapping[str(c.get("campaignId"))] = c.get("name", "")
        next_token = data.get("nextToken")
        if not next_token:
            break
    return mapping


def get_campaign_names_sb(access_token: str) -> dict:
    """
    Sponsored Brands campagnelijst. Amazon heeft dit endpoint geversioneerd
    (vandaar ook "SB2" als campagnetype in bulksheet-exports) -- de kale
    GET /sb/campaigns met Accept: application/json geeft een 406
    ("No match for accept header"). We proberen daarom eerst het huidige
    v4 list-endpoint (POST, zelfde stramien als de wel-werkende SP v3-lijst),
    en loggen bij falen duidelijk welke poging het was, zodat een volgende
    aanpassing gericht kan zijn i.p.v. weer gokken.
    """
    # Poging 1: v4 list-endpoint (POST, media-type-versioned -- huidige Amazon-stijl)
    headers_v4 = {
        **_base_headers(access_token),
        "Content-Type": "application/vnd.sbcampaignresource.v4+json",
        "Accept": "application/vnd.sbcampaignresource.v4+json",
    }
    mapping = {}
    next_token = None
    while True:
        body = {}
        if next_token:
            body["nextToken"] = next_token
        r = requests.post(f"{BASE_URL}/sb/v4/campaigns/list", headers=headers_v4, json=body, timeout=30)
        if r.status_code != 200:
            print(f"   [SB] Poging 1 (v4 list) mislukt (HTTP {r.status_code}): {r.text[:300]}")
            mapping = None
            break
        data = r.json()
        for c in data.get("campaigns", []):
            mapping[str(c.get("campaignId"))] = c.get("name", "")
        next_token = data.get("nextToken")
        if not next_token:
            break
    if mapping is not None:
        return mapping

    # Poging 2 (fallback): kale GET met de v4-media-type als Accept (voor het
    # geval het endpoint wél GET verwacht, maar met het nieuwe media-type).
    headers_get = {**_base_headers(access_token), "Accept": "application/vnd.sbcampaignresource.v4+json"}
    r2 = requests.get(f"{BASE_URL}/sb/campaigns", headers=headers_get, timeout=30)
    if r2.status_code == 200:
        campaigns = r2.json()
        if isinstance(campaigns, dict):
            campaigns = campaigns.get("campaigns", [])
        return {str(c.get("campaignId")): c.get("name", "") for c in campaigns}
    print(f"   [SB] Poging 2 (GET met v4-media-type) ook mislukt (HTTP {r2.status_code}): {r2.text[:300]}")
    print("   [SB] Campagnenamen niet beschikbaar -- SB-spend kan deze run niet aan een ASIN worden "
          "toegewezen (blijft dan ten onrechte op 0 staan). Zie campaign_mapping.json als handmatig vangnet.")
    return {}


def get_campaign_names_sd(access_token: str) -> dict:
    """
    Sponsored Display campagnelijst. NOG NIET LIVE GETEST -- endpoint
    gebaseerd op documentatie. Faalt dit, dan printen we duidelijk waarom en
    gaat de rest van het script gewoon door (SP-cijfers blijven intact).
    """
    headers = {**_base_headers(access_token), "Accept": "application/json"}
    r = requests.get(f"{BASE_URL}/sd/campaigns", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"   [SD] Kon campagnelijst niet ophalen (HTTP {r.status_code}): {r.text[:600]}")
        return {}
    campaigns = r.json()
    return {str(c.get("campaignId")): c.get("name", "") for c in campaigns}


def get_campaign_names(access_token: str) -> dict:
    """
    Haalt campaignId -> campaignName op voor alle 3 advertentietypes, via
    (snelle, niet-asynchrone) campagnelijst-APIs -- in plaats van deze namen
    als kolom in het trage async-rapport op te vragen (Amazon-support: dat
    maakt rapporten traag of laat ze eindeloos in PENDING blijven staan).
    """
    mapping = {}
    for label, fn in (("SP", get_campaign_names_sp), ("SB", get_campaign_names_sb), ("SD", get_campaign_names_sd)):
        try:
            sub = fn(access_token)
            print(f"   [{label}] {len(sub)} campagne(s) gevonden.")
            mapping.update(sub)
        except Exception as e:
            print(f"   [{label}] Fout bij ophalen campagnelijst: {e}. Wordt overgeslagen.")
    return mapping


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
        **_base_headers(access_token),
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
def request_report(access_token: str, day: dt.date, ad_product: str, report_type_id: str, columns: list) -> str:
    """Vraagt een campagne-rapport aan voor 1 dag + 1 advertentietype. Geeft reportId terug."""
    body = {
        "name": f"{report_type_id} report {day}",
        "startDate": str(day),
        "endDate": str(day),
        "configuration": {
            "adProduct": ad_product,
            "groupBy": ["campaign"],
            "columns": columns,
            "reportTypeId": report_type_id,
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
            print(f"   Rate limit (429) bij aanvragen {report_type_id}-rapport {day}. "
                  f"Poging {attempt}/{max_attempts}, {wait}s wachten...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"{report_type_id}-rapport aanvragen mislukt ({day}): HTTP {r.status_code}: {r.text[:800]}")
    raise RuntimeError(f"{report_type_id}-rapport bleef 429 geven voor {day} na {max_attempts} pogingen.")


def poll_report(access_token: str, report_id: str, timeout_s: int = 3600) -> str:
    """
    Wacht tot het rapport klaar is. Geeft de download-url terug.
    Amazon-support: rapporten kunnen tot 3 uur duren, maar met 3 rapporten
    per dag (SP+SB+SD) houden we het per rapport op max 1 uur -- anders kan
    de totale wachttijd de 6-uurs hard-limit van GitHub-hosted runners
    overschrijden. Duurt 1 type te lang, dan wordt alleen dat type voor die
    dag overgeslagen (zie main()); de andere 2 tellen dan gewoon mee.
    """
    deadline = time.time() + timeout_s
    start_time = time.time()
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
            elapsed_min = int((time.time() - start_time) / 60)
            print(f"   Status: {status} (na {elapsed_min} min wachten)")
            last_status = status
        if status == "COMPLETED":
            return data["url"]
        if status in ("FAILURE", "FAILED", "CANCELLED"):
            raise RuntimeError(f"Rapport genereren mislukt: {data}")
        time.sleep(60)
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
def extract_rows(rows: list, campaign_mapping: dict, campaign_names: dict, target_asins: list[str],
                  sales_field: str = "sales14d", orders_field: str = "purchases14d") -> dict:
    """
    Telt alle campagnes per ASIN uit target_asins bij elkaar op. sales_field en
    orders_field verschillen per advertentietype (zie AD_PRODUCTS) -- SP
    gebruikt sales14d/purchases14d (14-dagen attributie), SB/SD gebruiken
    andere kolomnamen.

    Matching-logica per campagne, in deze volgorde:
      1. Staat het campaignId in campaign_mapping.json? -> die koppeling geldt
         altijd (override), voor uitzonderingsgevallen. Wijst de mapping naar
         een ASIN dat niet in products.json staat, dan wordt de campagne genegeerd.
      2. Anders (de normale situatie): voor elk ASIN in target_asins checken
         of het letterlijk in de campagnenaam voorkomt (opgezocht via
         campaign_names) -> meetellen bij dat/die ASIN(s).

    Geeft een dict {asin: {spend, clicks, impressions, sales, orders}} terug,
    met een entry voor ELK ASIN in target_asins (ook als er niks aan gekoppeld is).
    """
    totals = {asin: {"spend": 0.0, "clicks": 0, "impressions": 0, "sales": 0.0, "orders": 0}
              for asin in target_asins}
    matched_via_override = matched_via_name = 0

    for row in rows:
        campaign_id = str(row.get("campaignId", ""))
        campaign_name = campaign_names.get(campaign_id, "")

        if campaign_id in campaign_mapping:
            mapped_asin = campaign_mapping[campaign_id]
            if mapped_asin not in target_asins:
                continue  # override wijst naar een ASIN dat niet (meer) in products.json staat
            targets = [mapped_asin]
            matched_via_override += 1
        else:
            targets = [asin for asin in target_asins if asin in campaign_name]
            if not targets:
                continue
            matched_via_name += 1

        for asin in targets:
            t = totals[asin]
            t["spend"] += row.get("cost", 0) or 0
            t["clicks"] += row.get("clicks", 0) or 0
            t["impressions"] += row.get("impressions", 0) or 0
            t["sales"] += row.get(sales_field, 0) or 0
            t["orders"] += row.get(orders_field, 0) or 0

    if matched_via_override:
        print(f"   ({matched_via_override} campagne(s) meegeteld via campaign_mapping.json-override)")
    if matched_via_name:
        print(f"   ({matched_via_name} campagne(s) meegeteld via ASIN in campagnenaam)")
    return totals


# ----------------------------------------------------------------------------
def read_history_rows() -> list[dict]:
    """
    Ontsleutelt ads_spend_history.csv.enc en geeft de rijen terug. Bestaat dat
    bestand nog niet (bv. de eerste run na deze encryptie-update), dan wordt
    automatisch de OUDE leesbare CSV ingelezen als die er nog staat -- zodat
    de bestaande geschiedenis niet verloren gaat. Die oude data wordt bij de
    volgende write() gewoon versleuteld weggeschreven (zie write_history_rows).
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
    """Schrijft alle rijen versleuteld terug naar ads_spend_history.csv.enc."""
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

    if os.path.exists(HISTORY_CSV_LEGACY):
        os.remove(HISTORY_CSV_LEGACY)


def upsert_history(new_rows: list[dict]):
    existing = {(r["date"], r["childAsin"]): r for r in read_history_rows()}
    for row in new_rows:
        existing[(row["date"], row["childAsin"])] = row
    write_history_rows(existing)


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
    ap.add_argument("--force", action="store_true",
                    help="Haal dagen die al in de CSV staan toch opnieuw op (overschrijft de bestaande rij)")
    args = ap.parse_args()

    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    start = parse_date(args.start) if args.start else yesterday
    end = parse_date(args.end) if args.end else start

    target_asins = load_target_asins()
    print(f"Doel-ASIN's uit {PRODUCTS_FILE}: {', '.join(target_asins)}")

    access_token = get_access_token()
    token_obtained_at = time.time()
    print(f"OK: access token opgehaald. Periode: {start} t/m {end}.")

    print("-> Campagnenamen ophalen (los van de rapporten, snelle lijst-call) ...")
    campaign_names = get_campaign_names(access_token)
    print(f"   OK: {len(campaign_names)} campagne(s) gevonden.")

    campaign_mapping = load_campaign_mapping()
    if campaign_mapping:
        print(f"OK: {len(campaign_mapping)} campagne-override(s) geladen uit {CAMPAIGN_MAPPING_FILE}.")

    existing_rows = [] if args.force else read_history_rows()
    already_have = {(r["date"], r["childAsin"]) for r in existing_rows}

    if not os.path.exists(HISTORY_ENC) and existing_rows:
        write_history_rows({(r["date"], r["childAsin"]): r for r in existing_rows})
        print(f"   Migratie voltooid: {len(existing_rows)} bestaande rij(en) direct versleuteld weggeschreven.")

    ok_count = 0
    skip_count = 0
    failed_days = []
    TOKEN_MAX_AGE_S = 50 * 60
    for day in daterange(start, end):
        # Alleen overslaan als ALLE doel-ASIN's voor deze dag al aanwezig zijn.
        if not args.force and all((str(day), asin) in already_have for asin in target_asins):
            skip_count += 1
            continue
        if time.time() - token_obtained_at > TOKEN_MAX_AGE_S:
            print("   Access token wordt oud, nieuw token ophalen...")
            access_token = get_access_token()
            token_obtained_at = time.time()

        print(f"-> Rapporten aanvragen voor {day} (SP + SB + SD) ...")
        day_totals = {asin: {"spend": 0.0, "clicks": 0, "impressions": 0, "sales": 0.0, "orders": 0}
                      for asin in target_asins}
        any_success = False
        for product in AD_PRODUCTS:
            try:
                report_id = request_report(access_token, day, product["key"], product["reportTypeId"], product["columns"])
                download_url = poll_report(access_token, report_id)
                rows = download_report(download_url)
                sub_totals = extract_rows(rows, campaign_mapping, campaign_names, target_asins,
                                           product["salesField"], product["ordersField"])
            except Exception as e:
                print(f"   [{product['label']}] FOUT bij {day}: {e}. Dit advertentietype wordt "
                      f"overgeslagen voor deze dag (andere types gaan door).")
                continue
            any_success = True
            for asin, sub in sub_totals.items():
                if sub["spend"] or sub["clicks"] or sub["impressions"]:
                    print(f"   [{product['label']}] {asin}: spend={round(sub['spend'],2)} "
                          f"clicks={sub['clicks']} sales={round(sub['sales'],2)} orders={sub['orders']}")
                t = day_totals[asin]
                t["spend"] += sub["spend"]
                t["clicks"] += sub["clicks"]
                t["impressions"] += sub["impressions"]
                t["sales"] += sub["sales"]
                t["orders"] += sub["orders"]

        if not any_success:
            print(f"   FOUT bij {day}: geen van de 3 advertentietypes leverde resultaat op. Deze dag wordt overgeslagen.")
            failed_days.append(str(day))
            continue

        day_rows = []
        for asin, t in day_totals.items():
            row = {
                "date": str(day),
                "childAsin": asin,
                "adSpend": round(t["spend"], 2),
                "adClicks": t["clicks"],
                "adImpressions": t["impressions"],
                "adSales14d": round(t["sales"], 2),
                "adOrders14d": t["orders"],
            }
            day_rows.append(row)
            print(f"   TOTAAL {asin}: spend={row['adSpend']} clicks={row['adClicks']} "
                  f"sales14d={row['adSales14d']} orders14d={row['adOrders14d']}")
        upsert_history(day_rows)
        ok_count += 1
        time.sleep(3)  # lichte pauze tussen dagen

    print(f"\nKlaar: {ok_count} dag(en) succesvol opgehaald, {skip_count} dag(en) al aanwezig (overgeslagen).")
    if failed_days:
        print(f"LET OP: {len(failed_days)} dag(en) mislukt en overgeslagen: {', '.join(failed_days)}")
        print("Draai het script later opnieuw met --start/--end over (een deel van) deze dagen.")


if __name__ == "__main__":
    main()
