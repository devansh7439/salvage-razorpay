"""LLM-assisted diagnosis for failures the taxonomy cannot resolve.

Everywhere else in this system, deterministic code wins. 95.4% of failures match
a documented Razorpay `error_reason` exactly, and putting a language model in
front of a lookup table would add hallucination risk to a solved problem.

The residual is a different case. Razorpay's generic `payment_failed`, an
unmapped reason, a missing field - roughly 5% of a batch, and worth over
Rs 1,00,000 on the demo run - arrive with no recovery signal at all. The rules
have nothing to work with, so those payments are reported as exceptions and no
intervention is attempted. That is honest, and it is also money left on the
floor.

This module is the one place where language reasoning genuinely beats a rule:
`error_description` is prose written by a bank or a PSP, and reading prose is
what the model is for. It runs *only* on payments the taxonomy already gave up
on, so it can add coverage and cannot subtract correctness.

The authority boundary is unchanged and enforced structurally:

    the model     proposes a failure class, with a confidence
    this module   validates the proposal against an allowlist
    the policy    decides whether any money is spent

An AI-derived diagnosis is deliberately worth *less* than a documented one. It
cannot name a risk block, it cannot unlock the classes the rules reserve, and
it carries a reduced autonomy ceiling, so a wrong guess costs a nudge rather
than a bad payout. The model widens coverage; it never widens authority.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from salvage.config import settings
from salvage.taxonomy import Classification, FailureClass

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0

#: Matches the generator's budget for the same reason: a reasoning model spends
#: this allowance on its chain of thought before emitting any JSON, and returns
#: an empty string if it runs out first. 220 was enough for a plain model and
#: silently produced zero diagnoses on a reasoning one.
MAX_TOKENS = 600

#: Classes the model may propose.
#:
#: Deliberately a subset. RISK_BLOCKED, ALREADY_PAID and MERCHANT_CONFIG are
#: absent because each one is a *control*, not a diagnosis: they stop recovery,
#: route to humans, or forbid contact. Those determinations are made from
#: documented Razorpay reasons, and a model that could assert them - or assert
#: something else *instead* of them - would be able to move a payment out of a
#: guardrail by describing it differently.
#:
#: The model can only propose classes that lead to ordinary customer recovery.
#: The worst outcome of a confident wrong answer is a payment link nobody uses.
PROPOSABLE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.BANK_DOWNTIME,
        FailureClass.INSUFFICIENT_FUNDS,
        FailureClass.INSTRUMENT_INVALID,
        FailureClass.AUTH_FAILURE,
        FailureClass.CUSTOMER_ABANDONED,
        FailureClass.LIMIT_EXCEEDED,
    }
)

#: Below this, the proposal is discarded and the payment stays an exception.
#:
#: Set high on purpose. The alternative to acting on a weak diagnosis is
#: reporting an honest exception, which costs nothing; acting on one spends
#: money and contacts a customer. The asymmetry says be strict.
MIN_CONFIDENCE = 0.70

#: Autonomy ceiling for AI-derived diagnoses, as a fraction of the merchant's
#: normal limit. A guess earns less rope than a documented match.
AI_AUTONOMY_FRACTION = 0.25

#: Consecutive failures after which the model is left alone for this process.
#:
#: This stage runs once per *undiagnosable* payment, serially, and blocks the
#: batch for up to REQUEST_TIMEOUT on each one. A provider that is rate-limiting
#: or hanging therefore turns a thousand-payment run into minutes of dead time -
#: in front of whoever is watching - to buy coverage the system is explicitly
#: willing to do without.
#:
#: Skipping it is safe by construction: a payment the model does not classify is
#: reported as an exception, which is exactly what happened before this module
#: existed. So the right response to a provider that keeps failing is to stop
#: asking it.
BREAKER_THRESHOLD = 5

_breaker_lock = threading.Lock()
_consecutive_failures = 0


def _breaker_open() -> bool:
    """Whether the provider has failed often enough to stop calling it."""
    with _breaker_lock:
        return _consecutive_failures >= BREAKER_THRESHOLD


def _record_call(ok: bool) -> None:
    global _consecutive_failures
    with _breaker_lock:
        if ok:
            _consecutive_failures = 0
            return
        _consecutive_failures += 1
        if _consecutive_failures == BREAKER_THRESHOLD:
            logger.warning(
                "Diagnosis model failed %d times in a row; skipping it for the "
                "rest of this process. Undiagnosable payments are reported as "
                "exceptions, which is the behaviour without a model at all.",
                BREAKER_THRESHOLD,
            )


def reset_breaker() -> None:
    """Close the breaker again. For tests, and for a deliberate retry."""
    global _consecutive_failures
    with _breaker_lock:
        _consecutive_failures = 0

SYSTEM_PROMPT = """You classify failed Indian card and UPI payments for a recovery system.

