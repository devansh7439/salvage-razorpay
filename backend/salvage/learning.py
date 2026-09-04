"""LEARN: hold the system's own assumptions to account against outcomes.

`ACTION_EFFECTIVENESS` and `ORGANIC_BASELINE` in `economics` are hand-authored
domain judgement. Every expected value the system computes, and therefore every
rupee it decides to spend, rests on them. They were written as an argument -
documented per entry, so a reviewer could disagree with a specific number - but
an argument nobody checks is just an assertion with better formatting.

This module checks them. It reads what actually happened and reports, per
failure class and per action:

    assumed effectiveness   vs   observed recovery rate

That comparison is the learning loop, and it is deliberately a *report* rather
than an autoupdate. Silently refitting the constants that govern spending, from
a few hundred outcomes, would be the single most dangerous thing this system
could do to itself: a bad fortnight would teach it to stop recovering, and a
lucky one to overspend, with no human in the path. So the loop produces
evidence and a recommendation, and a person changes the number.

Small samples are marked rather than hidden. A class with nine observations
carries a confidence interval wide enough to contain almost anything, and
saying so is more useful than reporting a point estimate that looks precise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from salvage import db
from salvage.economics import (
    ACTION_EFFECTIVENESS,
    ORGANIC_BASELINE,
    RecoveryAction,
    effectiveness,
)

#: Below this many observations, a rate is reported but not acted on. Chosen so
#: the 95% interval on a mid-range rate is narrower than the gap between
#: adjacent effectiveness values in the matrix - below that, the data cannot
#: distinguish the assumptions from each other.
MIN_SAMPLES = 30

#: Relative divergence beyond which an assumption is flagged for review.
DRIFT_THRESHOLD = 0.25


def _wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval.

    Used rather than the normal approximation because recovery rates sit near
    0 and 1 often enough that the naive interval produces bounds outside [0, 1]
    - which reads as a bug to anyone checking the arithmetic.
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True, slots=True)
class AssumptionCheck:
    """One assumption, measured against reality.

    Attributes:
        failure_class: Which class this covers.
        action: Which intervention.
        assumed: The effectiveness the economics currently use.
        observed: Recovery rate actually seen, conditioned on propensity.
        n: Observations behind `observed`.
        ci_low, ci_high: 95% Wilson interval on the observed rate.
        sufficient: Whether `n` clears MIN_SAMPLES.
        drifted: Whether the assumption sits outside the interval *and* the
            divergence is material. Both conditions matter - a statistically
            distinguishable gap of two percentage points is not worth changing
            a spending constant over.
        recommendation: Plain-English next step for a human.
    """

    failure_class: str
    action: str
    assumed: float
    observed: float
    n: int
    ci_low: float
    ci_high: float
    sufficient: bool
    drifted: bool
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "action": self.action,
            "assumed": round(self.assumed, 3),
            "observed": round(self.observed, 3),
            "n": self.n,
            "ci_low": round(self.ci_low, 3),
            "ci_high": round(self.ci_high, 3),
            "sufficient": self.sufficient,
            "drifted": self.drifted,
            "recommendation": self.recommendation,
        }


def check_assumptions() -> list[AssumptionCheck]:
    """Compare every effectiveness assumption against observed outcomes.

    The observed rate is conditioned on the model's own propensity estimate,
    because effectiveness is defined as a *multiplier* on propensity rather
    than a raw recovery rate. Comparing a raw rate against a multiplier would
    make every assumption look wrong for reasons that have nothing to do with
    the assumption.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT d.failure_class, d.action,
                   COUNT(*)                        AS n,
                   SUM(o.recovered)                AS recovered,
                   AVG(d.base_propensity)          AS mean_propensity
            FROM decisions d
            JOIN outcomes o ON o.event_id = d.event_id
            WHERE d.action != 'DROP'
            GROUP BY d.failure_class, d.action
            """
        ).fetchall()

    checks: list[AssumptionCheck] = []

    for row in rows:
        n = row["n"]
        recovered = row["recovered"] or 0
        propensity = row["mean_propensity"] or 0.0
        if n == 0 or propensity <= 0:
            continue

        try:
            action = RecoveryAction(row["action"])
        except ValueError:
            continue

        assumed = effectiveness(row["failure_class"], action)

        # Back out the implied multiplier: observed rate / mean propensity.
        raw_rate = recovered / n
        observed = min(1.0, raw_rate / propensity)

        lo, hi = _wilson(recovered, n)
        lo, hi = min(1.0, lo / propensity), min(1.0, hi / propensity)

        sufficient = n >= MIN_SAMPLES
        outside = not (lo <= assumed <= hi)
        material = assumed > 0 and abs(observed - assumed) / assumed >= DRIFT_THRESHOLD
        drifted = sufficient and outside and material

        if not sufficient:
            recommendation = (
                f"Only {n} observations - too few to judge. Interval "
                f"[{lo:.2f}, {hi:.2f}] contains almost anything."
            )
        elif drifted:
            direction = "over" if assumed > observed else "under"
            recommendation = (
                f"Assumption {assumed:.2f} sits outside the observed interval "
                f"[{lo:.2f}, {hi:.2f}] and {direction}states effectiveness by "
                f"{abs(observed - assumed) / assumed:.0%}. Review this entry in "
                "ACTION_EFFECTIVENESS."
            )
        else:
            recommendation = (
                f"Consistent with {n} observations - interval "
                f"[{lo:.2f}, {hi:.2f}] contains the assumption."
            )

        checks.append(
            AssumptionCheck(
                failure_class=row["failure_class"],
                action=action.value,
                assumed=assumed,
                observed=observed,
                n=n,
                ci_low=lo,
                ci_high=hi,
                sufficient=sufficient,
                drifted=drifted,
                recommendation=recommendation,
            )
        )

    checks.sort(key=lambda c: (not c.drifted, -c.n))
    return checks


def check_organic_baseline() -> list[dict[str, Any]]:
    """Compare assumed organic recovery against what DROPped payments did.

    Payments the system declined to act on are a natural control group: nobody
    contacted them, so whatever came back came back on its own. That makes this
    the one assumption the system can measure almost directly - and it is the
    assumption that decides how much credit Salvage claims, so it is the one
    most worth checking.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT d.failure_class,
                   COUNT(*)             AS n,
                   SUM(o.recovered)     AS recovered,
                   AVG(d.base_propensity) AS mean_propensity
            FROM decisions d
            JOIN outcomes o ON o.event_id = d.event_id
            WHERE d.action = 'DROP'
            GROUP BY d.failure_class
            """
        ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        n = row["n"]
        propensity = row["mean_propensity"] or 0.0
        if n == 0 or propensity <= 0:
            continue

        assumed = ORGANIC_BASELINE.get(row["failure_class"], 0.10)
        observed = min(1.0, (row["recovered"] or 0) / n / propensity)
        lo, hi = _wilson(row["recovered"] or 0, n)

        out.append(
            {
                "failure_class": row["failure_class"],
                "assumed": round(assumed, 3),
                "observed": round(observed, 3),
                "n": n,
                "ci_low": round(min(1.0, lo / propensity), 3),
                "ci_high": round(min(1.0, hi / propensity), 3),
                "sufficient": n >= MIN_SAMPLES,
            }
        )

    out.sort(key=lambda r: -r["n"])
    return out


def report() -> dict[str, Any]:
    """The full learning report, for the API and the dashboard."""
    checks = check_assumptions()
    organic = check_organic_baseline()

    drifted = [c for c in checks if c.drifted]
    insufficient = [c for c in checks if not c.sufficient]

    return {
        "assumptions_checked": len(checks),
        "drifted": len(drifted),
        "insufficient_data": len(insufficient),
        "min_samples": MIN_SAMPLES,
        "drift_threshold": DRIFT_THRESHOLD,
        "checks": [c.to_dict() for c in checks],
        "organic_baseline": organic,
        "policy": (
            "Assumptions are reported, never auto-updated. Refitting the "
            "constants that govern spending from a few hundred outcomes would "
            "let a bad fortnight teach the system to stop recovering, with no "
            "human in the path. A person changes the number."
        ),
        "matrix_entries": sum(len(v) for v in ACTION_EFFECTIVENESS.values()),
    }
