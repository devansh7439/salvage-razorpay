"""Inference: turn a failed payment into a calibrated recovery propensity.

The model is trained on historical outcomes under one reference intervention
(a payment link), so its raw output is P(recovery | payment link). The policy
engine needs something intervention-independent, because it has to price
several candidate actions against each other.

Dividing out the reference action's known effectiveness recovers that:

    base_propensity = P(recovery | reference) / effectiveness[class][reference]

The division is by a documented constant from `economics.ACTION_EFFECTIVENESS`,
not by anything learned, so the operation is inspectable and reversible.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from salvage.economics import RecoveryAction, effectiveness
from salvage.ml.features import ALL_FEATURES, extract
from salvage.taxonomy import classify

MODEL_PATH = Path(__file__).resolve().parents[3] / "data" / "recovery_model.joblib"

#: Failure classes the model is never trained on, because hard constraints
#: resolve them before it is consulted. Kept here rather than in `train` so
#: training and inference cannot disagree about the population.
UNTRAINED_CLASSES: frozenset[str] = frozenset(
    {"RISK_BLOCKED", "ALREADY_PAID", "MERCHANT_CONFIG", "UNKNOWN"}
)

#: Divisor used for those classes, roughly the mean payment-link effectiveness
#: across the classes the model *was* trained on.
#:
#: The per-class divisor is correct for trained classes and meaningless for
#: untrained ones - and actively dangerous for two of them, where the
#: effectiveness is legitimately zero and the division explodes. Substituting a
#: neutral constant keeps the propensity in a sane range for the one untrained
#: class that still needs a number (MERCHANT_CONFIG, which is priced for
#: escalation) without pretending to a precision that does not exist.
NEUTRAL_REFERENCE_FIT = 0.63

#: Below this, a divisor is treated as unusable rather than merely small.
MIN_USABLE_FIT = 0.05

_lock = threading.Lock()
_bundle: dict[str, Any] | None = None


class ModelNotTrainedError(RuntimeError):
    """Raised when inference is attempted before the model exists on disk."""


def load_model(path: Path | None = None) -> dict[str, Any]:
    """Load the serialised model once and cache it.

    Guarded by a lock because FastAPI serves requests from a thread pool and a
    torn read of a partially-loaded bundle would be a miserable thing to debug.

    An explicit `path` loads that model *without* touching the process cache.
    The previous behaviour reloaded from disk whenever a path was passed and
    then overwrote the shared cache with it, so a single test or script that
    loaded an alternative model silently repointed inference for the rest of
    the process.
    """
    global _bundle

    if path is not None:
        if not path.exists():
            raise ModelNotTrainedError(
                f"No model at {path}. Run: python -m salvage.ml.train"
            )
        return joblib.load(path)

    with _lock:
        if _bundle is None:
            if not MODEL_PATH.exists():
                raise ModelNotTrainedError(
                    f"No model at {MODEL_PATH}. Run: python -m salvage.ml.train"
                )
            _bundle = joblib.load(MODEL_PATH)
    return _bundle


def is_loaded() -> bool:
    """Whether a model is resident in memory."""
    return _bundle is not None


def predict_propensity(event: Any) -> float:
    """Calibrated base propensity for one failed payment, in [0, 1].

    Args:
        event: A failed payment exposing the webhook and history fields that
            `features.extract` reads.

    Returns:
        The customer's underlying propensity to complete this payment, stripped
        of any particular intervention's contribution.
    """
    return predict_propensity_batch([event])[0]


#: Rows per `predict_proba` call. Caps the size of the dense feature matrix
#: built for one inference pass. One-hot encoding widens each event to ~25
#: float64 columns, so an unbounded batch allocates a matrix proportional to
#: the whole input - fine at a thousand events, an out-of-memory error at ten
#: million. Throughput is flat above a few thousand rows, so there is nothing
#: to lose by bounding it.
INFERENCE_CHUNK = 4096


def predict_propensity_batch(
    events: list[Any], chunk_size: int = INFERENCE_CHUNK
) -> list[float]:
    """Vectorised propensity scoring, in bounded chunks.

    Batched deliberately: scoring a thousand payments one at a time spends
    almost all its time in per-call pipeline overhead rather than in the forest.
    Chunked equally deliberately, so peak memory does not scale with the input.
    """
    if not events:
        return []

    bundle = load_model()
    pipeline = bundle["pipeline"]
    reference = RecoveryAction(bundle["reference_action"])

    out: list[float] = []
    for start in range(0, len(events), chunk_size):
        window = events[start : start + chunk_size]
        frame = pd.DataFrame(
            [extract(e) for e in window], columns=list(ALL_FEATURES)
        )
        probabilities = pipeline.predict_proba(frame)[:, 1]

        for event, p_reference in zip(window, probabilities):
            classification = classify(
                getattr(event, "error_reason", None),
                getattr(event, "error_code", None),
                getattr(event, "error_source", None),
                getattr(event, "error_step", None),
            )
            failure_class = classification.failure_class.value
            fit = effectiveness(failure_class, reference)

            # Untrained classes get a neutral divisor. Dividing by a genuinely
            # zero effectiveness would send the propensity to its ceiling and
            # inflate every expected value derived from it.
            if failure_class in UNTRAINED_CLASSES or fit < MIN_USABLE_FIT:
                fit = NEUTRAL_REFERENCE_FIT

            out.append(float(min(1.0, max(0.0, p_reference / fit))))

    return out
