"""
app/services/razorpay_client.py
================================
Thin async-compatible wrapper around the synchronous Razorpay Python SDK.

The SDK is blocking, so every call is offloaded to a thread-pool executor so
it doesn't block the asyncio event loop.

Initialization
--------------
Key credentials are read from app.core.config (RAZORPAY_KEY_ID / KEY_SECRET).
A module-level singleton is created at import time; use ``get_razorpay()`` to
obtain it from FastAPI dependency injection.

Test-mode
---------
Use ``rzp_test_*`` keys from the Razorpay Dashboard → Settings → API Keys.
With these keys:
  - All created orders are test orders (visible in Dashboard → Test mode)
  - Payment IDs for test capture: use IDs starting with ``pay_`` produced by
    the Razorpay test checkout or the helper ``TEST_PAYMENT_ID`` below.

Razorpay Test Payment simulation:
  POST /api/transact/{order_id}/confirm-payment accepts a ``payment_id`` in
  the request body.  In test mode, the SDK's ``payment.capture()`` call
  accepts any ``pay_*`` ID that Razorpay test checkout produced.
  For automated tests (no real Razorpay call) we use a mock.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

import razorpay

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Razorpay test-mode payment ID prefix (for docs / UI guidance)
RAZORPAY_TEST_PAYMENT_PREFIX = "pay_"


class RazorpayClient:
    """
    Async wrapper around the razorpay.Client.

    All blocking SDK calls are run in a thread-pool executor so they don't
    stall the asyncio event loop.
    """

    def __init__(self, key_id: str, key_secret: str) -> None:
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.set_app_details({"title": "ap2-merchant-gateway", "version": "0.1.0"})
        logger.info("RazorpayClient initialized (key_id=%r)", key_id[:8] + "...")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def create_order(
        self,
        amount_paise: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Create a Razorpay order.

        Parameters
        ----------
        amount_paise : int
            Amount in smallest currency unit (paise for INR).
        currency : str
            ISO 4217 currency code.
        receipt : str | None
            Optional merchant receipt identifier (max 40 chars).
        notes : dict[str, str] | None
            Key-value metadata attached to the order (visible in Dashboard).

        Returns
        -------
        dict
            Full Razorpay order object (id, amount, currency, status, …).
        """
        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "payment_capture": 1,   # auto-capture
        }
        if receipt:
            payload["receipt"] = receipt[:40]   # Razorpay limit
        if notes:
            payload["notes"] = {k: str(v)[:254] for k, v in notes.items()}

        loop = asyncio.get_event_loop()
        order = await loop.run_in_executor(
            None, partial(self._client.order.create, data=payload)
        )
        logger.info(
            "Razorpay order created id=%r amount=%d currency=%s",
            order.get("id"),
            amount_paise,
            currency,
        )
        return order

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Fetch a Razorpay order by ID."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._client.order.fetch, order_id)
        )

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------

    async def capture_payment(
        self,
        payment_id: str,
        amount_paise: int,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """
        Capture a Razorpay payment.

        In test mode, use a ``pay_*`` ID from the test checkout flow.
        The amount must match the originally authorized amount exactly.
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            partial(
                self._client.payment.capture,
                payment_id,
                amount_paise,
                {"currency": currency},
            ),
        )
        logger.info(
            "Razorpay payment captured payment_id=%r amount=%d",
            payment_id,
            amount_paise,
        )
        return result

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch a Razorpay payment by ID."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self._client.payment.fetch, payment_id)
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_razorpay_client: RazorpayClient | None = None


def get_razorpay() -> RazorpayClient:
    """
    Return (or create) the module-level RazorpayClient singleton.

    Safe to call from FastAPI Depends() or directly.
    """
    global _razorpay_client
    if _razorpay_client is None:
        settings = get_settings()
        _razorpay_client = RazorpayClient(
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret,
        )
    return _razorpay_client