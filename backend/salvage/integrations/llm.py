"""Recovery message generation.

The LLM's entire remit in this system is phrasing. It receives a decision that
has already been made, priced, and approved by the policy engine, and turns it
into a sentence a customer will read. It never chooses an action, never sees an
expected value it could argue with, and never has a tool that moves money.

That boundary is the reason a generative model is safe to use here at all. The
worst possible outcome of a bad generation is an awkwardly worded SMS. The
worst outcome of an LLM with authority over retries is a customer charged four
times.

Transport is the OpenAI-compatible chat-completions contract, which Groq,
OpenRouter, Together, Fireworks, DeepInfra, Gemini's compatibility endpoint and
local Ollama all speak. Point `LLM_BASE_URL` and `LLM_API_KEY` at any of them.
With no credentials, a template renderer produces the same messages
deterministically, so the pipeline is never blocked on a provider being up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from salvage.config import settings
from salvage.economics import RecoveryAction

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0

SYSTEM_PROMPT = """You write short payment-recovery messages for Indian customers on WhatsApp.

Rules:
- Casual Hinglish. Natural Roman-script Hindi mixed with English, the way people actually message. Not formal Hindi, not pure English.
- Exactly two sentences.
- Warm and helpful, never accusatory. The failure was not their fault.
- Name the reason in plain language. Never quote error codes, technical terms, or internal jargon.
- If a link is provided, include it verbatim, exactly once.
- No emojis. No greetings like "Dear Customer". No signature.
- Never invent discounts, deadlines, offers, or consequences.

