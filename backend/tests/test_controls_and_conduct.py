"""Tests for merchant control and agent conduct.

These pin the properties Razorpay names in its own published position on
agents in payments (Agent Studio: Principles, Guardrails, and Merchant
Control): review-first mode, an immediate kill switch, consent enforcement,
and no dark patterns.

They are not decoration. Each one is a claim the system makes about what it
will refuse to do, and an untested claim of that kind is just marketing.
"""

from __future__ import annotations

import threading

import pytest
from salvage import db
from salvage.controls import AgentMode, ControlPlane
from salvage.economics import RecoveryAction
from salvage.integrations.llm import (
    DARK_PATTERN_MARKERS,
    find_dark_patterns,
    generate_message,
)
from salvage.pipeline import process_batch
from salvage.policy import RecoveryContext, decide
from salvage.simulator.generate import generate_events
from salvage.taxonomy import FailureClass, classify


@pytest.fixture
def plane() -> ControlPlane:
    return ControlPlane()


class TestKillSwitch:
    """"Merchants can disable any agent instantly." """

    def test_default_is_active_and_autonomous(self, plane):
        c = plane.get()
        assert c.enabled and c.executes
        assert c.mode is AgentMode.AUTONOMOUS

    def test_kill_stops_execution(self, plane):
        c = plane.kill(reason="suspicious activity")
        assert not c.enabled
        assert not c.executes
        assert c.status == "disabled"
        assert c.disabled_reason == "suspicious activity"

    def test_kill_is_reversible_and_clears_the_reason(self, plane):
        plane.kill(reason="pause")
        c = plane.set(enabled=True)
        assert c.executes
        assert c.disabled_reason is None

    def test_change_is_attributed_and_timestamped(self, plane):
        before = plane.get().changed_at
        c = plane.set(mode=AgentMode.REVIEW_FIRST, actor="ops@merchant.com")
        assert c.changed_by == "ops@merchant.com"
        assert c.changed_at >= before

    def test_set_does_not_deadlock(self, plane):
        """Regression for INC-011.

        `set` used to return `self.get()` from inside its own critical
        section. `threading.Lock` is not reentrant, so the call blocked
        forever - and the first person to hit it would have been whoever was
        trying to throw the kill switch during an incident.

        Run on a worker with a join timeout so a regression fails the test
        rather than hanging the whole suite.
        """
        done = threading.Event()

        def toggle() -> None:
            for _ in range(50):
                plane.set(mode=AgentMode.REVIEW_FIRST)
                plane.kill()
                plane.set(enabled=True, mode=AgentMode.AUTONOMOUS)
            done.set()

        worker = threading.Thread(target=toggle, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert done.is_set(), "ControlPlane.set deadlocked"

    def test_concurrent_readers_and_writers(self, plane):
        """The switch is most useful mid-incident, under load, from another
        thread. A control that is only safe when idle is not a control."""
        errors: list[BaseException] = []
        done = threading.Event()

        def reader() -> None:
            try:
                for _ in range(200):
                    assert plane.get().executes is not None
            except BaseException as exc:
                errors.append(exc)

        def writer() -> None:
            try:
                for _ in range(200):
                    plane.kill()
                    plane.set(enabled=True)
                done.set()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
        threads.append(threading.Thread(target=writer, daemon=True))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors
        assert done.is_set()

    def test_snapshot_is_isolated_from_later_changes(self, plane):
        snapshot = plane.get()
        plane.kill()
        assert snapshot.enabled, "get() must return a copy, not a live handle"


class TestReviewFirstMode:
    """"Agents prepare work but hold for merchant approval." """

    def test_decisions_are_recorded_but_nothing_executes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "rf.db")
        db.reset_db()
        events = generate_events(24, seed=5)

        result = process_batch(events, execute_actions=False)

        with db.connect() as conn:
            decisions = conn.execute(
                "SELECT COUNT(*) n FROM decisions"
            ).fetchone()["n"]
            executions = conn.execute(
                "SELECT COUNT(*) n FROM executions"
            ).fetchone()["n"]
            audit = conn.execute(
                "SELECT COUNT(*) n FROM audit_trail"
            ).fetchone()["n"]

        assert result["processed"] == 24
        assert result["executed"] is False
        # The whole point: a merchant can see exactly what the agent *would*
        # have done, with the full trail, before granting it authority.
        assert decisions == 24
        assert executions == 0
        assert audit > 0

    def test_review_first_claims_no_recovery_it_did_not_cause(
        self, tmp_path, monkeypatch
    ):
        """Regression: outcomes were adjudicated against the *decision*.

        In review-first mode the agent executes nothing, yet every decided
        action was scored through the oracle as though it had been carried
        out. A 300-payment batch reported Rs 3,31,947 of "incremental
        recovery genuinely caused by the system" while the system had, by
        construction, done nothing at all.

        That is the exact error the whole project claims to have designed
        against - billing for revenue that would have arrived anyway - and it
        appeared in the mode Razorpay's own guidance says to run on day one.
        """
        from salvage.pipeline import record_outcomes

        monkeypatch.setattr(db.settings, "database_path", tmp_path / "rfo.db")
        db.reset_db()
        events = generate_events(200, seed=909)

        process_batch(events, execute_actions=False)
        record_outcomes(events)

        with db.connect() as conn:
            executions = conn.execute(
                "SELECT COUNT(*) n FROM executions"
            ).fetchone()["n"]
            totals = conn.execute(
                """
                SELECT COALESCE(SUM(incremental_paise), 0) incremental,
                       COALESCE(SUM(recovered_paise), 0)   gross
                FROM outcomes
                """
            ).fetchone()

        assert executions == 0, "review-first mode must execute nothing"
        assert totals["incremental"] == 0, (
            f"claimed Rs {totals['incremental'] / 100:,.2f} of incremental "
            "recovery without taking a single action"
        )
        # Organic recovery still happens and is still reported - it is real
        # money. It is simply not Salvage's to claim.
        assert totals["gross"] > 0

    def test_autonomous_mode_does_execute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "au.db")
        db.reset_db()
        events = generate_events(24, seed=5)

        result = process_batch(events, execute_actions=True)

        with db.connect() as conn:
            executions = conn.execute(
                "SELECT COUNT(*) n FROM executions"
            ).fetchone()["n"]

        assert result["executed"] is True
        assert executions > 0


