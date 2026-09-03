"""Razorpay failure taxonomy.

Every entry in TAXONOMY is transcribed from Razorpay's published error
documentation, not invented:

  - Error object shape (code/description/source/step/reason/metadata):
    https://razorpay.com/docs/errors/
  - Per-reason list and merchant guidance:
    https://razorpay.com/docs/errors/payments/list/

The `guidance` field on each entry quotes or closely paraphrases Razorpay's
own recommended next step. The policy engine derives its actions from these
entries, which means any decision Salvage makes can be checked against the
vendor's public documentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorSource(str, Enum):
    """`error.source` - who owns the point of failure.

    Razorpay documents a prescribed response for each source. These four
    strings drive the coarse routing in the policy engine.
    """

    CUSTOMER = "customer"
    BUSINESS = "business"
    GATEWAY = "gateway"
    RAZORPAY = "razorpay"


#: Razorpay's own documented next action per source, quoted from
#: https://razorpay.com/docs/errors/payments/list/
SOURCE_GUIDANCE: dict[ErrorSource, str] = {
    ErrorSource.CUSTOMER: "Display a meaningful message and prompt them to retry.",
    ErrorSource.BUSINESS: "Fix the request parameters before retrying.",
    ErrorSource.GATEWAY: "Retry or ask the customer to use a different payment method.",
    ErrorSource.RAZORPAY: "Retry after a short delay. Contact support if it persists.",
}


class ErrorStep(str, Enum):
    """`error.step` - the stage of the transaction that failed."""

    PAYMENT_INITIATION = "payment_initiation"
    PAYMENT_AUTHENTICATION = "payment_authentication"
    PAYMENT_AUTHORIZATION = "payment_authorization"


class FailureClass(str, Enum):
    """Recovery-relevant grouping of raw Razorpay reasons.

    Razorpay's `error_code` is deliberately coarse - BAD_REQUEST_ERROR covers
    everything from an expired card to a cancelled checkout - so it is useless
    as a recovery signal on its own. These classes are the level at which
    recovery economics actually differ.
    """

    BANK_DOWNTIME = "BANK_DOWNTIME"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INSTRUMENT_INVALID = "INSTRUMENT_INVALID"
    AUTH_FAILURE = "AUTH_FAILURE"
    CUSTOMER_ABANDONED = "CUSTOMER_ABANDONED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    RISK_BLOCKED = "RISK_BLOCKED"
    MERCHANT_CONFIG = "MERCHANT_CONFIG"
    ALREADY_PAID = "ALREADY_PAID"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TaxonomyEntry:
    """One documented Razorpay failure reason and what it implies.

    Attributes:
        reason: The literal `error.reason` string Razorpay emits.
        source: Which actor owns the failure.
        step: Transaction stage at which it surfaced.
        failure_class: Recovery-relevant grouping.
        guidance: Razorpay's documented next step, shown in the audit trail.
        customer_actionable: Whether the *customer* can do anything about it.
            False for merchant-config and risk failures - messaging a customer
            about `merchant_not_activated` is nonsense and damages trust.
        auto_retryable: Whether re-presenting the same instrument unchanged
            could plausibly succeed. False when the instrument itself is bad.
        typical_resolution_hours: Rough time for the blocking condition to
            clear on its own. Drives retry backoff scheduling. None means
            waiting does not help.
    """

    reason: str
    source: ErrorSource
    step: ErrorStep
    failure_class: FailureClass
    guidance: str
    customer_actionable: bool
    auto_retryable: bool
    typical_resolution_hours: float | None


def _entry(
    reason: str,
    source: ErrorSource,
    step: ErrorStep,
    failure_class: FailureClass,
    guidance: str,
    *,
    customer_actionable: bool,
    auto_retryable: bool,
    typical_resolution_hours: float | None = None,
) -> TaxonomyEntry:
    return TaxonomyEntry(
        reason=reason,
        source=source,
        step=step,
        failure_class=failure_class,
        guidance=guidance,
        customer_actionable=customer_actionable,
        auto_retryable=auto_retryable,
        typical_resolution_hours=typical_resolution_hours,
    )


_ENTRIES: tuple[TaxonomyEntry, ...] = (
    # -- Authentication and verification -------------------------------------
    # Customer is present and mistyped something. The instrument is fine.
    _entry(
        "authentication_failed",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "3D Secure or OTP authentication failed. Prompt the customer to retry.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=0.0,
    ),
    _entry(
        "incorrect_otp",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "Wrong one-time password submitted. Request a new OTP.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=0.0,
    ),
    _entry(
        "otp_expired",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "OTP validity window elapsed. Request a fresh OTP.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=0.0,
    ),
    _entry(
        "otp_attempts_exceeded",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "Too many failed OTP tries. The customer must wait before retrying.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=24.0,
    ),
    _entry(
        "incorrect_cvv",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "Invalid card security code. Ask for the correct CVV.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=0.0,
    ),
    _entry(
        "incorrect_pin",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "Wrong PIN provided. Ask the customer to enter the correct PIN.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=0.0,
    ),
    _entry(
        "pin_attempts_exceeded",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.AUTH_FAILURE,
        "PIN attempt limit reached. The customer must wait to retry.",
        customer_actionable=True,
        auto_retryable=False,
        typical_resolution_hours=24.0,
    ),
    # -- Card and instrument -------------------------------------------------
    # The instrument itself is unusable. Retrying it is guaranteed waste; the
    # only path forward is a different instrument, i.e. a payment link.
    _entry(
        "card_expired",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.INSTRUMENT_INVALID,
        "The card is past its expiry date. Ask for a valid card.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "card_number_invalid",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.INSTRUMENT_INVALID,
        "Incorrect card number. Ask the customer to enter a valid number.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "card_not_enrolled",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.INSTRUMENT_INVALID,
        "Card is not enrolled for this method. Use a different card.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "debit_instrument_blocked",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.INSTRUMENT_INVALID,
        "Card blocked by the issuer or customer. Try an alternate card.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "invalid_vpa",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.INSTRUMENT_INVALID,
        "Incorrect UPI address format. Ask for the correct VPA.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "bank_account_invalid",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.INSTRUMENT_INVALID,
        "Closed or invalid bank account. Use a valid bank account.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "user_not_registered_for_netbanking",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.INSTRUMENT_INVALID,
        "Account lacks online access. Register with the issuing bank.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "international_transaction_not_allowed",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.INSTRUMENT_INVALID,
        "Cross-border transactions blocked. Use a domestic method.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "upi_autopay_not_supported_on_psp",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.INSTRUMENT_INVALID,
        "Recurring not available on this UPI app. Use a different PSP.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    # -- Funds ---------------------------------------------------------------
    # Balance is a moving target, so waiting genuinely helps here. This is why
    # the class earns a delayed retry rather than an immediate link.
    _entry(
        "insufficient_funds",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.INSUFFICIENT_FUNDS,
        "Account lacks the required balance. Use a different method.",
        customer_actionable=True,
        auto_retryable=True,
        typical_resolution_hours=48.0,
    ),
    # -- Limits --------------------------------------------------------------
    # Limits reset on a clock. Time is the fix, not a new instrument.
    _entry(
        "transaction_limit_exceeded",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.LIMIT_EXCEEDED,
        "Per-transaction ceiling exceeded. Try a different card.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "transaction_daily_limit_exceeded",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.LIMIT_EXCEEDED,
        "Daily cap reached. Use an alternate instrument or wait 24 hours.",
        customer_actionable=True,
        auto_retryable=True,
        typical_resolution_hours=24.0,
    ),
    _entry(
        "transaction_frequency_limit_exceeded",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.LIMIT_EXCEEDED,
        "NPCI frequency limit exhausted. Switch payment method.",
        customer_actionable=True,
        auto_retryable=True,
        typical_resolution_hours=24.0,
    ),
    _entry(
        "emi_greater_than_max_amount",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.LIMIT_EXCEEDED,
        "EMI exceeds the allowed maximum. Reduce EMI or change method.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    # -- Bank and gateway downtime -------------------------------------------
    # Nothing is wrong with the customer or the instrument. These are the
    # highest-value auto-retry candidates in the whole taxonomy.
    _entry(
        "bank_technical_error",
        ErrorSource.GATEWAY,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.BANK_DOWNTIME,
        "Core Banking System technical error. Try an alternate bank or method.",
        customer_actionable=False,
        auto_retryable=True,
        typical_resolution_hours=2.0,
    ),
    _entry(
        "bank_not_available",
        ErrorSource.GATEWAY,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.BANK_DOWNTIME,
        "Bank is experiencing downtime. Retry later.",
        customer_actionable=False,
        auto_retryable=True,
        typical_resolution_hours=4.0,
    ),
    _entry(
        "bank_cutoff_in_progress",
        ErrorSource.GATEWAY,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.BANK_DOWNTIME,
        "Scheduled bank maintenance. Retry after the cutoff window ends.",
        customer_actionable=False,
        auto_retryable=True,
        typical_resolution_hours=6.0,
    ),
    _entry(
        "upi_app_technical_error",
        ErrorSource.GATEWAY,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.BANK_DOWNTIME,
        "PSP application malfunction. Retry or switch UPI app.",
        customer_actionable=False,
        auto_retryable=True,
        typical_resolution_hours=1.0,
    ),
    _entry(
        "psp_app_not_available",
        ErrorSource.GATEWAY,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.BANK_DOWNTIME,
        "Payment Service Provider downtime. Try an alternate PSP app.",
        customer_actionable=False,
        auto_retryable=True,
        typical_resolution_hours=2.0,
    ),
    _entry(
        "server_error",
        ErrorSource.RAZORPAY,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.BANK_DOWNTIME,
        "Technical error at Razorpay's server. Retry or contact support.",
        customer_actionable=False,
        auto_retryable=True,
        typical_resolution_hours=0.5,
    ),
    # -- Customer abandonment ------------------------------------------------
    # Intent existed and may still exist. The customer walked away rather than
    # being refused, so a nudge is the natural intervention.
    _entry(
        "payment_cancelled",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.CUSTOMER_ABANDONED,
        "Customer abandoned the transaction. Prompt them to retry.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    _entry(
        "payment_timed_out",
        ErrorSource.CUSTOMER,
        ErrorStep.PAYMENT_AUTHENTICATION,
        FailureClass.CUSTOMER_ABANDONED,
        "Session expired during completion. Initiate a new payment.",
        customer_actionable=True,
        auto_retryable=False,
    ),
    # -- Merchant configuration ----------------------------------------------
    # source=business. Razorpay's guidance is "fix the request parameters",
    # aimed at the MERCHANT, not the customer. Messaging a customer about these
    # is both useless and reputationally damaging, so they route to a human ops
    # queue and never to a nudge.
    _entry(
        "invalid_order_id",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Order ID is missing or invalid. Pass a valid order ID.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "order_amount_mismatch",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Order and payment amounts differ. Ensure the amounts match.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "order_payment_method_mismatch",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Method discrepancy between order and payment requests.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "input_validation_failed",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Invalid parameters submitted. Rectify the validation issues.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "payment_method_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Method inactive for this merchant. Request activation from Razorpay.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "merchant_not_activated",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Merchant account is not active. Contact Razorpay for activation.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "live_mode_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Test keys used in production. Switch to live API keys.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "bank_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Bank not configured for this merchant. Contact Razorpay to enable.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "card_network_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Card network not active for this merchant. Contact Razorpay.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "upi_intent_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "UPI intent flow deactivated for this merchant. Enable intent flow.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "upi_collect_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "UPI collect flow deactivated for this merchant. Enable collect flow.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "recurring_payment_not_enabled",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Subscriptions inactive. Enable recurring payments for the account.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "amount_less_than_minimum_amount",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Amount is below the bank's minimum. Increase the payment amount.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "invalid_amount",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Malformed amount value. Provide a valid amount.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "invalid_currency",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.MERCHANT_CONFIG,
        "Unsupported or malformed currency. Use a supported currency code.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    # -- Risk and compliance -------------------------------------------------
    # Hard stop. Never retried, never messaged, never overridden by a score.
    _entry(
        "compliance_violation",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.RISK_BLOCKED,
        "Regulatory requirements unmet. Verify compliance standards.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "payment_risk_check_failed",
        ErrorSource.RAZORPAY,
        ErrorStep.PAYMENT_AUTHORIZATION,
        FailureClass.RISK_BLOCKED,
        "Blocked by the risk engine. Do not retry; route to manual review.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    # -- Already settled -----------------------------------------------------
    # Not a recovery target at all. Acting on these double-charges customers.
    _entry(
        "order_already_paid",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.ALREADY_PAID,
        "This order is already paid. Check status; do not re-collect.",
        customer_actionable=False,
        auto_retryable=False,
    ),
    _entry(
        "duplicate_request",
        ErrorSource.BUSINESS,
        ErrorStep.PAYMENT_INITIATION,
        FailureClass.ALREADY_PAID,
        "Repeated identical submission. Avoid submitting duplicates.",
        customer_actionable=False,
        auto_retryable=False,
    ),
)

#: Lookup keyed on the literal `error.reason` Razorpay sends.
TAXONOMY: dict[str, TaxonomyEntry] = {e.reason: e for e in _ENTRIES}

#: Razorpay's generic catch-all reasons. They carry no recovery signal at all,
#: so they are deliberately NOT mapped to a failure class - they fall through
#: to UNKNOWN and land on the exception list rather than being guessed at.
GENERIC_REASONS: frozenset[str] = frozenset({"payment_failed", "", "null", "none"})


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of diagnosing one failed payment.

    Attributes:
        failure_class: The recovery-relevant grouping.
        entry: The matched taxonomy entry, or None when unrecognised.
        confident: False when the reason was generic, missing, or absent from
            the taxonomy. Unconfident classifications are routed to the
            exception list instead of being acted on.
        note: Human-readable explanation, surfaced in the audit trail.
    """

    failure_class: FailureClass
    entry: TaxonomyEntry | None
    confident: bool
    note: str

    @property
    def source(self) -> ErrorSource | None:
        return self.entry.source if self.entry else None

    @property
    def customer_actionable(self) -> bool:
        return bool(self.entry and self.entry.customer_actionable)

    @property
    def auto_retryable(self) -> bool:
        return bool(self.entry and self.entry.auto_retryable)


