"""Tests for learned effectiveness and bounded exploration.

The headline test here starts the system from *deliberately wrong* priors.
That matters more than it looks: seeding the posterior with the hand-authored
matrix and then observing it stay near the hand-authored matrix demonstrates
nothing at all, because on simulated data those constants are also what the
oracle uses to generate outcomes. The estimate would be a mirror.

Breaking the prior is what makes the demonstration real. If the posterior
moves *from* a wrong starting point *toward* the generating truth, using only
outcomes the system actually observed, then the estimator works - and that is
the property which has to hold before the same code can be pointed at a
merchant's real history.
"""

from __future__ import annotations

import pytest
from salvage import bandit, db
from salvage.economics import (
    ACTION_EFFECTIVENESS,
    MerchantPolicy,
    RecoveryAction,
)
from salvage.pipeline import process_batch, record_outcomes
from salvage.simulator.generate import generate_events

EXPLORING = MerchantPolicy(explore_fraction=0.30, max_explore_amount_paise=500_000)


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "l.db")
    db.reset_db()
    events = generate_events(3000, seed=4242)
    process_batch(events, policy=EXPLORING)
    record_outcomes(events)
    return events


class TestBoundedExploration:
    """Learning costs money. The cost is capped before it is incurred."""

    def test_disabled_by_default(self):
        assert MerchantPolicy().explore_fraction == 0.0

    def test_never_explores_above_the_amount_ceiling(self):
        assert not bandit.should_explore("pay_x", 10_00_000, 0.5, 2_00_000)

    def test_respects_the_budget(self):
        ids = [f"pay_{i:05d}" for i in range(4000)]
        chosen = sum(
            bandit.should_explore(i, 1000, 0.10, 5_00_000) for i in ids
        )
        assert 0.07 < chosen / len(ids) < 0.13

    def test_assignment_is_reproducible(self):
        """A replay must explore exactly the same payments, or the evaluation
        stops being reproducible - and reproducibility is the reason anyone
        should believe the result."""
        first = [bandit.should_explore(f"p{i}", 1000, 0.3, 5_00_000) for i in range(500)]
        again = [bandit.should_explore(f"p{i}", 1000, 0.3, 5_00_000) for i in range(500)]
        assert first == again

    def test_absurd_budgets_are_refused(self):
        with pytest.raises(ValueError):
            MerchantPolicy(explore_fraction=0.9)
        with pytest.raises(ValueError):
            MerchantPolicy(explore_fraction=-0.1)

    def test_exploration_cannot_unlock_a_forbidden_action(self, batch):
        """Exploration reorders permitted actions. It never permits new ones."""
        with db.connect() as conn:
            leaked = conn.execute(
                """
                SELECT COUNT(*) n FROM executions x
                JOIN decisions d ON d.event_id = x.event_id
                WHERE d.failure_class IN ('RISK_BLOCKED','ALREADY_PAID')
                   OR (d.failure_class = 'MERCHANT_CONFIG'
                       AND x.action IN ('PAYMENT_LINK','NOTIFY'))
                   OR (d.failure_class = 'INSTRUMENT_INVALID'
                       AND x.action LIKE 'RETRY%')
                """
            ).fetchone()["n"]
        assert leaked == 0


class TestPosteriorMechanics:
    def test_priors_reproduce_the_matrix_with_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "empty.db")
        db.reset_db()
        for p in bandit.posteriors():
            assert p.observations == 0
            expected = ACTION_EFFECTIVENESS[p.failure_class][RecoveryAction(p.action)]
            assert p.mean == pytest.approx(expected, abs=0.01)

    def test_beta_never_goes_negative_on_a_lucky_sample(self, batch):
        for p in bandit.posteriors():
            assert p.alpha > 0 and p.beta > 0
            assert 0.0 <= p.mean <= 1.0

    def test_uncertainty_shrinks_with_evidence(self, batch):
        posts = [p for p in bandit.posteriors() if p.observations > 0]
        assert posts
        informed = max(posts, key=lambda p: p.observations)
        uninformed = min(bandit.posteriors(), key=lambda p: p.observations)
        assert informed.stdev < uninformed.stdev


class TestEstimatorRecoversTruth:
    """The test that actually demonstrates something."""

    def test_posterior_moves_from_a_wrong_prior_toward_the_truth(
        self, tmp_path, monkeypatch
    ):
        """Start every arm at 0.5 - deliberately wrong for almost all of them -
        and check the observed data pulls each estimate toward the value the
        generator actually uses.

        Only outcomes the system recorded are used. The simulator's latent
        truth is read at the end, to score the estimate, never to produce it.
        """
        truth = {
            (fc, action.value): value
            for fc, actions in ACTION_EFFECTIVENESS.items()
            for action, value in actions.items()
        }

        # Flatten every prior to 0.5.
        monkeypatch.setattr(
            bandit,
            "_prior",
            lambda _fc, _a: (0.5 * bandit.PRIOR_STRENGTH, 0.5 * bandit.PRIOR_STRENGTH),
        )

        monkeypatch.setattr(db.settings, "database_path", tmp_path / "wrong.db")
        db.reset_db()
        events = generate_events(8000, seed=99)
        process_batch(events, policy=EXPLORING)
        record_outcomes(events)

        moved_closer = 0
        considered = 0

        for p in bandit.posteriors():
            actual = truth.get((p.failure_class, p.action))
            if actual is None or p.observations < 100:
                continue
            # Skip arms where 0.5 was already about right; there is nothing to
            # move toward and including them would flatter the result.
            if abs(actual - 0.5) < 0.08:
                continue

            considered += 1
            if abs(p.mean - actual) < abs(0.5 - actual):
                moved_closer += 1

        assert considered >= 3, "not enough well-observed arms to judge"
        assert moved_closer / considered >= 0.75, (
            f"only {moved_closer}/{considered} arms moved toward the truth"
        )

    def test_unexplored_arms_stay_at_their_prior(self, batch):
        """Effectiveness is not identifiable where assignment never varies.

        An arm the policy never picks has no evidence about it, and the honest
        posterior for no evidence is the prior. A system that reported a
        confident learned value for an action it has never taken would be
        making it up.
        """
        for p in bandit.posteriors():
            if p.observations == 0:
                expected = ACTION_EFFECTIVENESS[p.failure_class][
                    RecoveryAction(p.action)
                ]
                assert p.mean == pytest.approx(expected, abs=0.01)


class TestConvergenceReportIsHonest:
    def test_report_states_the_identification_limit(self, batch):
        report = bandit.convergence_report()
        assert "identifiable" in report["identification"]
        assert "validates the estimator" in report["honesty"]

    def test_report_separates_informed_from_uninformed_arms(self, batch):
        report = bandit.convergence_report()
        assert report["arms"] >= report["informed_arms"]
        assert report["informed_arms"] > 0
