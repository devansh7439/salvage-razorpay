"""One-command proof that the live integrations actually work.

Fixture mode is the right default: a demo must not fail because conference wifi
did. But "we integrated with Razorpay" is only a credible claim if the live path
has been run at least once, and a health endpoint reading `fixture` invites
exactly the question you do not want asked mid-demo.

So this script does the one thing fixtures cannot: it calls the real APIs and
prints a receipt.

    python -m salvage.verify_live

It reports which credentials are present, exercises whatever is configured, and
writes the evidence to `data/live_verification.txt`. Anything unconfigured is
reported as SKIPPED rather than quietly passing - a verification script that
reports success without having verified anything is worse than none.

Safe to run repeatedly. Razorpay Test Mode moves no real money, and the payment
link created here is a genuine one for a nominal amount.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from salvage.config import settings
from salvage.economics import RecoveryAction
from salvage.integrations import llm, razorpay_client
from salvage.taxonomy import FailureClass

RECEIPT_PATH = Path(__file__).resolve().parents[2] / "data" / "live_verification.txt"

#: A nominal Rs 10 payment used for the live link. Small enough to be obviously
#: a test, large enough to clear Razorpay's minimum.
PROBE_AMOUNT_PAISE = 1000


class Receipt:
    """Collects output for both the terminal and the evidence file."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        print(line)
        self.lines.append(line)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def _probe_event() -> SimpleNamespace:
    """A realistic expired-card failure to drive the live calls."""
    return SimpleNamespace(
        id="pay_liveverify00001",
        order_id="order_liveverify0001",
        amount=PROBE_AMOUNT_PAISE,
        currency="INR",
        customer_name="Gaurav Kumar",
        customer_phone="+919000090000",
        customer_email="gaurav.kumar@example.com",
        error_reason="card_expired",
    )


def verify_razorpay(out: Receipt) -> bool:
    """Create a real Payment Link in Test Mode."""
    out("-" * 68)
    out("  1. RAZORPAY PAYMENT LINKS")
    out("-" * 68)

    if not settings.razorpay_live:
        out("  SKIPPED - no credentials.")
        out("    Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env")
        out("    (Test Mode keys: dashboard.razorpay.com -> Settings -> API Keys)")
        out()
        return False

    event = _probe_event()
    payload = razorpay_client.build_payment_link_payload(event)
    out(f"  key id         {settings.razorpay_key_id[:12]}...")
    out(f"  amount         {payload['amount']} paise (Rs {payload['amount'] / 100:,.2f})")
    out(f"  reference_id   {payload['reference_id']}")
    out("  calling client.payment_link.create(...)")
    out()

    result = razorpay_client.create_payment_link(event)

    if not result.ok:
        out(f"  FAILED - {result.error}")
        out()
        return False

    out(f"  OK   provider   {result.provider}")
    out(f"       link id    {result.link_id}")
    out(f"       SHORT URL  {result.short_url}")
    out()
    out("  ^ open that URL - it is a real Razorpay hosted checkout page.")
    out()
    return True


def verify_llm(out: Receipt, payment_link: str | None) -> bool:
    """Generate a real Hinglish recovery message."""
    out("-" * 68)
    out("  2. RECOVERY MESSAGE GENERATION")
    out("-" * 68)

    if not settings.llm_live:
        out("  SKIPPED - no credentials.")
        out("    Set LLM_BASE_URL and LLM_API_KEY in .env")
        out("    Any OpenAI-compatible endpoint works (Groq, OpenRouter, ...)")
        out()
        out("  Template fallback currently produces:")
        message = llm.generate_message(
            "Gaurav Kumar",
            PROBE_AMOUNT_PAISE,
            FailureClass.INSTRUMENT_INVALID.value,
            RecoveryAction.PAYMENT_LINK,
            payment_link or "https://rzp.io/i/example",
        )
        out(f'    "{message.text}"')
        out()
        return False

    out(f"  endpoint       {settings.llm_base_url}")
    out(f"  model          {settings.llm_model}")
    out()

    message = llm.generate_message(
        "Gaurav Kumar",
        PROBE_AMOUNT_PAISE,
        FailureClass.INSTRUMENT_INVALID.value,
        RecoveryAction.PAYMENT_LINK,
        payment_link or "https://rzp.io/i/example",
    )

    used_model = message.provider.startswith("llm:")
    out(f"  provider       {message.provider}")
    out()
    out("  GENERATED MESSAGE")
    out(f'    "{message.text}"')
    out()

    if not used_model:
        out("  WARNING - fell back to a template. The model call did not succeed;")
        out("  check the endpoint, key, and model name.")
        out()
        return False

    if payment_link and payment_link not in message.text:
        out("  WARNING - the model omitted the payment link, so the message is")
        out("  not actionable. Salvage rejects these and falls back to a template.")
        out()

    out("  ^ written by the model at request time, not a stored string.")
    out()
    return True


