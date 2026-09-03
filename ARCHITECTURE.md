# Architecture

Design rationale for Salvage. The [README](README.md) covers what it does and
how to run it; this document covers *why it is built this way*, and what the
alternatives were.

---

## 1. The core separation

The system is organised around one boundary: **prediction, judgement, and
phrasing are three different jobs, and only one of them may be probabilistic.**

```
                    ┌──────────────────────────────────────┐
  webhook  ────────▶│  DIAGNOSE       taxonomy.py          │  deterministic
                    │  lookup against Razorpay's docs      │  48 documented reasons
                    └──────────────────┬───────────────────┘
                                       │  failure_class + confidence
                    ┌──────────────────▼───────────────────┐
                    │  SCORE          ml/predict.py        │  probabilistic
                    │  calibrated P(customer will pay)     │  ← the only ML
                    └──────────────────┬───────────────────┘
                                       │  base_propensity ∈ [0,1]
                    ┌──────────────────▼───────────────────┐
                    │  DECIDE         policy.py            │  deterministic
                    │  ① constraints ② eligibility ③ EV    │  ← spends the money
                    └──────────────────┬───────────────────┘
                                       │  approved action
                    ┌──────────────────▼───────────────────┐
                    │  EXECUTE        executor.py          │  deterministic
                    │  Razorpay link · retry · escalate    │
                    │       └── LLM writes the copy only ──┼──  generative
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  AUDIT + MEASURE                     │
                    └──────────────────────────────────────┘
```

**The money-spending step is the deterministic one.** A model proposes, a rule
table disposes. This is not caution for its own sake: it means every rupee the
system spends traces to a named rule, and a merchant can read the rule.

### Why the LLM has no authority

An agent architecture where an LLM holds tools like `create_payment_link()` and
`schedule_retry()` is the obvious design, and it was rejected. The failure mode
is not that the LLM makes bad decisions occasionally — it is that when it does,
the reason is unrecoverable. There is no rule to point at, no threshold to
adjust, and no way to prove the same input will not produce a different action
tomorrow.

The LLM here receives a decision that has already been made and approved, and
turns it into a sentence. It never sees an expected value it could argue with.

---

## 2. Why `error_reason`, not `error_code`

The single most consequential technical decision in the repo.

Razorpay's `error_code` field is coarse by design — `BAD_REQUEST_ERROR` covers
insufficient funds, expired cards, wrong OTPs, and cancelled checkouts alike.
A policy keyed on it cannot distinguish a customer whose bank was briefly down
from one holding a dead card, and those need opposite treatments.

Razorpay documents `error_reason` as the field that is *"programmatically
handleable"*. Salvage keys on it, across 48 reasons transcribed from
`razorpay.com/docs/errors/payments/list/`, grouped into nine failure classes
chosen so that **recovery economics differ across classes and are similar
within them**.

| Failure class | n | Why it is its own class |
|---|---:|---|
| `BANK_DOWNTIME` | 6 | Nothing wrong with customer or card. Waiting fixes it. |
| `INSUFFICIENT_FUNDS` | 1 | Balance is a moving target; time genuinely helps. |
| `INSTRUMENT_INVALID` | 9 | Card is dead. Only a *different* instrument works. |
| `AUTH_FAILURE` | 7 | Customer was present and mistyped. Intent was real. |
| `CUSTOMER_ABANDONED` | 2 | They walked away rather than being refused. |
| `LIMIT_EXCEEDED` | 4 | Resets on a clock. |
| `RISK_BLOCKED` | 2 | Never actionable. Hard stop. |
| `MERCHANT_CONFIG` | 15 | `source=business`. No customer can fix it. |
| `ALREADY_PAID` | 2 | Acting would double-charge. |
| `UNKNOWN` | — | Refuses to guess. Goes to the exception report. |

**Resolution order** is `reason` → `source` → give up. `code` is never a primary
signal. Anything unrecognised is returned *unconfident* and lands on the
exception list rather than triggering a guessed intervention — which matters
because Razorpay adds reasons over time, and a system that silently
mis-classifies a new one is worse than one that admits it does not know.

---

## 3. The economics

### Net expected value

```
P(recovery | action) = base_propensity × effectiveness[class][action]
P(organic)           = base_propensity × organic_baseline[class]
lift                 = max(0, P(recovery | action) − P(organic))
net EV               = amount × lift × (1 − MDR) − action_cost
```

Four terms, each earning its place:

**`effectiveness`** makes probability action-conditional. Without it, every
action shares one probability, the amount and MDR terms cancel, and the ranking
collapses to *"whichever intervention is cheapest"*. This is not hypothetical —
it shipped, and it produced an engine that notified customers their expired card
had expired ([INC-003](INCIDENTS.md)).

