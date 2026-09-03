"""Tests for the guardrails.

These are the tests that matter most in the repo. The policy engine is the
component that decides whether to spend a merchant's money, so its failure
modes are financial rather than cosmetic. Each test below pins a property that
must hold no matter what the model predicts.
"""

from __future__ import annotations

import pytest

from salvage.economics import MerchantPolicy, RecoveryAction, value_action
from salvage.policy import RecoveryContext, decide
from salvage.taxonomy import FailureClass, classify

HIGH_VALUE = 5_00_000
CERTAIN = 0.99


def diagnose(reason: str):
    return classify(reason, "BAD_REQUEST_ERROR", None, None)


class TestHardConstraintsBeatEconomics:
    """No score may argue its way past a guardrail."""

    def test_risk_block_never_acted_on(self):
        # The most attractive possible payment: huge and near-certain.
        decision = decide(diagnose("payment_risk_check_failed"), 99_00_000, CERTAIN)
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_RISK_BLOCK"
        assert decision.is_exception

    def test_already_paid_never_recollected(self):
        decision = decide(diagnose("order_already_paid"), HIGH_VALUE, CERTAIN)
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_ALREADY_SETTLED"

    def test_opted_out_customer_never_contacted(self):
        decision = decide(
            diagnose("card_expired"),
            HIGH_VALUE,
            CERTAIN,
            RecoveryContext(customer_opted_out=True),
        )
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_OPT_OUT"

    def test_attempt_cap_stops_recovery(self):
        policy = MerchantPolicy(max_attempts_per_payment=3)
        decision = decide(
            diagnose("insufficient_funds"),
            HIGH_VALUE,
            CERTAIN,
            RecoveryContext(attempts_so_far=3),
            policy,
        )
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_ATTEMPT_CAP"

    def test_high_value_requires_human(self):
        policy = MerchantPolicy(max_autonomous_amount_paise=50_00_000)
        decision = decide(diagnose("card_expired"), 60_00_000, CERTAIN, None, policy)
        assert decision.action is RecoveryAction.ESCALATE
        assert decision.rule_id == "HARD_HIGH_VALUE_ESCALATION"

    @pytest.mark.parametrize("propensity", [0.0, 0.25, 0.5, 0.75, 1.0])
    def test_risk_block_holds_at_every_probability(self, propensity):
        """The guardrail is not a threshold - it does not bend anywhere."""
        decision = decide(
            diagnose("payment_risk_check_failed"), HIGH_VALUE, propensity
        )
        assert decision.action is RecoveryAction.DROP


class TestDiagnosisHonesty:
    """The system reports what it cannot resolve rather than guessing."""

    def test_generic_reason_becomes_an_exception(self):
        # Razorpay's catch-all carries no recovery signal.
        decision = decide(diagnose("payment_failed"), HIGH_VALUE, CERTAIN)
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_UNDIAGNOSED"
        assert decision.is_exception

    def test_unknown_future_reason_becomes_an_exception(self):
        """Razorpay adds reasons. Unrecognised ones must not be guessed at."""
        decision = decide(
            diagnose("some_reason_invented_next_year"), HIGH_VALUE, CERTAIN
        )
        assert decision.is_exception


class TestActionFitness:
    """Interventions must match the failure mode, not merely be cheap."""

    def test_dead_instrument_is_never_retried(self):
        decision = decide(diagnose("card_expired"), HIGH_VALUE, 0.8)
        assert decision.action not in (
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_SCHEDULED,
        )

    def test_dead_instrument_gets_a_link_not_a_bare_notification(self):
        """Regression test for INC-003.

        Telling someone their card expired without giving them another way to
        pay is the least useful thing the system could do, and it is exactly
        what the engine did before the effectiveness matrix existed.
        """
        decision = decide(diagnose("card_expired"), HIGH_VALUE, 0.8)
        assert decision.action is RecoveryAction.PAYMENT_LINK

    def test_transient_downtime_prefers_a_retry(self):
        """Also INC-003: nothing is wrong with the card, so re-present it."""
        decision = decide(diagnose("bank_not_available"), HIGH_VALUE, 0.8)
        assert decision.action in (
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_SCHEDULED,
        )

    def test_merchant_fault_never_reaches_the_customer(self):
        """A customer cannot fix an unactivated merchant account, and telling
        them about it only advertises the merchant's broken configuration."""
        decision = decide(diagnose("merchant_not_activated"), HIGH_VALUE, 0.9)
        assert decision.action is RecoveryAction.ESCALATE

    def test_scheduled_retry_waits_for_the_condition_to_clear(self):
        decision = decide(diagnose("bank_not_available"), HIGH_VALUE, 0.8)
        if decision.action is RecoveryAction.RETRY_SCHEDULED:
            assert decision.retry_after_hours and decision.retry_after_hours > 0