Return only the message text."""

#: Plain-language failure descriptions. Deliberately not the raw Razorpay
#: guidance, which is written for merchants and would confuse a customer.
CUSTOMER_FACING_REASON: dict[str, str] = {
    "BANK_DOWNTIME": "their bank was temporarily unavailable",
    "INSUFFICIENT_FUNDS": "the payment could not go through from their account",
    "INSTRUMENT_INVALID": "their card could not be used for this payment",
    "AUTH_FAILURE": "the OTP or verification step did not complete",
    "CUSTOMER_ABANDONED": "the payment was not completed",
    "LIMIT_EXCEEDED": "their bank's transaction limit was reached",
}


#: Phrases that constitute a dark pattern in a payment-recovery message.
#:
#: Razorpay's published position on agents in payments states that agents
#: "must not employ dark patterns" including false urgency and manufactured
#: pressure, and that they must not "set prices or invent discounts".
#:
#: The system prompt already forbids all of this, but a prompt is a request,
#: not a guarantee - a model under temperature will occasionally add "hurry,
#: offer ends today" because that is what recovery copy looks like in its
#: training data. So generated text is checked rather than trusted, and copy
#: that trips any of these is discarded in favour of the template.
#:
#: Grouped by the principle each one violates, so a reviewer can see the rule
#: being enforced rather than an opaque blocklist.
DARK_PATTERN_MARKERS: dict[str, tuple[str, ...]] = {
    "false_urgency": (
        "hurry", "act now", "last chance", "expires today", "expiring today",
        "only today", "limited time", "final notice", "immediately or",
        "before it's too late", "jaldi karo", "aaj hi", "turant",
    ),
    "manufactured_pressure": (
        "account will be", "will be suspended", "will be blocked",
        "will be cancelled", "penalty", "legal action", "band ho jayega",
        "block ho jayega",
    ),
    "invented_offers": (
        "discount", "% off", "cashback", "free delivery", "coupon",
        "special price", "offer", "chhoot", "muft",
    ),
}


def find_dark_patterns(text: str) -> list[str]:
    """Return the principles a message violates, empty when it is clean."""
    lowered = text.lower()
    return [
        principle
        for principle, markers in DARK_PATTERN_MARKERS.items()
        if any(marker in lowered for marker in markers)
    ]


@dataclass(frozen=True, slots=True)
class Message:
    """A generated recovery message.

    Attributes:
        text: The copy to send.
        provider: Which backend produced it, so live and fallback output are
            distinguishable in the audit trail rather than looking identical.
        channel: Delivery channel.
        blocked_for: Principles a rejected generation violated. Populated when
            model output was discarded, so the refusal is visible rather than
            silent.
    """

    text: str
    provider: str
    channel: str = "whatsapp"
    blocked_for: tuple[str, ...] = ()


def _template(
    customer_name: str,
    amount_paise: int,
    failure_class: str,
    payment_link: str | None,
) -> str:
    """Deterministic fallback copy.

    Written to be genuinely usable rather than a placeholder, because in
    fixture mode this is what the demo displays. Same two-sentence shape and
    same Hinglish register the prompt asks for.
    """
    first = (customer_name or "there").split()[0]
    rupees = f"{amount_paise / 100:,.0f}"

    reason = {
        "BANK_DOWNTIME": "aapke bank ki taraf se thodi dikkat thi",
        "INSUFFICIENT_FUNDS": "payment complete nahi ho paya",
        "INSTRUMENT_INVALID": "aapka card is payment ke liye kaam nahi kar raha",
        "AUTH_FAILURE": "OTP verify nahi ho paya",
        "CUSTOMER_ABANDONED": "aapka payment adhura reh gaya",
        "LIMIT_EXCEEDED": "aapke bank ki limit aa gayi thi",
    }.get(failure_class, "payment complete nahi ho paya")

    if payment_link:
        return (
            f"Hi {first}, aapka Rs {rupees} ka payment nahi ho paya kyunki {reason}. "
            f"Koi baat nahi, yahan se dobara try kar lijiye: {payment_link}"
        )
    return (
        f"Hi {first}, aapka Rs {rupees} ka payment nahi ho paya kyunki {reason}. "
        "Thodi der baad dobara try kijiye, ho jayega."
    )


def _call_llm(prompt: str) -> str | None:
    """One chat-completions request. Returns None on any failure.

    Failures are swallowed rather than raised because message generation is the
    least critical step in the pipeline. A provider outage must degrade the
    wording of an SMS, not stop recovery for a thousand payments.
    """
    try:
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 160,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("LLM generation failed, falling back to template: %s", exc)
        return None


def generate_message(
    customer_name: str,
    amount_paise: int,
    failure_class: str,
    action: RecoveryAction,
    payment_link: str | None = None,
) -> Message:
    """Write the customer-facing recovery message for an approved decision.

    Args:
        customer_name: Recipient's name.
        amount_paise: Failed amount.
        failure_class: FailureClass value, used for a plain-language reason.
        action: The already-approved action. Included so the copy matches what
            will actually happen - a link message and a wait-and-retry message
            say different things.
        payment_link: The short URL, when one was created.

    Returns:
        A Message. Always succeeds; falls back to templates.
    """
    if not settings.llm_live:
        return Message(
            text=_template(customer_name, amount_paise, failure_class, payment_link),
            provider="template",
        )

    reason = CUSTOMER_FACING_REASON.get(
        failure_class, "the payment did not go through"
    )
    parts = [
        f"Customer name: {customer_name}",
        f"Amount: Rs {amount_paise / 100:,.0f}",
        f"What happened: {reason}",
    ]
    if payment_link:
        parts.append(f"Payment link to include: {payment_link}")
    else:
        parts.append(
            "No link. Tell them it will be retried automatically and to wait."
        )

    text = _call_llm("\n".join(parts))
    if text is None:
        return Message(
            text=_template(customer_name, amount_paise, failure_class, payment_link),
            provider="template_fallback",
        )

    # A model that ignores the link instruction produces a message that cannot
    # be acted on, which is worse than the template. Verified rather than
    # trusted.
    if payment_link and payment_link not in text:
        logger.warning("LLM omitted the payment link; using template instead.")
        return Message(
            text=_template(customer_name, amount_paise, failure_class, payment_link),
            provider="template_fallback",
        )

    # The prompt forbids invented offers and manufactured urgency. Prompts are
    # requests, not guarantees, so the output is checked. A message that would
    # pressure or mislead a customer is discarded even though generating it
    # cost real money and latency.
    violations = find_dark_patterns(text)
    if violations:
        logger.warning(
            "LLM output rejected for %s; using template instead.",
            ", ".join(violations),
        )
        return Message(
            text=_template(customer_name, amount_paise, failure_class, payment_link),
            provider="template_fallback",
            blocked_for=tuple(violations),
        )

    return Message(text=text, provider=f"llm:{settings.llm_model}")


def mode() -> str:
    """Which mode the adapter is running in, for the health endpoint."""
    return f"live:{settings.llm_model}" if settings.llm_live else "template"
