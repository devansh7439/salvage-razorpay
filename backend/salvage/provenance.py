"""Telling a real payment from a simulated one.

The system is driven from two sources. Razorpay webhooks carry payments that
actually failed for actual customers. The simulator fabricates thousands more
so the policy engine can be evaluated against a known ground truth.

Almost everything treats those identically, which is the point - the pipeline
cannot have one code path for the demo and another for production, or the
demo stops being evidence about production. The exception is anything that
reaches outside the process: creating a Payment Link, calling a model. Doing
that for a payment which never happened is at best wasteful and at worst
wrong, so the two sources have to be distinguishable at the point of action.

The test is the presence of latent ground truth. The generator attaches
`_true_reliability`, `_true_intent` and friends so the oracle can decide
outcomes and evaluation can score calibration. Those values are unknowable for
a payment that really happened - no merchant holds them and no webhook carries
them - so an event that has them cannot be real.

Deriving it from the data rather than a configuration flag is deliberate. A
flag can be left in the wrong position by a deployment, and the failure mode
of getting this wrong is issuing genuine payment links against fabricated
customers, or spending an evaluation run's latency budget on live API calls
whose output nothing scores.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

#: Prefix of the simulator's latent ground-truth fields.
LATENT_TRUTH_PREFIX = "_true_"


def is_simulated(event: Any) -> bool:
    """Whether this event was fabricated by the simulator."""
    try:
        names = (
            (f.name for f in fields(event))
            if is_dataclass(event)
            else iter(vars(event))
        )
        return any(name.startswith(LATENT_TRUTH_PREFIX) for name in names)
    except TypeError:
        # An object exposing neither dataclass fields nor __dict__ cannot be
        # something the simulator produced.
        return False
