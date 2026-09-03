"""Counterfactual outcome oracle.

The track's bar is "show measured money recovered", not "show expected money
recovered". Expected value is a forecast the system makes about itself; on
synthetic data it can be inflated without limit and cannot be checked. So the
simulator needs something that actually adjudicates outcomes, independently of
the model that predicted them.

That is this module. It is the ground truth, and it is deliberately built to be
unkind to the system being evaluated, in two specific ways.

**Organic recovery.** Some customers come back and pay on their own, with no
intervention whatsoever. A recovery system that counts those as its own wins is
lying - it is billing for revenue that would have arrived anyway. The oracle
therefore models a no-contact baseline, and evaluation reports **incremental**
recovery: money that arrived *because* the system acted, net of what would have
happened had it done nothing. This is the single largest source of overstated
numbers in recovery tooling, and it is the number most likely to be probed.

**Common random numbers.** Every counterfactual for one payment is resolved
against a single uniform draw fixed by that payment's id. So "what if we had
retried instead of sending a link?" is answered against the same underlying
customer, not a fresh coin flip.

Two consequences, both wanted:

  - Comparisons between strategies are paired. The baseline and Salvage face
    identical luck on identical payments, so the measured delta is the effect
    of the decision policy and nothing else. Without this, a large slice of any
    reported improvement is sampling noise.
  - Interventions are monotone. If a weak action would have recovered a
    payment, a stronger one would have too. Effectiveness raises the bar an
    action clears, rather than re-rolling the dice.

The oracle never sees a model prediction, a policy decision, or an expected
value. It only reads latent truth. Nothing the system does can influence it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from salvage.economics import RecoveryAction, effectiveness
from salvage.simulator.generate import SyntheticEvent

#: How often a customer returns and pays unprompted, as a multiplier on their
#: latent propensity. This is the "do nothing" baseline every intervention has
#: to beat to have earned anything.
#:
#: Varies by failure mode for a concrete reason: a customer whose bank was
#: briefly down will very often just try again in an hour, whereas a customer
#: holding an expired card has no unprompted path back at all - they need to be
#: given a different way to pay. Ignoring that difference would credit the
#: system most heavily exactly where it did least.
ORGANIC_RECOVERY: dict[str, float] = {
    "BANK_DOWNTIME": 0.34,
    "AUTH_FAILURE": 0.26,
    "CUSTOMER_ABANDONED": 0.15,
    "INSUFFICIENT_FUNDS": 0.14,
    "LIMIT_EXCEEDED": 0.12,
    "INSTRUMENT_INVALID": 0.05,
    "MERCHANT_CONFIG": 0.0,
    "RISK_BLOCKED": 0.0,
    "ALREADY_PAID": 0.0,
    "UNKNOWN": 0.10,
}

DEFAULT_ORGANIC = 0.10


@dataclass(frozen=True, slots=True)
class Outcome:
    """What actually happened to one payment under one strategy.

    Attributes:
        recovered: Whether the money arrived.
        recovered_paise: Amount recovered, zero if not.
        would_have_recovered_organically: Whether this payment would have come
            back with no intervention at all.
        incremental_paise: Recovery genuinely caused by the intervention -
            zero when the customer would have returned anyway. This is the
            only figure the system is entitled to claim credit for.
        p_action: P(recovery | this action) under latent truth.
        p_organic: P(recovery | do nothing) under latent truth.
    """

    recovered: bool
    recovered_paise: int
    would_have_recovered_organically: bool
    incremental_paise: int
    p_action: float
    p_organic: float


def _draw(event_id: str) -> float:
    """The single uniform draw that resolves every counterfactual for a payment.

    Derived from the payment id, so it is stable across runs, across strategies,
    and across processes - no shared RNG state, no ordering dependence. Re-run
    the evaluation tomorrow on a different machine and every outcome is
    identical.
    """
    digest = hashlib.sha256(f"outcome:{event_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def organic_probability(event: SyntheticEvent, failure_class: str) -> float:
    """P(customer returns and pays with no intervention)."""
    return event._true_base_propensity * ORGANIC_RECOVERY.get(
        failure_class, DEFAULT_ORGANIC
    )


def observe(
    event: SyntheticEvent,
    action: RecoveryAction,
    failure_class: str,
) -> Outcome:
    """Adjudicate what happens when `action` is taken on `event`.

    Args:
        event: The failed payment, carrying its latent truth.
        action: The intervention taken. DROP means no intervention.
        failure_class: FailureClass value, selecting effectiveness and organic
            rates.

    Returns:
        An Outcome, including the counterfactual of having done nothing.
    """
    u = _draw(event.id)
    p_organic = organic_probability(event, failure_class)

    if action is RecoveryAction.DROP:
        p_action = p_organic
    else:
        # An intervention cannot make matters worse than leaving the customer
        # alone, so the effective probability is the better of the two.
        p_intervention = event._true_base_propensity * effectiveness(
            failure_class, action
        )
        p_action = max(p_intervention, p_organic)

    recovered = u < p_action
    organic = u < p_organic

    # Credit is only claimed where the intervention changed the result. Under
    # common random numbers and a monotone action, `organic and not recovered`
    # is impossible, so this is never negative.
    incremental = event.amount if (recovered and not organic) else 0

    return Outcome(
        recovered=recovered,
        recovered_paise=event.amount if recovered else 0,
        would_have_recovered_organically=organic,
        incremental_paise=incremental,
        p_action=p_action,
        p_organic=p_organic,
    )


def observe_do_nothing(event: SyntheticEvent, failure_class: str) -> Outcome:
    """The pure no-intervention counterfactual for one payment."""
    return observe(event, RecoveryAction.DROP, failure_class)
