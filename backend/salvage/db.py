"""SQLite persistence and the immutable audit trail.

Two design choices here are worth stating.

**The audit trail is append-only.** There is no UPDATE or DELETE against
`audit_trail` anywhere in the codebase. Every step a payment passes through -
ingestion, diagnosis, scoring, decision, execution, outcome - is a new row with
its own timestamp. Reconstructing what the system believed at any moment is a
matter of reading rows in order, not of trusting a mutable status column that
was overwritten five minutes later.

**Decisions are stored with their arithmetic, not just their conclusion.** The
alternatives that were considered and rejected are persisted alongside the
winner. "Why did you send a link instead of retrying?" is answerable from the
database months later, which is the difference between an audit trail and a log.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from salvage.config import settings

SCHEMA = """
-- Raw webhook payloads, exactly as received. Never modified.
CREATE TABLE IF NOT EXISTS events (
    id                        TEXT PRIMARY KEY,
    order_id                  TEXT,
    amount                    INTEGER NOT NULL,
    currency                  TEXT NOT NULL DEFAULT 'INR',
    method                    TEXT,
    status                    TEXT,
    created_at                TEXT NOT NULL,
    error_code                TEXT,
    error_description         TEXT,
    error_reason              TEXT,
    error_source              TEXT,
    error_step                TEXT,
    customer_id               TEXT,
    customer_name             TEXT,
    customer_phone            TEXT,
    customer_email            TEXT,
    customer_success_rate     REAL,
    customer_tenure_days      INTEGER,
    prior_payment_count       INTEGER,
    prior_failure_count       INTEGER,
    hours_since_last_success  REAL,
    attempt_number            INTEGER DEFAULT 1,
    hour_of_day               INTEGER,
    day_of_week               INTEGER,
    ingested_at               TEXT NOT NULL
);

-- One row per payment: the current decision and everything behind it.
CREATE TABLE IF NOT EXISTS decisions (
    event_id            TEXT PRIMARY KEY REFERENCES events(id),
    decided_at          TEXT NOT NULL,
    failure_class       TEXT NOT NULL,
    diagnosis_note      TEXT,
    diagnosis_confident INTEGER NOT NULL,
    base_propensity     REAL NOT NULL,
    action              TEXT NOT NULL,
    rule_id             TEXT NOT NULL,
    rationale           TEXT NOT NULL,
    action_probability  REAL,
    gross_expected      INTEGER,
    mdr                 INTEGER,
    action_cost         INTEGER,
    net_ev              INTEGER,
    considered_json     TEXT,
    constraints_json    TEXT,
    retry_after_hours   REAL,
    is_exception        INTEGER NOT NULL DEFAULT 0
);

-- What was actually executed, and what came back.
CREATE TABLE IF NOT EXISTS executions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            TEXT NOT NULL REFERENCES events(id),
    executed_at         TEXT NOT NULL,
    action              TEXT NOT NULL,
    status              TEXT NOT NULL,
    payment_link_url    TEXT,
    payment_link_id     TEXT,
    message_text        TEXT,
    message_channel     TEXT,
    scheduled_for       TEXT,
    provider            TEXT,
    error               TEXT,
    idempotency_key     TEXT UNIQUE
);

-- Adjudicated results. In production these arrive as payment.captured
-- webhooks; in evaluation they come from the simulator oracle.
CREATE TABLE IF NOT EXISTS outcomes (
    event_id                TEXT PRIMARY KEY REFERENCES events(id),
    observed_at             TEXT NOT NULL,
    recovered               INTEGER NOT NULL,
    recovered_paise         INTEGER NOT NULL DEFAULT 0,
    organic                 INTEGER NOT NULL DEFAULT 0,
    incremental_paise       INTEGER NOT NULL DEFAULT 0,
    source                  TEXT NOT NULL
);

