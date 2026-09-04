# Salvage

**Bounded autonomous revenue recovery for failed Razorpay payments.**

Razorpay AI Buildathon 2026 · AI Revenue Recovery track

Salvage ingests `payment.failed` events, diagnoses each one against Razorpay's
*published* error taxonomy, predicts recovery propensity with a calibrated
model, prices every available intervention, and acts only where the economics
clear a merchant-defined floor — then reports what it actually recovered, net
of what would have happened anyway.

---

## The result

Measured over 1,000 held-out failed payments, ₹31,53,901 at risk:

| Strategy | Actions | Spend | Incremental recovered | Net value |
|---|---:|---:|---:|---:|
| Do nothing | 0 | ₹0 | ₹0 | ₹0 |
| Blind retry (×3) | 3,000 | ₹6,000 | ₹3,67,855 | ₹3,61,855 |
| **Salvage** | **721** | **₹2,654** | **₹7,00,059** | **₹6,97,405** |

**90% more incremental revenue than blind retry, from 76% fewer interventions.**

The number that matters more:

```
gross recovered under Salvage        ₹11,10,463
of which would have arrived anyway    ₹4,10,404   ← organic, no credit claimed
genuinely caused by the system        ₹7,00,059
unresolved exceptions                        50
```

Most recovery tools would report **₹11.1L**. Salvage reports **₹7.0L**, because
some customers come back on their own and billing for them is not recovery.

---

## How to read these numbers

Three figures here are deliberately smaller than the ones a recovery dashboard
usually shows. Each is the correct call, and each reads as a weakness if
nobody explains it — so, plainly:

**1. Recovery is incremental, not gross — ₹7.0L, not ₹11.1L.**
₹4.1L of that gross figure came from customers who would have returned
unprompted; for a bank outage, about a third do. Salvage subtracts them and
claims only what it caused. It also *optimises* that same number, so it will
not spend money nudging someone already on their way back.

**2. AUC 0.575 is not a coin flip — the ceiling is 0.629.**
Recovery outcomes are Bernoulli draws, so no model can exceed the Bayes rate.
We measured that ceiling by scoring the oracle's *true* probabilities against
realised labels. The attainable range is 0.5 → 0.629, not 0.5 → 1.0, and 0.575
is **58% of the signal that exists**. Calibration is within ~1 point across the
buckets holding 90% of volume. A near-perfect score here would be evidence of
leakage, not quality.

