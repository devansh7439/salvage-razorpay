"""Train and calibrate the recovery-propensity model.

Three decisions here are worth more than the choice of estimator.

**Calibration, not accuracy.** This probability gets multiplied by rupees. A
model that ranks payments perfectly but reports 0.9 when it means 0.6 will
systematically overspend on recovery, and no amount of AUC will reveal that. So
the estimator is wrapped in `CalibratedClassifierCV` and the headline metric is
the Brier score. Ranking quality is reported too, but it is the secondary
number.

**Split by customer, not by row.** Customers recur across the batch - the same
person fails several payments. A random row split would put one customer's
payments on both sides of the wall, and the model would score well by
recognising people it had already been paid to memorise. Splitting on
`customer_id` makes the test set genuinely unseen.

**Train only on the population that gets scored.** Risk blocks, already-settled
orders, merchant misconfiguration, and undiagnosable failures never reach the
model in production - hard constraints resolve them first. Training on them
would teach the model to predict outcomes it will never be asked about, and
would inflate metrics with easy negatives. They are excluded, exactly as a real
merchant's outcome data would exclude payments it never tried to recover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from salvage.economics import RecoveryAction, effectiveness
from salvage.ml.features import (
    ALL_FEATURES,
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    extract,
)
from salvage.simulator.generate import SyntheticEvent, generate_events
from salvage.simulator.oracle import observe
from salvage.taxonomy import FailureClass, classify

#: The intervention the historical training data was collected under.
#:
#: A merchant's outcome history reflects whatever their old system did. We model
#: the common naive case - "send everyone a payment link" - which is what most
#: off-the-shelf recovery tools do. The model therefore learns
#: P(recovery | payment link), and propensity is recovered at inference by
#: dividing out that action's known effectiveness.
REFERENCE_ACTION = RecoveryAction.PAYMENT_LINK

#: Failure classes excluded from training. Hard constraints resolve these before
#: the model is ever consulted.
EXCLUDED_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.RISK_BLOCKED,
        FailureClass.ALREADY_PAID,
        FailureClass.MERCHANT_CONFIG,
        FailureClass.UNKNOWN,
    }
)

MODEL_PATH = Path(__file__).resolve().parents[3] / "data" / "recovery_model.joblib"
METRICS_PATH = Path(__file__).resolve().parents[3] / "data" / "model_metrics.json"


@dataclass(slots=True)
class TrainingReport:
    """Held-out performance. Every figure comes from unseen customers."""

    n_total: int
    n_train: int
    n_test: int
    n_excluded: int
    base_rate: float
    roc_auc: float
    brier: float
    brier_baseline: float
    brier_improvement: float
    propensity_correlation: float
    oracle_auc: float
    oracle_brier: float
    signal_captured: float
    calibration_bins: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_total": self.n_total,
            "n_train": self.n_train,
            "n_test": self.n_test,
            "n_excluded": self.n_excluded,
            "base_rate": round(self.base_rate, 4),
            "roc_auc": round(self.roc_auc, 4),
            "brier": round(self.brier, 4),
            "brier_baseline": round(self.brier_baseline, 4),
            "brier_improvement_pct": round(self.brier_improvement * 100, 2),
            "propensity_correlation": round(self.propensity_correlation, 4),
            "oracle_auc": round(self.oracle_auc, 4),
            "oracle_brier": round(self.oracle_brier, 4),
            "signal_captured_pct": round(self.signal_captured * 100, 1),
            "calibration_bins": self.calibration_bins,
        }


def build_pipeline(seed: int = 42) -> Pipeline:
    """Preprocessing plus a calibrated random forest.

    The forest is kept deliberately shallow and well-regularised. On a batch
    this size an unconstrained forest will memorise individual customers, and
    because customers recur, that memorisation looks like skill right up until
    it meets a new one.
    """
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), list(NUMERIC_FEATURES)),
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                list(CATEGORICAL_FEATURES),
            ),
        ]
    )

    # `max_features="sqrt"` starves each split on a one-hot-widened matrix -
    # roughly five of ~25 columns - and the strongest signal here lives in two
    # numeric columns. Sampling half the features per split lets them compete.
    forest = RandomForestClassifier(
        n_estimators=500,
        max_depth=10,
        min_samples_leaf=25,
        max_features=0.5,
        class_weight=None,
        random_state=seed,
        n_jobs=-1,
    )

    # Isotonic calibration on cross-validated folds. Sigmoid would impose a
    # parametric shape the forest's probabilities do not have; isotonic only
    # assumes monotonicity, which is the assumption we can actually defend.
    calibrated = CalibratedClassifierCV(forest, method="isotonic", cv=5)

    return Pipeline([("prep", preprocessor), ("model", calibrated)])


def _label_events(
    events: list[SyntheticEvent],
) -> tuple[list[dict], list[int], list[str], list[float], list[float]]:
    """Feature rows, labels, customer groups, and latent truth for diagnostics.

    Labels come from the oracle under the reference action, i.e. what the
    merchant would have observed historically. The latent propensities and true
    per-event probabilities are returned only so training can measure how much
    signal was theoretically available - they are never features and never
    targets.
    """
    rows: list[dict] = []
    labels: list[int] = []
    groups: list[str] = []
    latent: list[float] = []
    true_p: list[float] = []

    for event in events:
        classification = classify(
            event.error_reason, event.error_code, event.error_source, event.error_step
        )
        if classification.failure_class in EXCLUDED_CLASSES:
            continue

        outcome = observe(
            event, REFERENCE_ACTION, classification.failure_class.value
        )
        rows.append(extract(event))
        labels.append(int(outcome.recovered))
        groups.append(event.customer_id)
        latent.append(event._true_base_propensity)
        true_p.append(outcome.p_action)

    return rows, labels, groups, latent, true_p


def _calibration_bins(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 8
) -> list[dict[str, float]]:
    """Reliability table: predicted probability against observed frequency.

    This is the artifact that shows whether a probability means what it says.
    A well-calibrated model has `predicted` tracking `observed` down the table.
    """
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[dict[str, float]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi if hi < 1.0 else y_prob <= hi)
        if not mask.any():
            continue
        bins.append(
            {
                "range": f"{lo:.2f}-{hi:.2f}",
                "n": int(mask.sum()),
                "predicted": round(float(y_prob[mask].mean()), 4),
                "observed": round(float(y_true[mask].mean()), 4),
            }
        )
    return bins


def train(
    n_events: int = 6000,
    seed: int = 20260903,
    test_fraction: float = 0.25,
    save: bool = True,
) -> tuple[Pipeline, TrainingReport]:
    """Train, calibrate, and evaluate the propensity model.

    Args:
        n_events: Size of the synthetic training corpus. Larger than the
            evaluation batch on purpose - a merchant deploying this would have
            months of history behind them.
        seed: RNG seed for the corpus.
        test_fraction: Share of *customers* held out.
        save: Whether to write the model and metrics to `data/`.

    Returns:
        The fitted pipeline and its held-out report.
    """
    events = generate_events(n_events, seed=seed)
    rows, labels, groups, latent, true_p = _label_events(events)

    n_excluded = len(events) - len(rows)
    if len(rows) < 100:
        raise ValueError(f"Not enough trainable events: {len(rows)}")

    import pandas as pd

    X = pd.DataFrame(rows, columns=list(ALL_FEATURES))
    y = np.asarray(labels)
    g = np.asarray(groups)
    latent_arr = np.asarray(latent)
    true_p_arr = np.asarray(true_p)

    # Hold out whole customers, never individual payments.
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_fraction, random_state=seed)
    train_idx, test_idx = next(splitter.split(X, y, groups=g))

    pipeline = build_pipeline(seed=42)
    pipeline.fit(X.iloc[train_idx], y[train_idx])

    y_test = y[test_idx]
    y_prob = pipeline.predict_proba(X.iloc[test_idx])[:, 1]

    base_rate = float(y[train_idx].mean())
    brier = float(brier_score_loss(y_test, y_prob))
    # A model that always predicts the base rate. Anything that cannot beat
    # this has learned nothing, whatever its AUC says.
    brier_baseline = float(
        brier_score_loss(y_test, np.full_like(y_prob, base_rate))
    )

    # Diagnostic only: did the model actually recover the latent driver it was
    # never shown? Available solely because this is a simulation.
    derived = np.clip(
        y_prob / max(effectiveness("BANK_DOWNTIME", REFERENCE_ACTION), 1e-6), 0, 1
    )
    correlation = float(np.corrcoef(derived, latent_arr[test_idx])[0, 1])

    # The ceiling. Outcomes are Bernoulli draws, so even a model that knew each
    # payment's true probability exactly would still be wrong much of the time.
    # Scoring the true probabilities against the realised labels measures how
    # much signal the problem actually contains, which is the only honest
    # yardstick for the model's own numbers. A model near this line is not
    # mediocre - it is near-optimal on a genuinely noisy problem, and a model
    # that *beat* it would be evidence of a leak.
    p_test = true_p_arr[test_idx]
    oracle_auc = float(roc_auc_score(y_test, p_test))
    oracle_brier = float(brier_score_loss(y_test, p_test))
    attainable = oracle_auc - 0.5
    signal_captured = (
        (report_auc := float(roc_auc_score(y_test, y_prob))) - 0.5
    ) / attainable if attainable > 1e-9 else 0.0

    report = TrainingReport(
        n_total=len(events),
        n_train=len(train_idx),
        n_test=len(test_idx),
        n_excluded=n_excluded,
        base_rate=base_rate,
        roc_auc=report_auc,
        brier=brier,
        brier_baseline=brier_baseline,
        brier_improvement=(brier_baseline - brier) / brier_baseline,
        propensity_correlation=correlation,
        oracle_auc=oracle_auc,
        oracle_brier=oracle_brier,
        signal_captured=signal_captured,
        calibration_bins=_calibration_bins(y_test, y_prob),
    )

    if save:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "pipeline": pipeline,
                "reference_action": REFERENCE_ACTION.value,
                "features": list(ALL_FEATURES),
                "trained_on": n_events,
                "seed": seed,
            },
            MODEL_PATH,
        )
        METRICS_PATH.write_text(json.dumps(report.to_dict(), indent=2))

    return pipeline, report


def main() -> None:
    """CLI entry point: `python -m salvage.ml.train`."""
    pipeline, report = train()
    d = report.to_dict()

    print("=" * 68)
    print("  Salvage - recovery propensity model")
    print("=" * 68)
    print(f"  corpus              {d['n_total']:,} events")
    print(f"  excluded            {d['n_excluded']:,} (hard-constraint classes)")
    print(f"  train / test        {d['n_train']:,} / {d['n_test']:,} (split by customer)")
    print(f"  base recovery rate  {d['base_rate']:.1%}")
    print()
    print("  CALIBRATION (the metric that matters - this number buys rupees)")
    print(f"    Brier score       {d['brier']:.4f}")
    print(f"    always-base-rate  {d['brier_baseline']:.4f}  (learns nothing)")
    print(f"    perfect-knowledge {d['oracle_brier']:.4f}  (irreducible floor)")
    print(f"    improvement       {d['brier_improvement_pct']:.1f}%")
    print()
    print("  RANKING, against the attainable ceiling")
    print(f"    ROC AUC           {d['roc_auc']:.4f}")
    print(f"    ceiling           {d['oracle_auc']:.4f}  (knowing each true p)")
    print(f"    signal captured   {d['signal_captured_pct']:.1f}% of what exists")
    print()
    print(f"  Recovered latent propensity (r)  {d['propensity_correlation']:.3f}")
    print()
    print("  RELIABILITY TABLE (held-out customers)")
    print(f"    {'bucket':<14}{'n':>6}{'predicted':>12}{'observed':>11}")
    for b in d["calibration_bins"]:
        print(
            f"    {b['range']:<14}{b['n']:>6}{b['predicted']:>12.3f}"
            f"{b['observed']:>11.3f}"
        )
    print()
    print(f"  model  -> {MODEL_PATH}")
    print(f"  metrics-> {METRICS_PATH}")


if __name__ == "__main__":
    main()
