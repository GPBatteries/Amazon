"""
Gedeelde encrypt/decrypt-helpers (AES-GCM + PBKDF2), gebruikt om ALLES wat naar
de (publieke) repo gecommit wordt te versleutelen -- niet alleen de
eindresultaten (payload.enc / file_<asin>.enc via encrypt_site.py), maar ook
de tussentijdse bronbestanden (spapi_history.csv, ads_spend_history.csv) die
anders gewoon leesbaar in de repo zouden staan voor iedereen met de link.

Wachtwoord komt uit SITE_PASSWORD (dezelfde GitHub-secret als voor het
dashboard-wachtwoord zelf).

Blob-indeling: [16 byte salt][12 byte iv][ciphertext+tag] -- identiek aan wat
de browser (Web Crypto) in index.html verwacht, voor het geval een bestand
ooit ook client-side ontsleuteld moet worden.
"""
import os

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITERATIONS = 200_000


def _derive(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def encrypt_bytes(plaintext: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive(password, salt)
    ct = AESGCM(key).encrypt(iv, plaintext, None)  # ciphertext + 16-byte tag
    return salt + iv + ct


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    salt, iv, ct = blob[:16], blob[16:28], blob[28:]
    key = _derive(password, salt)
    return AESGCM(key).decrypt(iv, ct, None)


def get_site_password() -> str:
    pw = os.environ.get("SITE_PASSWORD")
    if not pw:
        raise SystemExit(
            "SITE_PASSWORD ontbreekt (zet hem als GitHub-secret) -- nodig om de "
            "historie-bestanden te versleutelen/ontsleutelen."
        )
    return pw
