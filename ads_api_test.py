"""
Testscript voor de Amazon Ads API-koppeling.

Waar dit voor is:
  Controleert of ADS_CLIENT_ID / ADS_CLIENT_SECRET / ADS_REFRESH_TOKEN kloppen,
  door (1) een access token op te halen via LWA en (2) de lijst met
  advertising-profielen op te vragen (GET /v2/profiles). Dat laatste heb je
  ook nodig om het juiste Profile ID (UK) te vinden voor ADS_PROFILE_ID.

Verwachte GitHub secrets:
  ADS_CLIENT_ID, ADS_CLIENT_SECRET, ADS_REFRESH_TOKEN
  (ADS_PROFILE_ID is optioneel voor dit testscript -- als hij al bekend is,
  wordt hij extra gecheckt tegen de opgehaalde profielenlijst.)

Let op -- twee dingen die pas te testen zijn zodra Amazon de Ads-API-aanvraag
heeft goedgekeurd:
  1. Of ADS_CLIENT_ID/SECRET horen bij een Security Profile met Advertising
     API-toegang (anders krijg je hier al een fout bij het token ophalen of
     bij de profielen-call).
  2. Of de juiste marketplace/regio-endpoint wordt gebruikt -- dit script
     probeert NA/EU/FE net als sp_api_test.py deed, om dat te bepalen.
"""
import os
import requests

CID = os.environ["ADS_CLIENT_ID"].strip()
CS = os.environ["ADS_CLIENT_SECRET"].strip()
RT = os.environ["ADS_REFRESH_TOKEN"].strip()
EXPECTED_PROFILE_ID = os.environ.get("ADS_PROFILE_ID", "").strip()

REGIONS = {
    "NA (Noord-Amerika)": "https://advertising-api.amazon.com",
    "EU (Europa, incl. UK)": "https://advertising-api-eu.amazon.com",
    "FE (Verre Oosten)": "https://advertising-api-fe.amazon.com",
}


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


def get_profiles(access_token: str, base_url: str):
    r = requests.get(
        f"{base_url}/v2/profiles",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Amazon-Advertising-API-ClientId": CID,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    return r


def main():
    print("=" * 60)
    print("STAP 1: access token ophalen via LWA")
    print("=" * 60)
    access_token = get_access_token()
    print("OK: access token opgehaald. Client id, secret en refresh token werken dus.\n")

    print("=" * 60)
    print("STAP 2: per regio de advertising-profielen opvragen")
    print("=" * 60)

    found_uk = False
    for region_name, base_url in REGIONS.items():
        r = get_profiles(access_token, base_url)
        if r.status_code != 200:
            print(f"\n{region_name} -> HTTP {r.status_code}")
            print(f"   (geen toegang in deze regio: {r.text[:300]})")
            continue

        profiles = r.json()
        print(f"\n{region_name} -> HTTP 200, {len(profiles)} profiel(en)")
        for p in profiles:
            country = p.get("countryCode", "?")
            currency = p.get("currencyCode", "?")
            profile_id = p.get("profileId")
            account_type = p.get("accountInfo", {}).get("type", "?")
            marker = "  <-- UK" if country == "GB" else ""
            print(f"   - {country} ({currency})  profileId={profile_id}  type={account_type}{marker}")
            if country == "GB":
                found_uk = True
                if EXPECTED_PROFILE_ID and str(profile_id) == EXPECTED_PROFILE_ID:
                    print("     (komt overeen met ADS_PROFILE_ID secret)")
                elif EXPECTED_PROFILE_ID:
                    print(f"     LET OP: dit wijkt af van de huidige ADS_PROFILE_ID secret ({EXPECTED_PROFILE_ID})")

    print("\n" + "=" * 60)
    print("CONCLUSIE")
    print("=" * 60)
    if found_uk:
        print("UK-profiel gevonden. Noteer het profileId hierboven en zet 'm in de secret ADS_PROFILE_ID.")
    else:
        print("Geen UK-profiel gevonden in enige regio. Mogelijke oorzaken:")
        print(" - Ads-API-toegang is nog niet (volledig) goedgekeurd door Amazon.")
        print(" - Refresh token hoort bij een ander account/Security Profile.")
        print(" - Er is nog geen actieve advertentie-account gekoppeld aan dit profiel.")


if __name__ == "__main__":
    main()
