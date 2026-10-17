#!/usr/bin/env python3
"""Generate VAPID keys for Web Push and print them as env-var lines.

    python scripts/gen_vapid.py

Paste the printed values into your cPanel "Setup Python App" environment
variables:  VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_CLAIMS_EMAIL.
Also writes vapid_keys.json in the current directory — delete it after use.

Keys are P-256 (prime256v1):
  * public key  = base64url of the uncompressed EC point (0x04 || X || Y)
  * private key = base64url of the scalar D
"""
import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1, generate_private_key)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def main() -> None:
    private_key = generate_private_key(SECP256R1(), None)  # NIST P-256
    priv_numbers = private_key.private_numbers()
    pub_numbers = priv_numbers.public_numbers

    d = priv_numbers.private_value.to_bytes(32, "big")
    x = pub_numbers.x.to_bytes(32, "big")
    y = pub_numbers.y.to_bytes(32, "big")
    public_uncompressed = b"\x04" + x + y

    out = {
        "VAPID_PRIVATE_KEY": b64u(d),
        "VAPID_PUBLIC_KEY": b64u(public_uncompressed),
        "VAPID_CLAIMS_EMAIL": "mailto:you@example.com",
    }
    for k, v in out.items():
        print(f"{k}={v}")

    path = os.path.join(os.getcwd(), "vapid_keys.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"\nSaved to {path}  (keep it secret / delete after configuring cPanel)")


if __name__ == "__main__":
    main()
