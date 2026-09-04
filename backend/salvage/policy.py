"""The deterministic policy engine.

This module decides what happens to a failed payment. It contains no model
inference, no LLM call, and no randomness - given the same inputs it returns
the same decision, every time, and the decision can be read off the code.

That is a deliberate architectural boundary, and it is the whole point of the
system. The ML model answers *"how likely is this to work?"*. The LLM answers
*"how do we say this to a human?"*. Neither is permitted to answer *"should we
spend money on this?"* - that question is resolved here, by rules a merchant
can audit and a regulator can read.

Decisions are made in three ordered stages, and the order is load-bearing:

  1. **Hard constraints.** Absolute prohibitions - risk blocks, already-settled
     orders, attempt caps, opt-outs. Checked first, so no probability score can
     argue its way past them. A 99% recovery estimate on a fraud-blocked
     payment still yields DROP.

  2. **Eligibility.** Which interventions are even coherent for this failure
     class. Retrying a permanently expired card is not a judgement call, it is
     a category error, so the option is never presented to the optimiser.

  3. **Economics.** Among genuinely eligible actions, pick the one with the
     highest net expected value. If none clears the merchant's floor, DROP.
     This is where stopping rules come from - they are an outcome of the
     arithmetic, not a hand-written table.
"""

from __future__ import annotations

from dataclasses import dataclass

from salvage.economics import (
    DEFAULT_POLICY,
    MerchantPolicy,
    RecoveryAction,
    Valuation,
    value_action,
)
from salvage.taxonomy import Classification, FailureClass

#: Interventions that put a message in front of a customer. These are the ones
#: anti-spam guardrails restrict. Retries are deliberately excluded: a silent
#: re-presentation of a card costs the customer no attention, so a contact cap
#: has no business blocking one. Conflating the two is a common way recovery
#: systems either spam people or leave free money on the table.
CUSTOMER_FACING: frozenset[RecoveryAction] = frozenset(
    {RecoveryAction.PAYMENT_LINK, RecoveryAction.NOTIFY}
)