def classify(
    error_reason: str | None,
    error_code: str | None = None,
    error_source: str | None = None,
    error_step: str | None = None,
) -> Classification:
    """Diagnose a failed payment from its Razorpay error fields.

    Resolution order is deliberate: `reason` is the only field Razorpay
    documents as "programmatically handleable", so it is tried first. When it
    is missing or generic we fall back to `source`, which still carries a
    documented next action. `code` is never used as a primary signal because
    BAD_REQUEST_ERROR spans half the taxonomy.

    Anything that survives both attempts is returned unconfident, so it lands
    on the exception list rather than triggering a guessed intervention.
    """
    normalised = (error_reason or "").strip().lower()

    if normalised and normalised not in GENERIC_REASONS:
        entry = TAXONOMY.get(normalised)
        if entry is not None:
            return Classification(
                failure_class=entry.failure_class,
                entry=entry,
                confident=True,
                note=f"Matched documented Razorpay reason '{normalised}'.",
            )

    # `reason` was absent, generic, or undocumented. Fall back to `source`,
    # which Razorpay pairs with its own prescribed next action.
    try:
        source = ErrorSource(str(error_source or "").strip().lower())
    except ValueError:
        return Classification(
            failure_class=FailureClass.UNKNOWN,
            entry=None,
            confident=False,
            note=(
                f"Unrecognised reason '{error_reason}' with no usable source "
                f"(code={error_code!r}, step={error_step!r}). Routed to exceptions."
            ),
        )

    return Classification(
        failure_class=FailureClass.UNKNOWN,
        entry=None,
        confident=False,
        note=(
            f"Reason '{error_reason}' is generic or undocumented. "
            f"Source '{source.value}' implies: {SOURCE_GUIDANCE[source]} "
            "Insufficient signal to choose an intervention; routed to exceptions."
        ),
    )


def reasons_for_class(failure_class: FailureClass) -> tuple[str, ...]:
    """All documented reasons that map to a given failure class."""
    return tuple(e.reason for e in _ENTRIES if e.failure_class is failure_class)
