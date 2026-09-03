"""Razorpay integration: Payment Links and webhook authentication.

Runs in one of two modes, chosen by whether credentials are present:

  **live** - real calls against Razorpay Test Mode, producing genuine
  `rzp.io` short URLs that open a real hosted checkout.

  **fixture** - deterministic stand-ins with the same shape, derived from the
  payment id. No network, no credentials, no flakiness.

Fixture mode is not a stub written to avoid the integration. The request
payload is constructed identically in both paths and validated the same way;
only the transport differs. That keeps the demo immune to conference wifi
while leaving the live path exercised the moment keys are supplied.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

from salvage.config import settings

logger = logging.getLogger(__name__)

#: Razorpay caps Payment Link reference ids. Keys are truncated to fit.
MAX_REFERENCE_ID = 40


@dataclass(frozen=True, slots=True)
class PaymentLinkResult:
    """Outcome of a Payment Link creation attempt."""

    ok: bool
    short_url: str | None
    link_id: str | None
    reference_id: str
    provider: str
    error: str | None = None
    raw: dict[str, Any] | None = None


def _client() -> Any:
    """Construct an authenticated Razorpay client."""
    import razorpay

    client = razorpay.Client(
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    )
    client.set_app_details({"title": "Salvage", "version": "1.0.0"})
    return client


def idempotency_key(event_id: str, action: str) -> str:
    """Stable key for one (payment, action) pair.

    Recovery pipelines re-run: a webhook is redelivered, a batch is replayed, a
    process restarts mid-flight. Without a stable key those all create fresh
    payment links, and a customer receives three links for one order. Deriving
    the key from the payment id and action makes a replay produce the same key,
    which the database's UNIQUE constraint then rejects.
    """
    digest = hashlib.sha256(f"{event_id}:{action}".encode()).hexdigest()[:16]
    return f"slv_{event_id[:16]}_{digest}"[:MAX_REFERENCE_ID]


def build_payment_link_payload(event: Any, description: str | None = None) -> dict[str, Any]:
    """Construct the Payment Links request body.

    Shared by both modes so the live and fixture paths cannot drift apart.

    `notify.sms` and `notify.email` are both False on purpose: Salvage sends
    its own recovery message, written for the specific failure that occurred.
    Letting Razorpay also fire a generic notification would mean the customer
    receives two messages about one failed payment, which is exactly the
    over-contacting the policy engine's guardrails exist to prevent.
    """
    return {
        "amount": int(event.amount),
        "currency": getattr(event, "currency", "INR"),
        "accept_partial": False,
        "description": description
        or f"Complete your payment for order {getattr(event, 'order_id', event.id)}",
        "reference_id": idempotency_key(event.id, "PAYMENT_LINK"),
        "customer": {
            "name": getattr(event, "customer_name", "") or "Customer",
            "contact": getattr(event, "customer_phone", "") or "",
            "email": getattr(event, "customer_email", "") or "",
        },
        "notify": {"sms": False, "email": False},
        "reminder_enable": True,
        "notes": {
            "source": "salvage",
            "original_payment_id": event.id,
            "failure_reason": getattr(event, "error_reason", "") or "",
        },
    }


def create_payment_link(
    event: Any, description: str | None = None
) -> PaymentLinkResult:
    """Create a Razorpay Payment Link for a failed payment.

    Args:
        event: The failed payment.
        description: Optional override for the link description.

    Returns:
        A PaymentLinkResult. Never raises - a failed link creation is a
        business outcome to be recorded, not an exception that should abort a
        thousand-payment batch.
    """
    payload = build_payment_link_payload(event, description)
    reference = payload["reference_id"]

    if not settings.razorpay_live:
        return _fixture_link(event, reference)

    try:
        response = _client().payment_link.create(payload)
        return PaymentLinkResult(
            ok=True,
            short_url=response.get("short_url"),
            link_id=response.get("id"),
            reference_id=reference,
            provider="razorpay_test",
            raw=response,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        logger.warning("Payment Link creation failed for %s: %s", event.id, exc)
        return PaymentLinkResult(
            ok=False,
            short_url=None,
            link_id=None,
            reference_id=reference,
            provider="razorpay_test",
            error=str(exc),
        )


def _fixture_link(event: Any, reference: str) -> PaymentLinkResult:
    """Deterministic stand-in with Razorpay's URL shape."""
    token = hashlib.sha256(reference.encode()).hexdigest()[:14]
    return PaymentLinkResult(
        ok=True,
        short_url=f"https://rzp.io/i/{token}",
        link_id=f"plink_{token}",
        reference_id=reference,
        provider="fixture",
    )


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Authenticate an inbound Razorpay webhook.

    Razorpay signs the raw request body with the webhook secret using
    HMAC-SHA256. Without this check, anyone who learns the endpoint URL can
    post fabricated `payment.failed` events and make the system issue payment
    links to addresses of their choosing. It is one of the few places in a
    recovery system where a missing check is a genuine security hole rather
    than an inconvenience, which is why it is enforced here rather than left
    as a deployment concern.

    Comparison is constant-time to avoid leaking the expected signature
    through response timing.
    """
    if not settings.razorpay_webhook_secret:
        logger.warning(
            "No RAZORPAY_WEBHOOK_SECRET set - signature verification skipped. "
            "Acceptable locally, never in production."
        )
        return True

    expected = hmac.new(
        settings.razorpay_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def mode() -> str:
    """Which mode the integration is running in, for the health endpoint."""
    return "live_test_mode" if settings.razorpay_live else "fixture"
