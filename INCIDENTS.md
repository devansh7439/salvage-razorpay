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

---

## INC-004 — "The model is broken" turned out to be "the problem is noisy"
**When:** 2026-09-03, Phase 4
**Severity:** Medium — nearly caused a wrong fix

**What looked broken.** First training run:

```
ROC AUC           0.5664
Brier             0.2268
always-base-rate  0.2290   ->  0.9% improvement
```

An AUC of 0.57 is close enough to a coin flip to look like a failed model, and
a 0.9% Brier improvement over a constant predictor reads as "learned nothing."

**The trap.** The obvious response is to reach for a stronger estimator —
gradient boosting, more trees, more features — or to conclude the feature set
is inadequate. Both would have consumed hours. Neither would have helped, and
one of them would have quietly made things worse.

**What was actually going on.** Recovery outcomes are Bernoulli draws. A
payment with a true recovery probability of 0.35 recovers 35% of the time and
fails 65% of the time, and *no* model can do better than knowing that number.
The ceiling on any metric here is set by irreducible noise, not by the
estimator. The right question was never "is 0.57 good?" but "what is the best
score anything could achieve on this data?"

Because this is a simulation, that ceiling is computable. Scoring the oracle's
*true* per-payment probabilities against the realised labels gives the
Bayes-optimal result:

```
ceiling AUC     0.6286     (a model that knew every true probability)
ceiling Brier   0.2138     (irreducible floor)
```

So the attainable range for AUC was never 0.5 → 1.0. It was 0.5 → 0.629. The
model was capturing a meaningful share of it, and the headline number was
measuring the problem's difficulty, not the model's quality.

**Fix.** Two changes, in that order of importance:

1. **Report against the ceiling, permanently.** `TrainingReport` now carries
   `oracle_auc`, `oracle_brier`, and `signal_captured` — the fraction of
   attainable ranking signal the model actually recovers. Every future
   evaluation is read against what is possible rather than against 1.0.
2. **One real tuning fix.** `max_features="sqrt"` was starving each split: on a
   one-hot-widened matrix of ~25 columns it sampled about five per split, while
   nearly all the signal sits in two numeric columns, so most splits could not
   see the predictive features at all. Moving to `max_features=0.5` addressed
   the actual constraint. Customers were also given more history (~8 events
   each rather than ~3), since an observed success rate over three payments is
   almost pure noise — a data-realism fix, not a model fix.

**After.**

```
ROC AUC          0.5746  against a ceiling of 0.6286  ->  58% of attainable signal
Brier            0.2198  vs 0.2233 base rate, 0.2138 floor
propensity r     0.540   (latent driver the model never sees)

reliability, held-out customers:
  0.25-0.38   n=833   predicted 0.320   observed 0.329
  0.38-0.50   n=344   predicted 0.409   observed 0.404
```

Calibration is within roughly one point in the buckets holding ~90% of volume,
which is the property that matters, because this probability is multiplied by
rupees.

**Lesson applied.** A metric with no stated ceiling is not a result, it is a
number. On a noisy problem the ceiling is the whole story — and reporting "58%
of attainable signal, calibrated to within a point" is both more useful and
more honest than reporting an AUC that a reviewer has no way to interpret. It
is also the safer posture: a suspiciously high AUC on synthetic data is
evidence of leakage, not of quality, and we would rather be able to prove the
absence of a leak than post an impressive number.

---

## INC-005 — The system measured incremental recovery but optimised gross
**When:** 2026-09-03, Phase 5
**Severity:** High — a coherence failure between the metric and the objective

**What surfaced it.** Comparing two numbers from the live `/api/metrics`
response on the same batch:

```
expected_recoverable_paise   Rs 9,73,091     <- what the policy engine forecast
incremental_recovered_paise  Rs 7,00,059     <- what the oracle actually measured
```

A 39% overshoot, and consistent rather than noisy — the signature of a
systematic modelling error rather than bad luck.

**Root cause.** The oracle had been built to report *incremental* recovery, net
of customers who would have returned unprompted. The policy engine had not: its
expected value was

```python
net_ev = amount * P(recovery | action) * (1 - mdr) - cost
```

which is *gross*. The two components were answering different questions. The
engine was buying gross recovery while the scoreboard paid out on incremental,
so it would happily spend money messaging a customer whose bank was briefly
down — someone who returns on their own about a third of the time — and book
the arriving revenue as a win.

**Why this is the more dangerous class of bug.** Nothing was broken. Every
number was internally consistent, every test passed, and the dashboard looked
healthy. The defect was that two halves of the system had been built to
different definitions of value, and only comparing them side by side revealed
it. Had the mismatch gone the other way — optimising incremental, reporting
gross — it would have inflated the headline result instead of the forecast, and
would have been far harder to notice because the error would have flattered us.

**Fix.** Value the lift, not the outcome:

