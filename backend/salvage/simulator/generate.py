"""Synthetic failed-payment generator.

Razorpay's brief asks for a batch of synthetic data, which raises the question
every synthetic-data ML project has to answer honestly: if the same code writes
both the features and the labels, has the model learned anything at all?

The usual failure mode is circular. A generator picks a failure reason, applies
a rule like "insufficient funds recovers 60% of the time", writes that label,
and a model then rediscovers the rule with near-perfect accuracy. The resulting
metric measures nothing except that the model can read the generator's source.

This generator is built to avoid that:

  1. **Outcomes are driven by latent variables the model never sees.** True
     customer reliability, purchase intent, and issuer health all influence
     whether a payment recovers, and none of them appear in the feature set.

  2. **Observable features are noisy proxies, not copies.** The model sees
     `customer_success_rate`, a small, noisy sample of true reliability - the
     same partial view a real merchant has. It can infer the latent value, but
     never read it off.

  3. **Irreducible noise is deliberate.** Outcomes are Bernoulli draws, so even
     a perfect model cannot exceed the Bayes rate. A model scoring 0.99 AUC
     here would be evidence of a leak, not of quality.

The point is a probability that means something when multiplied by rupees.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from salvage.taxonomy import TAXONOMY, FailureClass

#: Failure reasons weighted toward a realistic Indian online-payments mix.
#:
#: Authentication drop-off and insufficient funds dominate real decline
#: reports; infrastructure downtime is common but bursty; risk blocks and
#: merchant misconfiguration are rare but disproportionately important because
#: they are exactly the cases a blind-retry strategy wastes money on.
REASON_WEIGHTS: dict[str, float] = {
    # Customer-side, high frequency
    "insufficient_funds": 18.0,
    "authentication_failed": 12.0,
    "incorrect_otp": 8.0,
    "otp_expired": 5.0,
    "payment_cancelled": 9.0,
    "payment_timed_out": 6.0,
    "incorrect_cvv": 3.0,
    "incorrect_pin": 2.0,
    "otp_attempts_exceeded": 1.5,
    # Infrastructure
    "bank_not_available": 7.0,
    "bank_technical_error": 5.0,
    "upi_app_technical_error": 3.5,
    "psp_app_not_available": 2.0,
    "bank_cutoff_in_progress": 1.5,
    "server_error": 1.5,
    # Instrument
    "card_expired": 3.5,
    "debit_instrument_blocked": 2.5,
    "card_number_invalid": 1.5,
    "invalid_vpa": 2.0,
    "card_not_enrolled": 1.0,
    "international_transaction_not_allowed": 0.8,
    "bank_account_invalid": 0.7,
    # Limits
    "transaction_limit_exceeded": 2.5,
    "transaction_daily_limit_exceeded": 2.0,
    "transaction_frequency_limit_exceeded": 1.0,
    # Risk - rare, but blind retry burns money on every one of them
    "payment_risk_check_failed": 2.0,
    "compliance_violation": 0.5,
    # Merchant configuration - never customer-fixable
    "payment_method_not_enabled": 1.2,
    "invalid_order_id": 0.8,
    "merchant_not_activated": 0.5,
    "bank_not_enabled": 0.5,
    "upi_collect_not_enabled": 0.4,
    "order_amount_mismatch": 0.4,
    # Already settled
    "order_already_paid": 0.6,
    "duplicate_request": 0.4,
}

#: Razorpay's generic reason. Emitted for a slice of events so the exception
#: path is exercised by real data rather than only by unit tests - a live
#: gateway does send these, and a system that has never seen one is untested.
GENERIC_REASON_RATE = 0.04

#: Payment methods weighted to the Indian market, where UPI dominates.
METHOD_WEIGHTS: dict[str, float] = {
    "upi": 45.0,
    "card": 28.0,
    "netbanking": 15.0,
    "wallet": 8.0,
    "emi": 4.0,
}

FIRST_NAMES = (
    "Aarav", "Diya", "Vihaan", "Ananya", "Arjun", "Ishita", "Rohan", "Sneha",
    "Kabir", "Meera", "Aditya", "Priya", "Karthik", "Divya", "Rahul", "Nisha",
    "Siddharth", "Kavya", "Manish", "Pooja", "Farhan", "Zoya", "Tanvi", "Yash",
)
LAST_NAMES = (
    "Sharma", "Patel", "Reddy", "Iyer", "Nair", "Gupta", "Singh", "Desai",
    "Menon", "Kulkarni", "Banerjee", "Chauhan", "Joshi", "Rao", "Verma", "Khan",
)


@dataclass(slots=True)
class SyntheticEvent:
    """One synthetic `payment.failed` event, shaped like Razorpay's webhook.

    Fields fall into three groups, and the split is what keeps the evaluation
    honest:

      - **Webhook fields** (`id` through `error_step`) mirror Razorpay's real
        payload. These are what a live integration would receive.
      - **Observable context** (`customer_success_rate` onward) is merchant-side
        history a real system would hold. These plus the webhook fields are the
        model's entire input.
      - **Latent truth** (`_true_*`, underscore-prefixed) never reaches the
        model. It exists so the oracle can decide outcomes and so evaluation can
        check calibration against a ground truth the model could not have seen.
    """

    # -- Razorpay webhook payload -------------------------------------------
    id: str
    order_id: str
    amount: int
    currency: str
    method: str
    status: str
    created_at: str
    error_code: str
    error_description: str
    error_reason: str
    error_source: str
    error_step: str

    # -- Merchant-side observable context -----------------------------------
    customer_id: str
    customer_name: str
    customer_phone: str
    customer_email: str
    customer_success_rate: float
    customer_tenure_days: int
    prior_payment_count: int
    prior_failure_count: int
    hours_since_last_success: float
    attempt_number: int
    hour_of_day: int
    day_of_week: int

    # -- Latent ground truth: never a model input ---------------------------
    _true_reliability: float
    _true_intent: float
    _true_issuer_health: float
    _true_base_propensity: float

    def features(self) -> dict[str, float | str | int]:
        """The model's input. Latent fields are structurally excluded."""
        return {
            k: v for k, v in asdict(self).items() if not k.startswith("_")
        }


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _amount_paise(rng: random.Random) -> int:
    """Draw a ticket size from a heavy-tailed, realistic distribution.

    Log-normal, because payment amounts are: a dense mass of small consumer
    purchases with a long tail of large ones. The tail matters enormously here
    - it is where the expected-value optimiser earns its keep, since a handful
    of large tickets dominate total recoverable revenue.
    """
    value = rng.lognormvariate(mu=7.4, sigma=1.15)
    return max(1000, min(int(value) * 100, 20_000_000))


