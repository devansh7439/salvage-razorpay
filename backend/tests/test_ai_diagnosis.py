"""Tests for the LLM diagnosis layer, written as attacks on its boundary.

The model is allowed to widen coverage on failures the deterministic taxonomy
could not resolve. It is not allowed to widen authority. These tests try to
make it do the latter.

The threat model is not a malicious LLM - it is an ordinary one that is
confidently wrong, or that returns something structurally unexpected because
the provider changed a default. Both must fail closed.
"""

from __future__ import annotations

import pytest
from salvage import diagnosis
from salvage.diagnosis import (
    AI_AUTONOMY_FRACTION,
    MIN_CONFIDENCE,
    PROPOSABLE,
    AIDiagnosis,
    apply,
    autonomy_ceiling,
    diagnose,
)
from salvage.economics import DEFAULT_POLICY, RecoveryAction
from salvage.policy import decide
from salvage.taxonomy import FailureClass, classify


@pytest.fixture
def live(monkeypatch):
    """Pretend a provider is configured, so `diagnose` reaches the parser."""
    monkeypatch.setattr(diagnosis.settings, "llm_base_url", "https://x.invalid/v1")
    monkeypatch.setattr(diagnosis.settings, "llm_api_key", "k")


def _reply(monkeypatch, text: str) -> None:
    monkeypatch.setattr(diagnosis, "_call", lambda _p: text)


UNDIAGNOSED = classify("payment_failed", "BAD_REQUEST_ERROR", "customer", None)


class TestItOnlyRunsWhereRulesFailed:
    def test_documented_reasons_are_already_confident(self):
        """The model must never be consulted about a payment the taxonomy
        resolved - that would add hallucination risk to a solved problem."""
        for reason in ("card_expired", "insufficient_funds", "bank_not_available"):
            assert classify(reason, "BAD_REQUEST_ERROR").confident

    def test_the_generic_reason_is_not_confident(self):
        assert not UNDIAGNOSED.confident
        assert UNDIAGNOSED.failure_class is FailureClass.UNKNOWN


class TestMalformedOutputFailsClosed:
    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "I think it's probably a bank issue?",
            "{broken json",
            '{"failure_class": ',
            "null",
            "[]",
        ],
    )
    def test_unparseable_replies_are_rejected(self, live, monkeypatch, reply):
        _reply(monkeypatch, reply)
        result = diagnose("payment_failed", "something odd", "BAD_REQUEST_ERROR", None, None)
        assert not result.accepted
        assert result.failure_class is None or not result.accepted

    def test_json_wrapped_in_prose_is_still_read(self, live, monkeypatch):
        """Models fence and pad JSON regardless of instructions. Extracting it
        is worth doing; guessing at broken JSON is not."""
        _reply(
            monkeypatch,
            'Sure!\n```json\n{"failure_class":"BANK_DOWNTIME","confidence":0.9,'
            '"reasoning":"gateway timeout"}\n```',
        )
        result = diagnose("payment_failed", "gateway timeout", None, None, None)
        assert result.accepted
        assert result.failure_class is FailureClass.BANK_DOWNTIME

    def test_missing_confidence_is_rejected(self, live, monkeypatch):
        _reply(monkeypatch, '{"failure_class":"BANK_DOWNTIME"}')
        assert not diagnose("payment_failed", "x", None, None, None).accepted

    def test_non_numeric_confidence_is_rejected(self, live, monkeypatch):
        _reply(
            monkeypatch,
            '{"failure_class":"BANK_DOWNTIME","confidence":"very sure"}',
        )
        assert not diagnose("payment_failed", "x", None, None, None).accepted

    def test_reasoning_is_truncated(self, live, monkeypatch):
        _reply(
            monkeypatch,
            '{"failure_class":"BANK_DOWNTIME","confidence":0.95,"reasoning":"'
            + "x" * 5000
            + '"}',
        )
        result = diagnose("payment_failed", "x", None, None, None)
        assert len(result.reasoning) <= 300


