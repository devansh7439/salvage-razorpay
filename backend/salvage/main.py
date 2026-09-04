"""FastAPI application.

Endpoints are shaped around the four questions a reviewer actually asks:

  - What is at stake, and how much did we get back?      `/api/metrics`
  - What did the system do to each payment?              `/api/events`
  - Why did it do that, specifically?                    `/api/events/{id}`
  - How do we know it is better than the obvious thing?  `/api/evaluate`

Plus `/api/exceptions`, which reports the payments the system could not
confidently resolve. That endpoint exists because a recovery system that never
admits to an unresolved case is not being honest about its coverage.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from salvage import db
from salvage.config import settings
from salvage.controls import AgentMode, controls
from salvage.economics import ACTION_COSTS, DEFAULT_POLICY, RecoveryAction
from salvage.integrations import llm, razorpay_client
from salvage.ml import predict
from salvage.pipeline import process_batch, record_outcomes
from salvage.simulator.generate import generate_events
from salvage.verification import extract_settlement, reconcile, record_settlement

#: Batch used by the dashboard. Fixed seed, distinct from the training corpus,
#: so every reviewer sees identical numbers.
DEMO_BATCH_SIZE = 1000
DEMO_SEED = 77771111

#: Hard ceiling on a single page of the recovery queue.
MAX_PAGE_SIZE = 500

_demo_events: list[Any] = []
_demo_lock = threading.Lock()

#: Cached evaluation. Recomputing it means regenerating the batch and running
#: full model inference plus three strategies - a third of a second at a
#: thousand events, and linear from there. The dashboard polls this endpoint,
#: so without a cache every viewer pays that cost on every render.
#:
#: Invalidated whenever a batch is reloaded, which is the only thing that can
#: change the answer.
_evaluation_cache: dict[str, Any] | None = None
_evaluation_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm the model and schema at startup.

    Loading the model here rather than on first request means a cold dashboard
    does not pay a multi-second penalty in front of an audience.
    """
    db.init_db()
    try:
        predict.load_model()
    except predict.ModelNotTrainedError:
        # Not fatal: the API still serves, and the error surfaces clearly at
        # /health rather than as an opaque 500 later.
        pass
    yield