**`lift`** makes the calculation incremental. Value is only created where the
intervention *changed* the outcome. An action weaker than leaving the customer
alone scores zero lift and is rejected, however much money happens to arrive
afterwards ([INC-005](INCIDENTS.md)).

**`MDR`** keeps rupee figures honest. The merchant never banks gross recovery;
the processor's cut comes off first.

**`action_cost`** is what makes `DROP` principled. Without a cost term there is
no reason ever to stop, and "stopping rules" become an arbitrary table.

### Where the numbers come from

Costs are order-of-magnitude estimates for the Indian market, chosen to be
defensible rather than precise. Their **ratios** drive behaviour: `ESCALATE`
costs ~25× a retry, so human attention is only spent on payments large enough
to justify it.

`ACTION_EFFECTIVENESS` and `ORGANIC_BASELINE` are hand-authored domain
judgement, documented per-entry with the reasoning inline. This is deliberate:
a reviewer can disagree with the specific claim that a scheduled retry beats an
immediate one for bank downtime, because that claim is a number in a table with
a comment next to it — not a weight inside a forest. In production both would
be fitted from holdout experiments.

`ORGANIC_BASELINE` is kept **separate from the oracle's ground truth**. The
policy engine is not permitted to read the simulator's reality; it uses the kind
of estimate any merchant can produce by holding out a no-contact cohort. The two
are allowed to disagree, exactly as an estimate and reality do in production.

---

## 4. Guardrails

Every field of `MerchantPolicy` is a limit the system may not exceed regardless
of how attractive the economics look, checked **before** the probability is read.

| Guardrail | Default | Rationale |
|---|---|---|
| `max_attempts_per_payment` | 3 | Hard stop on chasing one payment. |
| `max_contacts_per_customer_per_day` | 2 | Anti-spam, across all their payments. |
| `cooldown_hours` | 12 | Minimum gap between contacts. |
| `min_net_ev_paise` | ₹5 | Floor; guards against acting on rounding noise. |
| `max_autonomous_amount_paise` | ₹50,000 | Above this, human sign-off. |
| `mdr_rate` | 2% | Processor's cut. |

Two details worth calling out.

**Contact caps suppress messaging, not silent retries.** A retry costs the
customer no attention, so an anti-spam limit has no business blocking one.
Conflating the two either spams people or leaves free money on the table.

**Consent is absolute.** `customer_opted_out` returns `DROP` before any
arithmetic runs. There is no expected value large enough to override it.

---

## 5. Measurement

The track asks for *measured* money recovered. Expected value is a forecast the
system makes about itself and can inflate without limit, so the simulator
includes an **outcome oracle** that adjudicates results independently of any
prediction.

### Common random numbers

Every counterfactual for one payment resolves against a single uniform draw
keyed on its id. Two consequences, both wanted:

- **Paired comparison.** Baseline and Salvage face identical customers with
  identical luck, so the measured delta is the effect of the decision policy
  and nothing else. Without this, much of any reported improvement is noise.
- **Monotone interventions.** If a weak action would have recovered a payment,
  a stronger one would too. Effectiveness raises the bar an action clears
  rather than re-rolling the dice.

Outcomes are reproducible across runs, processes, and machines.

### Anti-circularity

The standard synthetic-data failure is that the generator writes both features
and labels, so the model rediscovers the generator's rules and posts a
meaningless 0.99 AUC. Three defences:

1. Outcomes are driven by **latent variables the model never sees** — true
   reliability, purchase intent, issuer health.
2. Observable features are **noisy proxies**. The model gets a small, noisy
   sample of reliability, the same partial view a real merchant has.
3. Outcomes are **Bernoulli draws**, so a hard ceiling exists.

`SyntheticEvent.features()` filters underscore-prefixed fields *structurally*,
so latent truth cannot leak even if a future author adds a field carelessly.

The model is reported against the Bayes-optimal ceiling — 58% of attainable
ranking signal, calibrated to within ~1 point. A near-perfect score here would
be evidence of leakage ([INC-004](INCIDENTS.md)).

### Validation splits by customer

Customers recur across the batch. A random row split would put one customer's
payments on both sides of the wall, and the model would score well by
recognising people it had been paid to memorise. `GroupShuffleSplit` on
`customer_id` makes the test set genuinely unseen.

---

## 6. Auditability

`audit_trail` is **append-only** — there is no `UPDATE` or `DELETE` against it
anywhere in the codebase. Six rows per payment: `INGESTED`, `DIAGNOSED`,
`SCORED`, `DECIDED`, `EXECUTED`, `OUTCOME`.

A single "processed" log line tells a reviewer nothing about *where* a
judgement was formed. Six rows let them see the diagnosis came from a documented
Razorpay reason, the score from a calibrated model, and the action from a named
rule — separate steps that can be disputed independently.