class TestConsentEnforcement:
    """"Customer communication follows consent rules" with permanent opt-out."""

    def test_opt_out_overrides_any_expected_value(self):
        decision = decide(
            classify("card_expired", "BAD_REQUEST_ERROR"),
            99_00_000,
            0.99,
            RecoveryContext(customer_opted_out=True),
        )
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_OPT_OUT"

    def test_opt_out_is_checked_before_economics(self):
        """No valuation is even computed - consent is not a tiebreaker."""
        decision = decide(
            classify("card_expired", "BAD_REQUEST_ERROR"),
            99_00_000,
            0.99,
            RecoveryContext(customer_opted_out=True),
        )
        assert decision.valuation is None
        assert decision.considered == ()


class TestNoDarkPatterns:
    """"Agents must not employ dark patterns" - false urgency, pressure,
    or invented offers."""

    @pytest.mark.parametrize(
        "text,principle",
        [
            ("Hurry, complete your payment now", "false_urgency"),
            ("This offer expires today, pay immediately", "false_urgency"),
            ("Jaldi karo, aaj hi payment karo", "false_urgency"),
            ("Your account will be suspended if unpaid", "manufactured_pressure"),
            ("Failure to pay may result in legal action", "manufactured_pressure"),
            ("Pay now and get 20% off your order", "invented_offers"),
            ("Complete payment for free delivery", "invented_offers"),
        ],
    )
    def test_violations_are_detected(self, text, principle):
        assert principle in find_dark_patterns(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Hi Gaurav, aapka Rs 1,000 ka payment nahi ho paya kyunki aapka "
            "card is payment ke liye kaam nahi kar raha. Koi baat nahi, yahan "
            "se dobara try kar lijiye: https://rzp.io/i/abc",
            "Hi Priya, your payment did not go through because your bank was "
            "briefly unavailable. We will try again shortly, nothing needed "
            "from you.",
        ],
    )
    def test_legitimate_copy_passes(self, text):
        assert find_dark_patterns(text) == []

    def test_the_shipped_template_is_clean(self):
        """The fallback copy is what the demo actually displays, so it has to
        satisfy the same rule the model output is held to."""
        for failure_class in (
            FailureClass.INSTRUMENT_INVALID,
            FailureClass.BANK_DOWNTIME,
            FailureClass.INSUFFICIENT_FUNDS,
            FailureClass.AUTH_FAILURE,
            FailureClass.CUSTOMER_ABANDONED,
            FailureClass.LIMIT_EXCEEDED,
        ):
            for link in (None, "https://rzp.io/i/test123"):
                message = generate_message(
                    "Gaurav Kumar",
                    250000,
                    failure_class.value,
                    RecoveryAction.PAYMENT_LINK,
                    link,
                )
                assert find_dark_patterns(message.text) == [], message.text

    def test_every_principle_has_markers(self):
        for principle, markers in DARK_PATTERN_MARKERS.items():
            assert markers, f"{principle} has no markers"
