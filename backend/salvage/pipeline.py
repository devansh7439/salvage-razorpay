"""End-to-end recovery pipeline.

Six stages, each writing its own immutable audit row:

    INGESTED -> DIAGNOSED -> SCORED -> DECIDED -> EXECUTED -> OUTCOME

The staging matters for explainability. A single "processed" log line tells a
reviewer nothing about *where* a judgement was formed; six rows let them see
that the diagnosis came from a documented Razorpay reason, the score from a
calibrated model, and the action from a rule with a name - and that these are
separate steps that can be inspected and disputed independently.

The pipeline is built so that trail does not become the cost at volume: events
stream in, inference runs over large windows, writes commit in small
transactions, and audit rows are batched rather than issued one at a time.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable, Iterator
from typing import Any

from salvage import db
from salvage.controls import controls
from salvage.economics import DEFAULT_POLICY, MerchantPolicy, RecoveryAction
from salvage.executor import execute
from salvage.ml.predict import predict_propensity_batch
from salvage.policy import RecoveryContext, decide
from salvage.taxonomy import classify
from salvage.verification import recovered_event_ids

#: Events per write transaction. Bounds three things at once: peak memory, how
#: much work a failure can roll back, and how long a writer holds the database.
#:
#: A single transaction spanning a whole batch is fine at a thousand events and
#: pathological at a million - it pins every row in memory, blocks the WAL from
#: checkpointing, and turns any error into a total loss of the run.
CHUNK_SIZE = 500

#: Events scored per inference call.
#:
#: Deliberately decoupled from CHUNK_SIZE, because the two bound different
#: costs and want opposite sizes. A transaction wants to be small. An inference
#: call wants to be large: the calibrated model is five cross-validated forests
#: of 500 trees, and `predict_proba` pays roughly a second of fixed cost per
#: call almost regardless of how many rows it is handed.
#:
#:      500 rows/call -> 1.10 ms/event
#:     2000 rows/call -> 0.33 ms/event
#:     4000 rows/call -> 0.25 ms/event
#:
#: Tying inference batching to transaction batching cost ~4x throughput for no
#: benefit. Events are scored a window at a time, then written out in
#: transaction-sized slices of that window.
SCORING_WINDOW = 4096

#: SQLite caps host parameters per statement, so any IN list is itself chunked
#: rather than trusting the caller to keep it small.
PARAM_BATCH = 400


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
    which is the classic way a batch job turns quadratic without anyone
    noticing until the data grows.
    """
    ids = [c for c in customer_ids if c]
    if not ids:
        return {}

    counts: dict[str, int] = {}
    for i in range(0, len(ids), PARAM_BATCH):
        window = ids[i : i + PARAM_BATCH]
        placeholders = ",".join("?" * len(window))
        # Reads executions alone. Joining to events for customer_id made this
        # cost grow with the size of both tables, and since it runs once per
        # chunk that made ingest quadratic in batch size.
        rows = conn.execute(
            f"""
            SELECT customer_id AS cid, COUNT(*) AS n
            FROM executions
            WHERE customer_id IN ({placeholders})
              AND action IN ('PAYMENT_LINK', 'NOTIFY')
              AND status = 'EXECUTED'
            GROUP BY customer_id
            """,
            window,
        ).fetchall()
        counts.update({r["cid"]: r["n"] for r in rows if r["cid"]})
    return counts


def _already_decided(
    conn: sqlite3.Connection, event_ids: Collection[str]
) -> set[str]:
    """Which of these payments already carry a decision."""
    ids = [i for i in event_ids if i]
    if not ids:
        return set()

    seen: set[str] = set()
    for i in range(0, len(ids), PARAM_BATCH):
        window = ids[i : i + PARAM_BATCH]
        placeholders = ",".join("?" * len(window))
        seen.update(
            r["event_id"]
            for r in conn.execute(
                f"SELECT event_id FROM decisions WHERE event_id IN ({placeholders})",
                window,
            ).fetchall()
        )
    return seen


