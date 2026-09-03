"""Evaluation harness: does the decision policy actually earn its keep?

Three strategies are run over the *same* batch, adjudicated by the *same*
oracle, resolved against the *same* random draws. Because outcomes are keyed on
payment id rather than sampled per run, the comparison is paired: every
strategy meets identical customers having identical luck. The measured
difference is the effect of the decision policy and nothing else.

The strategies:

  **do_nothing** - the organic baseline. Send nothing, retry nothing. Some
  customers come back anyway. This is the number that must be subtracted before
  any recovery system may claim credit, and it is the one most often omitted.

  **blind_retry** - what unsophisticated recovery does: retry every failure,
  repeatedly, regardless of why it failed. Cheap per attempt, and it does
  recover real money on transient failures. It also spends on risk blocks,
  expired cards, and merchant misconfiguration, where the return is exactly
  zero by construction.

  **salvage** - diagnose, price, act only where net expected value clears the
  merchant's floor, and stop otherwise.

Every rupee reported is an outcome the oracle adjudicated, never an expected
value the system predicted about itself.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from salvage.economics import (
    ACTION_COSTS,
    DEFAULT_POLICY,
    MerchantPolicy,
    RecoveryAction,
)
from salvage.ml.predict import predict_propensity_batch
from salvage.policy import PolicyDecision, RecoveryContext, decide
from salvage.simulator.generate import SyntheticEvent
from salvage.simulator.oracle import observe, observe_do_nothing
from salvage.taxonomy import Classification, FailureClass, classify

#: Failure classes where no intervention can succeed, by construction. Money
#: spent acting on these is unambiguously wasted - not unlucky, wasted.
#:
#: MERCHANT_CONFIG is deliberately *not* here. It is unrecoverable by any
#: customer-facing action, but an ops engineer fixing the configuration does
#: recover it, which is exactly why the policy engine routes it to ESCALATE.
#: Counting those escalations as waste would penalise the system for taking the
#: one action that works.
STRUCTURALLY_UNRECOVERABLE: frozenset[FailureClass] = frozenset(
    {FailureClass.RISK_BLOCKED, FailureClass.ALREADY_PAID}
)


@dataclass(slots=True)
class StrategyResult:
    """Measured outcome of running one strategy over the batch."""

    name: str
    n_events: int
    revenue_at_risk_paise: int
    actions_taken: int
    action_breakdown: dict[str, int] = field(default_factory=dict)
    action_cost_paise: int = 0
    gross_recovered_paise: int = 0
    organic_recovered_paise: int = 0
    incremental_recovered_paise: int = 0
    wasted_actions: int = 0
    wasted_cost_paise: int = 0
    actions_on_unrecoverable: int = 0
    unrecoverable_cost_paise: int = 0
    exceptions: int = 0

    @property
    def net_value_paise(self) -> int:
        """Incremental revenue actually caused, less what was spent causing it.

        The bottom line. A strategy can recover a great deal of money and still
        land here in the negative if it paid too much for it.
        """
        return self.incremental_recovered_paise - self.action_cost_paise

    @property
    def incremental_rate(self) -> float:
        return (
            self.incremental_recovered_paise / self.revenue_at_risk_paise
            if self.revenue_at_risk_paise
            else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "n_events": self.n_events,
            "revenue_at_risk_paise": self.revenue_at_risk_paise,
            "actions_taken": self.actions_taken,
            "action_breakdown": self.action_breakdown,
            "action_cost_paise": self.action_cost_paise,
            "gross_recovered_paise": self.gross_recovered_paise,
            "organic_recovered_paise": self.organic_recovered_paise,
            "incremental_recovered_paise": self.incremental_recovered_paise,
            "net_value_paise": self.net_value_paise,
            "incremental_rate": round(self.incremental_rate, 4),
            "wasted_actions": self.wasted_actions,
            "wasted_cost_paise": self.wasted_cost_paise,
            "actions_on_unrecoverable": self.actions_on_unrecoverable,
            "unrecoverable_cost_paise": self.unrecoverable_cost_paise,
            "exceptions": self.exceptions,
        }


def _organic_totals(
    events: list[SyntheticEvent], classes: list[Classification]
) -> int:
    """Revenue that arrives with no intervention at all."""
    total = 0
    for event, classification in zip(events, classes):
        outcome = observe_do_nothing(event, classification.failure_class.value)
        total += outcome.recovered_paise
    return total


def run_do_nothing(
    events: list[SyntheticEvent], classes: list[Classification]
) -> StrategyResult:
    """The organic baseline: no contact, no retries, no cost."""
    at_risk = sum(e.amount for e in events)
    organic = _organic_totals(events, classes)
    return StrategyResult(
        name="Do nothing",
        n_events=len(events),
        revenue_at_risk_paise=at_risk,
        actions_taken=0,
        gross_recovered_paise=organic,
        organic_recovered_paise=organic,
        incremental_recovered_paise=0,
    )


def run_blind_retry(
    events: list[SyntheticEvent],
    classes: list[Classification],
    attempts: int = 3,
) -> StrategyResult:
    """Retry everything, `attempts` times, with no diagnosis.

    Charged for every attempt on every payment, because that is what this
    strategy actually does. Outcomes still come from the oracle, so retries on
    transient failures genuinely recover money - blind retry is not a straw man,
    it works on the cases where retrying is the right answer. It simply cannot
    tell those apart from the ones where it is throwing money away.
    """
    at_risk = sum(e.amount for e in events)
    result = StrategyResult(
        name=f"Blind retry (x{attempts})",
        n_events=len(events),
        revenue_at_risk_paise=at_risk,
        actions_taken=0,
    )
    per_attempt = ACTION_COSTS[RecoveryAction.RETRY_NOW].total_paise
    breakdown: Counter[str] = Counter()

    for event, classification in zip(events, classes):
        failure_class = classification.failure_class

        result.actions_taken += attempts
        breakdown[RecoveryAction.RETRY_NOW.value] += attempts
        spend = per_attempt * attempts
        result.action_cost_paise += spend

        outcome = observe(event, RecoveryAction.RETRY_NOW, failure_class.value)
        result.gross_recovered_paise += outcome.recovered_paise
        result.organic_recovered_paise += (
            event.amount if outcome.would_have_recovered_organically else 0
        )
        result.incremental_recovered_paise += outcome.incremental_paise

        if not outcome.recovered:
            result.wasted_actions += attempts
            result.wasted_cost_paise += spend

        if failure_class in STRUCTURALLY_UNRECOVERABLE:
            result.actions_on_unrecoverable += attempts
            result.unrecoverable_cost_paise += spend

    result.action_breakdown = dict(breakdown)
    return result


def run_salvage(
    events: list[SyntheticEvent],
    classes: list[Classification],
    propensities: list[float],
    policy: MerchantPolicy = DEFAULT_POLICY,
) -> tuple[StrategyResult, list[tuple[SyntheticEvent, Classification, PolicyDecision]]]:
    """Diagnose, price, and act only where it pays.

    Returns the measured result plus every per-payment decision, so the audit
    trail and the exception report are built from the same objects the
    evaluation scored rather than a parallel reconstruction.
    """
    at_risk = sum(e.amount for e in events)
    result = StrategyResult(
        name="Salvage",
        n_events=len(events),
        revenue_at_risk_paise=at_risk,
        actions_taken=0,
    )
    breakdown: Counter[str] = Counter()
    decisions: list[tuple[SyntheticEvent, Classification, PolicyDecision]] = []

    # Contact frequency is enforced per customer across the whole batch, not
    # per payment. A customer with four failed payments must not receive four
    # messages just because each decision looked reasonable alone.
    contacts_today: Counter[str] = Counter()

    for event, classification, propensity in zip(events, classes, propensities):
        context = RecoveryContext(
            attempts_so_far=max(0, event.attempt_number - 1),
            contacts_today=contacts_today[event.customer_id],
        )
        decision = decide(
            classification, event.amount, propensity, context, policy
        )
        decisions.append((event, classification, decision))

        if decision.is_exception:
            result.exceptions += 1

        if decision.action is RecoveryAction.DROP:
            # Doing nothing still has an outcome: the customer may return.
            outcome = observe_do_nothing(event, classification.failure_class.value)
            result.gross_recovered_paise += outcome.recovered_paise
            result.organic_recovered_paise += outcome.recovered_paise
            continue

        cost = ACTION_COSTS[decision.action].total_paise
        result.actions_taken += 1
        breakdown[decision.action.value] += 1
        result.action_cost_paise += cost

        if decision.action in (RecoveryAction.PAYMENT_LINK, RecoveryAction.NOTIFY):
            contacts_today[event.customer_id] += 1

        outcome = observe(event, decision.action, classification.failure_class.value)
        result.gross_recovered_paise += outcome.recovered_paise
        result.organic_recovered_paise += (
            event.amount if outcome.would_have_recovered_organically else 0
        )
        result.incremental_recovered_paise += outcome.incremental_paise

        if not outcome.recovered:
            result.wasted_actions += 1
            result.wasted_cost_paise += cost

        if classification.failure_class in STRUCTURALLY_UNRECOVERABLE:
            result.actions_on_unrecoverable += 1
            result.unrecoverable_cost_paise += cost

    result.action_breakdown = dict(breakdown)
    return result, decisions


def evaluate_batch(
    events: list[SyntheticEvent], policy: MerchantPolicy = DEFAULT_POLICY
) -> dict[str, Any]:
    """Run all three strategies over one batch and return the comparison."""
    classes = [
        classify(e.error_reason, e.error_code, e.error_source, e.error_step)
        for e in events
    ]
    propensities = predict_propensity_batch(events)

    nothing = run_do_nothing(events, classes)
    blind = run_blind_retry(events, classes)
    salvage, decisions = run_salvage(events, classes, propensities, policy)

    return {
        "batch_size": len(events),
        "revenue_at_risk_paise": sum(e.amount for e in events),
        "strategies": {
            "do_nothing": nothing.to_dict(),
            "blind_retry": blind.to_dict(),
            "salvage": salvage.to_dict(),
        },
        "delta_vs_blind": {
            "net_value_paise": salvage.net_value_paise - blind.net_value_paise,
            "actions_saved": blind.actions_taken - salvage.actions_taken,
            "cost_saved_paise": blind.action_cost_paise - salvage.action_cost_paise,
            "wasted_actions_avoided": blind.wasted_actions - salvage.wasted_actions,
            "unrecoverable_actions_avoided": (
                blind.actions_on_unrecoverable - salvage.actions_on_unrecoverable
            ),
        },
        "exceptions": salvage.exceptions,
        "_decisions": decisions,
    }


def _rs(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def main() -> None:
    """CLI entry point: `python -m salvage.evaluate`."""
    from salvage.simulator.generate import generate_events

    # Evaluation batch, generated with a different seed from the training
    # corpus. The model has never seen these customers.
    events = generate_events(1000, seed=77771111)
    report = evaluate_batch(events)

    s = report["strategies"]
    d = report["delta_vs_blind"]
    at_risk = report["revenue_at_risk_paise"]

    print("=" * 74)
    print("  SALVAGE - measured recovery over a held-out batch")
    print("=" * 74)
    print(f"  batch              {report['batch_size']:,} failed payments")
    print(f"  revenue at risk    {_rs(at_risk)}")
    print()
    print(f"  {'':<18}{'actions':>9}{'spend':>13}{'incremental':>15}{'net value':>14}")
    print("  " + "-" * 70)
    for key in ("do_nothing", "blind_retry", "salvage"):
        r = s[key]
        print(
            f"  {r['name']:<18}{r['actions_taken']:>9,}"
            f"{_rs(r['action_cost_paise']):>13}"
            f"{_rs(r['incremental_recovered_paise']):>15}"
            f"{_rs(r['net_value_paise']):>14}"
        )
    print()
    print("  WHERE THE DIFFERENCE COMES FROM")
    print(f"    net value vs blind retry      {_rs(d['net_value_paise'])}")
    print(f"    interventions avoided         {d['actions_saved']:,}")
    print(f"    spend avoided                 {_rs(d['cost_saved_paise'])}")
    print(f"    wasted actions avoided        {d['wasted_actions_avoided']:,}")
    print(
        f"    spent on unrecoverable        "
        f"blind {s['blind_retry']['actions_on_unrecoverable']:,} "
        f"vs salvage {s['salvage']['actions_on_unrecoverable']:,}"
    )
    print()
    print("  SALVAGE ACTION MIX")
    for action, n in sorted(
        s["salvage"]["action_breakdown"].items(), key=lambda kv: -kv[1]
    ):
        print(f"    {action:<20}{n:>6,}")
    dropped = report["batch_size"] - s["salvage"]["actions_taken"]
    print(f"    {'DROP (no action)':<20}{dropped:>6,}")
    print()
    print("  HONESTY LINE")
    org = s["salvage"]["organic_recovered_paise"]
    gross = s["salvage"]["gross_recovered_paise"]
    print(f"    gross recovered under Salvage      {_rs(gross)}")
    print(f"    of which would have arrived anyway {_rs(org)}")
    print(f"    genuinely caused by the system     "
          f"{_rs(s['salvage']['incremental_recovered_paise'])}")
    print(f"    unresolved exceptions              {report['exceptions']:,}")


if __name__ == "__main__":
    main()