```python
lift   = max(0, P(recovery | action) - P(organic))
net_ev = amount * lift * (1 - mdr) - cost
```

`ORGANIC_BASELINE` was added to `economics.py` as a merchant-side estimate —
the kind any merchant can produce by holding out a no-contact cohort for a
fortnight. It is deliberately separate from the oracle's own organic rates: the
policy engine is not permitted to read ground truth, and the two are allowed to
disagree, exactly as an estimate and reality do in production.

The consequence is the behaviour we wanted all along. A bare notification on a
bank-downtime failure now prices at **zero lift and negative net value**,
because it is weaker than simply leaving the customer alone. The engine declines
to send it.

**Lesson applied.** Optimise the number you report. When the objective and the
scoreboard come apart, the system will exploit the gap, and it will look like
success while doing it.

---

## INC-006 — Fraud-blocked payments were recoverable in the simulation
**When:** 2026-09-03, Phase 5
**Severity:** Medium — corrupted the baseline comparison

**What broke.** A test written to assert an obvious invariant failed:

```
FAILED test_risk_blocked_payments_never_recover
  Outcome(recovered=True, recovered_paise=63500, p_action=0.091875)
```

**Root cause.** `ACTION_EFFECTIVENESS` had no row for `RISK_BLOCKED`, so lookups
fell through to `DEFAULT_EFFECTIVENESS = 0.25`. A fraud-blocked payment
therefore had a ~9% chance of "recovering" in the oracle. The default had been
written as a conservative catch-all for unmodelled *combinations*; it was never
meant to apply to classes that are unrecoverable by construction.

**Why it mattered beyond tidiness.** The policy engine never acts on risk blocks
— a hard constraint stops it long before economics — so Salvage was unaffected.
But `blind_retry` acts on everything indiscriminately, which is the entire point
of it as a baseline. It was being credited with revenue from payments that
cannot be recovered at all, which made the baseline look better than it is.

The bug was therefore biased *against* the thesis being argued, and fixing it
moved the comparison in our favour: blind retry fell from Rs 3,79,808 to
Rs 3,67,855 incremental. That direction is worth stating plainly — it is
evidence the evaluation is not being tuned toward a flattering answer.

**Fix.** `ZERO_EFFECTIVENESS_CLASSES` makes `RISK_BLOCKED` and `ALREADY_PAID`
explicitly zero for every action, rather than inheriting a permissive default.

**Lesson applied.** A default that is safe in one context is not safe in all of
them. This one was reasonable for unmodelled pairings and wrong for structural
impossibilities, and only an explicitly stated invariant caught the difference —
the bug was invisible in aggregate metrics, which is exactly where it was doing
its damage.

---

## INC-007 — Fixing INC-006 broke inference in a different module
**When:** 2026-09-04, Phase 6
**Severity:** High — inflated every expected value on the dashboard

**What broke.** After shipping the INC-005 incremental-value fix, the forecast
error on the live dashboard had not moved at all:

```
expected_recoverable   Rs 9,73,092     <- policy forecast
incremental_measured   Rs 7,00,059     <- oracle measured
error                  +39.0%          <- identical to before the fix
```

An unchanged number after a fix that should have changed it is a stronger
signal than a wrong number. The formula was not the problem.

**Diagnosis.** Rather than reason about it, the predicted and true propensities
were compared class by class:

```
MERCHANT_CONFIG  1.67x     AUTH_FAILURE        0.94x
RISK_BLOCKED     1.62x     BANK_DOWNTIME       0.97x
ALREADY_PAID     1.63x     INSTRUMENT_INVALID  0.93x
UNKNOWN          1.70x     INSUFFICIENT_FUNDS  1.02x
```

The bias was entirely inside the four classes deliberately *excluded* from
training. Everything the model was actually trained on was accurate to within
7%. The model was never the problem either.

**Root cause — and it was self-inflicted.** `predict_propensity` recovers
intervention-independent propensity by dividing the model's output by the
reference action's effectiveness:

```python
base = p_reference / max(effectiveness(failure_class, PAYMENT_LINK), 1e-6)
```

INC-006 had just set `RISK_BLOCKED` and `ALREADY_PAID` to effectiveness
**zero** — correctly, for the oracle. But this division site, in a different
module, then divided by `1e-6`. Propensity exploded and clipped at 1.0 for 84
payments. `MERCHANT_CONFIG` and `UNKNOWN` had no effectiveness row either, so
they divided by the 0.25 default and inflated fourfold.

So a correct fix in `economics.py` silently broke a consumer in `ml/predict.py`
that had been relying on the old permissive default. The `max(fit, 1e-6)` guard
was the tell: it was written to prevent a crash, and what it actually did was
convert a crash into a plausible-looking wrong number.

**Fix.** `UNTRAINED_CLASSES` and a neutral divisor in `predict.py`. Classes the
model was never trained on now use a constant roughly equal to the mean
payment-link effectiveness across the trained population, rather than a
per-class value that is meaningless or zero for them.

