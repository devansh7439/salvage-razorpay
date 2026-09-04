"""Tests for the Razorpay taxonomy and the outcome oracle."""

from __future__ import annotations

import pytest
from salvage.economics import RecoveryAction
from salvage.simulator.generate import generate_events
from salvage.simulator.oracle import observe, observe_do_nothing
from salvage.taxonomy import (
    GENERIC_REASONS,
    SOURCE_GUIDANCE,
    TAXONOMY,
    ErrorSource,
    FailureClass,
    classify,
)


class TestTaxonomy:
    def test_every_documented_source_has_guidance(self):
        for source in ErrorSource:
            assert SOURCE_GUIDANCE[source]

    def test_entries_are_keyed_consistently(self):
        for reason, entry in TAXONOMY.items():
            assert entry.reason == reason

    def test_business_source_failures_are_not_customer_actionable(self):
        """source=business means the merchant must fix it. Messaging the
        customer about these is useless and reputationally damaging."""
        for entry in TAXONOMY.values():
            if entry.source is ErrorSource.BUSINESS:
                assert not entry.customer_actionable

    def test_dead_instruments_are_not_auto_retryable(self):
        for reason in ("card_expired", "card_number_invalid", "invalid_vpa"):
            assert not TAXONOMY[reason].auto_retryable

    def test_downtime_is_auto_retryable(self):
        for reason in ("bank_not_available", "bank_technical_error", "server_error"):
            assert TAXONOMY[reason].auto_retryable

    @pytest.mark.parametrize("reason", sorted(GENERIC_REASONS - {""}))
    def test_generic_reasons_are_never_confidently_classified(self, reason):
        assert not classify(reason, "BAD_REQUEST_ERROR", "customer", None).confident

    def test_missing_reason_is_not_confidently_classified(self):
        assert not classify(None, "GATEWAY_ERROR", "gateway", None).confident

    def test_unknown_reason_still_reports_source_guidance(self):
        """Razorpay adds reasons over time. An unrecognised one should still
        surface what its source implies, while refusing to act."""
        result = classify("brand_new_reason", "BAD_REQUEST_ERROR", "gateway", None)
        assert not result.confident
        assert result.failure_class is FailureClass.UNKNOWN
        assert "different payment method" in result.note

    def test_documented_reasons_classify_confidently(self):
        for reason in TAXONOMY:
            assert classify(reason, "BAD_REQUEST_ERROR", None, None).confident

    def test_classification_is_case_insensitive(self):
        assert classify("INSUFFICIENT_FUNDS", None, None, None).confident


class TestOracle:
    def test_outcomes_are_deterministic(self):
        event = generate_events(5, seed=1)[0]
        a = observe(event, RecoveryAction.PAYMENT_LINK, "INSTRUMENT_INVALID")
        b = observe(event, RecoveryAction.PAYMENT_LINK, "INSTRUMENT_INVALID")
        assert a == b

    def test_stronger_actions_are_monotone(self):
        """Common random numbers: if a weak action recovers a payment, a
        stronger one must too. Without this, strategy comparisons measure
        luck rather than policy."""
        for event in generate_events(200, seed=7):
            weak = observe(event, RecoveryAction.NOTIFY, "INSTRUMENT_INVALID")
            strong = observe(event, RecoveryAction.PAYMENT_LINK, "INSTRUMENT_INVALID")
            if weak.recovered:
                assert strong.recovered

    def test_intervention_never_underperforms_doing_nothing(self):
        for event in generate_events(200, seed=11):
            nothing = observe_do_nothing(event, "BANK_DOWNTIME")
            acted = observe(event, RecoveryAction.RETRY_SCHEDULED, "BANK_DOWNTIME")
            if nothing.recovered:
                assert acted.recovered

    def test_incremental_is_never_negative(self):
        for event in generate_events(200, seed=13):
            outcome = observe(event, RecoveryAction.PAYMENT_LINK, "AUTH_FAILURE")
            assert outcome.incremental_paise >= 0

    def test_organic_recovery_is_not_claimed_as_incremental(self):
        """The central honesty property: revenue that would have arrived
        anyway earns the system no credit."""
        for event in generate_events(300, seed=17):
            outcome = observe(event, RecoveryAction.RETRY_SCHEDULED, "BANK_DOWNTIME")
            if outcome.would_have_recovered_organically:
                assert outcome.incremental_paise == 0

    def test_risk_blocked_payments_never_recover(self):
        for event in generate_events(100, seed=19):
            assert not observe(
                event, RecoveryAction.PAYMENT_LINK, "RISK_BLOCKED"
            ).recovered


class TestGenerator:
    def test_generation_is_reproducible(self):
        assert [e.id for e in generate_events(50, seed=3)] == [
            e.id for e in generate_events(50, seed=3)
        ]

    def test_latent_truth_is_excluded_from_features(self):
        """Structural guarantee against leakage: no underscore-prefixed field
        can reach the model, whatever a future feature author does."""
        features = generate_events(1, seed=5)[0].features()
        assert not any(k.startswith("_") for k in features)
        assert "_true_base_propensity" not in features

    def test_amounts_are_positive_paise(self):
        assert all(e.amount > 0 for e in generate_events(200, seed=23))

    def test_generic_reasons_are_present_in_the_data(self):
        """The exception path must be exercised by real data, not only by
        unit tests - a live gateway does emit these."""
        events = generate_events(500, seed=29)
        assert any(not classify(
            e.error_reason, e.error_code, e.error_source, e.error_step
        ).confident for e in events)
