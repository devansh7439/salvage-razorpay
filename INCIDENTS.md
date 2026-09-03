# Engineering Incident Log

Razorpay's buildathon rubric scores **Failure Recovery** — "what broke during
development and how it was resolved." This file is written as things break, not
reconstructed afterwards. Entries are append-only and in chronological order.

Each entry records what broke, what the root cause turned out to be, how it was
fixed, and what the fix cost. Where a breakage changed a design decision, that
is recorded too — those are the entries worth reading.

---

## INC-001 — Heredoc corruption while writing the taxonomy module
**When:** 2026-09-03, Phase 1
**Severity:** Trivial (tooling)

**What broke.** Writing `backend/salvage/taxonomy.py` via a bash quoted
heredoc failed with `unexpected EOF while looking for matching '`. The file
never landed.

**Root cause.** The module is ~600 lines and densely quoted — apostrophes in
prose docstrings (`Razorpay's`, `bank's`) plus nested single and double quotes
in the data entries. The heredoc delimiter was being resolved before the shell
saw the full body.

**Fix.** Wrote the file directly instead of streaming it through the shell.
Cost: one retry, ~2 minutes.

**Lesson applied.** Source files above roughly 50 lines are written directly
from here on; the shell is reserved for commands, not for content authoring.

---

## INC-002 — Python 3.14 wheel availability for the ML stack
**When:** 2026-09-03, Phase 1
**Severity:** Open — being verified

**What we are watching.** The dev machine runs Python 3.14.4, which is newer
than the interpreter most scientific-Python wheels are routinely built for.
scikit-learn, numpy, and scipy all ship compiled extensions; if prebuilt
wheels are unavailable for 3.14 on Windows, pip falls back to building from
source, which needs a C/Fortran toolchain that is not present.

**Why it is logged before it breaks.** The ML layer is on the critical path for
the entire evaluation story. Discovering a toolchain wall at hour 30 would be
unrecoverable, so it is being tested first, before any code depends on it.

**Resolution.** Clean. Every dependency resolved to a prebuilt `cp314`
Windows wheel — scipy 1.18.1, numpy 2.5.2, scikit-learn 1.9.0, pandas 3.0.5 —
so no compiler was needed. Full install took roughly four minutes, dominated by
scipy's 37 MB download. No pin-back to an older Python was required.

**What the check surfaced instead.** Two version facts that would have bitten
later:

1. **`razorpay` resolved to 2.0.1, not 1.x.** The original build spec was
   written against the 1.x SDK. Verified before writing any integration code
   that `client.payment_link.create(data)` still takes the same dict payload in
   v2, and that `razorpay.Utility.verify_webhook_signature` exists for webhook
   authentication. Both confirmed — the planned call shape is valid on v2.
2. **`pandas` resolved to 3.0.x**, a major version. Noted so that any
   deprecated-API surprises in the data layer are recognised immediately rather
   than debugged as logic errors.

**Lesson applied.** Verifying the riskiest dependency before writing code that
depends on it cost four minutes and removed the single largest schedule risk in
the project. It also caught an SDK major-version drift that would otherwise have
surfaced as a confusing runtime error during integration.