class TestItCannotNameAControl:
    """The model may propose a diagnosis. It may not assert a control."""

    @pytest.mark.parametrize(
        "forbidden", ["RISK_BLOCKED", "ALREADY_PAID", "MERCHANT_CONFIG", "UNKNOWN"]
    )
    def test_control_classes_are_refused(self, live, monkeypatch, forbidden):
        _reply(
            monkeypatch,
            f'{{"failure_class":"{forbidden}","confidence":0.99,"reasoning":"x"}}',
        )
        result = diagnose("payment_failed", "x", None, None, None)
        assert not result.accepted
        assert "not proposable" in (result.rejected_because or "")

    def test_invented_classes_are_refused(self, live, monkeypatch):
        _reply(
            monkeypatch,
            '{"failure_class":"DEFINITELY_RECOVERABLE","confidence":1.0,'
            '"reasoning":"trust me"}',
        )
        result = diagnose("payment_failed", "x", None, None, None)
        assert not result.accepted
        assert "unknown class" in (result.rejected_because or "")

    def test_proposable_set_excludes_every_control(self):
        for control in (
            FailureClass.RISK_BLOCKED,
            FailureClass.ALREADY_PAID,
            FailureClass.MERCHANT_CONFIG,
            FailureClass.UNKNOWN,
        ):
            assert control not in PROPOSABLE


class TestConfidenceThreshold:
    def test_low_confidence_stays_an_exception(self, live, monkeypatch):
        _reply(
            monkeypatch,
            '{"failure_class":"BANK_DOWNTIME","confidence":0.4,"reasoning":"maybe"}',
        )
        result = diagnose("payment_failed", "x", None, None, None)
        assert not result.accepted
        assert "below" in (result.rejected_because or "")

    def test_a_rejected_proposal_leaves_the_classification_untouched(self):
        rejected = AIDiagnosis(
            FailureClass.BANK_DOWNTIME, 0.4, "maybe", False, "too low"
        )
        assert apply(UNDIAGNOSED, rejected) is UNDIAGNOSED

    def test_threshold_is_strict_rather_than_permissive(self):
        """Acting on a weak guess spends money and contacts someone; reporting
        an exception costs nothing. The asymmetry says be strict."""
        assert MIN_CONFIDENCE >= 0.7


class TestReducedAutonomy:
    """A guess earns less rope than a documented match."""

    def test_ceiling_is_a_fraction_of_the_normal_limit(self):
        base = DEFAULT_POLICY.max_autonomous_amount_paise
        assert autonomy_ceiling(base) == int(base * AI_AUTONOMY_FRACTION)
        assert autonomy_ceiling(base) < base

    def test_a_large_ai_diagnosed_payment_escalates(self):
        """Same amount, same propensity. Documented diagnosis acts; an
        AI-derived one hands it to a human."""
        from dataclasses import replace as dc_replace

        amount = 30_00_000  # under the normal ceiling, over the AI one
        accepted = AIDiagnosis(
            FailureClass.INSTRUMENT_INVALID, 0.95, "expired card", True
        )
        ai_classification = apply(UNDIAGNOSED, accepted)

        documented = decide(
            classify("card_expired", "BAD_REQUEST_ERROR"), amount, 0.8
        )
        ai_policy = dc_replace(
            DEFAULT_POLICY,
            max_autonomous_amount_paise=autonomy_ceiling(
                DEFAULT_POLICY.max_autonomous_amount_paise
            ),
        )
        ai_derived = decide(ai_classification, amount, 0.8, None, ai_policy)

        assert documented.action is RecoveryAction.PAYMENT_LINK
        assert ai_derived.action is RecoveryAction.ESCALATE
        assert ai_derived.rule_id == "HARD_HIGH_VALUE_ESCALATION"


