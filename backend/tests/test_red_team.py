"""Red-team suite: deliberate attempts to make the system misbehave.

Every test here describes a way to lose a merchant money, charge a customer
twice, or report a recovery that did not happen. They are written as attacks
rather than as feature checks, because a guardrail that has only ever been
tested by the person who wrote it is an assumption, not a control.

Where an attack succeeds, the fix belongs in the system, not in the test.
"""

from __future__ import annotations

import threading
import time

import pytest
from salvage import db
from salvage.controls import AgentMode, controls
from salvage.economics import DEFAULT_POLICY, MerchantPolicy, RecoveryAction
from salvage.integrations import llm
from salvage.pipeline import process_batch
from salvage.policy import RecoveryContext, decide
from salvage.simulator.generate import generate_events
from salvage.taxonomy import classify
from salvage.verification import Settlement, record_settlement


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "rt.db")
    db.reset_db()
    controls.set(enabled=True, mode=AgentMode.AUTONOMOUS)
    yield tmp_path
    controls.set(enabled=True, mode=AgentMode.AUTONOMOUS)


def _executions(event_id: str | None = None) -> int:
    with db.connect() as conn:
        if event_id:
            return conn.execute(
                "SELECT COUNT(*) n FROM executions WHERE event_id = ?", (event_id,)
            ).fetchone()["n"]
        return conn.execute("SELECT COUNT(*) n FROM executions").fetchone()["n"]


class TestDoubleCharge:
    """Attempts to make the system take money twice."""

    def test_cannot_act_twice_via_replay(self, clean_db):
        events = generate_events(30, seed=101)
        process_batch(events)
        before = _executions()
        process_batch(events)
        process_batch(events)
        assert _executions() == before

    def test_cannot_act_after_settlement(self, clean_db):
        events = generate_events(30, seed=103)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM decisions WHERE action != 'DROP' LIMIT 1"
            ).fetchone()
        target = next(e for e in events if e.id == row["event_id"])

        record_settlement(
            Settlement(
                payment_id="pay_paid",
                order_id=target.order_id,
                reference_id=target.id,
                amount_paise=target.amount,
                status="captured",
                source="webhook",
            )
        )

        before = _executions(target.id)
        process_batch(events, reprocess=True)
        assert _executions(target.id) == before

    def test_concurrent_workers_cannot_double_execute(self, clean_db):
        """Two workers handed the same payments at the same moment.

        The realistic shape of this is a webhook redelivered while the first
        delivery is still being processed. Both readers can legitimately see
        'not yet decided'; only one may end up executing.
        """
        events = generate_events(40, seed=107)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                process_batch(events)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert not errors, errors

        with db.connect() as conn:
            dupes = conn.execute(
                "SELECT event_id, COUNT(*) n FROM executions"
                " GROUP BY event_id HAVING n > 1"
            ).fetchall()
        assert dupes == [], f"{len(dupes)} payments executed more than once"


class TestUnauthorisedContact:
    """Attempts to reach customers the system must not reach."""

    def test_opted_out_customer_is_never_contacted(self, clean_db):
        for reason in ("card_expired", "payment_cancelled", "insufficient_funds"):
            decision = decide(
                classify(reason, "BAD_REQUEST_ERROR"),
                50_00_000,
                0.99,
                RecoveryContext(customer_opted_out=True),
            )
            assert decision.action is RecoveryAction.DROP

    def test_contact_cap_cannot_be_exceeded_across_a_batch(self, clean_db):
        """Guardrails must hold across the whole run, not per payment."""
        events = generate_events(300, seed=109)
        process_batch(events)

        with db.connect() as conn:
            worst = conn.execute(
                """
                SELECT customer_id, COUNT(*) n FROM executions
                WHERE action IN ('PAYMENT_LINK','NOTIFY') AND status = 'EXECUTED'
                GROUP BY customer_id ORDER BY n DESC LIMIT 1
                """
            ).fetchone()

        if worst is not None:
            assert worst["n"] <= DEFAULT_POLICY.max_contacts_per_customer_per_day