class TestStoppingRules:
    """DROP is an economic outcome, not a hand-written special case."""

    def test_trivial_amount_is_not_worth_chasing(self):
        decision = decide(diagnose("card_expired"), 500, 0.6)
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "ECON_BELOW_THRESHOLD"

    def test_hopeless_propensity_is_not_worth_chasing(self):
        decision = decide(diagnose("card_expired"), 1000, 0.01)
        assert decision.action is RecoveryAction.DROP

    def test_threshold_is_enforced_from_the_merchant_policy(self):
        generous = MerchantPolicy(min_net_ev_paise=1)
        strict = MerchantPolicy(min_net_ev_paise=50_00_000)
        args = (diagnose("card_expired"), 2_00_000, 0.6)
        assert decide(*args, None, generous).action is RecoveryAction.PAYMENT_LINK
        assert decide(*args, None, strict).action is RecoveryAction.DROP


class TestContactGuardrails:
    """Anti-spam limits restrict messaging without blocking silent retries."""

    def test_contact_cap_blocks_messaging(self):
        decision = decide(
            diagnose("card_expired"),
            HIGH_VALUE,
            0.8,
            RecoveryContext(contacts_today=5),
        )
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "CONTACT_GUARDRAIL_EXHAUSTED"

    def test_contact_cap_does_not_block_a_silent_retry(self):
        """A retry costs the customer no attention, so a contact cap has no
        business blocking one. Conflating the two either spams people or
        leaves free money on the table."""
        decision = decide(
            diagnose("bank_not_available"),
            HIGH_VALUE,
            0.8,
            RecoveryContext(contacts_today=5),
        )
        assert decision.action in (
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_SCHEDULED,
        )

    def test_cooldown_suppresses_repeat_contact(self):
        decision = decide(
            diagnose("card_expired"),
            HIGH_VALUE,
            0.8,
            RecoveryContext(hours_since_last_contact=1.0),
        )
        assert decision.action is RecoveryAction.DROP


class TestIncrementalValuation:
    """Value is only claimed where the intervention changed the outcome."""

    def test_action_weaker_than_organic_earns_nothing(self):
        # NOTIFY on bank downtime is weaker than customers simply retrying.
        valuation = value_action(
            RecoveryAction.NOTIFY, 10_00_000, 0.8, FailureClass.BANK_DOWNTIME.value
        )
        assert valuation.lift == 0.0
        assert valuation.net_ev_paise < 0

    def test_lift_is_never_negative(self):
        for action in RecoveryAction:
            valuation = value_action(
                action, 1_00_000, 0.5, FailureClass.BANK_DOWNTIME.value
            )
            assert valuation.lift >= 0.0

    def test_valuation_rejects_impossible_probabilities(self):
        with pytest.raises(ValueError):
            value_action(RecoveryAction.NOTIFY, 1000, 1.5, "BANK_DOWNTIME")
        with pytest.raises(ValueError):
            value_action(RecoveryAction.NOTIFY, -1, 0.5, "BANK_DOWNTIME")


class TestDeterminism:
    """Identical inputs must always produce an identical decision."""

    def test_decisions_are_reproducible(self):
        args = (diagnose("insufficient_funds"), 3_00_000, 0.55)
        first, second = decide(*args), decide(*args)
        assert first.action is second.action
        assert first.rule_id == second.rule_id
        assert first.valuation.net_ev_paise == second.valuation.net_ev_paise
