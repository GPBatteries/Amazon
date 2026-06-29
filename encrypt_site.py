"""Versleutelt de data en de Excel met een wachtwoord (AES-GCM, PBKDF2).

Het wachtwoord komt uit de omgevingsvariabele SITE_PASSWORD (GitHub-secret).
Er wordt alleen ciphertext naar de site geschreven; de leesbare bronbestanden
worden daarna verwijderd zodat ze niet geserveerd of gecommit worden.

Blob-indeling per bestand: [16 byte salt][12 byte iv][ciphertext+tag].
Dit matcht exact wat de browser (Web Crypto) verwacht: PBKDF2-HMAC-SHA256,
200.000 iteraties, 256-bit sleutel, AES-GCM met aangehangen 16-byte tag.
"""
import os
import glob
import json

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITERATIONS = 200_000
OUT = "output"


def derive(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def encrypt(plaintext: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = derive(password, salt)
    ct = AESGCM(key).encrypt(iv, plaintext, None)  # ciphertext + 16-byte tag
    return salt + iv + ct


def main():
    password = os.environ.get("SITE_PASSWORD")
    if not password:
        raise SystemExit("SITE_PASSWORD ontbreekt (zet hem als GitHub-secret).")

    # Payload = meta + data samengevoegd, zodat niets in leesbare vorm geserveerd wordt
    with open(os.path.join(OUT, "data.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    with open(os.path.join(OUT, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    payload = {**meta, **data}  # data overschrijft niets relevants; bevat rows + assumptions
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    xlsx_path = glob.glob(os.path.join(OUT, "Amazon_*.xlsx"))[0]
    with open(xlsx_path, "rb") as fh:
        xlsx_bytes = fh.read()

    with open(os.path.join(OUT, "payload.enc"), "wb") as fh:
        fh.write(encrypt(payload_bytes, password))
    with open(os.path.join(OUT, "file.enc"), "wb") as fh:
        fh.write(encrypt(xlsx_bytes, password))

    # Leesbare bronbestanden verwijderen: alleen ciphertext blijft over
    for f in ["data.json", "meta.json", os.path.basename(xlsx_path)]:
        p = os.path.join(OUT, f)
        if os.path.exists(p):
            os.remove(p)

    print(f"OK: payload.enc + file.enc geschreven, plaintext verwijderd "
          f"({len(payload['rows'])} rijen).")


if __name__ == "__main__":
    main()