**3. The taxonomy is real; only the volume is synthetic.**
See [Data provenance](#data-provenance) — the split matters and is testable.

---

## Where this sits next to Razorpay Agent Studio

Razorpay already ships recovery agents — a **Subscription Recovery Agent** that
applies "smarter retry logic", and an **Abandoned Cart Conversion Agent** that
calls or messages customers with a payment link. Both are built on Claude.

So "an agent that chases failed payments" is not a gap in their platform. It is
their platform.

**The gap is the layer underneath.** Those agents decide *how* to recover.
Nothing published decides **which revenue is worth spending money on, and when
to stop** — the economics that turn a recovery workflow into an investment
decision. That is what Salvage is:

```
Agent Studio agents    HOW to recover   →  call, message, retry, send a link
Salvage                WHETHER to       →  is this worth it, which action, when to stop
```

Concretely, this layer contributes three things a workflow agent does not have:
an **action-conditional net expected value** with real costs attached, an
**incremental measurement** that refuses to claim credit for organic recovery,
and a **stopping rule that falls out of the arithmetic** rather than a retry cap.

### Mapping to Razorpay's stated agent principles

Razorpay publishes nine principles and four autonomy limits for agents in
payments. Salvage was built to that shape:

| Razorpay's principle | Where it lives here |
|---|---|
| "Every agent operates within boundaries the merchant defines" | `MerchantPolicy` — attempt caps, contact caps, cooldown, EV floor, autonomy ceiling |
| "Every action is validated before it executes" | Three-stage engine: hard constraints → eligibility → economics |
| "Agents don't set prices or invent discounts" | LLM writes copy only; it never sees an amount it can alter |
| "Customer communication follows consent rules" | `customer_opted_out` → `HARD_OPT_OUT`, checked before any valuation |
| "Agents must not employ dark patterns" | Generated copy is **scanned and rejected** for false urgency, manufactured pressure, invented offers |
| "Every single action is logged with a full audit trail" | Append-only `audit_trail`, six stages per payment, with the rejected alternatives |
| **Review-first mode** | `AgentMode.REVIEW_FIRST` — decides and records, executes nothing |
| **Immediate kill switch** | `POST /api/controls` — runtime, no restart |
| **Escalation defaults** | `HARD_HIGH_VALUE_ESCALATION` above the autonomous ceiling |
| **Irreversibility blocks** | Risk blocks and settled orders are structurally unreachable, at any score |

The dark-patterns check is worth singling out: the system prompt already
forbids invented offers, but a prompt is a request, not a guarantee. Generated
copy is checked against the rule and discarded if it trips — because "we told
the model not to" is not a control.

---

## The thesis

Recovery is an **investment decision**, not a routing decision.

Every intervention costs real money — an SMS, a gateway attempt, five minutes
of an ops analyst. So the question is never "can we do something?" but "is
doing something worth more than doing nothing?" Once framed that way, stopping
rules stop being a policy you write and become arithmetic you compute: the
system stops chasing a payment for the same reason a business would.

Three claims follow, and the repo is organised around them.

### 1. The failure taxonomy is Razorpay's, not ours

`error_code` is useless as a recovery signal — `BAD_REQUEST_ERROR` spans
everything from an expired card to a cancelled checkout. Salvage keys on
`error_reason`, which Razorpay documents as *"programmatically handleable"*,
across **48 real reasons** transcribed from their docs into
[`taxonomy.py`](backend/salvage/taxonomy.py).

Each entry carries Razorpay's own recommended next step, so any decision the
system makes can be checked against the vendor's public documentation. Their
`error_source` field even supplies the coarse routing:

| `error_source` | Razorpay's documented guidance |
|---|---|
| `customer` | "Display a meaningful message and prompt them to retry" |
| `business` | "Fix the request parameters before retrying" |
| `gateway` | "Retry or ask the customer to use a different payment method" |
| `razorpay` | "Retry after a short delay. Contact support if it persists" |

That last row is why `source=business` failures — `merchant_not_activated`,
`payment_method_not_enabled` — are marked **not customer-actionable** and route
to a human ops queue. A customer cannot fix a misconfigured merchant account,
and messaging them about it only advertises the merchant's own problem.

### 2. The LLM never decides anything

The judging criteria ask for AI "used appropriately, with deterministic
solutions where AI is unnecessary". The split here is absolute:

| Component | Owns | Cannot do |
|---|---|---|
| **Model** (`ml/`) | "How likely is this to work?" | Choose an action |
| **Policy** (`policy.py`) | "Should we spend money on this?" | Anything non-deterministic |
| **LLM** (`integrations/llm.py`) | "How do we say this to a human?" | See an amount, choose an action, call a tool |

The worst outcome of a bad generation is an awkward SMS. The worst outcome of
an LLM with authority over retries is a customer charged four times.

### 3. Guardrails are checked before the score is read

[`policy.py`](backend/salvage/policy.py) decides in three ordered stages, and
the order is load-bearing:

1. **Hard constraints** — risk blocks, already-settled orders, attempt caps,
   opt-outs, high-value escalation. A ₹99,000 fraud-blocked payment at 99%
   predicted recovery still returns `DROP`.
2. **Eligibility** — which interventions are coherent for this failure mode.
   Retrying an expired card is not a judgement call, it is a category error, so
   the option is never offered to the optimiser.
3. **Economics** — among what remains, the highest net expected value wins. If
   nothing clears the merchant's floor, `DROP`.

---

## Honest accounting

Three deliberate choices that make the headline number smaller and defensible.

**Incremental, not gross.** Some customers return unprompted — for bank
downtime, about a third of them. Salvage models a do-nothing baseline and
subtracts it. It optimises the same quantity it reports, so it will not spend
money nudging someone who was coming back anyway ([INC-005](INCIDENTS.md)).

**Paired comparison.** All three strategies are adjudicated by the same oracle
under **common random numbers** keyed on payment id. Every strategy meets
identical customers having identical luck, so the delta is the effect of the
decision policy and not sampling noise. Results are byte-identical across runs
and machines.

**The model is reported against its ceiling.** Outcomes are Bernoulli draws, so
there is a hard limit on any metric:

| | Salvage | Ceiling | Baseline |
|---|---:|---:|---:|
| ROC AUC | 0.5746 | 0.6286 | 0.5 |
| Brier | 0.2198 | 0.2138 | 0.2233 |

**58% of the attainable ranking signal**, calibrated to within ~1 point across
the buckets holding 90% of volume. A near-perfect AUC on synthetic data would
be evidence of leakage, not quality ([INC-004](INCIDENTS.md)).

**Exceptions are published, not hidden.** 50 of 1,000 payments are reported
unresolved — mostly Razorpay's generic `payment_failed`, which carries no
recovery signal. The system declines to guess an intervention on a failure it
cannot diagnose.

---

## Data provenance

"It's synthetic" is too blunt a caveat, because most of this system is not.
What is real and what is simulated is a precise, testable split:

| Component | Source | Verified by |
|---|---|---|
| Failure taxonomy (48 `error_reason` values) | Razorpay's published docs | `test_taxonomy_and_oracle.py` |
| `error_source` → action guidance | Razorpay's published docs | quoted in `taxonomy.py` |
| `payment.failed` webhook contract | Razorpay's documented payload | `test_webhook_contract.py` |
| Payment Links request payload | Razorpay Payment Links API | `test_webhook_contract.py` |
| Webhook signature (HMAC-SHA256) | real algorithm, real rejection | `test_webhook_contract.py` |
| **Event volume and outcomes** | **synthetic** | `simulator/` |

`backend/tests/fixtures/payment_failed_webhook.json` is a real Razorpay
envelope — correct nesting, field names, integer paise, the full five-field
error object. It is driven end to end through the pipeline in tests and comes
out as `INSTRUMENT_INVALID → PAYMENT_LINK` with a five-stage audit trail.

The signature tests are worth singling out because they are genuinely live: a
forged signature and a tampered body are both rejected by real HMAC-SHA256,
with no credentials and no network.

Only the last row is simulated — and the generator is built specifically so the
model cannot memorise it (latent drivers it never sees, noisy proxies,
irreducible Bernoulli noise, and a measured Bayes ceiling to check against).

### Proving the live path

Fixture mode keeps the demo immune to a flaky network, but "we integrated with
Razorpay" is only credible if the live path has run at least once:

```bash
python -m salvage.verify_live
```

It creates a **real Test Mode Payment Link**, generates a **real model-written
Hinglish message**, exercises signature enforcement, and writes a receipt to
`data/live_verification.txt`. Anything unconfigured reports `SKIPPED` rather
than quietly passing. Signature enforcement passes with no credentials at all.

---

## Architecture

```
payment.failed webhook  (HMAC-SHA256 verified against raw body)
        │
        ▼
  DIAGNOSE ── taxonomy.py ──── 48 documented Razorpay reasons
        │      └─ diagnosis.py ──── LLM reads bank prose where rules gave up;
        │                           six proposable classes, 0.70 floor,
        │                           quarter autonomy — else stays an exception
        ▼
   SCORE ──── ml/predict.py ── calibrated propensity, customer-level split
        │
        ▼
  DECIDE ──── policy.py ────── ① hard constraints  (score not yet read)
        │                      ② eligibility       (what is even coherent)
        │                      ③ economics         (net EV, or DROP)
        ▼
 EXECUTE ──── executor.py ──── Razorpay Payment Link · Hinglish nudge
        │                      scheduled retry · ops escalation
        ▼
   AUDIT ──── db.py ────────── append-only, one row per stage per payment
        │
        ▼
  VERIFY ──── verification.py ─ settlement webhooks + a reconcile poll;
        │                       a settled payment is never actioned again
        ▼
   LEARN ──── learning.py ───── every effectiveness constant audited against
        │      bandit.py        outcomes; Beta posteriors moved only by
        │                       bounded exploration. Reported, not auto-applied.
        ▼
 MEASURE ──── evaluate.py ──── vs do-nothing and blind retry, paired
```

Full design rationale: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Running it

Requires Python 3.11+ and Node 18+. **No credentials needed** — the Razorpay
client falls back to deterministic fixtures and the LLM adapter to templates,
so the whole pipeline runs offline.

```bash
# Backend
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on Unix
pip install -r backend/requirements.txt

cd backend
python -m salvage.ml.train      # trains + calibrates, ~1 min
python -m salvage.evaluate      # prints the comparison table above
python -m pytest tests/ -q      # 49 tests

uvicorn salvage.main:app --port 8099
```

```bash
# Dashboard
cd frontend && npm install && npm run dev
# http://localhost:3000  → press "Run batch"
```

### Going live

Copy `.env.example` to `.env`. Both integrations upgrade to live calls with no
code change, and `/health` always reports which mode is active.

```bash
RAZORPAY_KEY_ID=rzp_test_xxx        # real rzp.io Payment Links
RAZORPAY_KEY_SECRET=xxx
RAZORPAY_WEBHOOK_SECRET=xxx         # enables signature enforcement

LLM_BASE_URL=https://api.groq.com/openai/v1   # any OpenAI-compatible endpoint
LLM_API_KEY=xxx                               # Groq, OpenRouter, Together, Ollama…
LLM_MODEL=llama-3.3-70b-versatile
```

---

## Repository

```
backend/salvage/
  taxonomy.py        48 documented Razorpay reasons → 9 failure classes
  diagnosis.py       LLM fallback for what the rules cannot resolve
  economics.py       action costs, effectiveness matrix, net EV, guardrails
  policy.py          the deterministic decision engine
  controls.py        merchant kill switch and review-first mode
  executor.py        turns approved decisions into real actions
  verification.py    settlement matching and reconciliation
  learning.py        audits the effectiveness matrix against outcomes
  bandit.py          Beta posteriors + bounded Thompson exploration
  pipeline.py        stage orchestration with audit at every step
  evaluate.py        paired strategy comparison
  db.py              SQLite, append-only audit trail
  main.py            FastAPI
  ml/                features · calibrated training · inference
  simulator/         event generator · counterfactual outcome oracle
  integrations/      Razorpay Payment Links · provider-agnostic LLM
backend/tests/       187 tests, guardrails first, hermetic by default
frontend/            Next.js dashboard, six views
INCIDENTS.md         engineering log — what broke, why, and the fix
```

## What broke

[`INCIDENTS.md`](INCIDENTS.md) is written as things break, not reconstructed
afterwards. Thirteen entries. The ones worth reading:

- **INC-003** — the policy engine ranked interventions by *cost*, not fitness,
  and confidently told customers their expired card had expired. One
  probability was being applied to every action, so the ranking silently
  collapsed to "cheapest wins".
- **INC-004** — a model that looked broken (AUC 0.566) was near noise-limited.
  Measuring the Bayes ceiling reframed it, and prevented hours of pointless
  tuning.
- **INC-005** — the system *measured* incremental recovery while *optimising*
  gross. Nothing errored; the two halves had simply been built to different
  definitions of value.
- **INC-007** — fixing INC-006 silently broke inference in another module,
  where a `max(x, 1e-6)` guard converted a crash into a plausible wrong number
  and inflated every expected value by 39%.

---

## Limitations

Stated plainly, because they are the first things a reviewer should ask about.

- **Data is synthetic.** No real merchant history was available. The generator
  is built specifically against circularity — outcomes are driven by latent
  variables the model never sees, and it is scored against the Bayes ceiling —
  but synthetic remains synthetic.
- **Action effectiveness starts hand-authored, and is only partly learned.**
  The matrix in `economics.py` encodes domain judgement, documented per-entry
  so a reviewer can disagree with a specific number. `bandit.py` now fits it
  from observed outcomes — but only where bounded exploration has created the
  variation that makes effectiveness identifiable at all. Under the default
  deterministic policy, exploration is off and most arms stay at their prior
  forever, correctly: the Learning view lists exactly which, rather than
  reporting a confident number for an action the system has never taken.
- **Retries are scheduled, not executed.** Razorpay's API has no
  "retry this failed payment" endpoint; genuine re-presentment requires stored
  credentials or a mandate. Payment Links are the honest recovery mechanism and
  are what the system actually creates.
- **Outcomes in the demo batch come from the simulator.** The production path
  exists and is exercised: `payment.captured`, `payment.authorized`,
  `order.paid` and `payment_link.paid` are matched back to the failed payment,
  and `reconcile()` polls for links whose webhook never arrived. The `source`
  column records which produced each outcome, so a measured recovery is never
  confusable with a simulated one — but on this batch, most are simulated.
