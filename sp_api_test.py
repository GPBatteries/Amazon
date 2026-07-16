"""Testcall: welke marketplaces en regio horen bij het refresh token?

Leest de drie secrets uit de omgeving (LWA_CLIENT_ID, LWA_CLIENT_SECRET,
SPAPI_REFRESH_TOKEN), haalt een access token op en vraagt per regio de
marketplaceParticipations op. De regio die HTTP 200 teruggeeft is de regio
waar dit token bij hoort; de lijst toont de marketplaces (UK = A1F83G8C2ARO7P).

Er worden geen geheimen geprint. GitHub maskeert secretwaarden bovendien in de log.
"""
import os
import requests

CID = os.environ["LWA_CLIENT_ID"].strip()
CS = os.environ["LWA_CLIENT_SECRET"].strip()
RT = os.environ["SPAPI_REFRESH_TOKEN"].strip()

# --- Tijdelijke debug: check op verborgen whitespace/afkapping, zonder geheimen te printen ---
def _debug_check(name, value):
    stripped = value.strip()
    print(f"[debug] {name}: lengte={len(value)}, na strip()={len(stripped)}, "
          f"begint_met_whitespace={value != value.lstrip()}, "
          f"eindigt_met_whitespace={value != value.rstrip()}, "
          f"bevat_newline={'\\n' in value or '\\r' in value}")

_debug_check("LWA_CLIENT_ID", CID)
_debug_check("LWA_CLIENT_SECRET", CS)
_debug_check("SPAPI_REFRESH_TOKEN", RT)
print(f"[debug] CID begint met 'amzn1.application-oa2-client': {CID.startswith('amzn1.application-oa2-client')}")
print(f"[debug] RT begint met 'Atzr|': {RT.startswith('Atzr|')}")
print("-" * 60)
# --- Einde debug ---

ENDPOINTS = {
    "NA (Noord-Amerika)": "https://sellingpartnerapi-na.amazon.com",
    "EU (Europa, incl. UK)": "https://sellingpartnerapi-eu.amazon.com",
    "FE (Verre Oosten)": "https://sellingpartnerapi-fe.amazon.com",
}
UK_ID = "A1F83G8C2ARO7P"


def get_access_token():
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
    return r


def main():
    print("=" * 60)
    print("STAP 1: access token ophalen via LWA")
    print("=" * 60)
    r = get_access_token()
    if r.status_code != 200:
        err = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        print(f"MISLUKT (HTTP {r.status_code}): {err.get('error')} / {err.get('error_description')}")
        print("Controleer of client id, client secret en refresh token kloppen (en of de")
        print("secret sinds de laatste rotatie is bijgewerkt).")
        raise SystemExit(1)
    access = r.json()["access_token"]
    print("OK: access token opgehaald. Client id, secret en refresh token werken dus.\n")

    print("=" * 60)
    print("STAP 2: per regio de marketplaces opvragen")
    print("=" * 60)
    found_region = None
    uk_found = False
    for name, base in ENDPOINTS.items():
        try:
            resp = requests.get(
                base + "/sellers/v1/marketplaceParticipations",
                headers={"x-amz-access-token": access},
                timeout=30,
            )
        except Exception as e:
            print(f"{name}: fout bij verbinden ({e})")
            continue

        print(f"\n{name}  ->  HTTP {resp.status_code}")
        if resp.status_code == 200:
            found_region = name
            payload = resp.json().get("payload", [])
            for p in payload:
                m = p.get("marketplace", {})
                mid = m.get("id", "?")
                mark = "  <-- UK" if mid == UK_ID else ""
                if mid == UK_ID:
                    uk_found = True
                print(f"   - {m.get('countryCode','?')}  {m.get('name','?')}  ({mid}){mark}")
        else:
            body = resp.text[:180].replace("\n", " ")
            print(f"   (geen toegang in deze regio: {body})")

    print("\n" + "=" * 60)
    print("CONCLUSIE")
    print("=" * 60)
    if found_region:
        print(f"Het token hoort bij regio: {found_region}")
        if uk_found:
            print("UK (A1F83G8C2ARO7P) staat ertussen. We zitten goed voor UK-data.")
        else:
            print("LET OP: UK staat NIET in de lijst. Dit token dekt een andere marketplace.")
            print("Voor UK moet je het UK-account autoriseren (via de Europese Seller Central).")
    else:
        print("Geen enkele regio gaf toegang. Waarschijnlijk mist de app nog de juiste rol")
        print("(Brand Analytics), of de autorisatie is nog niet afgerond. Dat lossen we dan op.")


if __name__ == "__main__":
    main()
