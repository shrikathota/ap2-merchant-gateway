#!/usr/bin/env python
"""
scripts/sign_sample_mandate.py
------------------------------
End-to-end demonstration of the AP2 mandate chain:

  1. Generate (or load) an Ed25519 keypair
  2. Build and sign an IntentMandate
  3. Build and sign a CartMandate referencing the intent
  4. POST /api/mandates/intent  → registers the intent
  5. POST /api/mandates/verify-cart → verifies the cart (should pass)
  6. Replay: POST /api/mandates/verify-cart again → should fail (NONCE_REUSED)

Usage (server must be running: make dev  or  uvicorn app.main:app):
    python scripts/sign_sample_mandate.py [--url http://localhost:8000]

Environment variables (or pass --priv / --pub):
    PRIVATE_KEY_B64  — base64 32-byte Ed25519 private seed
    PUBLIC_KEY_B64   — base64 32-byte Ed25519 public key
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.mandates import sign_mandate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def build_intent_payload(user_id: str, max_paise: int, expires_at: datetime) -> dict:
    return {
        "user_id": user_id,
        "max_amount_paise": max_paise,
        "currency": "INR",
        "allowed_categories": ["electronics", "books"],
        "expires_at": _iso(expires_at),
        "nonce": str(uuid.uuid4()),
    }


def build_cart_payload(
    intent_nonce: str,
    total_paise: int,
    expires_at: datetime,
) -> dict:
    return {
        "parent_intent_id": intent_nonce,
        "agent_id": "agent-demo-001",
        "sku_list": [
            {"sku": "BOOK-001", "qty": 2, "unit_price_paise": 50000, "category": "books"},
            {"sku": "ELEC-999", "qty": 1, "unit_price_paise": total_paise - 100000, "category": "electronics"},
        ],
        "total_amount_paise": total_paise,
        "expires_at": _iso(expires_at),
        "nonce": str(uuid.uuid4()),
    }


def sign(payload: dict, priv: Ed25519PrivateKey) -> dict:
    sig = sign_mandate(payload, priv)
    return {**payload, "signature": sig}


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def post(client: httpx.Client, path: str, body: dict) -> dict:
    resp = client.post(path, json=body)
    print(f"  POST {path} → HTTP {resp.status_code}")
    data = resp.json()
    print(f"  Response: {json.dumps(data, indent=2)}")
    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000", help="Gateway base URL")
    parser.add_argument("--priv", default=os.getenv("PRIVATE_KEY_B64"), help="Private key b64")
    parser.add_argument("--pub", default=os.getenv("PUBLIC_KEY_B64"), help="Public key b64")
    args = parser.parse_args()

    # --- Keypair ---
    if args.priv:
        raw_priv = base64.b64decode(args.priv)
        priv_key = Ed25519PrivateKey.from_private_bytes(raw_priv)
        pub_b64 = args.pub or base64.b64encode(priv_key.public_key().public_bytes_raw()).decode()
    else:
        print("[INFO] No key provided — generating ephemeral keypair")
        priv_key = Ed25519PrivateKey.generate()
        pub_b64 = base64.b64encode(priv_key.public_key().public_bytes_raw()).decode()
        priv_b64 = base64.b64encode(priv_key.private_bytes_raw()).decode()
        print(f"  PRIVATE_KEY_B64={priv_b64}")
        print(f"  PUBLIC_KEY_B64={pub_b64}")

    now = _utcnow()
    intent_expires = now + timedelta(hours=1)
    cart_expires = now + timedelta(minutes=10)

    # -----------------------------------------------------------------------
    # 1. Build & sign IntentMandate
    # -----------------------------------------------------------------------
    section("Step 1 — Register IntentMandate")
    intent_payload = build_intent_payload("user-demo-42", max_paise=500_000, expires_at=intent_expires)
    intent_signed = sign(intent_payload, priv_key)
    intent_nonce = intent_payload["nonce"]

    with httpx.Client(base_url=args.url) as client:
        resp = post(client, "/api/mandates/intent", {
            "mandate": intent_signed,
            "public_key_b64": pub_b64,
        })
        assert resp.get("intent_id") == intent_nonce, "Intent registration failed"
        print(f"\n  ✅ Intent registered: {intent_nonce}")

        # -----------------------------------------------------------------------
        # 2. Valid CartMandate — should pass
        # -----------------------------------------------------------------------
        section("Step 2 — Verify valid CartMandate (under budget)")
        cart_payload = build_cart_payload(intent_nonce, total_paise=300_000, expires_at=cart_expires)
        cart_signed = sign(cart_payload, priv_key)

        resp = post(client, "/api/mandates/verify-cart", {
            "mandate": cart_signed,
            "public_key_b64": pub_b64,
        })
        assert resp.get("verified") is True, f"Expected verified=true, got {resp}"
        print("\n  ✅ Cart verified successfully")

        # -----------------------------------------------------------------------
        # 3. Replay same CartMandate — should fail with NONCE_REUSED
        # -----------------------------------------------------------------------
        section("Step 3 — Replay same CartMandate (NONCE_REUSED expected)")
        resp = post(client, "/api/mandates/verify-cart", {
            "mandate": cart_signed,
            "public_key_b64": pub_b64,
        })
        assert resp.get("verified") is False, f"Expected verified=false, got {resp}"
        assert resp.get("reason") == "NONCE_REUSED", f"Expected NONCE_REUSED, got {resp.get('reason')}"
        print("\n  ✅ Replay correctly rejected: NONCE_REUSED")

        # -----------------------------------------------------------------------
        # 4. Cart exceeding budget — should fail with BUDGET_EXCEEDED
        # -----------------------------------------------------------------------
        section("Step 4 — CartMandate exceeding budget (BUDGET_EXCEEDED expected)")
        big_cart = build_cart_payload(intent_nonce, total_paise=999_999, expires_at=cart_expires)
        big_cart_signed = sign(big_cart, priv_key)

        resp = post(client, "/api/mandates/verify-cart", {
            "mandate": big_cart_signed,
            "public_key_b64": pub_b64,
        })
        assert resp.get("verified") is False, f"Expected verified=false, got {resp}"
        assert resp.get("reason") == "BUDGET_EXCEEDED", f"Expected BUDGET_EXCEEDED, got {resp.get('reason')}"
        print("\n  ✅ Over-budget cart correctly rejected: BUDGET_EXCEEDED")

        # -----------------------------------------------------------------------
        # 5. Tampered signature — should fail with CART_SIGNATURE_INVALID (422)
        # -----------------------------------------------------------------------
        section("Step 5 — Tampered signature (CART_SIGNATURE_INVALID expected)")
        clean_cart = build_cart_payload(intent_nonce, total_paise=100_000, expires_at=cart_expires)
        clean_signed = sign(clean_cart, priv_key)
        # Flip one byte in the middle of the base64 signature
        sig_bytes = bytearray(base64.b64decode(clean_signed["signature"]))
        sig_bytes[32] ^= 0xFF
        clean_signed["signature"] = base64.b64encode(bytes(sig_bytes)).decode()

        resp_raw = client.post("/api/mandates/verify-cart", json={
            "mandate": clean_signed,
            "public_key_b64": pub_b64,
        })
        print(f"  POST /api/mandates/verify-cart → HTTP {resp_raw.status_code}")
        print(f"  Response: {json.dumps(resp_raw.json(), indent=2)}")
        assert resp_raw.status_code == 422, f"Expected 422, got {resp_raw.status_code}"
        assert resp_raw.json()["detail"]["reason"] == "CART_SIGNATURE_INVALID"
        print("\n  ✅ Tampered signature correctly rejected: CART_SIGNATURE_INVALID (HTTP 422)")

    # -----------------------------------------------------------------------
    section("ALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()