class TestBlockedPaymentsStayBlocked:
    def test_risk_block_survives_every_probability(self, clean_db):
        for p in (0.0, 0.5, 0.99, 1.0):
            assert (
                decide(
                    classify("payment_risk_check_failed", "BAD_REQUEST_ERROR"),
                    99_00_000,
                    p,
                ).action
                is RecoveryAction.DROP
            )

    def test_risk_blocks_never_execute_in_a_real_batch(self, clean_db):
        events = generate_events(400, seed=113)
        process_batch(events)

        with db.connect() as conn:
            leaked = conn.execute(
                """
                SELECT COUNT(*) n FROM executions x
                JOIN decisions d ON d.event_id = x.event_id
                WHERE d.failure_class IN ('RISK_BLOCKED','ALREADY_PAID')
                """
            ).fetchone()["n"]
        assert leaked == 0

    def test_merchant_config_never_reaches_a_customer(self, clean_db):
        events = generate_events(400, seed=127)
        process_batch(events)

        with db.connect() as conn:
            leaked = conn.execute(
                """
                SELECT COUNT(*) n FROM executions x
                JOIN decisions d ON d.event_id = x.event_id
                WHERE d.failure_class = 'MERCHANT_CONFIG'
                  AND x.action IN ('PAYMENT_LINK','NOTIFY')
                """
            ).fetchone()["n"]
        assert leaked == 0, "customer contacted about a merchant-side fault"


class TestKillSwitchLatency:
    """A kill switch that only takes effect on the next batch is not one."""

    def test_kill_mid_batch_stops_execution_promptly(self, clean_db, tmp_path, monkeypatch):
        """Measured against an undisturbed run of the same batch.

        An earlier version of this test asserted `executed < decided`, which is
        vacuously true - DROP decisions never execute - so it passed while the
        kill switch was in fact only read once per batch. The baseline is what
        makes the assertion mean anything.
        """
        events = generate_events(2000, seed=131)

        monkeypatch.setattr(db.settings, "database_path", tmp_path / "base.db")
        db.reset_db()
        process_batch(events, chunk_size=100, scoring_window=200)
        baseline = _executions()

        monkeypatch.setattr(db.settings, "database_path", tmp_path / "killed.db")
        db.reset_db()

        def killer() -> None:
            time.sleep(0.3)
            controls.kill(reason="red team")

        t = threading.Thread(target=killer, daemon=True)
        t.start()
        process_batch(events, chunk_size=100, scoring_window=200)
        t.join(timeout=10)

        with db.connect() as conn:
            decided = conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        executed = _executions()

        assert decided == len(events), "decisions must still be recorded"
        assert executed < baseline, (
            f"kill switch had no effect mid-batch: {executed} executions "
            f"against an undisturbed baseline of {baseline}"
        )


class TestLLMCannotEscape:
    """The model writes sentences. It must not be able to do anything else."""

    def test_llm_output_cannot_carry_a_dark_pattern(self, monkeypatch):
        monkeypatch.setattr(llm.settings, "llm_base_url", "https://x.invalid/v1")
        monkeypatch.setattr(llm.settings, "llm_api_key", "k")
        monkeypatch.setattr(
            llm,
            "_call_llm",
            lambda _p: "Hurry Asha, 50% off, offer expires today! https://rzp.io/i/x",
        )
        msg = llm.generate_message(
            "Asha", 100000, "INSTRUMENT_INVALID",
            RecoveryAction.PAYMENT_LINK, "https://rzp.io/i/x",
        )
        assert msg.provider == "template_fallback"
        assert msg.blocked_for
        assert llm.find_dark_patterns(msg.text) == []

    def test_llm_dropping_the_link_is_rejected(self, monkeypatch):
        monkeypatch.setattr(llm.settings, "llm_base_url", "https://x.invalid/v1")
        monkeypatch.setattr(llm.settings, "llm_api_key", "k")
        monkeypatch.setattr(llm, "_call_llm", lambda _p: "Please pay soon.")
        msg = llm.generate_message(
            "Asha", 100000, "INSTRUMENT_INVALID",
            RecoveryAction.PAYMENT_LINK, "https://rzp.io/i/abc",
        )
        assert "https://rzp.io/i/abc" in msg.text
        assert msg.provider == "template_fallback"

    def test_llm_unavailable_degrades_to_template(self, monkeypatch):
        monkeypatch.setattr(llm.settings, "llm_base_url", "https://x.invalid/v1")
        monkeypatch.setattr(llm.settings, "llm_api_key", "k")
        monkeypatch.setattr(llm, "_call_llm", lambda _p: None)
        msg = llm.generate_message(
            "Asha", 100000, "BANK_DOWNTIME", RecoveryAction.NOTIFY, None
        )
        assert msg.text
        assert msg.provider == "template_fallback"

    def test_llm_has_no_route_to_a_financial_action(self):
        """Structural, not behavioural: the module must not import or expose
        anything that can move money or alter a decision."""
        import inspect

        source = inspect.getsource(llm)
        for forbidden in (
            "payment_link.create",
            "razorpay_client",
            "decide(",
            "process_batch",
            "insert_execution",
        ):
            assert forbidden not in source, (
                f"llm module references {forbidden!r} - it must only write copy"
            )