def _true_base_propensity(
    reliability: float,
    intent: float,
    issuer_health: float,
    failure_class: FailureClass,
    amount_paise: int,
    attempt_number: int,
) -> float:
    """The latent probability a customer completes payment given any remedy.

    Deliberately a *smooth interaction* of latent factors rather than a lookup
    keyed on failure class. If this were a per-class constant the model would
    recover it exactly from the reason field and the whole exercise would be
    circular.

    Failure class contributes only a mild tilt - some failure modes really do
    correlate with weaker intent - but reliability and intent dominate, and
    neither is observable.
    """
    base = 0.55 * reliability + 0.35 * intent + 0.10 * issuer_health

    # Large tickets get more deliberation and more abandonment.
    amount_drag = min(0.18, (amount_paise / 1_000_000) * 0.05)
    base -= amount_drag

    # Each prior failed attempt is evidence of a weaker underlying case.
    base *= 0.86**max(0, attempt_number - 1)

    # Mild class tilt. Someone who cancelled deliberately is a harder sell than
    # someone whose bank was simply down.
    tilt = {
        FailureClass.BANK_DOWNTIME: 0.08,
        FailureClass.AUTH_FAILURE: 0.02,
        FailureClass.INSUFFICIENT_FUNDS: -0.04,
        FailureClass.CUSTOMER_ABANDONED: -0.09,
        FailureClass.INSTRUMENT_INVALID: -0.02,
        FailureClass.LIMIT_EXCEEDED: 0.01,
    }.get(failure_class, 0.0)

    return max(0.02, min(0.97, base + tilt))


