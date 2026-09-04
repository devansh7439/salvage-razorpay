"""Test isolation.

The suite must produce the same result on a maintainer's machine as on a
reviewer's clone, and it must not reach the network to do it. Both properties
were being silently broken by a local `.env`.

`Settings` reads credentials from that file, so on a machine configured for
the live demo every test that runs the pipeline took the live path: real
Payment Links against the merchant's Razorpay account, and one LLM call per
generated message. `test_learning`'s fixture alone processes 3,000 events and
is function-scoped, so a full run issued thousands of real API calls - slow,
rate-limited, non-deterministic, and it filled a real account with junk links.

Credentials are therefore cleared for every test. Anything wanting the live
path monkeypatches them back for the duration of that test, which several
tests already do - the difference is that it is now explicit rather than
inherited from whatever happens to be on the developer's disk.
"""

from __future__ import annotations

import pytest
from salvage.config import settings

#: Fields that decide whether an integration runs live or on fixtures.
CREDENTIAL_FIELDS = (
    "razorpay_key_id",
    "razorpay_key_secret",
    "razorpay_webhook_secret",
    "llm_base_url",
    "llm_api_key",
)


@pytest.fixture(autouse=True)
def hermetic_credentials():
    """Force fixture mode for every test, then restore the real settings."""
    saved = {field: getattr(settings, field) for field in CREDENTIAL_FIELDS}
    for field in CREDENTIAL_FIELDS:
        setattr(settings, field, "")
    try:
        yield
    finally:
        for field, value in saved.items():
            setattr(settings, field, value)