class TestRecoveryAccounting:
    """Attempts to make the system claim revenue it did not cause."""

    def test_organic_settlement_is_not_credited(self, clean_db):
        events = generate_events(40, seed=137)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM decisions WHERE action = 'DROP' LIMIT 1"
            ).fetchone()
        target = next(e for e in events if e.id == row["event_id"])

        record_settlement(
            Settlement(
                payment_id="pay_org",
                order_id=target.order_id,
                reference_id=target.id,
                amount_paise=target.amount,
                status="captured",
                source="webhook",
            )
        )

        with db.connect() as conn:
            out = conn.execute(
                "SELECT * FROM outcomes WHERE event_id = ?", (target.id,)
            ).fetchone()
        assert out["recovered"] == 1
        assert out["incremental_paise"] == 0

    def test_incremental_never_exceeds_gross(self, clean_db):
        from salvage.evaluate import evaluate_batch

        report = evaluate_batch(generate_events(400, seed=139))
        for name, s in report["strategies"].items():
            assert (
                s["incremental_recovered_paise"] <= s["gross_recovered_paise"]
            ), name

    def test_a_settlement_for_an_untracked_payment_is_ignored(self, clean_db):
        assert (
            record_settlement(
                Settlement(
                    payment_id="pay_zzz",
                    order_id="order_zzz",
                    reference_id="nope",
                    amount_paise=9_00_000,
                    status="captured",
                    source="webhook",
                )
            )
            is None
        )


class TestEvaluationIntegrity:
    def test_evaluation_is_bit_for_bit_reproducible(self):
        from salvage.evaluate import evaluate_batch

        def run():
            r = evaluate_batch(generate_events(300, seed=149))
            r.pop("_decisions", None)
            return r

        assert run() == run()

    def test_strategies_face_identical_cases(self):
        from salvage.evaluate import evaluate_batch

        report = evaluate_batch(generate_events(300, seed=151))
        sizes = {s["n_events"] for s in report["strategies"].values()}
        risks = {s["revenue_at_risk_paise"] for s in report["strategies"].values()}
        assert len(sizes) == 1 and len(risks) == 1

    def test_policy_tightening_reduces_spend(self, clean_db):
        """A sanity property: a stricter merchant floor must not spend more."""
        from salvage.evaluate import evaluate_batch

        loose = evaluate_batch(
            generate_events(300, seed=157), MerchantPolicy(min_net_ev_paise=1)
        )["strategies"]["salvage"]
        strict = evaluate_batch(
            generate_events(300, seed=157),
            MerchantPolicy(min_net_ev_paise=50_00_000),
        )["strategies"]["salvage"]

        assert strict["action_cost_paise"] <= loose["action_cost_paise"]
        assert strict["actions_taken"] <= loose["actions_taken"]


class TestAuditIntegrity:
    def test_every_decision_leaves_a_trail(self, clean_db):
        events = generate_events(120, seed=163)
        process_batch(events)

        with db.connect() as conn:
            orphans = conn.execute(
                """
                SELECT COUNT(*) n FROM decisions d
                WHERE NOT EXISTS (
                  SELECT 1 FROM audit_trail a
                  WHERE a.event_id = d.event_id AND a.stage = 'DECIDED'
                )
                """
            ).fetchone()["n"]
        assert orphans == 0

    def test_every_execution_leaves_a_trail(self, clean_db):
        events = generate_events(120, seed=167)
        process_batch(events)

        with db.connect() as conn:
            orphans = conn.execute(
                """
                SELECT COUNT(*) n FROM executions x
                WHERE NOT EXISTS (
                  SELECT 1 FROM audit_trail a
                  WHERE a.event_id = x.event_id AND a.stage = 'EXECUTED'
                )
                """
            ).fetchone()["n"]
        assert orphans == 0
