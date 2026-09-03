"""End-to-end recovery pipeline.

Six stages, each writing its own immutable audit row:

    INGESTED -> DIAGNOSED -> SCORED -> DECIDED -> EXECUTED -> OUTCOME

The staging matters for explainability. A single "processed" log line tells a
reviewer nothing about *where* a judgement was formed; six rows let them see
that the diagnosis came from a documented Razorpay reason, the score from a
calibrated model, and the action from a rule with a name - and that these are
separate steps that can be inspected and disputed independently.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from salvage import db
from salvage.economics import DEFAULT_POLICY, MerchantPolicy, RecoveryAction
from salvage.executor import execute
from salvage.ml.predict import predict_propensity_batch
from salvage.policy import RecoveryContext, decide
from salvage.taxonomy import classify


def _contact_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Messages already sent per customer, read back from executions.

    Contact limits are enforced against what was actually executed rather than
    an in-memory counter, so they survive a restart and hold across separate
    batches. A counter that resets when the process does is not a guardrail.
    """
    rows = conn.execute(
        """
        SELECT e.customer_id AS cid, COUNT(*) AS n
        FROM executions x
        JOIN events e ON e.id = x.event_id
        WHERE x.action IN ('PAYMENT_LINK', 'NOTIFY') AND x.status = 'EXECUTED'
        GROUP BY e.customer_id
        """
    ).fetchall()
    return {r["cid"]: r["n"] for r in rows if r["cid"]}


def process_batch(
    events: list[Any],
    policy: MerchantPolicy = DEFAULT_POLICY,
    execute_actions: bool = True,
) -> dict[str, Any]:
    """Run a batch of failed payments through the full pipeline.

    Args:
        events: Failed payments to process.
        policy: Merchant guardrails.
        execute_actions: When False, decisions are made and persisted but no
            external calls occur. This is the "review-first" posture a merchant
            would run on day one, before granting the agent authority to act.

    Returns:
        A summary of what was decided and executed.
    """
    if not events:
        return {"processed": 0, "actions": {}, "exceptions": 0}

    # Scored in one batch: per-event inference spends nearly all its time in
    # pipeline overhead rather than in the model.
    propensities = predict_propensity_batch(events)

    summary: dict[str, int] = {}
    exceptions = 0

    with db.connect() as conn:
        contacts = _contact_counts(conn)

        for event, propensity in zip(events, propensities):
            db.insert_event(conn, event)
            db.audit(
                conn,
                event.id,
                "INGESTED",
                f"payment.failed received for Rs {event.amount / 100:,.2f}",
                {
                    "amount_paise": event.amount,
                    "method": getattr(event, "method", None),
                    "error_code": getattr(event, "error_code", None),
                    "error_reason": getattr(event, "error_reason", None),
                },
            )

            classification = classify(
                event.error_reason,
                event.error_code,
                event.error_source,
                event.error_step,
            )
            db.audit(
                conn,
                event.id,
                "DIAGNOSED",
                f"{classification.failure_class.value}: {classification.note}",
                {
                    "failure_class": classification.failure_class.value,
                    "confident": classification.confident,
                    "razorpay_guidance": (
                        classification.entry.guidance if classification.entry else None
                    ),
                },
            )

            db.audit(
                conn,
                event.id,
                "SCORED",
                f"Recovery propensity {propensity:.1%} (calibrated)",
                {"base_propensity": round(propensity, 4)},
            )

            context = RecoveryContext(
                attempts_so_far=max(0, getattr(event, "attempt_number", 1) - 1),
                contacts_today=contacts.get(getattr(event, "customer_id", ""), 0),
            )
            decision = decide(
                classification, event.amount, propensity, context, policy
            )
            db.insert_decision(conn, event.id, classification, propensity, decision)
            db.audit(
                conn,
                event.id,
                "DECIDED",
                f"{decision.action.value} - {decision.rationale}",
                {
                    "action": decision.action.value,
                    "rule_id": decision.rule_id,
                    "net_ev_paise": (
                        decision.valuation.net_ev_paise if decision.valuation else None
                    ),
                    "considered": [
                        {"action": c.action.value, "net_ev_paise": c.net_ev_paise}
                        for c in decision.considered
                    ],
                    "constraints_applied": list(decision.constraints_applied),
                },
            )

            summary[decision.action.value] = summary.get(decision.action.value, 0) + 1
            if decision.is_exception:
                exceptions += 1

            if not execute_actions or decision.action is RecoveryAction.DROP:
                continue

            result = execute(event, classification, decision)
            db.insert_execution(conn, event.id, result)
            db.audit(
                conn,
                event.id,
                "EXECUTED",
                f"{result.action.value} -> {result.status}"
                + (f" ({result.payment_link_url})" if result.payment_link_url else ""),
                {
                    "status": result.status,
                    "provider": result.provider,
                    "payment_link_url": result.payment_link_url,
                    "message_text": result.message_text,
                    "scheduled_for": result.scheduled_for,
                    "error": result.error,
                },
            )

            if result.action in (RecoveryAction.PAYMENT_LINK, RecoveryAction.NOTIFY):
                cid = getattr(event, "customer_id", "")
                if cid:
                    contacts[cid] = contacts.get(cid, 0) + 1

    return {
        "processed": len(events),
        "actions": summary,
        "exceptions": exceptions,
    }


def record_outcomes(events: list[Any]) -> int:
    """Adjudicate outcomes for processed payments and persist them.

    In production these arrive as `payment.captured` webhooks. In the demo the
    simulator oracle stands in, and the `source` column records which, so a
    reviewer can always tell a measured outcome from a simulated one.
    """
    from salvage.simulator.oracle import observe, observe_do_nothing

    written = 0
    with db.connect() as conn:
        rows = {
            r["event_id"]: r["action"]
            for r in conn.execute("SELECT event_id, action FROM decisions").fetchall()
        }

        for event in events:
            action_name = rows.get(event.id)
            if action_name is None:
                continue

            classification = classify(
                event.error_reason,
                event.error_code,
                event.error_source,
                event.error_step,
            )
            failure_class = classification.failure_class.value
            action = RecoveryAction(action_name)

            outcome = (
                observe_do_nothing(event, failure_class)
                if action is RecoveryAction.DROP
                else observe(event, action, failure_class)
            )

            db.insert_outcome(
                conn,
                event.id,
                outcome.recovered,
                outcome.recovered_paise,
                outcome.would_have_recovered_organically,
                outcome.incremental_paise,
                "simulator_oracle",
            )
            db.audit(
                conn,
                event.id,
                "OUTCOME",
                (
                    f"Recovered Rs {outcome.recovered_paise / 100:,.2f}"
                    if outcome.recovered
                    else "Not recovered"
                )
                + (
                    " (would have recovered organically - no credit claimed)"
                    if outcome.would_have_recovered_organically
                    else ""
                ),
                {
                    "recovered": outcome.recovered,
                    "recovered_paise": outcome.recovered_paise,
                    "organic": outcome.would_have_recovered_organically,
                    "incremental_paise": outcome.incremental_paise,
                },
            )
            written += 1

    return written