You are given a payment whose failure reason could NOT be matched to Razorpay's documented error taxonomy. Your job is to read the free-text description and decide which recovery-relevant category it belongs to.

Categories:
- BANK_DOWNTIME: bank, gateway or PSP was unavailable or erroring. Nothing wrong with the customer or their instrument.
- INSUFFICIENT_FUNDS: the account did not have the balance.
- INSTRUMENT_INVALID: the card, VPA or account cannot be used - expired, blocked, invalid, not enrolled.
- AUTH_FAILURE: OTP, PIN, CVV or 3D-Secure verification did not complete.
- CUSTOMER_ABANDONED: the customer cancelled, closed the page, or the session timed out.
- LIMIT_EXCEEDED: a per-transaction, daily or frequency limit was hit.

Rules:
- Reply with JSON only: {"failure_class": "...", "confidence": 0.0, "reasoning": "..."}
- confidence is your genuine probability that the category is correct, 0.0 to 1.0.
- If the text is generic, empty, or could plausibly be several categories, return low confidence. A low-confidence answer is useful; a confident wrong one is not.
- Never invent a category outside the six listed.
- reasoning: one short sentence, citing the words you relied on."""


@dataclass(frozen=True, slots=True)
class AIDiagnosis:
    """A model proposal, after validation.

    Attributes:
        failure_class: The proposed class, guaranteed to be in PROPOSABLE.
        confidence: The model's stated confidence, clamped to [0, 1].
        reasoning: One-sentence justification, shown in the audit trail.
        accepted: Whether the proposal cleared validation and the threshold.
        rejected_because: Why it was discarded, when it was.
        provider: Which model answered.
    """

    failure_class: FailureClass | None
    confidence: float
    reasoning: str
    accepted: bool
    rejected_because: str | None = None
    provider: str = "none"


def _parse(raw: str) -> tuple[dict | None, str | None]:
    """Extract the JSON object from a model reply.

    Models wrap JSON in prose and fences no matter how firmly the prompt says
    not to, so the first balanced object is extracted rather than trusting the
    whole reply to parse. Anything that still fails is a rejection, not a
    guess - there is no partial credit for malformed output that will be used
    to spend money.
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    # `null`, `[]` and bare scalars are all *valid* JSON, so parsing success is
    # not the same as getting an object back. Without this check a reply of
    # `[]` reached the validator and crashed it on `.get`, taking the pipeline
    # down over a model's formatting whim.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, None
        return None, f"reply was {type(parsed).__name__}, not an object"
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None, "no JSON object in reply"

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return None, f"malformed JSON: {exc.msg}"

    if not isinstance(parsed, dict):
        return None, f"reply was {type(parsed).__name__}, not an object"
    return parsed, None


def _validate(payload: dict) -> AIDiagnosis:
    """Turn a parsed reply into a proposal, or reject it.

    Every field is treated as untrusted. The model is a text generator being
    asked about money; nothing it returns is taken on trust, including the
    shape of its own output.
    """
    raw_class = payload.get("failure_class")
    if not isinstance(raw_class, str):
        return AIDiagnosis(None, 0.0, "", False, "no failure_class in reply")

    try:
        proposed = FailureClass(raw_class.strip().upper())
    except ValueError:
        return AIDiagnosis(
            None, 0.0, "", False, f"unknown class {raw_class!r}"
        )

    if proposed not in PROPOSABLE:
        # The model tried to name a control rather than a diagnosis.
        return AIDiagnosis(
            None,
            0.0,
            "",
            False,
            f"{proposed.value} is not proposable by a model",
        )

    raw_confidence = payload.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError):
        return AIDiagnosis(
            proposed, 0.0, "", False, "confidence missing or not a number"
        )

    reasoning = str(payload.get("reasoning", "")).strip()[:300]

    if confidence < MIN_CONFIDENCE:
        return AIDiagnosis(
            proposed,
            confidence,
            reasoning,
            False,
            f"confidence {confidence:.2f} below {MIN_CONFIDENCE:.2f} threshold",
        )

    return AIDiagnosis(proposed, confidence, reasoning, True)


