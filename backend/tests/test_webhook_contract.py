"""Contract tests against a real Razorpay webhook payload.

The batch data in this project is synthetic, and says so. What is *not*
synthetic is the payload shape the system ingests: `fixtures/` holds a
`payment.failed` envelope with the field names, nesting, and types Razorpay
actually sends, per their published webhook documentation.

That distinction is the point of these tests. They separate what is real from
what is simulated, so the honest claim is precise rather than blanket:

  - the failure taxonomy is real - 48 documented `error_reason` values
  - the webhook contract is real - this file proves the ingest path handles it
  - the *volume and outcomes* are synthetic, and only those

Signature verification is likewise real: HMAC-SHA256 over the raw body, the
same algorithm Razorpay signs with. A forged webhook is genuinely rejected
here, with no credentials and no network involved.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from salvage import db
from salvage.config import settings
from salvage.integrations import razorpay_client
from salvage.taxonomy import FailureClass, classify

FIXTURE = Path(__file__).parent / "fixtures" / "payment_failed_webhook.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def entity(payload: dict) -> dict:
    return payload["payload"]["payment"]["entity"]


class TestRealPayloadShape:
    """The ingest path must handle Razorpay's documented envelope."""

    def test_envelope_matches_documented_structure(self, payload):
        assert payload["event"] == "payment.failed"
        assert payload["entity"] == "event"
        assert "payment" in payload["payload"]
        assert "entity" in payload["payload"]["payment"]

    def test_error_fields_are_all_present(self, entity):
        """Razorpay's five-field error object, exactly as documented."""
        for field in (
            "error_code",
            "error_description",
            "error_reason",
            "error_source",
            "error_step",
        ):
            assert field in entity, f"missing documented error field: {field}"

    def test_amount_is_in_paise(self, entity):
        """Razorpay sends integer paise. Treating this as rupees would be a
        hundredfold error in every downstream figure."""
        assert isinstance(entity["amount"], int)
        assert entity["amount"] == 100000  # Rs 1,000.00

    def test_real_payload_classifies_confidently(self, entity):
        """The taxonomy must resolve a genuine Razorpay error object, not just
        the synthetic ones the generator produces."""
        result = classify(
            entity["error_reason"],
            entity["error_code"],
            entity["error_source"],
            entity["error_step"],
        )
        assert result.confident
        assert result.failure_class is FailureClass.INSTRUMENT_INVALID

    def test_coarse_error_code_carries_no_signal(self, entity):
        """BAD_REQUEST_ERROR spans half the taxonomy. This payload is an
        expired card; the same code is also sent for cancelled checkouts and
        insufficient funds. Keying policy on it would be wrong."""
        assert entity["error_code"] == "BAD_REQUEST_ERROR"
        by_reason = classify(entity["error_reason"], entity["error_code"])
        by_code_only = classify(None, entity["error_code"])
        assert by_reason.confident
        assert not by_code_only.confident


