"""Learned action effectiveness, with bounded exploration.

`ACTION_EFFECTIVENESS` in `economics` is hand-authored domain judgement. That
was always the weakest claim in this system: every rupee it decides to spend
rests on numbers somebody typed.

The obvious fix - fit them from observed outcomes - does not work on its own,
and the reason is worth stating because it is the whole design.

**Effectiveness is not identifiable under a deterministic policy.** The engine
sends every `BANK_DOWNTIME` payment to a scheduled retry. It therefore has no
observation of what a payment link would have done to those payments, and never
will. Fitting effectiveness from that data recovers the policy's own choices,
not the world. It would look like learning and be a mirror.

Exploration creates the variation that makes the estimate mean something. A
small, bounded fraction of payments are assigned an action *other* than the
current best, chosen by Thompson sampling from the posterior over
effectiveness. Those payments are the experiment; everything else continues to
be optimised.

Three properties make that safe enough to do with a merchant's money:

**Priors, not cold start.** The posterior is seeded from the hand-authored
matrix with a strength of `PRIOR_STRENGTH` pseudo-observations. On day one the
system behaves exactly as it did before; the data moves it only as evidence
accumulates. A payments system that starts by guessing uniformly is not
deployable.

**Exploration is bounded by amount and by budget.** Never above
`max_explore_amount_paise`, never more than `explore_fraction` of payments, and
never at all on anything a hard constraint has already settled. The cost of
learning is capped in rupees before it is incurred.

**It is off by default.** Deliberate exploration is a decision a merchant
makes, not a default they discover.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any

from salvage import db
from salvage.economics import ACTION_EFFECTIVENESS, RecoveryAction, effectiveness

#: Weight of the hand-authored prior, in pseudo-observations.
#:
#: 60 is chosen so the prior dominates for the first few dozen outcomes per
#: arm and yields to data after a few hundred - roughly a fortnight of traffic
#: for a mid-size merchant. Lower would let a quiet week rewrite the economics;
#: higher would make the whole exercise decorative.
PRIOR_STRENGTH = 60.0


@dataclass(frozen=True, slots=True)
class Posterior:
    """Beta posterior over one arm's effectiveness.

    Effectiveness is a *multiplier* on propensity rather than a raw rate, so
    the usual Beta-Bernoulli update does not apply directly: a payment with
    propensity 0.4 that recovers is weaker evidence than one with propensity
    0.9 that recovers.

    The trick is to count exposure in propensity rather than in payments.
    Since E[recovered] = sum(p_i) * effectiveness, the sum of propensities is
    the effective number of trials, and

        alpha = alpha_0 + successes
        beta  = beta_0  + sum(p_i) - successes

    gives a posterior whose mean is the propensity-adjusted recovery rate. It
    is an approximation - the exact likelihood is Poisson-binomial - but it is
    conjugate, cheap, and accurate in the regime that matters.
    """

    failure_class: str
    action: str
    alpha: float
    beta: float
    successes: int
    exposure: float
    observations: int

    @property
    def mean(self) -> float:
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.0

    @property
    def stdev(self) -> float:
        a, b = self.alpha, self.beta
        total = a + b
        if total <= 1:
            return 0.5
        return math.sqrt((a * b) / (total**2 * (total + 1)))

    def sample(self, rng: random.Random) -> float:
        """One draw from the posterior. This is the Thompson step."""
        return min(1.0, max(0.0, rng.betavariate(max(self.alpha, 1e-6), max(self.beta, 1e-6))))

    def to_dict(self) -> dict[str, Any]:
        prior = effectiveness(self.failure_class, RecoveryAction(self.action))
        return {
            "failure_class": self.failure_class,
            "action": self.action,
            "prior": round(prior, 3),
            "posterior_mean": round(self.mean, 3),
            "stdev": round(self.stdev, 3),
            "observations": self.observations,
            "exposure": round(self.exposure, 1),
            "successes": self.successes,
            "moved": round(self.mean - prior, 3),
        }


def _prior(failure_class: str, action: RecoveryAction) -> tuple[float, float]:
    """Beta parameters encoding the hand-authored assumption."""
    e = max(1e-3, min(1 - 1e-3, effectiveness(failure_class, action)))
    return e * PRIOR_STRENGTH, (1 - e) * PRIOR_STRENGTH


def observed_arms() -> dict[tuple[str, str], tuple[int, float, int]]:
    """Read (successes, exposure, n) per arm from adjudicated outcomes.

    Only outcomes the system actually observed. The simulator's latent truth is
    never consulted here - if it were, the estimate would be a restatement of
    the generator rather than a measurement.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT d.failure_class, d.action,
                   COUNT(*)               AS n,
                   SUM(o.recovered)       AS successes,
                   SUM(d.base_propensity) AS exposure
            FROM decisions d
            JOIN outcomes o ON o.event_id = d.event_id
            WHERE d.action != 'DROP'
            GROUP BY d.failure_class, d.action
            """
        ).fetchall()

    return {
        (r["failure_class"], r["action"]): (
            int(r["successes"] or 0),
            float(r["exposure"] or 0.0),
            int(r["n"]),
        )
        for r in rows
    }


def posteriors() -> list[Posterior]:
    """Current posterior for every arm the economics knows about."""
    observed = observed_arms()
    out: list[Posterior] = []

    for failure_class, actions in ACTION_EFFECTIVENESS.items():
        for action in actions:
            successes, exposure, n = observed.get(
                (failure_class, action.value), (0, 0.0, 0)
            )
            a0, b0 = _prior(failure_class, action)
            out.append(
                Posterior(
                    failure_class=failure_class,
                    action=action.value,
                    alpha=a0 + successes,
                    # Exposure below successes would push beta negative on a
                    # small, lucky sample. Clamped rather than trusted.
                    beta=b0 + max(0.0, exposure - successes),
                    successes=successes,
                    exposure=exposure,
                    observations=n,
                )
            )

    out.sort(key=lambda p: -p.observations)
    return out


def posterior_map() -> dict[tuple[str, str], Posterior]:
    return {(p.failure_class, p.action): p for p in posteriors()}


def should_explore(
    event_id: str,
    amount_paise: int,
    explore_fraction: float,
    max_explore_amount_paise: int,
) -> bool:
    """Whether this payment is assigned to the exploration arm.

    Deterministic in the payment id rather than randomly sampled, so a replay
    of the same batch explores exactly the same payments. An exploration arm
    that moves between runs would make the evaluation irreproducible, and the
    reproducibility is the reason anyone should believe the result.

    Bounded twice over: a payment is only eligible below the amount ceiling,
    and only the configured fraction of eligible payments is used. The cost of
    learning is capped in rupees before it is spent.
    """
    if explore_fraction <= 0 or amount_paise > max_explore_amount_paise:
        return False

    digest = hashlib.sha256(f"explore:{event_id}".encode()).digest()
    draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return draw < explore_fraction


def exploration_effectiveness(
    failure_class: str,
    action: RecoveryAction,
    event_id: str,
) -> float:
    """Thompson draw for one arm, seeded per payment for reproducibility."""
    key = posterior_map().get((failure_class, action.value))
    if key is None:
        return effectiveness(failure_class, action)

    seed = int.from_bytes(
        hashlib.sha256(f"ts:{event_id}:{action.value}".encode()).digest()[:8], "big"
    )
    return key.sample(random.Random(seed))


def learned_effectiveness(failure_class: str, action: RecoveryAction) -> float:
    """Posterior mean - the estimate that replaces the hand-authored constant.

    Falls back to the prior when an arm has no observations, which is exactly
    what the posterior mean already does, so the behaviour is continuous.
    """
    key = posterior_map().get((failure_class, action.value))
    return key.mean if key else effectiveness(failure_class, action)


def convergence_report() -> dict[str, Any]:
    """How far the data has moved each assumption, and how sure it is.

    The honest headline is not "the matrix was learned" - on a simulated batch
    the estimates converge toward constants the generator already knows. What
    it demonstrates is that the *estimator* recovers a known truth from
    observed outcomes alone, which is the property that has to hold before the
    same code can be pointed at a merchant's real history.
    """
    posts = posteriors()
    informed = [p for p in posts if p.observations >= 30]
    moved = [p for p in informed if abs(p.mean - effectiveness(p.failure_class, RecoveryAction(p.action))) >= 0.05]

    return {
        "arms": len(posts),
        "informed_arms": len(informed),
        "materially_moved": len(moved),
        "prior_strength": PRIOR_STRENGTH,
        "posteriors": [p.to_dict() for p in posts],
        "identification": (
            "Effectiveness is only identifiable where assignment varies. A "
            "deterministic policy sends every payment in a class to the same "
            "action, so arms it never picks stay at their prior forever - "
            "correctly, since no evidence about them exists. Bounded "
            "exploration is what creates that evidence."
        ),
        "honesty": (
            "On simulated data these estimates converge toward the generator's "
            "own constants. That validates the estimator, not the system's "
            "intelligence. The claim is that the same code fits from real "
            "outcomes, not that the matrix has been independently confirmed."
        ),
    }
