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

---

## INC-003 — Policy engine ranked interventions by cost, not by fitness
**When:** 2026-09-03, Phase 2
**Severity:** High — wrong decisions, and wrong in a way that looked right

**What broke.** The first smoke test of the policy engine produced two
decisions that are backwards to anyone who knows payments:

```
bank downtime, p=0.72   -> PAYMENT_LINK   (should be a retry)
expired card,  p=0.65   -> NOTIFY         (should be a payment link)
```

Telling a customer their expired card expired, without giving them a way to pay
with a different one, is the single least useful thing the system could do.

**Root cause.** `value_action` applied *one* probability to every candidate
action:

```python
net_ev = amount * probability * (1 - mdr) - action_cost
```

With `probability` identical across actions, the amount and MDR terms are also
identical, so the ranking reduces to `-action_cost`. The engine was not choosing
the best intervention at all — it was choosing the cheapest one, every time, and
the ordering of `ACTION_COSTS` was silently deciding all policy. NOTIFY (175
paise) beat PAYMENT_LINK (185) beat RETRY (200), which is exactly the sequence
the bad output showed.

**Why it was dangerous rather than merely wrong.** The system still produced a
decision for every payment, with a plausible rupee figure attached and a
confident rationale string. Nothing errored. On a dashboard it would have looked
entirely healthy, and the arithmetic shown in the inspector would have been
internally consistent — just answering the wrong question.

**The modelling error underneath.** P(recovery) was being treated as a property
of a payment. It is a property of a payment *and an intervention together*.
Retrying a bank-outage failure works most of the time; retrying an expired card
works never. A single scalar per payment cannot represent that, so no amount of
model tuning would have fixed it.

**Fix.** Split the probability into two factors with different owners:

```python
P(recovery | action) = base_propensity x effectiveness[failure_class][action]
```

- `base_propensity` stays with the ML model — the customer's underlying
  willingness and ability to pay, learned from amount, history and context.
- `effectiveness` is a documented, human-authored matrix in `economics.py`
  giving the structural fit between each failure mode and each remedy.

Keeping them separate is deliberate: the learned part stays learnable, and the
part that encodes domain truth stays inspectable and arguable. A reviewer can
disagree with the claim that a scheduled retry beats an immediate one for bank
downtime, because that claim is a number in a table with a comment next to it,
not a weight inside a forest.

**After the fix.**

```
bank downtime    -> RETRY_SCHEDULED  (considered RETRY_NOW, PAYMENT_LINK)
expired card     -> PAYMENT_LINK     (NOTIFY priced 3.4x lower)
razorpay 5xx     -> RETRY_SCHEDULED
Rs 8 payment     -> DROP             (NOTIFY prices at negative net EV)
```

**Lesson applied.** The bug was invisible in the code and obvious in the output
— it took a smoke test printing *every considered alternative*, not just the
winner, to see it. The `considered` field was added to `PolicyDecision` for
debugging and then kept permanently: it is now what the dashboard's decision
inspector renders, so the same visibility that caught this is what a judge sees.
