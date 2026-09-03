#!/usr/bin/env python
"""
scripts/generate_keypair.py
---------------------------
Generate an Ed25519 keypair and print both keys as base64.

Usage:
    python scripts/generate_keypair.py
    python scripts/generate_keypair.py --out keys/

The output format is base64-encoded raw bytes:
  - Private key: 32-byte seed
  - Public  key: 32-byte raw public key
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

# Allow running from project root without installation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64) — both 32-byte raw, base64-encoded."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    public_bytes = private_key.public_key().public_bytes_raw()
    return base64.b64encode(private_bytes).decode(), base64.b64encode(public_bytes).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an Ed25519 keypair")
    parser.add_argument("--out", metavar="DIR", default=None,
                        help="Directory to write key files into (optional)")
    args = parser.parse_args()

    priv_b64, pub_b64 = generate_keypair()

    print("=== Ed25519 Keypair ===")
    print(f"PRIVATE_KEY_B64={priv_b64}")
    print(f"PUBLIC_KEY_B64={pub_b64}")
    print()
    print("Add these to your .env or pass them directly to the signing script.")

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "private.b64"), "w") as f:
            f.write(priv_b64)
        with open(os.path.join(args.out, "public.b64"), "w") as f:
            f.write(pub_b64)
        print(f"\nKeys written to {args.out}/private.b64 and {args.out}/public.b64")


if __name__ == "__main__":
    main()