Decisions persist **the alternatives they beat**, not just the winner. "Why a
link instead of a retry?" is answerable from the database months later. The
dashboard's decision inspector renders exactly this, which is also how INC-003
was caught: the bug was invisible in the code and obvious the moment every
considered alternative was printed.

---

## 7. Reliability

**Idempotency.** Razorpay redelivers webhooks; batches get replayed; processes
restart mid-flight. Without stable keys those all create fresh payment links and
a customer receives three links for one order. Keys derive from
`(payment_id, action)`, enforced by a `UNIQUE` constraint.

**Webhook signatures.** HMAC-SHA256 over the raw body, constant-time compared,
verified before parsing. One of the few places where a missing check is a real
security hole rather than an inconvenience: an unauthenticated caller could make
the system issue payment links to numbers of their choosing.

**Degradation.** Both external integrations build their payloads identically in
live and fixture modes; only the transport differs. A provider outage degrades
the wording of an SMS rather than stopping recovery for a thousand payments, and
the demo cannot fail on conference wifi. `/health` always reports which mode is
active — a demo that quietly runs on fixtures while implying live calls is the
kind of thing that destroys credibility when a judge asks.

---

## 8. Scaling

The demo batch is 1,000 payments. A real merchant's is not, so the pipeline is
built so that nothing in it scales with total input size.

### Streaming, in bounded windows

`process_batch` accepts any iterable and consumes it lazily, so events can come
from a file, a queue, or a cursor without first building a list.

```
stream ──▶ window (4,096) ──▶ one inference call
                 │
                 └──▶ slice (500) ──▶ one write transaction
```

Peak memory is one window, regardless of whether the input is a thousand
events or ten million. Measured: **8.9 MB at 10,000 events, 11.6 MB at
50,000** — flat, as intended.

### Two chunk sizes, because they bound different costs

This is the non-obvious part. A **write transaction** wants to be *small*: it
caps rollback scope, lock duration, and how much a failure destroys. An
**inference call** wants to be *large*: the calibrated model is five
cross-validated forests of 500 trees, and `predict_proba` pays about a second
of fixed cost per call almost regardless of row count.

| rows per inference call | cost |
|---:|---:|
| 500 | 1.10 ms/event |
| 2,000 | 0.33 ms/event |
| 4,000 | 0.25 ms/event |

Using one knob for both cost roughly 4× throughput for no benefit. They are now
`SCORING_WINDOW` and `CHUNK_SIZE` respectively.

### Nothing per-chunk may scan a whole table

The rule that matters at volume: work done *once per chunk* must not grow with
the data already written, or total cost becomes quadratic in batch size.

Three places violated it and were fixed:

- **Contact-frequency guardrails** joined `executions` to `events` to reach
  `customer_id`. That query grew linearly with the table and ran once per
  chunk — 13 ms at 4,000 rows, 112 ms at 24,000. `customer_id` is now
  denormalised onto `executions` with a covering index, making it a single
  index lookup that stays flat.
- **Decision lookups** for idempotency loaded every decision ever made. Now
  scoped to the ids in the current chunk.
- **Audit writes** issued six separate statements per payment, making
  per-statement overhead rather than the write itself the dominant ingest cost.
  Rows are buffered and flushed with one `executemany` per chunk. The trail is
  unchanged; only the number of round trips is.

### Query safety

`limit` on the recovery queue is clamped server-side (500). `executions` is
one-to-many with `events`, so the queue reaches the latest execution through a
correlated subquery rather than a join — a direct join multiplied result rows
per execution and silently inflated every count taken from it.

`/api/evaluate` is cached until the next batch load, since its result is
deterministic for a given batch: **1.29 s → 0.004 s**.

---

## 9. Rejected alternatives

| Considered | Why not |
|---|---|
| LLM agent with payment tools | Unauditable money decisions. See §1. |
| Postgres + Docker | Operational cost, zero judge-visible benefit. SQLite is zero-config and the schema is portable. |
| 50,000 synthetic events | The bar is a 50+ record batch. Volume is not rigour; the held-out split and the ceiling comparison are. |
| XGBoost | The problem is noise-limited, not capacity-limited. Calibration matters far more than the estimator, and a forest plus isotonic calibration is easier to defend. |
| Voice recovery | Listed as a valid direction, but 2–3 days of work that improves no measured outcome. |
| Uncalibrated classifier | The probability is multiplied by rupees. A model reporting 0.9 when it means 0.6 systematically overspends, and AUC would never reveal it. |
| Reporting gross recovery | Would have inflated the headline from ₹7.0L to ₹11.1L by claiming credit for customers who returned unprompted. |