#: Which actions are coherent for each failure class.
#:
#: Derived from Razorpay's documented guidance per reason (see taxonomy.py).
#: The absences matter as much as the entries:
#:
#:  - INSTRUMENT_INVALID has no retry. The card is expired or blocked; the
#:    identical request will fail identically. Retrying is pure waste and
#:    contributes to the issuer decline rate.
#:  - MERCHANT_CONFIG has *only* ESCALATE. These are source=business faults.
#:    No customer can fix `merchant_not_activated`, and messaging them about it
#:    advertises the merchant's own broken configuration.
#:  - RISK_BLOCKED and ALREADY_PAID are empty. Not "discouraged" - impossible.
#:  - UNKNOWN is empty by design. When the system cannot confidently diagnose a
#:    failure it does not guess; it books the payment to the exception list and
#:    reports it as unresolved.
ELIGIBLE_ACTIONS: dict[FailureClass, frozenset[RecoveryAction]] = {
    FailureClass.BANK_DOWNTIME: frozenset(
        {
            RecoveryAction.RETRY_NOW,
            RecoveryAction.RETRY_SCHEDULED,
            RecoveryAction.PAYMENT_LINK,
        }
    ),
    FailureClass.INSUFFICIENT_FUNDS: frozenset(
        {
            RecoveryAction.RETRY_SCHEDULED,
            RecoveryAction.NOTIFY,
            RecoveryAction.PAYMENT_LINK,
        }
    ),
    FailureClass.INSTRUMENT_INVALID: frozenset(
        {RecoveryAction.PAYMENT_LINK, RecoveryAction.NOTIFY}
    ),
    FailureClass.AUTH_FAILURE: frozenset(
        {RecoveryAction.NOTIFY, RecoveryAction.PAYMENT_LINK}
    ),
    FailureClass.CUSTOMER_ABANDONED: frozenset(
        {RecoveryAction.NOTIFY, RecoveryAction.PAYMENT_LINK}
    ),
    FailureClass.LIMIT_EXCEEDED: frozenset(
        {RecoveryAction.RETRY_SCHEDULED, RecoveryAction.PAYMENT_LINK}
    ),
    FailureClass.MERCHANT_CONFIG: frozenset({RecoveryAction.ESCALATE}),
    FailureClass.RISK_BLOCKED: frozenset(),
    FailureClass.ALREADY_PAID: frozenset(),
    FailureClass.UNKNOWN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    """Everything known about a payment's recovery history at decision time.

    Attributes:
        attempts_so_far: Interventions already made against this payment.
        contacts_today: Messages already sent to this customer today, across
            all of their payments.
        hours_since_last_contact: None if never contacted.
        customer_opted_out: Whether the customer has withdrawn consent to be
            contacted. Absolute - overrides any expected value.
        already_recovered: Whether this payment has since been settled - the
            customer paid through the link, retried on their own, or the
            merchant collected another way.

            Recovery is asynchronous: a link is issued, and minutes or days
            later the customer pays. Any scheduled retry, reminder or second
            link created after that point is chasing money already in the
            account. At best it wastes spend and annoys someone who has already
            paid; at worst a retry against a live instrument takes the money
            twice. Verified state must therefore beat every other input,
            including a high expected value.
    """

    attempts_so_far: int = 0
    contacts_today: int = 0
    hours_since_last_contact: float | None = None
    customer_opted_out: bool = False
    already_recovered: bool = False


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A decision, with the complete reasoning that produced it.

    The audit trail is not a log written alongside the decision - it *is* the
    decision object. Anything the system did, it can show its working for.

    Attributes:
        action: What to do.
        rationale: Plain-English justification, written by the rules themselves.
        rule_id: Stable identifier for the specific rule that fired, so
            decisions can be grouped and counted in evaluation.
        valuation: The winning action's arithmetic. None when a hard constraint
            fired before economics were reached.
        considered: Every eligible action that was priced, best first. Shows
            what the system weighed, not just what it picked.
        constraints_applied: Guardrails that were checked and passed or fired.
        is_exception: True when the payment could not be confidently resolved
            and belongs on the exception report.
        retry_after_hours: Backoff delay for scheduled retries.
    """

    action: RecoveryAction
    rationale: str
    rule_id: str
    valuation: Valuation | None = None
    considered: tuple[Valuation, ...] = ()
    constraints_applied: tuple[str, ...] = ()
    is_exception: bool = False
    retry_after_hours: float | None = None


def decide(
    classification: Classification,
    amount_paise: int,
    base_propensity: float,
    context: RecoveryContext | None = None,
    policy: MerchantPolicy = DEFAULT_POLICY,
) -> PolicyDecision:
    """Choose a recovery action for one failed payment.

    Args:
        classification: Output of `taxonomy.classify`.
        amount_paise: Failed amount, in paise.
        base_propensity: Calibrated model output in [0, 1] - this customer's
            underlying willingness and ability to pay. It is deliberately *not*
            a per-action probability; the policy engine derives those by pairing
            it with the action-effectiveness matrix in `economics`.
        context: Recovery history. Defaults to a fresh, untouched payment.
        policy: Merchant guardrails.

    Returns:
        A PolicyDecision carrying the action and its full derivation.
    """
    ctx = context or RecoveryContext()
    applied: list[str] = []

    # -- Stage 1: hard constraints ------------------------------------------
    # Checked before the probability is even looked at. This ordering is the
    # mechanical expression of "bounded autonomy": there exist decisions the
    # system is structurally incapable of making, no matter what it predicts.

    if classification.failure_class is FailureClass.RISK_BLOCKED:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="HARD_RISK_BLOCK",
            rationale=(
                "Payment was blocked by risk or compliance controls. Recovery is "
                "prohibited outright: re-presenting a transaction the risk engine "
                "refused would undermine the control and may breach regulation. "
                "Routed to manual review, never retried."
            ),
            constraints_applied=("risk_block",),
            is_exception=True,
        )

    if classification.failure_class is FailureClass.ALREADY_PAID:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="HARD_ALREADY_SETTLED",
            rationale=(
                "This order is already paid or the request was a duplicate. Acting "
                "would risk double-charging the customer. No recovery is owed."
            ),
            constraints_applied=("already_settled",),
        )

    if not classification.confident:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="HARD_UNDIAGNOSED",
            rationale=(
                f"Failure could not be confidently diagnosed. {classification.note} "
                "The system does not guess an intervention on an unrecognised "
                "failure; the payment is reported as an unresolved exception."
            ),
            constraints_applied=("diagnosis_confidence",),
            is_exception=True,
        )

    if ctx.already_recovered:
        # Checked before consent and before economics, because it is the only
        # constraint whose violation can take a customer's money twice.
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="HARD_ALREADY_RECOVERED",
            rationale=(
                "This payment has since been settled. Recovery is asynchronous, so "
                "a scheduled retry or reminder can fire after the customer has "
                "already paid - chasing money that is in the account, and risking "
                "collecting it twice. Verified settlement overrides expected value."
            ),
            constraints_applied=("already_recovered",),
        )

    if ctx.customer_opted_out:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="HARD_OPT_OUT",
            rationale=(
                "Customer has withdrawn consent to be contacted. Consent overrides "
                "expected value unconditionally."
            ),
            constraints_applied=("customer_opt_out",),
        )

    if ctx.attempts_so_far >= policy.max_attempts_per_payment:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="HARD_ATTEMPT_CAP",
            rationale=(
                f"Attempt cap reached: {ctx.attempts_so_far} of "
                f"{policy.max_attempts_per_payment} permitted interventions already "
                "made against this payment. Recovery stops here regardless of "
                "remaining expected value."
            ),
            constraints_applied=("max_attempts_per_payment",),
        )

    if (
        policy.require_human_above_threshold
        and amount_paise > policy.max_autonomous_amount_paise
    ):
        return PolicyDecision(
            action=RecoveryAction.ESCALATE,
            rule_id="HARD_HIGH_VALUE_ESCALATION",
            rationale=(
                f"Amount of Rs {amount_paise / 100:,.2f} exceeds the autonomous "
                f"ceiling of Rs {policy.max_autonomous_amount_paise / 100:,.2f}. "
                "High-value recoveries require human sign-off before any customer "
                "contact; the agent will not act unilaterally at this size."
            ),
            constraints_applied=("max_autonomous_amount",),
        )

    applied.extend(
        ("risk_block", "already_settled", "diagnosis_confidence", "customer_opt_out",
         "max_attempts_per_payment", "max_autonomous_amount")
    )

    # -- Stage 2: eligibility -----------------------------------------------

    eligible = set(ELIGIBLE_ACTIONS[classification.failure_class])

    if not eligible:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="NO_ELIGIBLE_ACTION",
            rationale=(
                f"No intervention is coherent for failure class "
                f"{classification.failure_class.value}."
            ),
            constraints_applied=tuple(applied),
            is_exception=True,
        )

    # Anti-spam guardrails suppress *customer-facing* actions only. A silent
    # retry costs the customer no attention, so it stays on the table.
    contact_blocked_reason: str | None = None

    if ctx.contacts_today >= policy.max_contacts_per_customer_per_day:
        contact_blocked_reason = (
            f"daily contact cap reached ({ctx.contacts_today}/"
            f"{policy.max_contacts_per_customer_per_day})"
        )
    elif (
        ctx.hours_since_last_contact is not None
        and ctx.hours_since_last_contact < policy.cooldown_hours
    ):
        contact_blocked_reason = (
            f"within {policy.cooldown_hours:g}h cooldown "
            f"(last contact {ctx.hours_since_last_contact:.1f}h ago)"
        )

    if contact_blocked_reason:
        eligible -= CUSTOMER_FACING
        applied.append("contact_frequency")
        if not eligible:
            return PolicyDecision(
                action=RecoveryAction.DROP,
                rule_id="CONTACT_GUARDRAIL_EXHAUSTED",
                rationale=(
                    f"Only customer-facing interventions remained, but {contact_blocked_reason}. "
                    "Deferring rather than over-contacting."
                ),
                constraints_applied=tuple(applied),
            )

    # A retry is only meaningful if re-presenting the *same* instrument could
    # succeed. The taxonomy already knows whether that is true.
    entry = classification.entry
    if entry is not None and not entry.auto_retryable:
        eligible -= {RecoveryAction.RETRY_NOW, RecoveryAction.RETRY_SCHEDULED}
        applied.append("instrument_retryability")

    if not eligible:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="NO_VIABLE_ACTION",
            rationale=(
                "Every candidate intervention was excluded by a guardrail or by the "
                "nature of the failure. Nothing left that could plausibly work."
            ),
            constraints_applied=tuple(applied),
        )

    # -- Stage 3: economics -------------------------------------------------
    # Among the actions that are permitted and coherent, buy the best one - but
    # only if it beats doing nothing by the merchant's stated margin.

    priced = sorted(
        (
            value_action(
                a,
                amount_paise,
                base_propensity,
                classification.failure_class.value,
                policy,
            )
            for a in eligible
        ),
        key=lambda v: v.net_ev_paise,
        reverse=True,
    )
    best = priced[0]

    if best.net_ev_paise < policy.min_net_ev_paise:
        return PolicyDecision(
            action=RecoveryAction.DROP,
            rule_id="ECON_BELOW_THRESHOLD",
            rationale=(
                f"Best available action ({best.action.value}) returns "
                f"Rs {best.net_ev_paise / 100:,.2f} net, below the merchant's "
                f"Rs {policy.min_net_ev_paise / 100:,.2f} floor. "
                f"{best.explain()}. Chasing this payment costs more than it is "
                "worth, so the system stops."
            ),
            valuation=best,
            considered=tuple(priced),
            constraints_applied=tuple(applied),
        )

    retry_after = None
    if best.action is RecoveryAction.RETRY_SCHEDULED:
        # Wait for the blocking condition to clear on its own. The taxonomy
        # carries a typical resolution time per reason; default to a day when
        # it is unknown.
        retry_after = (
            entry.typical_resolution_hours
            if entry is not None and entry.typical_resolution_hours
            else 24.0
        )

    return PolicyDecision(
        action=best.action,
        rule_id=f"ECON_MAX_EV__{classification.failure_class.value}",
        rationale=(
            f"{classification.failure_class.value}: {best.action.value} carries the "
            f"highest net expected value of {len(priced)} eligible action(s). "
            f"{best.explain()}."
            + (
                f" Scheduled {retry_after:g}h out, sized to how long this failure "
                "typically takes to clear."
                if retry_after
                else ""
            )
        ),
        valuation=best,
        considered=tuple(priced),
        constraints_applied=tuple(applied),
        retry_after_hours=retry_after,
    )
