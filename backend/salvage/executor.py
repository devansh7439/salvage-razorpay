"""Execution: turning an approved decision into a real-world action.

The executor is deliberately incurious. It receives a `PolicyDecision` that has
already cleared every guardrail and does exactly what it says - it contains no
thresholds, no scores, and no capacity to change its mind. If a decision to
DROP arrives here, nothing happens; there is no path through this module that
can escalate a DROP into an action.

Keeping execution free of judgement is what makes the policy engine's audit
trail trustworthy. If the executor could second-guess a decision, the recorded
rationale would describe something other than what actually occurred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from salvage.economics import RecoveryAction
from salvage.integrations import llm, razorpay_client
from salvage.policy import PolicyDecision
from salvage.taxonomy import Classification


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What the executor actually did.

    Attributes:
        action: The action executed.
        status: EXECUTED, SCHEDULED, QUEUED, SKIPPED, or FAILED.
        payment_link_url: Live or fixture Razorpay short URL.
        payment_link_id: Razorpay's link identifier.
        message_text: The customer-facing copy, when one was generated.
        message_channel: Delivery channel for that copy.
        scheduled_for: ISO timestamp for a deferred retry.
        provider: Which backend serviced the call, so live and fixture runs are
            distinguishable in the audit trail rather than looking identical.
        error: Failure detail, if any.
        idempotency_key: Stable key preventing duplicate execution on replay.
    """

    action: RecoveryAction
    status: str
    payment_link_url: str | None = None
    payment_link_id: str | None = None
    message_text: str | None = None
    message_channel: str | None = None
    scheduled_for: str | None = None
    provider: str | None = None
    error: str | None = None
    idempotency_key: str | None = None


def execute(
    event: Any,
    classification: Classification,
    decision: PolicyDecision,
) -> ExecutionResult:
    """Carry out an approved recovery decision.

    Args:
        event: The failed payment.
        classification: Its diagnosis.
        decision: The approved action, from the policy engine.

    Returns:
        An ExecutionResult describing what happened.
    """
    action = decision.action
    key = razorpay_client.idempotency_key(event.id, action.value)
    failure_class = classification.failure_class.value

    if action is RecoveryAction.DROP:
        return ExecutionResult(
            action=action,
            status="SKIPPED",
            provider="none",
            idempotency_key=key,
        )

    if action is RecoveryAction.ESCALATE:
        # Merchant-side faults and high-value payments go to people. No
        # customer is contacted: they cannot fix a misconfigured account, and
        # telling them about it only advertises the merchant's own problem.
        return ExecutionResult(
            action=action,
            status="QUEUED",
            provider="ops_queue",
            idempotency_key=key,
        )

    if action in (RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_SCHEDULED):
        delay = decision.retry_after_hours or 0.0
        when = datetime.now(timezone.utc) + timedelta(hours=delay)
        return ExecutionResult(
            action=action,
            status="SCHEDULED",
            scheduled_for=when.isoformat(),
            provider="retry_scheduler",
            idempotency_key=key,
        )

    if action is RecoveryAction.PAYMENT_LINK:
        link = razorpay_client.create_payment_link(event)
        if not link.ok:
            return ExecutionResult(
                action=action,
                status="FAILED",
                provider=link.provider,
                error=link.error,
                idempotency_key=key,
            )

        message = llm.generate_message(
            customer_name=getattr(event, "customer_name", ""),
            amount_paise=event.amount,
            failure_class=failure_class,
            action=action,
            payment_link=link.short_url,
        )
        return ExecutionResult(
            action=action,
            status="EXECUTED",
            payment_link_url=link.short_url,
            payment_link_id=link.link_id,
            message_text=message.text,
            message_channel=message.channel,
            provider=f"{link.provider}+{message.provider}",
            idempotency_key=key,
        )

    if action is RecoveryAction.NOTIFY:
        message = llm.generate_message(
            customer_name=getattr(event, "customer_name", ""),
            amount_paise=event.amount,
            failure_class=failure_class,
            action=action,
            payment_link=None,
        )
        return ExecutionResult(
            action=action,
            status="EXECUTED",
            message_text=message.text,
            message_channel=message.channel,
            provider=message.provider,
            idempotency_key=key,
        )

    return ExecutionResult(
        action=action,
        status="FAILED",
        error=f"No executor implemented for {action.value}",
        idempotency_key=key,
    )
