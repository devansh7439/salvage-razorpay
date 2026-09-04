"""Verification: did the money actually arrive?

An attempted action is not recovered revenue. This module is the difference
between the two.

Until now the only thing that adjudicated outcomes was the simulator's oracle,
which is fine for evaluation and useless in production - it means a live
deployment would issue payment links and never learn whether a single one was
paid. Every recovery figure would have been a forecast wearing the clothes of a
measurement.

Two paths close that loop, and they exist for different failure modes:

**Push** - Razorpay sends `payment.captured`, `order.paid` and
`payment_link.paid` events when money lands. These are the fast path: recovery
is confirmed within seconds, by the payment processor rather than by us.

**Pull** - `reconcile` asks Razorpay for the current state of payments we
believe are still outstanding. Webhooks get dropped, retried out of order, and
missed entirely while a service is restarting; a system that only listens will
silently under-report recovery and, worse, keep chasing customers who have
already paid. Polling is the backstop that makes the push path safe to trust.

Verified settlement is authoritative over local belief in both directions: it
credits recovery that happened, and it stops interventions against payments
that no longer need them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from salvage import db
from salvage.config import settings
from salvage.executor import ACTED_STATUSES

logger = logging.getLogger(__name__)

#: Razorpay events that mean money arrived. Each carries the settled payment in
#: a different part of the envelope, which is why the extraction below is
#: per-event rather than one generic path.
SETTLEMENT_EVENTS: frozenset[str] = frozenset(
    {"payment.captured", "payment.authorized", "order.paid", "payment_link.paid"}
)

#: Payment statuses Razorpay reports for money that is in hand.
SETTLED_STATUSES: frozenset[str] = frozenset({"captured", "authorized"})


@dataclass(frozen=True, slots=True)
class Settlement:
    """A confirmed payment, extracted from a webhook or a status fetch.

    Attributes:
        payment_id: The payment that settled. For a recovery this is usually a
            *new* payment id, not the original failed one.
        order_id: Links the settlement back to the failed attempt.
        reference_id: Our idempotency key, echoed back on payment links. The
            most reliable join, because we chose it.
        amount_paise: Amount settled.
        status: Razorpay's status string.
        source: How we learned - `webhook` or `reconcile`.
    """

    payment_id: str
    order_id: str | None
    reference_id: str | None
    amount_paise: int
    status: str
    source: str


def extract_settlement(payload: dict[str, Any]) -> Settlement | None:
    """Pull a settlement out of a Razorpay webhook envelope.

    Returns None for events that are not settlements, so the caller can accept
    and ignore the long tail of events Razorpay sends without special-casing
    each one.
    """
    event = payload.get("event", "")
    if event not in SETTLEMENT_EVENTS:
        return None

    body = payload.get("payload", {}) or {}

    entity: dict[str, Any] = {}
    if "payment" in body:
        entity = (body.get("payment") or {}).get("entity", {}) or {}
    elif "payment_link" in body:
        entity = (body.get("payment_link") or {}).get("entity", {}) or {}
    elif "order" in body:
        entity = (body.get("order") or {}).get("entity", {}) or {}

    if not entity:
        return None

    notes = entity.get("notes") or {}

    return Settlement(
        payment_id=str(entity.get("id", "")),
        order_id=entity.get("order_id") or entity.get("id"),
        # `reference_id` is what we set on the payment link; `notes` carries the
        # original payment id when Razorpay echoes our metadata back.
        reference_id=entity.get("reference_id") or notes.get("original_payment_id"),
        amount_paise=int(entity.get("amount") or entity.get("amount_paid") or 0),
        status=str(entity.get("status", "")),
        source="webhook",
    )


def resolve_original_event(settlement: Settlement) -> str | None:
    """Find which failed payment a settlement belongs to.

    Tried in order of reliability. The reference id is our own idempotency key,
    so it is unambiguous when present. The order id is Razorpay's, and holds
    when the customer retried the same order. Falling back to the payment id
    only matches when the original attempt itself later settled.
    """
    with db.connect() as conn:
        if settlement.reference_id:
            row = conn.execute(
                "SELECT event_id FROM executions WHERE idempotency_key = ?",
                (settlement.reference_id,),
            ).fetchone()
            if row:
                return row["event_id"]

            row = conn.execute(
                "SELECT id FROM events WHERE id = ?", (settlement.reference_id,)
            ).fetchone()
            if row:
                return row["id"]

        if settlement.order_id:
            row = conn.execute(
                "SELECT id FROM events WHERE order_id = ? ORDER BY created_at DESC"
                " LIMIT 1",
                (settlement.order_id,),
            ).fetchone()
            if row:
                return row["id"]

        row = conn.execute(
            "SELECT id FROM events WHERE id = ?", (settlement.payment_id,)
        ).fetchone()
        return row["id"] if row else None


def record_settlement(settlement: Settlement) -> str | None:
    """Credit a verified settlement against the failed payment it recovered.

    Returns the event id it was matched to, or None when it belongs to no
    payment we are tracking - a normal case, since a merchant takes payments
    Salvage never saw fail.

    Credit is deliberately conservative. A settlement confirms the money
    arrived; it does not prove the intervention caused it. `incremental_paise`
    is only credited where the system actually acted, so a customer who
    returned unprompted still counts as recovered revenue and still earns
    Salvage nothing.
    """
    if settlement.status and settlement.status not in SETTLED_STATUSES:
        return None

    event_id = resolve_original_event(settlement)
    if event_id is None:
        logger.info(
            "Settlement %s matched no tracked payment; ignoring.",
            settlement.payment_id,
        )
        return None

    with db.connect() as conn:
        event = conn.execute(
            "SELECT amount FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if event is None:
            return None

        # Credit requires an intervention that actually happened, not merely
        # one that was decided on. Review-first mode records a full decision
        # and executes nothing; the kill switch stops execution mid-batch; a
        # payment link the API refused to create never reached the customer.
        # In all three the customer was left untouched, so money arriving
        # afterwards arrived on its own - and billing it as recovery would be
        # exactly the organic-credit error this system exists to refuse.
        placeholders = ",".join("?" * len(ACTED_STATUSES))
        execution = conn.execute(
            f"SELECT action FROM executions WHERE event_id = ?"
            f" AND status IN ({placeholders}) LIMIT 1",
            (event_id, *ACTED_STATUSES),
        ).fetchone()
        acted = execution is not None

        amount = settlement.amount_paise or event["amount"]

        db.insert_outcome(
            conn,
            event_id,
            recovered=True,
            recovered_paise=amount,
            organic=not acted,
            incremental_paise=amount if acted else 0,
            source=f"razorpay_{settlement.source}",
        )
        db.audit(
            conn,
            event_id,
            "VERIFIED",
            f"Settlement confirmed by Razorpay: Rs {amount / 100:,.2f}"
            + ("" if acted else " (no intervention was taken - not credited)"),
            {
                "settled_payment_id": settlement.payment_id,
                "reference_id": settlement.reference_id,
                "status": settlement.status,
                "source": settlement.source,
                "credited": acted,
            },
        )

    return event_id


def recovered_event_ids(event_ids: list[str]) -> set[str]:
    """Which of these payments are already settled.

    Read by the pipeline before deciding, so a scheduled retry cannot fire
    against a payment the customer has since paid.
    """
    ids = [i for i in event_ids if i]
    if not ids:
        return set()

    found: set[str] = set()
    with db.connect() as conn:
        for i in range(0, len(ids), 400):
            window = ids[i : i + 400]
            placeholders = ",".join("?" * len(window))
            found.update(
                r["event_id"]
                for r in conn.execute(
                    f"SELECT event_id FROM outcomes WHERE recovered = 1"
                    f" AND event_id IN ({placeholders})",
                    window,
                ).fetchall()
            )
    return found


def reconcile(limit: int = 100) -> dict[str, Any]:
    """Poll Razorpay for payments we believe are still outstanding.

    The backstop for the webhook path. Webhooks are dropped, delivered out of
    order, and missed entirely during a restart - a listener-only system
    silently under-reports recovery and keeps chasing customers who have
    already paid.

    Asks the processor for current state rather than trusting local belief,
    which is the only way to be right after an outage.
    """
    if not settings.razorpay_live:
        return {
            "checked": 0,
            "settled": 0,
            "skipped": "no Razorpay credentials - reconciliation needs the live API",
        }

    import razorpay

    client = razorpay.Client(
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    )

    with db.connect() as conn:
        pending = conn.execute(
            """
            SELECT x.event_id, x.payment_link_id, x.idempotency_key
            FROM executions x
            LEFT JOIN outcomes o ON o.event_id = x.event_id
            WHERE x.action = 'PAYMENT_LINK'
              AND x.status = 'EXECUTED'
              AND x.payment_link_id IS NOT NULL
              AND (o.event_id IS NULL OR o.recovered = 0)
            ORDER BY x.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    checked = 0
    settled = 0

    for row in pending:
        checked += 1
        try:
            link = client.payment_link.fetch(row["payment_link_id"])
        except Exception as exc:  # one bad row must not stop the sweep
            logger.warning(
                "Reconcile failed for %s: %s", row["payment_link_id"], exc
            )
            continue

        if link.get("status") != "paid":
            continue

        if record_settlement(
            Settlement(
                payment_id=str(link.get("id", "")),
                order_id=link.get("order_id"),
                reference_id=link.get("reference_id") or row["idempotency_key"],
                amount_paise=int(link.get("amount_paid") or link.get("amount") or 0),
                status="captured",
                source="reconcile",
            )
        ):
            settled += 1

    return {"checked": checked, "settled": settled}