def process_batch(
    events: Iterable[Any],
    policy: MerchantPolicy = DEFAULT_POLICY,
    execute_actions: bool | None = None,
    chunk_size: int = CHUNK_SIZE,
    scoring_window: int = SCORING_WINDOW,
    reprocess: bool = False,
) -> dict[str, Any]:
    """Run failed payments through the full pipeline, in bounded chunks.

    Accepts any iterable, so a caller can stream events from a file, a queue,
    or a cursor without first building a list of them. Memory stays flat in the
    size of the input: only one scoring window is ever resident.

    Args:
        events: Failed payments to process. Consumed lazily.
        policy: Merchant guardrails.
        execute_actions: Override the live control plane. Leave as None in
            normal operation so the merchant's kill switch and review-first
            setting govern execution; a control the pipeline can ignore by
            default is not a control. When False, decisions are still made,
            persisted and auditable - only the side effects are withheld.
        chunk_size: Events per write transaction.
        scoring_window: Events per inference call. Larger than `chunk_size` on
            purpose - see SCORING_WINDOW.
        reprocess: Re-decide payments that already carry a decision. Off by
            default, so webhook redelivery is a no-op. Turn it on only for a
            deliberate replay, such as after changing policy parameters.

    Returns:
        A summary of what was decided and executed, including how many payments
        were skipped as already decided.
    """
    summary: dict[str, int] = {}
    exceptions = 0
    processed = 0
    skipped = 0

    # Read once per batch rather than per event, so a mid-batch toggle cannot
    # produce a run where some payments executed and others did not for no
    # recorded reason. The control is honoured at a boundary a human can point
    # at afterwards.
    live = controls.get()
    if execute_actions is None:
        execute_actions = live.executes

    for window in _chunks(events, scoring_window):
        # Razorpay redelivers a webhook on any non-2xx response or timeout, so
        # the same `payment.failed` arrives repeatedly in normal operation.
        #
        # The per-action idempotency key on `executions` stops the *same*
        # action running twice, but not a *different* one: on redelivery the
        # contact guardrails see the earlier message and legitimately pick
        # another action, so one payment could collect both a payment link and
        # a scheduled retry. Deciding a payment twice is the bug; skipping
        # anything already decided is the fix.
        #
        # Filtered before scoring, because inference is the expensive step and
        # there is no sense paying it for work about to be discarded.
        if not reprocess:
            with db.connect() as conn:
                decided = _already_decided(conn, [e.id for e in window])
            if decided:
                skipped += len(decided)
                window = [e for e in window if e.id not in decided]
        if not window:
            continue

        propensities = predict_propensity_batch(window, chunk_size=scoring_window)

        # Recovery is asynchronous: a payment can settle between one batch and
        # the next. Re-read verified settlement immediately before deciding, so
        # a scheduled retry cannot fire against money already collected.
        settled = recovered_event_ids([e.id for e in window])

        for start in range(0, len(window), chunk_size):
            result = _write_chunk(
                window[start : start + chunk_size],
                propensities[start : start + chunk_size],
                policy,
                execute_actions,
                settled,
            )
            processed += result["processed"]
            exceptions += result["exceptions"]
            for action, n in result["actions"].items():
                summary[action] = summary.get(action, 0) + n

    return {
        "processed": processed,
        "actions": summary,
        "exceptions": exceptions,
        "skipped": skipped,
        "executed": execute_actions,
        "agent_status": live.status,
    }