```
overall predicted/true   1.041 -> 0.981
clipped at ceiling       84 events -> 10
forecast error           +39.0% -> -9.9%
```

The residual −9.9% is the model's own slight conservatism, and it errs in the
right direction: the system under-promises against what it then delivers.

**Lesson applied.** Two lessons, and the second is the one that generalises.

First: `max(x, 1e-6)` is not a guard, it is a way of turning a loud failure
into a quiet one. Where a zero denominator means "this quantity is undefined
here", the code should say so explicitly rather than substitute an
infinitesimal and carry on.

Second: the bug was found by comparing a forecast against a measurement, and it
was only findable because the system produces both. A pipeline that reported
only its own expected values would have had nothing to check itself against,
and this error would have shipped — inflating the headline number by 39%, in
the direction most likely to be believed and least likely to be questioned.

---

## INC-008 — A verified fix appeared to regress, because the server was stale
**When:** 2026-09-04, final verification
**Severity:** Low (no defect) — but it nearly caused a good fix to be reverted

**What happened.** A full end-to-end run was executed to confirm the system
before submission. The INC-007 forecast-error fix had been measured at −9.9% by
a direct script run. Going through the live API, the same metric read:

```
forecast vs measured   Rs 973,092 vs Rs 700,059   (+39.0%)
```

Exactly the pre-fix number, to a tenth of a percent.

**Root cause.** No defect. The `uvicorn` process had been started earlier in the
session, *before* `ml/predict.py` was edited, and had the old module resident in
memory. It was serving pre-fix bytecode. The CLI paths ran fresh interpreters
and so picked up the fix; only the long-lived server did not.

**Resolution.** Restarted the process. The metric read −9.9% immediately.

**Why it is logged despite being no bug.** The obvious reading of that output
was that the INC-007 fix had not worked, and the obvious next step would have
been to go back and change working code. The tell was the number being
*identical* to the pre-fix value rather than merely close — a real regression
almost never lands on the old value to a tenth of a percent. That precision is
the signature of stale state, not of broken logic.

**Lesson applied.** Final verification runs against freshly started processes,
never against ones that have been alive across edits. `--reload` is a
development convenience, not a substitute for confirming what is actually
loaded, and a long-lived server is a cache like any other.

---

## INC-009 — Webhook redelivery produced two interventions for one payment
**When:** 2026-09-04, scalability pass
**Severity:** High — customer-visible, and would have shipped

**What broke.** A test written to assert that reprocessing a batch is a no-op
failed:

```
run 1: {'PAYMENT_LINK': 7, 'RETRY_SCHEDULED': 8, 'DROP': 5}
run 2: {'DROP': 10, 'RETRY_SCHEDULED': 9, 'PAYMENT_LINK': 1}

pay_0c5c5678047a65  x2  actions = PAYMENT_LINK, RETRY_SCHEDULED
```

One payment received a payment link on the first delivery and a scheduled
retry on the second.

**Root cause.** Idempotency was enforced one level too low. `executions` carries
a `UNIQUE` idempotency key derived from `(payment_id, action)`, which correctly
prevents the *same* action running twice. But on redelivery the pipeline
re-decides from scratch, and by then the contact guardrails can see the message
sent on the first pass — so they legitimately suppress the payment link and pick
a different action instead. A different action means a different key, and the
uniqueness constraint never fires.

Every individual component behaved exactly as designed. The guardrails
suppressing the second contact is *correct*; that is what they are for. The bug
was that the payment was being decided twice at all.

**Why this would have shipped.** Razorpay redelivers `payment.failed` on any
non-2xx response or timeout, so this is normal operation, not an edge case. It
was invisible in every measurement taken so far because the demo batch is loaded
exactly once into a freshly reset database. Only an explicit
"process the same batch twice" test surfaced it.

**Fix.** Idempotency moved up to the decision layer. `_process_chunk` looks up
which payments already carry a decision and skips them, returning a `skipped`
count so a redelivery is observably a no-op rather than silently one. A
`reprocess=True` flag exists for deliberate replays, such as after changing
policy parameters.

```
run 1: processed 20, skipped 0
run 2: processed  0, skipped 20     duplicated interventions: 0
```

Verified end to end against the live webhook endpoint: the first POST returns
`{"processed": 1}`, every subsequent identical POST returns
`{"processed": 0, "skipped": 1}`.

**Lesson applied.** Idempotency has to be enforced at the level of the decision,
not the level of the side effect. Guarding each individual action still permits
a *sequence* of different actions, and the guard looks correct in isolation the
entire time. The general form: a uniqueness constraint only protects the
identity it is keyed on, so key it on the thing that must happen once — here,
"this payment gets one recovery decision" — rather than on the mechanism that
happens to carry it out.
