"""The gate that keeps simulated payments away from live services.

Each test that matters here is written so that a *failure of the gate* shows
up as a network call. The live client is replaced with something that raises
on contact, so a test passing is evidence the call was never attempted rather
than evidence it happened to succeed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from salvage import diagnosis
from salvage.config import settings
from salvage.economics import RecoveryAction
from salvage.integrations import llm, razorpay_client
from salvage.provenance import is_simulated
from salvage.simulator.generate import generate_events


@pytest.fixture
def live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite-wide isolation, so the gate is what is under test."""
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_gate")
    monkeypatch.setattr(settings, "razorpay_key_secret", "secret")
    monkeypatch.setattr(settings, "llm_base_url", "https://x.invalid/v1")
    monkeypatch.setattr(settings, "llm_api_key", "k")


def _exploding(*_a, **_k):
    raise AssertionError("a live call was attempted for a simulated event")


class TestDiscriminator:
    def test_generated_events_are_simulated(self):
        assert is_simulated(generate_events(1, seed=1)[0])

    def test_webhook_shaped_events_are_not(self):
        assert not is_simulated(SimpleNamespace(id="pay_1", amount=1000))

    def test_an_object_without_fields_is_not(self):
        assert not is_simulated(object())

    def test_the_marker_is_latent_truth_not_the_type(self):
        """A SimpleNamespace carrying latent truth is still simulated: the
        rule follows the data, so it cannot be defeated by reshaping it."""
        assert is_simulated(SimpleNamespace(id="p", _true_intent=0.5))


class TestPaymentLinks:
    def test_a_simulated_event_never_reaches_razorpay(
        self, live_credentials, monkeypatch
    ):
        monkeypatch.setattr(razorpay_client, "_client", _exploding)
        event = generate_events(1, seed=7)[0]
        result = razorpay_client.create_payment_link(event)
        assert result.ok
        assert result.provider == "fixture"

    def test_a_real_event_still_goes_live(self, live_credentials, monkeypatch):
        """The gate must not disable the integration it is protecting."""
        monkeypatch.setattr(
            razorpay_client,
            "_client",
            lambda: SimpleNamespace(
                payment_link=SimpleNamespace(
                    create=lambda _p: {"short_url": "https://rzp.io/i/x", "id": "plink_x"}
                )
            ),
        )
        event = SimpleNamespace(
            id="pay_real", order_id="order_real", amount=1000, currency="INR",
            customer_name="A", customer_phone="+910000000000",
            customer_email="a@example.com", error_reason="card_expired",
        )
        result = razorpay_client.create_payment_link(event)
        assert result.ok
        assert result.provider == "razorpay_test"
        assert result.short_url == "https://rzp.io/i/x"


class TestGeneration:
    def test_allow_live_false_uses_the_template(self, live_credentials, monkeypatch):
        monkeypatch.setattr(llm.httpx, "post", _exploding)
        message = llm.generate_message(
            "Gaurav", 250000, "BANK_DOWNTIME", RecoveryAction.PAYMENT_LINK,
            "https://rzp.io/i/x", allow_live=False,
        )
        assert message.provider == "template"
        assert "https://rzp.io/i/x" in message.text

    def test_allow_live_defaults_to_true(self):
        """The gate is opt-out at the call site, so a new caller that forgets
        it gets the live path rather than silently degraded copy."""
        import inspect

        assert inspect.signature(llm.generate_message).parameters[
            "allow_live"
        ].default is True


class TestDiagnosis:
    def test_allow_live_false_proposes_nothing(self, live_credentials, monkeypatch):
        monkeypatch.setattr(diagnosis, "_call", _exploding)
        result = diagnosis.diagnose(
            "payment_failed", "bank down", "BANK_ERROR", "bank", "authorization",
            allow_live=False,
        )
        assert result.provider == "none"
        assert not result.accepted
        assert result.failure_class is None

    def test_a_real_event_still_reaches_the_model(self, live_credentials, monkeypatch):
        monkeypatch.setattr(
            diagnosis, "_call",
            lambda _p: '{"failure_class":"BANK_DOWNTIME","confidence":0.9,'
                       '"reasoning":"bank server down"}',
        )
        result = diagnosis.diagnose(
            "payment_failed", "bank server was down", "BANK_ERROR", "bank",
            "authorization",
        )
        assert result.provider != "none"
        assert result.accepted
