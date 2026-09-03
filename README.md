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
See [Honest accounting](#honest-accounting).

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

## Architecture

```
payment.failed webhook  (HMAC-SHA256 verified against raw body)
        │
        ▼
  DIAGNOSE ── taxonomy.py ──── 48 documented Razorpay reasons
        │                      undiagnosable → exception list, never guessed
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
   AUDIT ──── db.py ────────── append-only, six rows per payment
        │
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
  economics.py       action costs, effectiveness matrix, net EV, guardrails
  policy.py          the deterministic decision engine
  executor.py        turns approved decisions into real actions
  pipeline.py        six-stage orchestration with audit at every step
  evaluate.py        paired strategy comparison
  db.py              SQLite, append-only audit trail
  main.py            FastAPI
  ml/                features · calibrated training · inference
  simulator/         event generator · counterfactual outcome oracle
  integrations/      Razorpay Payment Links · provider-agnostic LLM
backend/tests/       49 tests, guardrails first
frontend/            Next.js dashboard, four views
INCIDENTS.md         engineering log — what broke, why, and the fix
```

## What broke

[`INCIDENTS.md`](INCIDENTS.md) is written as things break, not reconstructed
afterwards. Seven entries. The ones worth reading:

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
- **Action effectiveness is hand-authored.** The matrix in `economics.py`
  encodes domain judgement, not learned parameters. It is documented per-entry
  so a reviewer can disagree with a specific number. In production these would
  be fitted from holdout experiments.
- **Retries are scheduled, not executed.** Razorpay's API has no
  "retry this failed payment" endpoint; genuine re-presentment requires stored
  credentials or a mandate. Payment Links are the honest recovery mechanism and
  are what the system actually creates.
- **Outcomes come from the simulator.** In production these arrive as
  `payment.captured` webhooks. The `source` column records which, so a measured
  outcome is never confusable with a simulated one.