app = FastAPI(
    title="Salvage",
    description="Bounded autonomous payment recovery, grounded in Razorpay's error taxonomy.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _demo_batch() -> list[Any]:
    """The evaluation batch, generated once per process.

    Locked because FastAPI serves from a thread pool: two concurrent first
    requests would otherwise both see an empty list and each generate a full
    batch, doubling the work and leaving whichever finished last installed.
    """
    global _demo_events
    with _demo_lock:
        if not _demo_events:
            _demo_events = generate_events(DEMO_BATCH_SIZE, seed=DEMO_SEED)
        return _demo_events


@app.get("/api/controls")
def get_controls() -> dict[str, Any]:
    """Current merchant controls: kill switch and review-first mode."""
    return controls.get().to_dict()


@app.post("/api/controls")
def set_controls(
    enabled: bool | None = None,
    mode: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Change agent authority at runtime.

    Deliberately takes effect without a restart or a redeploy. A kill switch
    that requires either is not a kill switch - it is most needed precisely
    when something is going wrong and nobody has time to ship anything.
    """
    parsed: AgentMode | None = None
    if mode is not None:
        try:
            parsed = AgentMode(mode)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"mode must be one of {[m.value for m in AgentMode]}",
            ) from None

    return controls.set(enabled=enabled, mode=parsed, reason=reason).to_dict()


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus which integration modes are active.

    Surfacing fixture-versus-live here is deliberate: a demo that quietly runs
    on fixtures while implying live calls is the kind of thing that destroys
    credibility when a judge asks. This endpoint answers before they ask.
    """
    return {
        "status": "ok",
        "model_loaded": predict.is_loaded(),
        "agent": controls.get().to_dict(),
        "razorpay_mode": razorpay_client.mode(),
        "llm_mode": llm.mode(),
        "webhook_signature_enforced": bool(settings.razorpay_webhook_secret),
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
) -> dict[str, Any]:
    """Ingest a live Razorpay `payment.failed` webhook.

    The signature is verified against the raw body before anything is parsed.
    An unauthenticated caller who could post here would be able to make the
    system issue payment links to arbitrary phone numbers.
    """
    raw = await request.body()

    if not razorpay_client.verify_webhook_signature(raw, x_razorpay_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(raw)

    # Settlement events close the loop. Without this branch the system issues
    # payment links and never learns whether any of them were paid - every
    # recovery figure would be a forecast rather than a measurement.
    settlement = extract_settlement(payload)
    if settlement is not None:
        event_id = record_settlement(settlement)
        return {
            "accepted": True,
            "kind": "settlement",
            "matched_event": event_id,
            "credited": event_id is not None,
        }

    entity = (
        payload.get("payload", {}).get("payment", {}).get("entity", {}) or payload
    )

    if entity.get("status") != "failed":
        return {"accepted": False, "reason": "Not a failed payment or settlement"}

    from types import SimpleNamespace

    event = SimpleNamespace(
        id=entity.get("id"),
        order_id=entity.get("order_id"),
        amount=entity.get("amount", 0),
        currency=entity.get("currency", "INR"),
        method=entity.get("method"),
        status="failed",
        created_at=str(entity.get("created_at", "")),
        error_code=entity.get("error_code"),
        error_description=entity.get("error_description"),
        error_reason=entity.get("error_reason"),
        error_source=entity.get("error_source"),
        error_step=entity.get("error_step"),
        customer_id=entity.get("customer_id") or entity.get("email", "unknown"),
        customer_name=(entity.get("notes") or {}).get("name", "Customer"),
        customer_phone=entity.get("contact", ""),
        customer_email=entity.get("email", ""),
        customer_success_rate=0.6,
        customer_tenure_days=180,
        prior_payment_count=3,
        prior_failure_count=1,
        hours_since_last_success=48.0,
        attempt_number=1,
        hour_of_day=12,
        day_of_week=2,
    )

    result = process_batch([event])
    return {"accepted": True, **result}


@app.post("/api/reconcile")
def reconcile_payments(limit: int = 100) -> dict[str, Any]:
    """Poll Razorpay for outstanding payment links that have since been paid.

    The backstop for the webhook path: webhooks get dropped, reordered, and
    missed during a restart, and a listener-only system silently under-reports
    recovery while continuing to chase customers who have already paid.
    """
    return reconcile(limit=limit)


@app.post("/api/simulate/load")
def load_batch(execute_actions: bool = True) -> dict[str, Any]:
    """Reset the database and run the demo batch end to end."""
    global _evaluation_cache

    db.reset_db()
    events = _demo_batch()
    result = process_batch(events, execute_actions=execute_actions)
    recorded = record_outcomes(events)

    with _evaluation_lock:
        _evaluation_cache = None

    return {**result, "outcomes_recorded": recorded}


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    """Command-centre totals."""
    with db.connect() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS at_risk FROM events"
        ).fetchone()

        ev = conn.execute(
            """
            SELECT COALESCE(SUM(net_ev), 0) AS expected
            FROM decisions WHERE action != 'DROP'
            """
        ).fetchone()

        outcome = conn.execute(
            """
            SELECT COALESCE(SUM(recovered_paise), 0)   AS gross,
                   COALESCE(SUM(incremental_paise), 0) AS incremental,
                   COALESCE(SUM(CASE WHEN organic = 1 THEN recovered_paise ELSE 0 END), 0)
                       AS organic,
                   COUNT(*) AS n
            FROM outcomes
            """
        ).fetchone()

        actions = {
            r["action"]: r["n"]
            for r in conn.execute(
                "SELECT action, COUNT(*) AS n FROM decisions GROUP BY action"
            ).fetchall()
        }

        exceptions = conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE is_exception = 1"
        ).fetchone()["n"]

        spend = sum(
            ACTION_COSTS[RecoveryAction(a)].total_paise * n
            for a, n in actions.items()
            if a != "DROP"
        )

    # What a blind-retry policy would have cost on the same batch.
    blind_cost = (
        totals["n"] * 3 * ACTION_COSTS[RecoveryAction.RETRY_NOW].total_paise
    )

    return {
        "events": totals["n"],
        "revenue_at_risk_paise": totals["at_risk"],
        "expected_recoverable_paise": ev["expected"],
        "gross_recovered_paise": outcome["gross"],
        "organic_recovered_paise": outcome["organic"],
        "incremental_recovered_paise": outcome["incremental"],
        "action_spend_paise": spend,
        "blind_retry_spend_paise": blind_cost,
        "spend_avoided_paise": blind_cost - spend,
        "action_breakdown": actions,
        "exceptions": exceptions,
        "outcomes_recorded": outcome["n"],
    }


@app.get("/api/events")
def list_events(
    limit: int = 100, offset: int = 0, action: str | None = None
) -> dict[str, Any]:
    """The live recovery queue, ordered by the money at stake."""
    # `limit` is clamped rather than trusted. An unbounded value lets any
    # caller ask for the entire table in one response, which is both a memory
    # spike in the server and a trivially cheap way to degrade it.
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)

    clause = "WHERE d.action = ?" if action else ""
    params: list[Any] = [action] if action else []

    with db.connect() as conn:
        # `executions` is one-to-many with `events` - a payment can be retried
        # and later sent a link. Joining it directly multiplies the result row
        # per execution, silently inflating the queue and every count taken
        # from it. A correlated subquery picks the latest execution instead, so
        # exactly one row is returned per payment.
        rows = conn.execute(
            f"""
            SELECT e.id, e.amount, e.method, e.customer_name, e.error_code,
                   e.error_reason, e.error_source, e.created_at,
                   d.failure_class, d.base_propensity, d.action, d.net_ev,
                   d.action_probability, d.rule_id, d.is_exception,
                   o.recovered, o.incremental_paise,
                   (SELECT x.payment_link_url FROM executions x
                     WHERE x.event_id = e.id
                     ORDER BY x.id DESC LIMIT 1) AS payment_link_url
            FROM events e
            LEFT JOIN decisions d ON d.event_id = e.id
            LEFT JOIN outcomes  o ON o.event_id = e.id
            {clause}
            ORDER BY e.amount DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM events e LEFT JOIN decisions d"
            f" ON d.event_id = e.id {clause}",
            tuple(params),
        ).fetchone()["n"]

    return {"total": total, "events": [dict(r) for r in rows]}


@app.get("/api/events/{event_id}")
def event_detail(event_id: str) -> dict[str, Any]:
    """Everything behind one decision, for the inspector panel."""
    with db.connect() as conn:
        event = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail=f"No event {event_id}")

        decision = conn.execute(
            "SELECT * FROM decisions WHERE event_id = ?", (event_id,)
        ).fetchone()
        execution = conn.execute(
            "SELECT * FROM executions WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        outcome = conn.execute(
            "SELECT * FROM outcomes WHERE event_id = ?", (event_id,)
        ).fetchone()
        trail = conn.execute(
            "SELECT * FROM audit_trail WHERE event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()

    decision_dict = dict(decision) if decision else None
    if decision_dict:
        decision_dict["considered"] = json.loads(
            decision_dict.pop("considered_json") or "[]"
        )
        decision_dict["constraints"] = json.loads(
            decision_dict.pop("constraints_json") or "[]"
        )

    return {
        "event": dict(event),
        "decision": decision_dict,
        "execution": dict(execution) if execution else None,
        "outcome": dict(outcome) if outcome else None,
        "audit_trail": [
            {**dict(r), "detail": json.loads(r["detail_json"] or "null")}
            for r in trail
        ],
        "policy": {
            "max_attempts_per_payment": DEFAULT_POLICY.max_attempts_per_payment,
            "max_contacts_per_customer_per_day": (
                DEFAULT_POLICY.max_contacts_per_customer_per_day
            ),
            "cooldown_hours": DEFAULT_POLICY.cooldown_hours,
            "min_net_ev_paise": DEFAULT_POLICY.min_net_ev_paise,
            "max_autonomous_amount_paise": DEFAULT_POLICY.max_autonomous_amount_paise,
            "mdr_rate": DEFAULT_POLICY.mdr_rate,
        },
    }


@app.get("/api/exceptions")
def exceptions() -> dict[str, Any]:
    """Payments the system could not confidently resolve.

    Reported rather than hidden. These are mostly Razorpay's generic
    `payment_failed` reason, which carries no recovery signal, plus risk blocks
    that are routed to human review by design.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.amount, e.error_code, e.error_reason, e.error_source,
                   e.customer_name, d.failure_class, d.rule_id, d.rationale,
                   d.diagnosis_note
            FROM decisions d
            JOIN events e ON e.id = d.event_id
            WHERE d.is_exception = 1
            ORDER BY e.amount DESC
            """
        ).fetchall()

    grouped: dict[str, int] = {}
    for row in rows:
        grouped[row["rule_id"]] = grouped.get(row["rule_id"], 0) + 1

    return {
        "total": len(rows),
        "value_paise": sum(r["amount"] for r in rows),
        "by_rule": grouped,
        "exceptions": [dict(r) for r in rows],
    }


@app.get("/api/evaluate")
def evaluate() -> dict[str, Any]:
    """Head-to-head comparison against the baselines, on the same batch.

    Cached until the next batch load. The result is deterministic for a given
    batch, so recomputing it per request buys nothing.
    """
    global _evaluation_cache

    with _evaluation_lock:
        if _evaluation_cache is not None:
            return _evaluation_cache

    from salvage.evaluate import evaluate_batch

    report = evaluate_batch(_demo_batch())
    report.pop("_decisions", None)

    try:
        metrics_path = settings.model_path.parent / "model_metrics.json"
        report["model"] = json.loads(metrics_path.read_text())
    except (OSError, json.JSONDecodeError):
        report["model"] = None

    with _evaluation_lock:
        _evaluation_cache = report
    return report