class TestPolicyStillHoldsAuthority:
    """The model proposes. The policy engine decides."""

    def test_an_accepted_diagnosis_is_still_subject_to_every_guardrail(self):
        from salvage.policy import RecoveryContext

        accepted = AIDiagnosis(
            FailureClass.BANK_DOWNTIME, 0.99, "gateway down", True
        )
        c = apply(UNDIAGNOSED, accepted)

        assert decide(c, 5_00_000, 0.99, RecoveryContext(customer_opted_out=True)).action is RecoveryAction.DROP
        assert decide(c, 5_00_000, 0.99, RecoveryContext(already_recovered=True)).action is RecoveryAction.DROP
        assert decide(c, 5_00_000, 0.99, RecoveryContext(attempts_so_far=99)).action is RecoveryAction.DROP
        assert decide(c, 200, 0.99).action is RecoveryAction.DROP  # below EV floor

    def test_the_note_records_that_a_model_decided_this(self):
        accepted = AIDiagnosis(
            FailureClass.AUTH_FAILURE, 0.88, "OTP not entered", True
        )
        c = apply(UNDIAGNOSED, accepted)
        assert "AI-assisted" in c.note
        assert "88%" in c.note
        assert c.entry is None, "must not masquerade as a documented reason"


class TestSlowProviderCannotStallTheBatch:
    """This stage is pure upside, so it must never become a liability.

    It runs once per undiagnosable payment, serially, and blocks for up to
    REQUEST_TIMEOUT on each. A provider that hangs or rate-limits would
    otherwise stall a thousand-payment batch for minutes to buy coverage the
    system is happy to do without.
    """

    def test_repeated_failures_stop_the_calls(self, live, monkeypatch):
        diagnosis.reset_breaker()
        attempts = {"n": 0}

        def failing(*_args, **_kwargs):
            attempts["n"] += 1
            raise RuntimeError("provider down")

        monkeypatch.setattr(diagnosis.httpx, "post", failing)

        for _ in range(diagnosis.BREAKER_THRESHOLD + 10):
            result = diagnose("weird_reason", "something", "X", "customer", None)
            assert not result.accepted

        assert attempts["n"] == diagnosis.BREAKER_THRESHOLD, (
            "kept calling a provider that had already failed "
            f"{diagnosis.BREAKER_THRESHOLD} times in a row"
        )
        diagnosis.reset_breaker()

    def test_a_success_closes_the_breaker_again(self, live, monkeypatch):
        diagnosis.reset_breaker()
        _reply(monkeypatch, '{"failure_class": "BANK_DOWNTIME", "confidence": 0.9}')
        assert diagnose("x", "bank was down", "X", "gateway", None).accepted
        assert not diagnosis._breaker_open()


class TestDegradesSafely:
    def test_no_provider_configured_is_a_clean_no(self, monkeypatch):
        monkeypatch.setattr(diagnosis.settings, "llm_base_url", "")
        monkeypatch.setattr(diagnosis.settings, "llm_api_key", "")
        result = diagnose("payment_failed", "x", None, None, None)
        assert not result.accepted
        assert result.provider == "none"

    def test_provider_unreachable_is_a_clean_no(self, live, monkeypatch):
        _reply(monkeypatch, None)
        result = diagnose("payment_failed", "x", None, None, None)
        assert not result.accepted
        assert "unavailable" in (result.rejected_because or "")

    def test_without_a_model_behaviour_is_exactly_as_before(self, monkeypatch):
        """Adding the model can only widen coverage. With it absent, an
        undiagnosable failure is reported as an exception, unchanged."""
        monkeypatch.setattr(diagnosis.settings, "llm_base_url", "")
        result = diagnose("payment_failed", "x", None, None, None)
        assert apply(UNDIAGNOSED, result) is UNDIAGNOSED

        decision = decide(UNDIAGNOSED, 5_00_000, 0.9)
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_UNDIAGNOSED"
        assert decision.is_exception
