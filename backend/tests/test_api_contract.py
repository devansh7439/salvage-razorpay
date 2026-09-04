"""Contract tests for the JSON the dashboard reads.

Every other test in this suite exercises Python calling Python. The dashboard
does not: it calls HTTP and destructures the result. That seam has no type
checker across it, so a key renamed in `learning.py` type-checks in mypy,
passes every unit test, and reaches the browser as a panel of blank cells and
`NaN` — silently, because React renders `undefined` as nothing at all.

This is the same failure INC-012 recorded from the other direction: the gap
was invisible because everything around it looked complete. A response shape
nobody asserts on is a comment describing an intention.

So these tests assert the shape, not the values. Values move with the batch;
the contract must not. Where a number is asserted at all it is asserted as a
property that has to hold for the UI to be truthful — a fraction that renders
as a percentage must be in [0, 1], and an interval drawn as a bar must have
its low bound below its high bound, or the bar renders inside out.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from salvage import db
from salvage.main import app
from salvage.pipeline import process_batch, record_outcomes
from salvage.simulator.generate import generate_events


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "database_path", tmp_path / "api.db")
    db.reset_db()
    events = generate_events(1200, seed=99)
    process_batch(events)
    record_outcomes(events)
    return TestClient(app)


class TestLearningContract:
    """`/api/learning` feeds the Learning view. These are its load-bearing keys."""

    def test_top_level_shape(self, client):
        body = client.get("/api/learning").json()

        for key in (
            "assumptions_checked",
            "drifted",
            "insufficient_data",
            "min_samples",
            "matrix_entries",
            "policy",
            "checks",
            "organic_baseline",
            "effectiveness",
        ):
            assert key in body, f"the dashboard reads {key!r}"

    def test_every_check_carries_what_the_bar_needs(self, client):
        checks = client.get("/api/learning").json()["checks"]
        assert checks, "the fixture batch must produce assumptions to audit"

        for c in checks:
            assert {
                "failure_class",
                "action",
                "assumed",
                "observed",
                "n",
                "ci_low",
                "ci_high",
                "sufficient",
                "drifted",
                "recommendation",
            } <= set(c)

            # The interval is drawn as a bar from ci_low to ci_high. Inverted
            # bounds would render as a negative width, which CSS clamps to
            # nothing - an interval that silently disappears.
            assert c["ci_low"] <= c["ci_high"]
            assert 0.0 <= c["ci_low"] <= 1.0
            assert 0.0 <= c["ci_high"] <= 1.0
            assert 0.0 <= c["assumed"] <= 1.0
            assert 0.0 <= c["observed"] <= 1.0

    def test_drift_is_never_claimed_on_thin_evidence(self, client):
        """The headline stat is 'contradicted by data'. It has to mean it."""
        body = client.get("/api/learning").json()

        for c in body["checks"]:
            if c["drifted"]:
                assert c["sufficient"], (
                    f"{c['failure_class']}/{c['action']} is reported as drifted "
                    f"on {c['n']} observations"
                )
        assert body["drifted"] == sum(c["drifted"] for c in body["checks"])

    def test_organic_baseline_shape(self, client):
        for o in client.get("/api/learning").json()["organic_baseline"]:
            assert {
                "failure_class",
                "assumed",
                "observed",
                "n",
                "ci_low",
                "ci_high",
                "sufficient",
            } <= set(o)
            assert o["ci_low"] <= o["ci_high"]

    def test_posteriors_shape(self, client):
        eff = client.get("/api/learning").json()["effectiveness"]

        for key in (
            "arms",
            "informed_arms",
            "materially_moved",
            "prior_strength",
            "posteriors",
            "identification",
            "honesty",
        ):
            assert key in eff

        assert eff["arms"] == len(eff["posteriors"])
        assert eff["informed_arms"] <= eff["arms"]

        for p in eff["posteriors"]:
            assert {
                "failure_class",
                "action",
                "prior",
                "posterior_mean",
                "stdev",
                "observations",
                "exposure",
                "successes",
                "moved",
            } <= set(p)
            assert 0.0 <= p["posterior_mean"] <= 1.0

    def test_an_arm_with_no_evidence_reports_its_prior_unchanged(self, client):
        """The UI lists these separately and says no evidence exists. If the
        posterior had drifted off the prior without observations, that caption
        would be a lie."""
        posteriors = client.get("/api/learning").json()["effectiveness"]["posteriors"]
        unobserved = [p for p in posteriors if p["observations"] == 0]
        assert unobserved, "the default policy leaves arms unexplored, by design"

        for p in unobserved:
            assert p["moved"] == 0.0
            assert p["posterior_mean"] == pytest.approx(p["prior"], abs=1e-3)

    def test_informed_count_matches_the_arms_it_describes(self, client):
        """The header renders 'informed / arms'. Both halves come from the same
        list, so they cannot be allowed to disagree with it."""
        eff = client.get("/api/learning").json()["effectiveness"]
        assert eff["informed_arms"] == sum(
            p["observations"] >= 30 for p in eff["posteriors"]
        )


class TestMetricsContract:
    """The sidebar and Command Centre read these on every refresh."""

    def test_shape(self, client):
        body = client.get("/api/metrics").json()

        for key in (
            "events",
            "revenue_at_risk_paise",
            "incremental_recovered_paise",
            "gross_recovered_paise",
            "organic_recovered_paise",
            "action_spend_paise",
            "action_breakdown",
            "exceptions",
        ):
            assert key in body

    def test_incremental_never_exceeds_gross(self, client):
        """Incremental is gross minus what would have come back anyway. If this
        inverts, the headline number on the dashboard overclaims - which is the
        single failure mode this project is built to avoid."""
        body = client.get("/api/metrics").json()
        assert body["incremental_recovered_paise"] <= body["gross_recovered_paise"]
        assert (
            body["organic_recovered_paise"] + body["incremental_recovered_paise"]
            == pytest.approx(body["gross_recovered_paise"], abs=1)
        )