def generate_events(n: int, seed: int = 20260903) -> list[SyntheticEvent]:
    """Generate a batch of synthetic failed payments.

    Args:
        n: How many events to produce.
        seed: RNG seed. Fixed by default so a reviewer running the repo gets
            byte-identical data and can reproduce every number in the report.

    Returns:
        A list of SyntheticEvent, chronologically ordered.
    """
    if n < 1:
        raise ValueError(f"n must be positive, got {n}")

    rng = random.Random(seed)

    # A pool of customers reused across events, so history means something.
    # Real recovery data is not one-shot: the same people fail repeatedly, and
    # that repetition is most of the signal.
    # Roughly eight events per customer. Real recovery populations are heavily
    # repeat: the same people fail repeatedly, and that history is most of what
    # a merchant can actually learn from. At three events each the observed
    # success rate is almost pure noise and there is nothing to learn.
    n_customers = max(4, n // 8)
    customers: list[dict] = []
    for i in range(n_customers):
        reliability = rng.betavariate(4.2, 2.6)
        customers.append(
            {
                "customer_id": f"cust_{i:05d}",
                "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "phone": f"+91{rng.randint(70000, 99999)}{rng.randint(10000, 99999)}",
                "reliability": reliability,
                "tenure_days": rng.randint(1, 1400),
                "prior_payments": 0,
                "prior_failures": 0,
            }
        )

    # Issuer health drifts over the batch window, producing correlated bursts
    # of downtime failures rather than uniform noise - which is how outages
    # actually arrive, and what makes scheduled retry worth modelling.
    issuer_health = 0.85

    base_time = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    events: list[SyntheticEvent] = []

    for i in range(n):
        cust = customers[rng.randrange(n_customers)]

        issuer_health = max(0.25, min(0.98, issuer_health + rng.gauss(0, 0.06)))

        # Reason selection, with an occasional generic reason so the exception
        # path is exercised by the data itself.
        if rng.random() < GENERIC_REASON_RATE:
            reason = "payment_failed"
            entry = None
            failure_class = FailureClass.UNKNOWN
        else:
            # Downtime becomes likelier while issuers are unhealthy.
            weights = dict(REASON_WEIGHTS)
            downtime_boost = (1.0 - issuer_health) * 22.0
            for r in ("bank_not_available", "bank_technical_error",
                      "upi_app_technical_error", "psp_app_not_available"):
                weights[r] += downtime_boost
            reason = _weighted_choice(rng, weights)
            entry = TAXONOMY[reason]
            failure_class = entry.failure_class

        method = _weighted_choice(rng, METHOD_WEIGHTS)
        amount = _amount_paise(rng)
        timestamp = base_time + timedelta(minutes=rng.randint(0, 60 * 24 * 3))

        attempt_number = 1
        if cust["prior_failures"] > 0 and rng.random() < 0.35:
            attempt_number = rng.randint(2, 3)

        intent = max(0.03, min(0.97, rng.betavariate(3.0, 2.4)))

        true_prop = _true_base_propensity(
            reliability=cust["reliability"],
            intent=intent,
            issuer_health=issuer_health,
            failure_class=failure_class,
            amount_paise=amount,
            attempt_number=attempt_number,
        )

        # The merchant's observable view of reliability: a small, noisy sample.
        # This is the crux of the anti-circularity design. The model gets a
        # blurred estimate of the latent driver, exactly as a real system does.
        observed_n = max(1, min(cust["prior_payments"], 40))
        noise = rng.gauss(0, 0.11 / (observed_n**0.5))
        success_rate = max(0.0, min(1.0, cust["reliability"] + noise))

        error_code = (
            "GATEWAY_ERROR"
            if entry is not None and entry.source.value == "gateway"
            else "SERVER_ERROR"
            if entry is not None and entry.source.value == "razorpay"
            else "BAD_REQUEST_ERROR"
        )

        events.append(
            SyntheticEvent(
                id=f"pay_{hashlib.md5(f'{seed}:{i}'.encode()).hexdigest()[:14]}",
                order_id=f"order_{hashlib.md5(f'{seed}:o:{i}'.encode()).hexdigest()[:14]}",
                amount=amount,
                currency="INR",
                method=method,
                status="failed",
                created_at=timestamp.isoformat(),
                error_code=error_code,
                error_description=entry.guidance if entry else "Payment failed",
                error_reason=reason,
                error_source=entry.source.value if entry else "customer",
                error_step=entry.step.value if entry else "payment_authorization",
                customer_id=cust["customer_id"],
                customer_name=cust["name"],
                customer_phone=cust["phone"],
                customer_email=(
                    f"{cust['name'].split()[0].lower()}"
                    f"{cust['customer_id'][-4:]}@example.com"
                ),
                customer_success_rate=round(success_rate, 4),
                customer_tenure_days=cust["tenure_days"],
                prior_payment_count=cust["prior_payments"],
                prior_failure_count=cust["prior_failures"],
                hours_since_last_success=round(rng.expovariate(1 / 72.0), 2),
                attempt_number=attempt_number,
                hour_of_day=timestamp.hour,
                day_of_week=timestamp.weekday(),
                _true_reliability=round(cust["reliability"], 4),
                _true_intent=round(intent, 4),
                _true_issuer_health=round(issuer_health, 4),
                _true_base_propensity=round(true_prop, 4),
            )
        )

        cust["prior_failures"] += 1
        if rng.random() < cust["reliability"]:
            cust["prior_payments"] += 1

    events.sort(key=lambda e: e.created_at)
    return events