def _call(prompt: str) -> str | None:
    """One chat-completions request. None on any failure."""
    body: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        # Low temperature: this is a classification, not composition.
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    if settings.llm_reasoning_effort:
        body["reasoning_effort"] = settings.llm_reasoning_effort

    try:
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        choice = response.json()["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if not content:
            # Returning "" here would reach the JSON parser as unparseable and
            # be logged as a malformed model reply, sending a reader after a
            # prompt bug that does not exist. The real cause is budget.
            logger.warning(
                "Diagnosis model returned empty content (finish_reason=%s); "
                "treating as unavailable. Raise MAX_TOKENS or set "
                "LLM_REASONING_EFFORT=low for reasoning models.",
                choice.get("finish_reason"),
            )
            _record_call(False)
            return None
        _record_call(True)
        return content
    except Exception as exc:
        logger.warning("Diagnosis model unavailable: %s", exc)
        _record_call(False)
        return None


def diagnose(
    error_reason: str | None,
    error_description: str | None,
    error_code: str | None,
    error_source: str | None,
    error_step: str | None,
    method: str | None = None,
    allow_live: bool = True,
) -> AIDiagnosis:
    """Ask the model to classify a failure the taxonomy could not.

    Returns an unaccepted proposal when the model is unconfigured, unreachable,
    or returns something unusable. The caller treats that exactly as it treated
    an undiagnosable failure before this module existed, so adding the model
    can only widen coverage.
    """
    if not settings.llm_live:
        return AIDiagnosis(None, 0.0, "", False, "no LLM configured", "none")

    # Simulated events are refused a live diagnosis, and this one is not about
    # speed. A diagnosis changes which action the policy engine picks, so it
    # changes the outcome an evaluation measures. Under a provider rate limit
    # some events in a batch would receive a proposal and others would not,
    # decided by nothing more than when the limit happened to trip - which
    # makes a paired comparison irreproducible and its arms unequal.
    #
    # It also means adding credentials silently changed what the evaluation
    # measured: rules-only before, rules-plus-intermittent-AI after, from the
    # same code and the same seed. Holding simulated runs to the deterministic
    # path keeps the number comparable across machines and across the day.
    if not allow_live:
        return AIDiagnosis(
            None, 0.0, "", False, "simulated event: live diagnosis skipped", "none"
        )

    if _breaker_open():
        return AIDiagnosis(
            None, 0.0, "", False, "diagnosis model circuit breaker open", "none"
        )

    prompt = "\n".join(
        [
            f"error_reason: {error_reason or '(none)'}",
            f"error_description: {error_description or '(none)'}",
            f"error_code: {error_code or '(none)'}",
            f"error_source: {error_source or '(none)'}",
            f"error_step: {error_step or '(none)'}",
            f"payment_method: {method or '(unknown)'}",
        ]
    )

    raw = _call(prompt)
    if raw is None:
        return AIDiagnosis(None, 0.0, "", False, "model unavailable", "none")

    payload, error = _parse(raw)
    if payload is None:
        return AIDiagnosis(
            None, 0.0, "", False, error or "unparseable reply",
            f"llm:{settings.llm_model}",
        )

    result = _validate(payload)
    return AIDiagnosis(
        result.failure_class,
        result.confidence,
        result.reasoning,
        result.accepted,
        result.rejected_because,
        f"llm:{settings.llm_model}",
    )


def apply(classification: Classification, diagnosis: AIDiagnosis) -> Classification:
    """Fold an accepted proposal into a classification.

    The result is marked confident so the policy engine will act on it, but the
    note records that the class came from a model rather than a documented
    reason - which is what the audit trail and the dashboard display, and what
    the reduced autonomy ceiling keys on.
    """
    if not diagnosis.accepted or diagnosis.failure_class is None:
        return classification

    return Classification(
        failure_class=diagnosis.failure_class,
        entry=None,
        confident=True,
        note=(
            f"AI-assisted: no documented Razorpay reason matched, so the failure "
            f"description was classified by a model as "
            f"{diagnosis.failure_class.value} at {diagnosis.confidence:.0%} "
            f"confidence. {diagnosis.reasoning} "
            "Reduced autonomy applies to model-derived diagnoses."
        ),
    )


def autonomy_ceiling(base_ceiling_paise: int) -> int:
    """The autonomy limit for an AI-derived diagnosis.

    A documented match is worth more than a guess, so a guess gets a quarter of
    the rope. Above this, the payment escalates to a human rather than being
    acted on - which is the correct response to "the model thinks it knows what
    went wrong" at a size where being wrong is expensive.
    """
    return int(base_ceiling_paise * AI_AUTONOMY_FRACTION)
