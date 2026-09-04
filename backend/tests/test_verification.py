"""Tests for the VERIFY stage.

An attempted action is not recovered revenue. These tests pin the difference,
and the safety property that falls out of it: once a payment is verified as
settled, nothing may act on it again.

The dangerous scenario is ordinary rather than exotic. Recovery is
asynchronous - a link is issued on Monday and paid on Wednesday - so any
scheduled retry, reminder or second link created in between is chasing money
that is already in the account. Against a live instrument, a retry at that
point takes it twice.
"""

from __future__ import annotations

import pytest
from salvage import db
from salvage.economics import RecoveryAction
from salvage.pipeline import process_batch
from salvage.policy import RecoveryContext, decide
from salvage.simulator.generate import generate_events
from salvage.taxonomy import classify
from salvage.verification import (
    SETTLEMENT_EVENTS,
    Settlement,
    extract_settlement,
    record_settlement,
    recovered_event_ids,
    resolve_original_event,
)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "v.db")
    db.reset_db()
    return tmp_path


def _captured_webhook(payment_id: str, order_id: str, amount: int, ref: str | None = None):
    entity = {
        "id": payment_id,
        "order_id": order_id,
        "amount": amount,
        "status": "captured",
        "notes": {"original_payment_id": ref} if ref else {},
    }
    return {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}


class TestSettlementExtraction:
    def test_captured_payment_is_a_settlement(self):
        s = extract_settlement(_captured_webhook("pay_new", "order_1", 250000))
        assert s is not None
        assert s.payment_id == "pay_new"
        assert s.amount_paise == 250000
        assert s.source == "webhook"

    def test_payment_link_paid_is_a_settlement(self):
        payload = {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "plink_1",
                        "reference_id": "slv_abc",
                        "amount_paid": 99000,
                        "status": "paid",
                    }
                }
            },
        }
        s = extract_settlement(payload)
        assert s is not None
        assert s.reference_id == "slv_abc"
        assert s.amount_paise == 99000

    def test_failure_events_are_not_settlements(self):
        assert (
            extract_settlement(
                {"event": "payment.failed", "payload": {"payment": {"entity": {}}}}
            )
            is None
        )

    def test_unrelated_events_are_ignored_rather_than_erroring(self):
        """Razorpay sends a long tail of events. Returning None lets the
        endpoint accept them without special-casing each one."""
        for event in ("refund.created", "subscription.charged", "settlement.processed"):
            assert extract_settlement({"event": event, "payload": {}}) is None

    def test_every_declared_settlement_event_extracts(self):
        for event in SETTLEMENT_EVENTS:
            payload = {
                "event": event,
                "payload": {
                    "payment": {
                        "entity": {"id": "p1", "amount": 1000, "status": "captured"}
                    }
                },
            }
            assert extract_settlement(payload) is not None, event


class TestSettlementMatching:
    def test_matched_by_order_id(self, db_path):
        events = generate_events(6, seed=41)
        process_batch(events)
        target = events[0]

        matched = resolve_original_event(
            Settlement(
                payment_id="pay_brand_new",
                order_id=target.order_id,
                reference_id=None,
                amount_paise=target.amount,
                status="captured",
                source="webhook",
            )
        )
        assert matched == target.id

    def test_matched_by_our_own_reference_id(self, db_path):
        """The idempotency key we set on the payment link is the most reliable
        join, because we chose it."""
        events = generate_events(20, seed=43)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id, idempotency_key FROM executions"
                " WHERE action = 'PAYMENT_LINK' LIMIT 1"
            ).fetchone()
        assert row is not None

        matched = resolve_original_event(
            Settlement(
                payment_id="pay_x",
                order_id=None,
                reference_id=row["idempotency_key"],
                amount_paise=1000,
                status="captured",
                source="webhook",
            )
        )
        assert matched == row["event_id"]

    def test_unknown_settlement_is_ignored(self, db_path):
        """A merchant takes payments Salvage never saw fail. Those must not
        be credited to it."""
        assert (
            record_settlement(
                Settlement(
                    payment_id="pay_unrelated",
                    order_id="order_unrelated",
                    reference_id=None,
                    amount_paise=500000,
                    status="captured",
                    source="webhook",
                )
            )
            is None
        )

    def test_uncaptured_status_is_not_treated_as_settled(self, db_path):
        assert (
            record_settlement(
                Settlement(
                    payment_id="p",
                    order_id="o",
                    reference_id=None,
                    amount_paise=1000,
                    status="failed",
                    source="webhook",
                )
            )
            is None
        )