-- Append-only. One row per step, forever.
CREATE TABLE IF NOT EXISTS audit_trail (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    stage       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_event  ON audit_trail(event_id, id);
CREATE INDEX IF NOT EXISTS idx_events_time  ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_dec_action   ON decisions(action);
CREATE INDEX IF NOT EXISTS idx_exec_event   ON executions(event_id);

-- Contact-frequency guardrails join executions back to a customer. Without
-- this index that lookup degrades to a full scan of both tables on every
-- batch, which is the first thing to bite as event volume grows.
CREATE INDEX IF NOT EXISTS idx_events_cust  ON events(customer_id);
CREATE INDEX IF NOT EXISTS idx_exec_contact ON executions(action, status);

-- The recovery queue orders by amount. Indexing it keeps pagination cheap
-- once the table is large enough that a sort would spill.
CREATE INDEX IF NOT EXISTS idx_events_amt   ON events(amount DESC);
CREATE INDEX IF NOT EXISTS idx_dec_except   ON decisions(is_exception);
"""


def now_iso() -> str:
    """Current UTC time, ISO 8601. One definition, used everywhere."""
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(
    path: Path | None = None, *, bulk: bool = False
) -> Iterator[sqlite3.Connection]:
    """Open a connection with sane defaults, committing or rolling back.

    WAL is enabled so the dashboard can read while a batch is still writing -
    without it, a long ingest run blocks every API request behind it.

    Args:
        path: Database file. Defaults to the configured location.
        bulk: Tune for write throughput on ingest. Raises the page cache and
            relaxes `synchronous` from FULL to NORMAL, which under WAL still
            survives a process crash - only a host power loss can lose the
            tail of the most recent transaction. That is the right trade for
            replayable batch ingest and the wrong one for interactive writes,
            so it is opt-in rather than the default.
    """
    target = path or settings.database_path
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if bulk:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-65536")  # 64 MB
        conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    """Create the schema if it does not exist. Safe to call repeatedly."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def reset_db(path: Path | None = None) -> None:
    """Drop and recreate everything. Used by the batch-load endpoint and tests."""
    target = path or settings.database_path
    with connect(target) as conn:
        for table in (
            "audit_trail",
            "outcomes",
            "executions",
            "decisions",
            "events",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)


def audit(
    conn: sqlite3.Connection,
    event_id: str,
    stage: str,
    summary: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one immutable step to the audit trail.

    Args:
        conn: Open connection.
        event_id: Payment this step belongs to.
        stage: Pipeline stage - INGESTED, DIAGNOSED, SCORED, DECIDED,
            EXECUTED, OUTCOME.
        summary: One human-readable line.
        detail: Structured payload, stored as JSON.
    """
    conn.execute(
        "INSERT INTO audit_trail (event_id, timestamp, stage, summary, detail_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            event_id,
            now_iso(),
            stage,
            summary,
            json.dumps(detail, default=str) if detail else None,
        ),
    )


def insert_event(conn: sqlite3.Connection, event: Any) -> None:
    """Persist a failed-payment event. Idempotent on payment id.

    Razorpay retries webhook deliveries, so the same `payment.failed` can and
    does arrive more than once. INSERT OR IGNORE makes redelivery a no-op
    rather than a duplicate recovery attempt against the same customer.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO events (
            id, order_id, amount, currency, method, status, created_at,
            error_code, error_description, error_reason, error_source, error_step,
            customer_id, customer_name, customer_phone, customer_email,
            customer_success_rate, customer_tenure_days, prior_payment_count,
            prior_failure_count, hours_since_last_success, attempt_number,
            hour_of_day, day_of_week, ingested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event.id,
            getattr(event, "order_id", None),
            event.amount,
            getattr(event, "currency", "INR"),
            getattr(event, "method", None),
            getattr(event, "status", "failed"),
            event.created_at,
            getattr(event, "error_code", None),
            getattr(event, "error_description", None),
            getattr(event, "error_reason", None),
            getattr(event, "error_source", None),
            getattr(event, "error_step", None),
            getattr(event, "customer_id", None),
            getattr(event, "customer_name", None),
            getattr(event, "customer_phone", None),
            getattr(event, "customer_email", None),
            getattr(event, "customer_success_rate", None),
            getattr(event, "customer_tenure_days", None),
            getattr(event, "prior_payment_count", None),
            getattr(event, "prior_failure_count", None),
            getattr(event, "hours_since_last_success", None),
            getattr(event, "attempt_number", 1),
            getattr(event, "hour_of_day", None),
            getattr(event, "day_of_week", None),
            now_iso(),
        ),
    )


def insert_decision(
    conn: sqlite3.Connection,
    event_id: str,
    classification: Any,
    base_propensity: float,
    decision: Any,
) -> None:
    """Persist a decision together with the alternatives it beat."""
    v = decision.valuation
    conn.execute(
        """
        INSERT OR REPLACE INTO decisions (
            event_id, decided_at, failure_class, diagnosis_note,
            diagnosis_confident, base_propensity, action, rule_id, rationale,
            action_probability, gross_expected, mdr, action_cost, net_ev,
            considered_json, constraints_json, retry_after_hours, is_exception
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            now_iso(),
            classification.failure_class.value,
            classification.note,
            int(classification.confident),
            base_propensity,
            decision.action.value,
            decision.rule_id,
            decision.rationale,
            v.probability if v else None,
            v.gross_expected_paise if v else None,
            v.mdr_paise if v else None,
            v.cost_paise if v else None,
            v.net_ev_paise if v else None,
            json.dumps(
                [
                    {
                        "action": c.action.value,
                        "probability": round(c.probability, 4),
                        "effectiveness": c.effectiveness,
                        "gross_expected": c.gross_expected_paise,
                        "cost": c.cost_paise,
                        "net_ev": c.net_ev_paise,
                    }
                    for c in decision.considered
                ]
            ),
            json.dumps(list(decision.constraints_applied)),
            decision.retry_after_hours,
            int(decision.is_exception),
        ),
    )


def insert_execution(
    conn: sqlite3.Connection, event_id: str, result: Any
) -> None:
    """Persist an executed action. Idempotency key prevents double execution."""
    conn.execute(
        """
        INSERT OR IGNORE INTO executions (
            event_id, executed_at, action, status, payment_link_url,
            payment_link_id, message_text, message_channel, scheduled_for,
            provider, error, idempotency_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            now_iso(),
            result.action.value,
            result.status,
            result.payment_link_url,
            result.payment_link_id,
            result.message_text,
            result.message_channel,
            result.scheduled_for,
            result.provider,
            result.error,
            result.idempotency_key,
        ),
    )


def insert_outcome(
    conn: sqlite3.Connection,
    event_id: str,
    recovered: bool,
    recovered_paise: int,
    organic: bool,
    incremental_paise: int,
    source: str,
) -> None:
    """Persist an adjudicated outcome."""
    conn.execute(
        """
        INSERT OR REPLACE INTO outcomes (
            event_id, observed_at, recovered, recovered_paise, organic,
            incremental_paise, source
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            event_id,
            now_iso(),
            int(recovered),
            recovered_paise,
            int(organic),
            incremental_paise,
            source,
        ),
    )
