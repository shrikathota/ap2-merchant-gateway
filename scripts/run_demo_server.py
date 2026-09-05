#!/usr/bin/env python
"""
scripts/run_demo_server.py
============================
Runs the real app (real Postgres, real Redis, real Gemini for the buyer
agent's LLM step) with ONLY the Razorpay client swapped for an in-memory
fake — so a full buyer-agent run reliably ends SETTLED, with no manual
checkout step and no dependency on Razorpay's live API being reachable.

Use this for a demo you want to always land on a clean green ✅ SETTLED,
including the --force-failure recovery path (which also settles once it
retries with the alternative). Every other part of the pipeline — mandate
signing, the policy engine, atomic stock decrement, the alternative-recovery
engine, the audit ledger — is fully real; only the payment gateway calls are
faked, because Razorpay itself always requires a real checkout to capture a
payment even in test mode (see README "Known limitations").

Contrast with running the app normally (`uvicorn app.main:app` or `make
dev`): that hits the REAL Razorpay test-mode API. A buyer-agent run against
it creates a genuinely real order (verifiable in your Razorpay Dashboard →
Test Mode), but ends in a CAPTURE_REJECTED / Transaction.status=FAILED
outcome unless you pass `agent/buyer_agent.py --payment-id <real captured
payment id>` — because the default payment_id it uses is synthetic, and a
real gateway correctly refuses to capture a payment that never happened.

Usage
-----
    python scripts/run_demo_server.py               # port 8000
    python scripts/run_demo_server.py --port 8001    # different port

Then, in another terminal:
    python agent/buyer_agent.py --goal "..."          # ends SETTLED
    python agent/buyer_agent.py --force-failure       # recovery, ends SETTLED
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import uvicorn
from unittest.mock import MagicMock

from app.main import app
from app.services.razorpay_client import get_razorpay


def _fake_razorpay():
    mock = MagicMock()

    async def create_order(*, amount_paise, currency, receipt, notes):
        return {
            "id": f"order_FAKEDEMO_{uuid.uuid4().hex[:16]}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
            "receipt": receipt,
        }

    async def capture_payment(*, payment_id, amount_paise, currency):
        return {"id": payment_id, "amount": amount_paise, "currency": currency, "status": "captured"}

    mock.create_order = create_order
    mock.capture_payment = capture_payment
    return mock


app.dependency_overrides[get_razorpay] = _fake_razorpay


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    print(f"Demo server: Razorpay is FAKED (always settles) — everything else is real.")
    print(f"Starting on http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