class TestCreditIsConservative:
    def test_settlement_after_an_action_is_credited(self, db_path):
        events = generate_events(30, seed=47)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM decisions WHERE action != 'DROP' LIMIT 1"
            ).fetchone()
        acted_id = row["event_id"]
        event = next(e for e in events if e.id == acted_id)

        matched = record_settlement(
            Settlement(
                payment_id="pay_settled",
                order_id=event.order_id,
                reference_id=event.id,
                amount_paise=event.amount,
                status="captured",
                source="webhook",
            )
        )
        assert matched == acted_id

        with db.connect() as conn:
            out = conn.execute(
                "SELECT * FROM outcomes WHERE event_id = ?", (acted_id,)
            ).fetchone()
        assert out["recovered"] == 1
        assert out["incremental_paise"] == event.amount
        assert out["source"].startswith("razorpay_")

    def test_settlement_on_a_dropped_payment_earns_no_credit(self, db_path):
        """The customer came back on their own. Real revenue, but the system
        did nothing to cause it, so it must not be billed as recovery."""
        events = generate_events(30, seed=47)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM decisions WHERE action = 'DROP' LIMIT 1"
            ).fetchone()
        dropped_id = row["event_id"]
        event = next(e for e in events if e.id == dropped_id)

        record_settlement(
            Settlement(
                payment_id="pay_organic",
                order_id=event.order_id,
                reference_id=event.id,
                amount_paise=event.amount,
                status="captured",
                source="webhook",
            )
        )

        with db.connect() as conn:
            out = conn.execute(
                "SELECT * FROM outcomes WHERE event_id = ?", (dropped_id,)
            ).fetchone()
        assert out["recovered"] == 1
        assert out["organic"] == 1
        assert out["incremental_paise"] == 0

    def test_a_decision_that_never_executed_earns_no_credit(self, db_path):
        """Regression: credit was keyed on the decision, not the action.

        A decision is not an intervention. Review-first mode records decisions
        and executes nothing; the kill switch stops execution mid-batch. In
        both cases the customer is untouched, so a settlement afterwards is
        indistinguishable from organic recovery - and was being billed as
        caused by Salvage.
        """
        events = generate_events(30, seed=71)
        process_batch(events, execute_actions=False)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM decisions WHERE action != 'DROP' LIMIT 1"
            ).fetchone()
            assert conn.execute(
                "SELECT COUNT(*) n FROM executions"
            ).fetchone()["n"] == 0
        target = next(e for e in events if e.id == row["event_id"])

        record_settlement(
            Settlement(
                payment_id="pay_unexecuted",
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

        assert out["recovered"] == 1, "the money did arrive"
        assert out["organic"] == 1
        assert out["incremental_paise"] == 0, (
            "credited a recovery to an action that was never taken"
        )

    def test_a_failed_payment_link_earns_no_credit(self, db_path, monkeypatch):
        """A link the API refused to create never reached the customer.

        The failure is recorded, which is right. What must not follow is
        crediting the recovery to an intervention that did not happen.
        """
        from salvage.integrations import razorpay_client

        monkeypatch.setattr(
            razorpay_client,
            "create_payment_link",
            lambda event, description=None: razorpay_client.PaymentLinkResult(
                ok=False,
                short_url=None,
                link_id=None,
                reference_id="ref",
                provider="razorpay_test",
                error="simulated API outage",
            ),
        )

        events = generate_events(40, seed=73)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM executions WHERE action = 'PAYMENT_LINK'"
                " AND status = 'FAILED' LIMIT 1"
            ).fetchone()
        assert row is not None, "expected a failed payment link to exercise this"
        target = next(e for e in events if e.id == row["event_id"])

        record_settlement(
            Settlement(
                payment_id="pay_after_failure",
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
        assert out["incremental_paise"] == 0
        assert out["organic"] == 1

    def test_verification_writes_an_audit_row(self, db_path):
        events = generate_events(10, seed=53)
        process_batch(events)
        event = events[0]

        record_settlement(
            Settlement(
                payment_id="pay_s",
                order_id=event.order_id,
                reference_id=event.id,
                amount_paise=event.amount,
                status="captured",
                source="webhook",
            )
        )

        with db.connect() as conn:
            stages = [
                r["stage"]
                for r in conn.execute(
                    "SELECT stage FROM audit_trail WHERE event_id = ?", (event.id,)
                ).fetchall()
            ]
        assert "VERIFIED" in stages


class TestNoActionOnSettledPayments:
    """The safety property: verified settlement stops everything."""

    def test_policy_drops_an_already_settled_payment(self):
        decision = decide(
            classify("bank_not_available", "GATEWAY_ERROR"),
            99_00_000,
            0.99,
            RecoveryContext(already_recovered=True),
        )
        assert decision.action is RecoveryAction.DROP
        assert decision.rule_id == "HARD_ALREADY_RECOVERED"

    def test_it_beats_even_a_perfect_expected_value(self):
        """No score may argue for taking a customer's money twice."""
        for propensity in (0.0, 0.5, 1.0):
            assert (
                decide(
                    classify("insufficient_funds", "BAD_REQUEST_ERROR"),
                    50_00_000,
                    propensity,
                    RecoveryContext(already_recovered=True),
                ).action
                is RecoveryAction.DROP
            )

    def test_no_valuation_is_computed(self):
        decision = decide(
            classify("card_expired", "BAD_REQUEST_ERROR"),
            10_00_000,
            0.9,
            RecoveryContext(already_recovered=True),
        )
        assert decision.valuation is None

    def test_recovered_ids_are_read_back(self, db_path):
        events = generate_events(12, seed=59)
        process_batch(events)
        event = events[0]

        assert recovered_event_ids([event.id]) == set()

        record_settlement(
            Settlement(
                payment_id="pay_s2",
                order_id=event.order_id,
                reference_id=event.id,
                amount_paise=event.amount,
                status="captured",
                source="webhook",
            )
        )
        assert recovered_event_ids([event.id]) == {event.id}

    def test_reprocessing_a_settled_payment_takes_no_action(self, db_path):
        """End to end: settle a payment, then replay the batch. The settled
        payment must not receive a second intervention."""
        events = generate_events(30, seed=61)
        process_batch(events)

        with db.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM decisions WHERE action != 'DROP' LIMIT 1"
            ).fetchone()
        acted_id = row["event_id"]
        event = next(e for e in events if e.id == acted_id)

        record_settlement(
            Settlement(
                payment_id="pay_done",
                order_id=event.order_id,
                reference_id=event.id,
                amount_paise=event.amount,
                status="captured",
                source="webhook",
            )
        )

        before = _execution_count(acted_id)
        process_batch(events, reprocess=True)
        after = _execution_count(acted_id)

        assert after == before, "acted again on a payment already settled"

        with db.connect() as conn:
            decision = conn.execute(
                "SELECT action, rule_id FROM decisions WHERE event_id = ?",
                (acted_id,),
            ).fetchone()
        assert decision["action"] == "DROP"
        assert decision["rule_id"] == "HARD_ALREADY_RECOVERED"


def _execution_count(event_id: str) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) n FROM executions WHERE event_id = ?", (event_id,)
        ).fetchone()["n"]
