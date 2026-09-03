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
from collections.abc import Collection, Iterable, Iterator
from typing import Any

from salvage import db
from salvage.economics import DEFAULT_POLICY, MerchantPolicy, RecoveryAction
from salvage.executor import execute
from salvage.ml.predict import predict_propensity_batch
from salvage.policy import RecoveryContext, decide
from salvage.taxonomy import classify


#: Events per transaction. Bounds three things at once: peak memory, how much
#: work a failure can roll back, and how long writers hold the database.
#:
#: A single transaction spanning a whole batch is fine at a thousand events and
#: pathological at a million - it pins every row in memory, blocks the WAL from
#: checkpointing, and turns any error into a total loss of the run.
CHUNK_SIZE = 500


def _chunks(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Yield fixed-size lists from any iterable, without materialising it."""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _contact_counts(
    conn: sqlite3.Connection, customer_ids: Collection[str]
) -> dict[str, int]:
    """Messages already sent to these specific customers.

    Contact limits are enforced against what was actually executed rather than
    an in-memory counter, so they survive a restart and hold across separate
    batches. A counter that resets when the process does is not a guardrail.

    Scoped to the customers in the current chunk rather than loading the whole
    table. The unscoped version was O(all customers ever seen) on every chunk,
    which is the classic way a batch job becomes quadratic without anyone
    noticing until the data grows.
    """
    ids = [c for c in customer_ids if c]
    if not ids:
        return {}

    counts: dict[str, int] = {}
    # SQLite caps host parameters (999 on older builds), so the IN list is
    # itself chunked rather than assuming the caller kept it small.
    for i in range(0, len(ids), 400):
        window = ids[i : i + 400]
        placeholders = ",".join("?" * len(window))
        rows = conn.execute(
            f"""
            SELECT e.customer_id AS cid, COUNT(*) AS n
            FROM executions x
            JOIN events e ON e.id = x.event_id
            WHERE x.action IN ('PAYMENT_LINK', 'NOTIFY')
              AND x.status = 'EXECUTED'
              AND e.customer_id IN ({placeholders})
            GROUP BY e.customer_id
            """,
            window,
        ).fetchall()
        counts.update({r["cid"]: r["n"] for r in rows if r["cid"]})
    return counts


def process_batch(
    events: Iterable[Any],
    policy: MerchantPolicy = DEFAULT_POLICY,
    execute_actions: bool = True,
    chunk_size: int = CHUNK_SIZE,
    reprocess: bool = False,
) -> dict[str, Any]:
    """Run failed payments through the full pipeline, in bounded chunks.

    Accepts any iterable, so a caller can stream events from a file, a queue,
    or a cursor without first building a list of them. Memory stays flat in the
    size of the input: only one chunk is ever resident.

    Args:
        events: Failed payments to process. Consumed lazily.
        policy: Merchant guardrails.
        execute_actions: When False, decisions are made and persisted but no
            external calls occur. This is the "review-first" posture a merchant
            would run on day one, before granting the agent authority to act.
        chunk_size: Events per transaction.
        reprocess: Re-decide payments that already carry a decision. Off by
            default so webhook redelivery is a no-op; turn it on only for a
            deliberate replay, such as after changing policy parameters.

    Returns:
        A summary of what was decided and executed across every chunk,
        including how many payments were skipped as already decided.
    """
    summary: dict[str, int] = {}
    exceptions = 0
    processed = 0
    skipped = 0

    for chunk in _chunks(events, chunk_size):
        result = _process_chunk(chunk, policy, execute_actions, reprocess)
        processed += result["processed"]
        exceptions += result["exceptions"]
        skipped += result["skipped"]
        for action, n in result["actions"].items():
            summary[action] = summary.get(action, 0) + n

    return {
        "processed": processed,
        "actions": summary,
        "exceptions": exceptions,
        "skipped": skipped,
    }


def _already_decided(
    conn: sqlite3.Connection, event_ids: Collection[str]
) -> set[str]:
    """Which of these payments already carry a decision."""
    ids = [i for i in event_ids if i]
    if not ids:
        return set()

    seen: set[str] = set()
    for i in range(0, len(ids), 400):
        window = ids[i : i + 400]
        placeholders = ",".join("?" * len(window))
        seen.update(
            r["event_id"]
            for r in conn.execute(
                f"SELECT event_id FROM decisions WHERE event_id IN ({placeholders})",
                window,
            ).fetchall()
        )
    return seen


def _process_chunk(
    events: list[Any],
    policy: MerchantPolicy,
    execute_actions: bool,
    reprocess: bool = False,
) -> dict[str, Any]:
    """Process one chunk inside a single transaction."""
    if not events:
        return {"processed": 0, "actions": {}, "exceptions": 0, "skipped": 0}

    summary: dict[str, int] = {}
    exceptions = 0
    skipped = 0

    with db.connect(bulk=True) as conn:
        # Razorpay redelivers a webhook on any non-2xx response or timeout, so
        # the same `payment.failed` arrives repeatedly in normal operation.
        #
        # The per-action idempotency key on `executions` stops the *same*
        # action running twice, but not a *different* one: on redelivery the
        # contact guardrails see the earlier message and legitimately pick
        # another action, so one payment could collect both a payment link and
        # a scheduled retry. Deciding a payment twice is the bug; skipping
        # anything already decided is the fix.
        if not reprocess:
            decided = _already_decided(conn, [e.id for e in events])
            if decided:
                skipped = len(decided)
                events = [e for e in events if e.id not in decided]
                if not events:
                    return {
                        "processed": 0,
                        "actions": {},
                        "exceptions": 0,
                        "skipped": skipped,
                    }

        # Scored in one batch: per-event inference spends nearly all its time
        # in pipeline overhead rather than in the model.
        propensities = predict_propensity_batch(events)

        contacts = _contact_counts(
            conn, {getattr(e, "customer_id", "") for e in events}
        )

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
        "skipped": skipped,
    }


def record_outcomes(
    events: Iterable[Any], chunk_size: int = CHUNK_SIZE
) -> int:
    """Adjudicate outcomes for processed payments and persist them.

    In production these arrive as `payment.captured` webhooks. In the demo the
    simulator oracle stands in, and the `source` column records which, so a
    reviewer can always tell a measured outcome from a simulated one.
    """
    return sum(
        _record_outcome_chunk(chunk) for chunk in _chunks(events, chunk_size)
    )


def _record_outcome_chunk(events: list[Any]) -> int:
    """Adjudicate and persist one chunk of outcomes."""
    from salvage.simulator.oracle import observe, observe_do_nothing

    if not events:
        return 0

    written = 0
    with db.connect(bulk=True) as conn:
        # Only the decisions for this chunk, rather than every decision ever
        # made. The unscoped version loaded the whole table on each call.
        ids = [e.id for e in events]
        rows: dict[str, str] = {}
        for i in range(0, len(ids), 400):
            window = ids[i : i + 400]
            placeholders = ",".join("?" * len(window))
            rows.update(
                {
                    r["event_id"]: r["action"]
                    for r in conn.execute(
                        f"SELECT event_id, action FROM decisions"
                        f" WHERE event_id IN ({placeholders})",
                        window,
                    ).fetchall()
                }
            )

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
