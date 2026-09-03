"""Recovery economics: what each intervention costs and what it is worth.

The central claim of Salvage is that a recovery decision is an *investment*
decision, not a routing decision. Every intervention costs real money, so the
question is never "can we do something?" but "is doing something worth more
than doing nothing?".

That framing is what makes stopping rules principled. DROP is not a special
case bolted onto the policy table - it is simply what happens when no available
action clears a net expected value of zero. The system stops chasing a payment
for the same reason a business would: the chase costs more than it returns.

All monetary values are in **paise** (1 INR = 100 paise), matching Razorpay's
own amount convention, so no float rounding ever touches a rupee figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RecoveryAction(str, Enum):
    """The complete set of interventions Salvage can take.

    Deliberately small. Every action here maps to a concrete, executable
    mechanism - there is no "analyse further" or "flag for review" escape
    hatch that would let the system look busy without doing anything.
    """

    RETRY_NOW = "RETRY_NOW"
    """Re-present the same instrument immediately. Only for transient
    infrastructure failures where nothing about the payment was wrong."""

    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    """Re-present after a backoff window sized to the failure's typical
    resolution time. For conditions that clear on a clock: balance top-ups,
    daily limits, bank cutoffs."""

    PAYMENT_LINK = "PAYMENT_LINK"
    """Issue a Razorpay Payment Link so the customer can pay with a
    *different* instrument. The only path when the original instrument is
    permanently unusable."""

    NOTIFY = "NOTIFY"
    """Message the customer without issuing a new link. For cases where the
    customer simply needs to come back and finish."""

    ESCALATE = "ESCALATE"
    """Route to a human ops queue. Reserved for merchant-side configuration
    faults, which no customer-facing action can fix."""

    DROP = "DROP"
    """Stop. Either a hard guardrail forbids action, or no action has positive
    expected value."""


@dataclass(frozen=True, slots=True)
class ActionCost:
    """The fully-loaded cost of attempting one intervention.

    Attributes:
        direct_paise: Out-of-pocket cost per attempt - SMS/WhatsApp delivery,
            gateway processing overhead, human time.
        goodwill_paise: Modelled cost of customer irritation. Not a real
            invoice, but real money: over-contacted customers churn. Pricing
            it forces the optimiser to stay quiet when the upside is thin,
            which is the behaviour a merchant actually wants.
    """

    direct_paise: int
    goodwill_paise: int

    @property
    def total_paise(self) -> int:
        return self.direct_paise + self.goodwill_paise


#: Cost model for each action, in paise.
#:
#: Figures are order-of-magnitude estimates for the Indian market, chosen to be
#: defensible rather than precise: transactional SMS runs a few paise to ~25
#: paise per message, WhatsApp business-initiated conversations cost more, and
#: an ops analyst at ~Rs 600/hour costs ~Rs 50 for a five-minute review.
#:
#: The exact numbers matter far less than their *ratios*. What drives behaviour
#: is that ESCALATE costs ~25x a retry, so the system only spends human
#: attention on payments large enough to justify it.
ACTION_COSTS: dict[RecoveryAction, ActionCost] = {
    # A failed retry costs almost nothing directly, but repeated declines
    # against the same card attract issuer scrutiny and hurt future auth rates.
    RecoveryAction.RETRY_NOW: ActionCost(direct_paise=200, goodwill_paise=0),
    RecoveryAction.RETRY_SCHEDULED: ActionCost(direct_paise=200, goodwill_paise=0),
    # Link creation is free; delivering it is not, and it does interrupt someone.
    RecoveryAction.PAYMENT_LINK: ActionCost(direct_paise=35, goodwill_paise=150),
    RecoveryAction.NOTIFY: ActionCost(direct_paise=25, goodwill_paise=150),
    # Five minutes of an ops analyst.
    RecoveryAction.ESCALATE: ActionCost(direct_paise=5000, goodwill_paise=0),
    # Doing nothing is the only free action. This is the baseline every other
    # action has to beat.
    RecoveryAction.DROP: ActionCost(direct_paise=0, goodwill_paise=0),
}


@dataclass(frozen=True, slots=True)
class MerchantPolicy:
    """Merchant-defined guardrails. The outer bound on agent autonomy.

    Every field here is a limit the system may not exceed regardless of how
    attractive the economics look. These are checked *before* expected value is
    ever computed, so no probability score can argue its way past them.

    Attributes:
        max_attempts_per_payment: Hard cap on total interventions against a
            single failed payment.
        max_contacts_per_customer_per_day: Anti-spam ceiling across all of a
            customer's payments.
        cooldown_hours: Minimum gap between two contacts to the same customer.
        mdr_rate: Merchant discount rate. Recovered revenue is not free -
            Razorpay's cut comes off the top, so gross recovery overstates what
            the merchant actually banks.
        min_net_ev_paise: Expected value floor. An action must clear this, not
            merely zero, to be worth executing. Guards against acting on
            rounding noise.
        max_autonomous_amount_paise: Above this ticket size, the system will not
            act unilaterally - it escalates for human sign-off instead. High
            value means high blast radius.
        require_human_above_threshold: Whether that escalation is enforced.
    """

    max_attempts_per_payment: int = 3
    max_contacts_per_customer_per_day: int = 2
    cooldown_hours: float = 12.0
    mdr_rate: float = 0.02
    min_net_ev_paise: int = 500
    max_autonomous_amount_paise: int = 5_000_000
    require_human_above_threshold: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.mdr_rate < 1.0:
            raise ValueError(f"mdr_rate must be in [0, 1): {self.mdr_rate}")
        if self.max_attempts_per_payment < 1:
            raise ValueError("max_attempts_per_payment must be at least 1")


DEFAULT_POLICY = MerchantPolicy()


#: How well each intervention actually works against each failure mode.
#:
#: This matrix exists because P(recovery) is not a property of a payment - it is
#: a property of a *payment and an intervention together*. Re-presenting a card
#: after a bank outage works most of the time; re-presenting an expired card
#: works never. A model that emits a single probability per payment cannot
#: express that difference, and a policy engine that consumes one will silently
#: rank actions by cost alone.
#:
#: So the model predicts a *base propensity* - roughly, this customer's
#: willingness and ability to pay, learned from amount, history and context -
#: and this matrix supplies the structural fit between the failure and the
#: remedy. The product is the action-conditional recovery probability:
#:
#:     P(recovery | action) = base_propensity x effectiveness[class][action]
#:
#: Values are modelling assumptions, documented here so a reviewer can disagree
#: with a specific number rather than with an opaque score. They encode a few
#: claims worth stating out loud:
#:
#:  - A scheduled retry beats an immediate one for anything that clears on a
#:    clock (downtime, balance, limits) - waiting is the actual remedy.
#:  - A payment link beats a bare notification whenever the customer needs a
#:    *different instrument*, because a notification tells them there is a
#:    problem without giving them a way to solve it.
#:  - A bare notification is never the strongest option for a dead instrument.
ACTION_EFFECTIVENESS: dict[str, dict[RecoveryAction, float]] = {
    # Nothing was wrong with the customer or the card. Wait for the
    # infrastructure to come back and re-present. Highest-yield case in the
    # taxonomy, and the one blind retry accidentally gets right.
    "BANK_DOWNTIME": {
        RecoveryAction.RETRY_SCHEDULED: 0.90,
        RecoveryAction.RETRY_NOW: 0.72,
        RecoveryAction.PAYMENT_LINK: 0.55,
        RecoveryAction.NOTIFY: 0.30,
    },
    # Balance recovers on payday cycles. Time helps; a link helps more because
    # it opens up a different funding source.
    "INSUFFICIENT_FUNDS": {
        RecoveryAction.PAYMENT_LINK: 0.62,
        RecoveryAction.RETRY_SCHEDULED: 0.55,
        RecoveryAction.NOTIFY: 0.38,
    },
    # The instrument is dead. Only a different instrument can succeed, and only
    # a link puts one in reach. Retry is not in the eligible set at all.
    "INSTRUMENT_INVALID": {
        RecoveryAction.PAYMENT_LINK: 0.74,
        RecoveryAction.NOTIFY: 0.22,
    },
    # Customer was present and mistyped an OTP or CVV. Intent was real and
    # recent, so a fresh payment surface converts well.
    "AUTH_FAILURE": {
        RecoveryAction.PAYMENT_LINK: 0.66,
        RecoveryAction.NOTIFY: 0.44,
    },
    # They walked away rather than being refused. Intent may survive; a link
    # removes the friction that lost them the first time.
    "CUSTOMER_ABANDONED": {
        RecoveryAction.PAYMENT_LINK: 0.58,
        RecoveryAction.NOTIFY: 0.40,
    },
    # Limits reset on a clock, but an alternate instrument sidesteps the cap
    # entirely, so the link edges out the wait.
    "LIMIT_EXCEEDED": {
        RecoveryAction.PAYMENT_LINK: 0.64,
        RecoveryAction.RETRY_SCHEDULED: 0.58,
    },
    # A human fixes the merchant's configuration and the payment becomes
    # collectable again. Slow and expensive, but reliable.
    "MERCHANT_CONFIG": {
        RecoveryAction.ESCALATE: 0.88,
    },
}

#: Applied when a failure class has no entry above. Conservative on purpose:
#: an unmodelled combination should look unattractive, not free.
DEFAULT_EFFECTIVENESS = 0.25


def effectiveness(failure_class: str, action: RecoveryAction) -> float:
    """Structural fit between a failure mode and an intervention, in [0, 1]."""
    return ACTION_EFFECTIVENESS.get(failure_class, {}).get(
        action, DEFAULT_EFFECTIVENESS
    )


@dataclass(frozen=True, slots=True)
class Valuation:
    """The full arithmetic behind one action's expected value.

    Every intermediate term is retained rather than just the final number, so
    the dashboard can show a judge the actual sum rather than asking them to
    trust a score.
    """

    action: RecoveryAction
    amount_paise: int
    base_propensity: float
    effectiveness: float
    probability: float
    gross_expected_paise: int
    mdr_paise: int
    cost_paise: int
    net_ev_paise: int

    def explain(self) -> str:
        """One-line derivation, rendered for the audit trail."""
        rupees = lambda p: f"Rs {p / 100:,.2f}"  # noqa: E731
        return (
            f"{rupees(self.amount_paise)} x "
            f"({self.base_propensity:.0%} propensity x "
            f"{self.effectiveness:.0%} action fit = {self.probability:.1%}) "
            f"= {rupees(self.gross_expected_paise)} gross, "
            f"less {rupees(self.mdr_paise)} MDR "
            f"and {rupees(self.cost_paise)} action cost "
            f"= {rupees(self.net_ev_paise)} net"
        )


def value_action(
    action: RecoveryAction,
    amount_paise: int,
    base_propensity: float,
    failure_class: str,
    policy: MerchantPolicy = DEFAULT_POLICY,
) -> Valuation:
    """Compute the net expected value of taking one action.

        P(recovery | action) = base_propensity x effectiveness[class][action]
        net EV = amount x P(recovery | action) x (1 - MDR) - action cost

    Two terms here do real work that a simpler formula would lose.

    The **effectiveness** factor makes the probability action-conditional.
    Without it every action shares one probability, the ranking silently
    collapses to whichever intervention is cheapest, and the engine will
    cheerfully notify a customer that their expired card expired.

    The **MDR** term keeps the rupee figures honest. Gross expected recovery is
    what most recovery dashboards report, but the merchant never banks it - the
    processor's cut comes off the top first.

    Args:
        action: The intervention being priced.
        amount_paise: The failed payment amount, in paise.
        base_propensity: Model output - this customer's underlying willingness
            and ability to pay, independent of intervention. Must be in [0, 1].
        failure_class: FailureClass value, selecting the effectiveness row.
        policy: Merchant guardrails supplying the MDR rate.

    Returns:
        A Valuation carrying every intermediate term.

    Raises:
        ValueError: If base_propensity is outside [0, 1] or amount is negative.
    """
    if not 0.0 <= base_propensity <= 1.0:
        raise ValueError(
            f"base_propensity must be in [0, 1], got {base_propensity}"
        )
    if amount_paise < 0:
        raise ValueError(f"amount_paise must be non-negative, got {amount_paise}")

    fit = effectiveness(failure_class, action)
    probability = base_propensity * fit

    gross = round(amount_paise * probability)
    mdr = round(gross * policy.mdr_rate)
    cost = ACTION_COSTS[action].total_paise
    net = gross - mdr - cost

    return Valuation(
        action=action,
        amount_paise=amount_paise,
        base_propensity=base_propensity,
        effectiveness=fit,
        probability=probability,
        gross_expected_paise=gross,
        mdr_paise=mdr,
        cost_paise=cost,
        net_ev_paise=net,
    )


def blind_retry_cost_paise(attempts: int) -> int:
    """Cost of the naive baseline: retry everything, every time.

    Used by the evaluation harness to price the strategy Salvage is competing
    against. A blind retry policy pays this on every failure including the ones
    that are structurally unrecoverable - expired cards, risk blocks, merchant
    misconfiguration - where the money is spent with zero chance of return.
    """
    if attempts < 0:
        raise ValueError(f"attempts must be non-negative, got {attempts}")
    return attempts * ACTION_COSTS[RecoveryAction.RETRY_NOW].total_paise