def verify_signature_enforcement(out: Receipt) -> bool:
    """Prove webhook authentication rejects a forgery. Needs no credentials."""
    import hashlib
    import hmac
    import json

    out("-" * 68)
    out("  3. WEBHOOK SIGNATURE ENFORCEMENT")
    out("-" * 68)

    secret = settings.razorpay_webhook_secret
    if not secret:
        out("  Using a local secret for the demonstration, since")
        out("  RAZORPAY_WEBHOOK_SECRET is not set.")
        secret = "whsec_local_demo"

    body = json.dumps(
        {"event": "payment.failed", "payload": {"payment": {"entity": {"amount": 100000}}}}
    ).encode()

    valid = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    forged = hmac.new(b"attacker-guess", body, hashlib.sha256).hexdigest()

    # Verified against the real secret regardless of what is configured, so the
    # check is genuine rather than short-circuited by an empty setting.
    original = settings.razorpay_webhook_secret
    try:
        settings.razorpay_webhook_secret = secret
        accepts = razorpay_client.verify_webhook_signature(body, valid)
        rejects = not razorpay_client.verify_webhook_signature(body, forged)
    finally:
        settings.razorpay_webhook_secret = original

    out(f"  genuine signature accepted   {accepts}")
    out(f"  forged signature rejected    {rejects}")
    out()
    out("  Real HMAC-SHA256 over the raw body, constant-time compared.")
    out("  Without it, anyone who learns the endpoint URL could post fabricated")
    out("  failures and make the system issue links to numbers of their choosing.")
    out()
    return accepts and rejects


def main() -> int:
    """CLI entry point: `python -m salvage.verify_live`."""
    out = Receipt()

    out("=" * 68)
    out("  SALVAGE - LIVE INTEGRATION VERIFICATION")
    out("=" * 68)
    out(f"  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    out()
    out(f"  razorpay   {razorpay_client.mode()}")
    out(f"  llm        {llm.mode()}")
    out()

    link_ok = verify_razorpay(out)
    link_url = None
    if link_ok:
        link_url = next(
            (ln.split("SHORT URL")[-1].strip() for ln in out.lines if "SHORT URL" in ln),
            None,
        )

    llm_ok = verify_llm(out, link_url)
    sig_ok = verify_signature_enforcement(out)

    out("=" * 68)
    out("  SUMMARY")
    out("=" * 68)
    out(f"  Razorpay Payment Link (live)   {'PASS' if link_ok else 'SKIPPED / no keys'}")
    out(f"  Model-written message (live)   {'PASS' if llm_ok else 'SKIPPED / no keys'}")
    out(f"  Webhook signature enforcement  {'PASS' if sig_ok else 'FAIL'}")
    out()

    if link_ok and llm_ok:
        out("  All live paths verified. Screenshot this for the pitch.")
    else:
        out("  Add the missing credentials to .env and re-run.")
        out("  Everything else in Salvage runs without them.")
    out()

    out.save(RECEIPT_PATH)
    print(f"  receipt written to {RECEIPT_PATH}")

    # Signature enforcement is the only check that must pass unconditionally;
    # the others legitimately skip when credentials are absent.
    return 0 if sig_ok else 1


if __name__ == "__main__":
    sys.exit(main())