def _write_chunk(
    events: list[Any],
    propensities: list[float],
    policy: MerchantPolicy,
    execute_actions: bool,
    settled: set[str] | None = None,
) -> dict[str, Any]:
    """Decide, execute and persist one chunk inside a single transaction.

    Receives pre-computed propensities: scoring happens a window at a time
    upstream, so the transaction stays small without forcing inference into
    small batches.
    """
    if not events:
        return {"processed": 0, "actions": {}, "exceptions": 0}

    summary: dict[str, int] = {}
    exceptions = 0
    # Audit rows are buffered and flushed once per chunk. Six per payment,
    # issued as six separate statements, makes per-statement overhead rather
    # than the write itself the dominant cost of ingest.
    trail: list[db.AuditRow] = []

    with db.connect(bulk=True) as conn:
        contacts = _contact_counts(
            conn, {getattr(e, "customer_id", "") for e in events}
        )

        for event, propensity in zip(events, propensities):
            db.insert_event(conn, event)
            trail.append(
                db.audit_row(
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
            )

            classification = classify(
                event.error_reason,
                event.error_code,
                event.error_source,
                event.error_step,
            )
            trail.append(
                db.audit_row(
                    event.id,
                    "DIAGNOSED",
                    f"{classification.failure_class.value}: {classification.note}",
                    {
                        "failure_class": classification.failure_class.value,
                        "confident": classification.confident,
                        "razorpay_guidance": (
                            classification.entry.guidance
                            if classification.entry
                            else None
                        ),
                    },
                )
            )

            trail.append(
                db.audit_row(
                    event.id,
                    "SCORED",
                    f"Recovery propensity {propensity:.1%} (calibrated)",
                    {"base_propensity": round(propensity, 4)},
                )
            )

            context = RecoveryContext(
                attempts_so_far=max(0, getattr(event, "attempt_number", 1) - 1),
                contacts_today=contacts.get(getattr(event, "customer_id", ""), 0),
                already_recovered=event.id in (settled or set()),
            )
            decision = decide(
                classification, event.amount, propensity, context, policy
            )
            db.insert_decision(conn, event.id, classification, propensity, decision)
            trail.append(
                db.audit_row(
                    event.id,
                    "DECIDED",
                    f"{decision.action.value} - {decision.rationale}",
                    {
                        "action": decision.action.value,
                        "rule_id": decision.rule_id,
                        "net_ev_paise": (
                            decision.valuation.net_ev_paise
                            if decision.valuation
                            else None
                        ),
                        "considered": [
                            {"action": c.action.value, "net_ev_paise": c.net_ev_paise}
                            for c in decision.considered
                        ],
                        "constraints_applied": list(decision.constraints_applied),
                    },
                )
            )

            summary[decision.action.value] = summary.get(decision.action.value, 0) + 1
            if decision.is_exception:
                exceptions += 1

            if not execute_actions or decision.action is RecoveryAction.DROP:
                continue

            result = execute(event, classification, decision)
            db.insert_execution(
                conn, event.id, result, getattr(event, "customer_id", None)
            )
            trail.append(
                db.audit_row(
                    event.id,
                    "EXECUTED",
                    f"{result.action.value} -> {result.status}"
                    + (
                        f" ({result.payment_link_url})"
                        if result.payment_link_url
                        else ""
                    ),
                    {
                        "status": result.status,
                        "provider": result.provider,
                        "payment_link_url": result.payment_link_url,
                        "message_text": result.message_text,
                        "scheduled_for": result.scheduled_for,
                        "error": result.error,
                    },
                )
            )

            if result.action in (RecoveryAction.PAYMENT_LINK, RecoveryAction.NOTIFY):
                cid = getattr(event, "customer_id", "")
                if cid:
                    contacts[cid] = contacts.get(cid, 0) + 1

        db.audit_many(conn, trail)

    return {
        "processed": len(events),
        "actions": summary,
        "exceptions": exceptions,
    }


def record_outcomes(events: Iterable[Any], chunk_size: int = CHUNK_SIZE) -> int:
    """Adjudicate outcomes for processed payments and persist them.

    In production these arrive as `payment.captured` webhooks. In the demo the
    simulator oracle stands in, and the `source` column records which, so a
    reviewer can always tell a measured outcome from a simulated one.
    """
    return sum(_record_outcome_chunk(chunk) for chunk in _chunks(events, chunk_size))


def _record_outcome_chunk(events: list[Any]) -> int:
    """Adjudicate and persist one chunk of outcomes."""
    from salvage.simulator.oracle import observe, observe_do_nothing

    if not events:
        return 0

    written = 0
    trail: list[db.AuditRow] = []

    with db.connect(bulk=True) as conn:
        # Only the decisions for this chunk, rather than every decision ever
        # made. The unscoped version loaded the whole table on each call.
        ids = [e.id for e in events]
        rows: dict[str, str] = {}
        for i in range(0, len(ids), PARAM_BATCH):
            window = ids[i : i + PARAM_BATCH]
            placeholders = ",".join("?" * len(window))
            rows.update(
                {
                    r["event_id"]: r["action"]
                    for r in conn.execute(
                        "SELECT event_id, action FROM decisions"
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
            trail.append(
                db.audit_row(
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
            )
            written += 1

        db.audit_many(conn, trail)

    return written