class TestSignatureVerification:
    """Real HMAC-SHA256, exercised without credentials or network."""

    SECRET = "whsec_test_salvage_2026"

    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_is_accepted(self, payload, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_webhook_secret", self.SECRET)
        body = json.dumps(payload).encode()
        assert razorpay_client.verify_webhook_signature(
            body, self._sign(body, self.SECRET)
        )

    def test_forged_signature_is_rejected(self, payload, monkeypatch):
        """Without this check, anyone who learns the endpoint URL can post
        fabricated failures and make the system issue payment links to
        addresses of their choosing."""
        monkeypatch.setattr(settings, "razorpay_webhook_secret", self.SECRET)
        body = json.dumps(payload).encode()
        assert not razorpay_client.verify_webhook_signature(
            body, self._sign(body, "attacker-guessed-secret")
        )

    def test_tampered_body_is_rejected(self, payload, monkeypatch):
        """Signature covers the body, so raising the amount after signing must
        invalidate it."""
        monkeypatch.setattr(settings, "razorpay_webhook_secret", self.SECRET)
        original = json.dumps(payload).encode()
        signature = self._sign(original, self.SECRET)

        payload["payload"]["payment"]["entity"]["amount"] = 99999999
        tampered = json.dumps(payload).encode()

        assert not razorpay_client.verify_webhook_signature(tampered, signature)

    def test_missing_signature_is_rejected(self, payload, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_webhook_secret", self.SECRET)
        body = json.dumps(payload).encode()
        assert not razorpay_client.verify_webhook_signature(body, "")


class TestPaymentLinkRequestShape:
    """The outbound request is built identically in live and fixture mode, so
    the payload can be asserted without credentials."""

    def test_payload_matches_razorpay_payment_links_api(self, entity):
        from types import SimpleNamespace

        event = SimpleNamespace(
            id=entity["id"],
            order_id=entity["order_id"],
            amount=entity["amount"],
            currency=entity["currency"],
            customer_name=entity["notes"]["name"],
            customer_phone=entity["contact"],
            customer_email=entity["email"],
            error_reason=entity["error_reason"],
        )
        body = razorpay_client.build_payment_link_payload(event)

        assert body["amount"] == 100000  # paise, unconverted
        assert body["currency"] == "INR"
        assert body["accept_partial"] is False
        assert body["customer"]["contact"] == "+919000090000"
        assert body["reference_id"]  # idempotency

        # Salvage sends its own message written for the specific failure, so
        # Razorpay must not also fire a generic one - that would mean two
        # messages for one failed payment.
        assert body["notify"]["sms"] is False
        assert body["notify"]["email"] is False

    def test_idempotency_key_is_stable_and_action_scoped(self, entity):
        first = razorpay_client.idempotency_key(entity["id"], "PAYMENT_LINK")
        again = razorpay_client.idempotency_key(entity["id"], "PAYMENT_LINK")
        other = razorpay_client.idempotency_key(entity["id"], "NOTIFY")
        assert first == again
        assert first != other
        assert len(first) <= razorpay_client.MAX_REFERENCE_ID


class TestEndToEndOnRealPayload:
    """The documented payload, driven through the whole pipeline."""

    def test_real_webhook_produces_a_decision_and_audit_trail(
        self, entity, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace

        from salvage.pipeline import process_batch

        monkeypatch.setattr(db.settings, "database_path", tmp_path / "w.db")
        db.reset_db()

        event = SimpleNamespace(
            id=entity["id"],
            order_id=entity["order_id"],
            amount=entity["amount"],
            currency=entity["currency"],
            method=entity["method"],
            status=entity["status"],
            created_at=str(entity["created_at"]),
            error_code=entity["error_code"],
            error_description=entity["error_description"],
            error_reason=entity["error_reason"],
            error_source=entity["error_source"],
            error_step=entity["error_step"],
            customer_id=entity["customer_id"],
            customer_name=entity["notes"]["name"],
            customer_phone=entity["contact"],
            customer_email=entity["email"],
            customer_success_rate=0.7,
            customer_tenure_days=200,
            prior_payment_count=5,
            prior_failure_count=1,
            hours_since_last_success=30.0,
            attempt_number=1,
            hour_of_day=14,
            day_of_week=2,
        )

        result = process_batch([event])
        assert result["processed"] == 1

        with db.connect() as conn:
            decision = conn.execute(
                "SELECT * FROM decisions WHERE event_id = ?", (event.id,)
            ).fetchone()
            stages = [
                r["stage"]
                for r in conn.execute(
                    "SELECT stage FROM audit_trail WHERE event_id = ? ORDER BY id",
                    (event.id,),
                ).fetchall()
            ]

        # An expired card cannot be retried; only a different instrument works.
        assert decision["failure_class"] == FailureClass.INSTRUMENT_INVALID.value
        assert decision["action"] == "PAYMENT_LINK"
        assert stages == [
            "INGESTED",
            "DIAGNOSED",
            "SCORED",
            "DECIDED",
            "EXECUTED",
        ]
