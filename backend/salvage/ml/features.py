"""Feature extraction for the recovery-propensity model.

The feature set is deliberately small and entirely made of things a real
merchant would already hold: the webhook payload, and their own payment history
for that customer. Nothing here requires data a merchant would have to buy,
instrument, or wait a quarter to accumulate.

What is *absent* matters as much as what is present. None of the simulator's
latent variables - true reliability, purchase intent, issuer health - appear
here, and they cannot, because `SyntheticEvent.features()` filters out every
underscore-prefixed field structurally rather than by convention. The model has
the same partial, noisy view of a customer that a production system does.
"""

from __future__ import annotations

import math
from typing import Any

from salvage.taxonomy import classify

#: Numeric features, in a fixed order. Order is pinned because the trained
#: model is serialised against it; a silent reordering would corrupt every
#: prediction without raising anything.
NUMERIC_FEATURES: tuple[str, ...] = (
    "log_amount",
    "customer_success_rate",
    "customer_tenure_days",
    "prior_payment_count",
    "prior_failure_count",
    "failure_ratio",
    "hours_since_last_success",
    "attempt_number",
    "hour_of_day",
    "day_of_week",
    "is_business_hours",
    "is_weekend",
)

#: Categorical features, one-hot encoded at fit time.
CATEGORICAL_FEATURES: tuple[str, ...] = (
    "method",
    "failure_class",
    "error_source",
)

ALL_FEATURES: tuple[str, ...] = NUMERIC_FEATURES + CATEGORICAL_FEATURES


def extract(event: Any) -> dict[str, Any]:
    """Build one feature row from a failed-payment event.

    Accepts either a SyntheticEvent or any object exposing the same webhook and
    history attributes, so the same code path serves training and live webhook
    inference. Training on one representation and serving on another is a
    classic source of skew, and sharing this function removes the opportunity.

    Args:
        event: A failed payment carrying webhook fields and customer history.

    Returns:
        A dict keyed by ALL_FEATURES.
    """
    classification = classify(
        getattr(event, "error_reason", None),
        getattr(event, "error_code", None),
        getattr(event, "error_source", None),
        getattr(event, "error_step", None),
    )

    prior_payments = int(getattr(event, "prior_payment_count", 0))
    prior_failures = int(getattr(event, "prior_failure_count", 0))
    total_prior = prior_payments + prior_failures

    hour = int(getattr(event, "hour_of_day", 12))
    dow = int(getattr(event, "day_of_week", 0))

    return {
        # Log-scaled: ticket sizes span four orders of magnitude, and the
        # difference between Rs 500 and Rs 5,000 matters far more than the
        # difference between Rs 50,000 and Rs 54,500.
        "log_amount": math.log1p(float(getattr(event, "amount", 0))),
        "customer_success_rate": float(getattr(event, "customer_success_rate", 0.5)),
        "customer_tenure_days": float(getattr(event, "customer_tenure_days", 0)),
        "prior_payment_count": float(prior_payments),
        "prior_failure_count": float(prior_failures),
        # Ratio rather than raw counts: a customer with 2 failures out of 3 is
        # a very different proposition from 2 out of 200.
        "failure_ratio": (prior_failures / total_prior) if total_prior else 0.5,
        "hours_since_last_success": float(
            getattr(event, "hours_since_last_success", 0.0)
        ),
        "attempt_number": float(getattr(event, "attempt_number", 1)),
        "hour_of_day": float(hour),
        "day_of_week": float(dow),
        "is_business_hours": 1.0 if 9 <= hour <= 20 else 0.0,
        "is_weekend": 1.0 if dow >= 5 else 0.0,
        "method": str(getattr(event, "method", "unknown")),
        "failure_class": classification.failure_class.value,
        "error_source": str(getattr(event, "error_source", "unknown")),
    }


def extract_batch(events: list[Any]) -> list[dict[str, Any]]:
    """Feature rows for a batch, preserving order."""
    return [extract(e) for e in events]
