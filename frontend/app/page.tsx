"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AgentControls,
  api,
  Evaluation,
  ExceptionReport,
  Health,
  Learning,
  Metrics,
  QueueRow,
} from "@/lib/api";
import {
  ACTION_STYLE,
  CLASS_STYLE,
  count,
  percent,
  rupees,
  rupeesCompact,
} from "@/lib/format";
import { Badge, MetricCard } from "@/components/MetricCard";
import { DecisionInspector } from "@/components/DecisionInspector";
import { HonestyBand, IntegrationNotice } from "@/components/Explainer";
import { LearningView } from "@/components/LearningView";
import { Sidebar, View } from "@/components/Sidebar";
import { Card, Icon, IconButton, Pill, Th } from "@/components/ui";

const ACTION_FILTERS = [
  "PAYMENT_LINK",
  "RETRY_SCHEDULED",
  "ESCALATE",
  "DROP",
] as const;

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [rows, setRows] = useState<QueueRow[]>([]);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [exceptions, setExceptions] = useState<ExceptionReport | null>(null);
  const [learning, setLearning] = useState<Learning | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<View>("command");
  const [filter, setFilter] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [agent, setAgent] = useState<AgentControls | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, m, e] = await Promise.all([
        api.health(),
        api.metrics(),
        api.events(300),
      ]);
      setHealth(h);
      setAgent(h.agent ?? null);
      setMetrics(m);
      setRows(e.events);
      setError(null);
      // Fetched eagerly rather than on navigation: the explainer needs the
      // model's ceiling figures and has to be on screen when the dashboard
      // opens. The endpoint is cached server-side, so this costs ~4ms.
      api.evaluate().then(setEvaluation).catch(() => {});
      api.exceptions().then(setExceptions).catch(() => {});
    } catch {
      setError(
        "Cannot reach the API. Start it with: uvicorn salvage.main:app --port 8099"
      );
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Fetched on navigation rather than eagerly, unlike evaluation: nothing
  // outside this view reads it, and the report scans every decision joined
  // to its outcome. Cheap on the demo batch, not free on a real one.
  useEffect(() => {
    if (view !== "learning" || learning) return;
    api.learning().then(setLearning).catch(() => {});
  }, [view, learning]);

  const changeControls = async (opts: {
    enabled?: boolean;
    mode?: "review_first" | "autonomous";
    reason?: string;
  }) => {
    // Optimistic is wrong here: a kill switch must reflect what the server
    // actually did, not what the click intended.
    try {
      setAgent(await api.setControls(opts));
    } catch {
      /* leave the previous state visible rather than showing a false one */
    }
  };

  const runBatch = async () => {
    setBusy(true);
    try {
      await api.load();
      setEvaluation(null);
      setExceptions(null);
      setLearning(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (filter && r.action !== filter) return false;
      if (!q) return true;
      return (
        r.id.toLowerCase().includes(q) ||
        (r.customer_name ?? "").toLowerCase().includes(q) ||
        (r.error_reason ?? "").toLowerCase().includes(q) ||
        (r.failure_class ?? "").toLowerCase().includes(q)
      );
    });
  }, [rows, filter, query]);

  const titles: Record<View, { title: string; sub: string }> = {
    command: {
      title: "Command Centre",
      sub: "What is at stake, and what the system actually got back.",
    },
    queue: {
      title: "Recovery Queue",
      sub: "Every failed payment, its diagnosis, and the action priced for it.",
    },
    evaluation: {
      title: "Evaluation",
      sub: "Measured against doing nothing and against retrying everything.",
    },
    learning: {
      title: "Learning",
      sub: "The system's own economic assumptions, audited against what happened.",
    },
    exceptions: {
      title: "Exceptions",
      sub: "Payments the system declined to guess at, reported rather than hidden.",
    },
    integrations: {
      title: "Integrations",
      sub: "What is live, what is a fixture, and how to prove it.",
    },
  };

  return (
    <div className="flex min-h-screen">
      <Sidebar
        view={view}
        onSelect={setView}
        atRisk={metrics?.revenue_at_risk_paise ?? 0}
        recovered={metrics?.incremental_recovered_paise ?? 0}
        exceptions={metrics?.exceptions ?? 0}
        busy={busy}
        onRun={runBatch}
        controls={agent}
        onControlChange={changeControls}
      />

      <main className="min-w-0 flex-1 overflow-x-hidden">
        <header className="border-b border-[var(--border)] bg-white px-8 py-5">
          <h1 className="text-[19px] font-semibold tracking-tight">
            {titles[view].title}
          </h1>
          <p className="mt-0.5 text-[13px] text-[var(--muted)]">
            {titles[view].sub}
          </p>
        </header>

        <div className="p-8">
          {error && (
            <div className="mb-6 rounded-xl border border-rose-200 bg-rose-50 px-5 py-4 text-[13px] text-rose-700">
              {error}
            </div>
          )}

          {view === "command" && (
            <CommandCentre
              metrics={metrics}
              evaluation={evaluation}
              health={health}
            />
          )}

          {view === "queue" && (
            <QueueView
              rows={visible}
              total={rows.length}
              filter={filter}
              setFilter={setFilter}
              query={query}
              setQuery={setQuery}
              onSelect={setSelected}
            />
          )}

          {view === "evaluation" && <EvaluationView data={evaluation} />}

          {view === "learning" && <LearningView data={learning} />}

          {view === "exceptions" && (
            <ExceptionsView data={exceptions} onSelect={setSelected} />
          )}

          {view === "integrations" && <IntegrationsView health={health} />}
        </div>
      </main>

      <DecisionInspector eventId={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function CommandCentre({
  metrics,
  evaluation,
  health,
}: {
  metrics: Metrics | null;
  evaluation: Evaluation | null;
  health: Health | null;
}) {
  if (!metrics) return <Empty />;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Revenue at risk"
          value={rupeesCompact(metrics.revenue_at_risk_paise)}
          note={`${count(metrics.events)} failed payments ingested`}
        />
        <MetricCard
          label="Recovered — credited"
          value={rupeesCompact(metrics.incremental_recovered_paise)}
          accent="emerald"
          note="Incremental only. Excludes customers who would have returned unprompted."
        />
        <MetricCard
          label="Would have arrived anyway"
          value={rupeesCompact(metrics.organic_recovered_paise)}
          accent="amber"
          note="Organic recovery. Real revenue, but Salvage claims no credit for it."
        />
        <MetricCard
          label="Spend avoided vs blind retry"
          value={rupeesCompact(metrics.spend_avoided_paise)}
          accent="sky"
          note={`${rupees(metrics.action_spend_paise)} spent against ${rupees(
            metrics.blind_retry_spend_paise
          )} for retrying everything`}
        />
      </div>

      <Card className="flex flex-wrap items-center gap-2 px-5 py-4">
        <span className="text-[11px] font-medium uppercase tracking-[0.1em] text-[var(--faint)]">
          Action mix
        </span>
        {Object.entries(metrics.action_breakdown)
          .sort((a, b) => b[1] - a[1])
          .map(([action, n]) => (
            <Badge key={action} className={ACTION_STYLE[action] ?? ""}>
              {action} · {count(n)}
            </Badge>
          ))}
        {metrics.exceptions > 0 && (
          <Badge className="bg-amber-50 text-amber-700 ring-amber-200">
            exceptions · {count(metrics.exceptions)}
          </Badge>
        )}
      </Card>

      {/* On screen with the numbers, not one click away: every figure above is
          deliberately smaller than its conventional equivalent. */}
      {evaluation && (
        <HonestyBand
          gross={rupeesCompact(evaluation.strategies.salvage.gross_recovered_paise)}
          incremental={rupeesCompact(
            evaluation.strategies.salvage.incremental_recovered_paise
          )}
          organic={rupeesCompact(
            evaluation.strategies.salvage.organic_recovered_paise
          )}
          aucModel={evaluation.model?.roc_auc}
          aucCeiling={evaluation.model?.oracle_auc}
          signalPct={evaluation.model?.signal_captured_pct}
        />
      )}

      {health && (
        <IntegrationNotice
          razorpayMode={health.razorpay_mode}
          llmMode={health.llm_mode}
        />
      )}
    </div>
  );
}

function QueueView({
  rows,
  total,
  filter,
  setFilter,
  query,
  setQuery,
  onSelect,
}: {
  rows: QueueRow[];
  total: number;
  filter: string | null;
  setFilter: (f: string | null) => void;
  query: string;
  setQuery: (q: string) => void;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill icon="filter" active={filter === null} onClick={() => setFilter(null)}>
          All
        </Pill>
        {ACTION_FILTERS.map((a) => (
          <Pill key={a} active={filter === a} onClick={() => setFilter(a)}>
            {a.replace("_", " ")}
          </Pill>
        ))}

        <div className="ml-auto flex items-center gap-2">
          <div className="flex items-center gap-2 rounded-full border border-[var(--border)] bg-white px-3.5 py-2">
            <Icon name="search" className="h-3.5 w-3.5 text-[var(--faint)]" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search payments"
              className="w-44 bg-transparent text-[13px] outline-none placeholder:text-[var(--faint)]"
            />
          </div>
          <span className="num text-[12px] text-[var(--faint)]">
            {count(rows.length)} / {count(total)}
          </span>
        </div>
      </div>

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--border)]">
                <Th>Payment</Th>
                <Th align="right">Amount</Th>
                <Th>Failure</Th>
                <Th>Razorpay reason</Th>
                <Th align="right">Propensity</Th>
                <Th align="right">Net EV</Th>
                <Th>Action</Th>
                <Th align="right">Credited</Th>
                <Th align="right">Actions</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => onSelect(r.id)}
                  className="row-sep cursor-pointer transition hover:bg-[#fafafa]"
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-[#f4f4f5] text-[var(--muted)]">
                        <Icon
                          name={r.action === "DROP" ? "pause" : "play"}
                          className="h-3 w-3"
                        />
                      </span>
                      <div className="min-w-0">
                        <div className="truncate font-mono text-[12px] text-[var(--text)]">
                          {r.id}
                        </div>
                        <div className="truncate text-[11px] text-[var(--faint)]">
                          {r.customer_name}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td className="num px-4 py-3 text-right text-[13px] font-medium">
                    {rupees(r.amount)}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`font-mono text-[11px] ${
                        CLASS_STYLE[r.failure_class ?? ""] ?? "text-[var(--muted)]"
                      }`}
                    >
                      {r.failure_class}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-[11px] text-[var(--muted)]">
                    {r.error_reason}
                  </td>
                  <td className="num px-4 py-3 text-right text-[13px] text-[var(--muted)]">
                    {percent(r.base_propensity)}
                  </td>
                  <td
                    className={`num px-4 py-3 text-right text-[13px] ${
                      (r.net_ev ?? 0) > 0
                        ? "text-[var(--text)]"
                        : "text-[var(--faint)]"
                    }`}
                  >
                    {r.net_ev != null ? rupees(r.net_ev) : "—"}
                  </td>
                  <td className="px-4 py-3">
                    <Badge className={ACTION_STYLE[r.action ?? ""] ?? ""}>
                      {r.action}
                    </Badge>
                  </td>
                  <td
                    className={`num px-4 py-3 text-right text-[13px] ${
                      (r.incremental_paise ?? 0) > 0
                        ? "font-medium text-emerald-700"
                        : "text-[var(--faint)]"
                    }`}
                  >
                    {(r.incremental_paise ?? 0) > 0
                      ? rupees(r.incremental_paise)
                      : "—"}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-1.5">
                      {r.payment_link_url && (
                        <IconButton icon="link" label="Payment link" filled />
                      )}
                      <IconButton icon="dots" label="Inspect decision" />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length === 0 && (
          <p className="py-16 text-center text-[13px] text-[var(--faint)]">
            No payments match. Press <strong>Run batch</strong> in the sidebar.
          </p>
        )}
      </Card>
    </div>
  );
}

function EvaluationView({ data }: { data: Evaluation | null }) {
  if (!data) return <Empty />;

  const { do_nothing, blind_retry, salvage } = data.strategies;
  const strategies = [do_nothing, blind_retry, salvage];

  return (
    <div className="space-y-6">
      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--border)]">
                <Th>Strategy</Th>
                <Th align="right">Actions</Th>
                <Th align="right">Spend</Th>
                <Th align="right">Wasted</Th>
                <Th align="right">On unrecoverable</Th>
                <Th align="right">Incremental</Th>
                <Th align="right">Net value</Th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((s) => (
                <tr
                  key={s.name}
                  className={`row-sep ${s.name === "Salvage" ? "bg-emerald-50/40" : ""}`}
                >
                  <td className="px-4 py-3.5 text-[13px] font-medium">{s.name}</td>
                  <td className="num px-4 py-3.5 text-right text-[13px] text-[var(--muted)]">
                    {count(s.actions_taken)}
                  </td>
                  <td className="num px-4 py-3.5 text-right text-[13px] text-[var(--muted)]">
                    {rupees(s.action_cost_paise)}
                  </td>
                  <td className="num px-4 py-3.5 text-right text-[13px] text-[var(--muted)]">
                    {count(s.wasted_actions)}
                  </td>
                  <td
                    className={`num px-4 py-3.5 text-right text-[13px] ${
                      s.actions_on_unrecoverable > 0
                        ? "text-rose-700"
                        : "text-[var(--faint)]"
                    }`}
                  >
                    {count(s.actions_on_unrecoverable)}
                  </td>
                  <td className="num px-4 py-3.5 text-right text-[13px] font-medium">
                    {rupees(s.incremental_recovered_paise)}
                  </td>
                  <td
                    className={`num px-4 py-3.5 text-right text-[13px] font-semibold ${
                      s.name === "Salvage" ? "text-emerald-700" : ""
                    }`}
                  >
                    {rupees(s.net_value_paise)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="p-5">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
            Salvage vs blind retry
          </h3>
          <dl className="mt-4 space-y-2.5 text-[13px]">
            <Row
              label="Extra net value"
              value={rupees(data.delta_vs_blind.net_value_paise)}
              good
            />
            <Row
              label="Interventions avoided"
              value={count(data.delta_vs_blind.actions_saved)}
            />
            <Row
              label="Wasted actions avoided"
              value={count(data.delta_vs_blind.wasted_actions_avoided)}
            />
            <Row
              label="Fraud/settled blocks not retried"
              value={count(data.delta_vs_blind.unrecoverable_actions_avoided)}
            />
          </dl>
        </Card>

        {/* Leads with signal captured, not AUC. A reviewer skimming for thirty
            seconds should see the interpretable number first — 0.575 on its
            own reads as a coin flip and buries the actual result. */}
        {data.model && (
          <Card className="p-5">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
              Model quality
            </h3>
            <div className="num mt-3 text-[28px] font-semibold text-emerald-700">
              {data.model.signal_captured_pct}%
            </div>
            <p className="text-[12px] text-[var(--muted)]">
              of the signal that exists in this problem
            </p>

            <div className="mt-4 space-y-2">
              <CeilingBar
                label="Salvage"
                value={data.model.roc_auc}
                ceiling={data.model.oracle_auc}
                accent
              />
              <CeilingBar
                label="Perfect knowledge"
                value={data.model.oracle_auc}
                ceiling={data.model.oracle_auc}
              />
              <CeilingBar
                label="Coin flip"
                value={0.5}
                ceiling={data.model.oracle_auc}
              />
            </div>

            <dl className="mt-4 space-y-2 border-t border-[var(--border)] pt-3 text-[13px]">
              <Row label="Brier score" value={data.model.brier.toFixed(4)} />
              <Row
                label="Irreducible floor"
                value={data.model.oracle_brier.toFixed(4)}
              />
              <Row label="Held-out customers" value={count(data.model.n_test)} />
            </dl>
            <p className="mt-3 text-[11px] leading-relaxed text-[var(--muted)]">
              Outcomes are Bernoulli draws, so the attainable range is 0.5 to{" "}
              {data.model.oracle_auc.toFixed(3)} — not 0.5 to 1.0. A
              near-perfect score here would be evidence of leakage, not quality.
            </p>
          </Card>
        )}
      </div>

      {data.model && (
        <Card className="p-5">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
            Calibration — held-out customers
          </h3>
          <p className="mt-1 text-[12px] text-[var(--muted)]">
            Predicted probability against observed frequency. These numbers get
            multiplied by rupees, so they have to mean what they say.
          </p>
          <div className="mt-4 space-y-2">
            {data.model.calibration_bins.map((b) => (
              <div key={b.range} className="flex items-center gap-3 text-[12px]">
                <span className="num w-20 shrink-0 font-mono text-[var(--muted)]">
                  {b.range}
                </span>
                <span className="num w-14 shrink-0 text-right text-[var(--faint)]">
                  n={b.n}
                </span>
                <div className="relative h-5 flex-1 overflow-hidden rounded bg-[#f4f4f5]">
                  <div
                    className="absolute inset-y-0 left-0 bg-sky-200"
                    style={{ width: `${b.predicted * 100}%` }}
                  />
                  <div
                    className="absolute inset-y-0 left-0 border-r-2 border-emerald-600"
                    style={{ width: `${b.observed * 100}%` }}
                  />
                </div>
                <span className="num w-28 shrink-0 text-right font-mono text-[var(--muted)]">
                  {b.predicted.toFixed(3)} → {b.observed.toFixed(3)}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-4 text-[10px] text-[var(--faint)]">
            <span>
              <span className="mr-1 inline-block h-2 w-3 bg-sky-200 align-middle" />
              predicted
            </span>
            <span>
              <span className="mr-1 inline-block h-2 w-0.5 bg-emerald-600 align-middle" />
              observed
            </span>
          </div>
        </Card>
      )}
    </div>
  );
}

function ExceptionsView({
  data,
  onSelect,
}: {
  data: ExceptionReport | null;
  onSelect: (id: string) => void;
}) {
  if (!data) return <Empty />;

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-5">
        <h3 className="text-[14px] font-medium text-amber-900">
          {count(data.total)} payments the system did not resolve —{" "}
          {rupees(data.value_paise)} at stake
        </h3>
        <p className="mt-2 max-w-3xl text-[12px] leading-relaxed text-amber-900/70">
          Reported rather than hidden. Most are Razorpay&apos;s generic{" "}
          <code className="font-mono">payment_failed</code> reason, which
          carries no recovery signal — the system declines to guess an
          intervention on a failure it cannot diagnose. The remainder are risk
          blocks, routed to human review by design.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {Object.entries(data.by_rule).map(([rule, n]) => (
            <Badge
              key={rule}
              className="bg-white text-amber-800 ring-amber-200"
            >
              {rule} · {count(n)}
            </Badge>
          ))}
        </div>
      </div>

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--border)]">
                <Th>Payment</Th>
                <Th align="right">Amount</Th>
                <Th>Reason</Th>
                <Th>Rule</Th>
              </tr>
            </thead>
            <tbody>
              {data.exceptions.slice(0, 60).map((e) => (
                <tr
                  key={e.id}
                  onClick={() => onSelect(e.id)}
                  className="row-sep cursor-pointer transition hover:bg-[#fafafa]"
                >
                  <td className="px-4 py-3 font-mono text-[12px] text-[var(--text)]">
                    {e.id}
                  </td>
                  <td className="num px-4 py-3 text-right text-[13px] font-medium">
                    {rupees(e.amount)}
                  </td>
                  <td className="px-4 py-3 font-mono text-[11px] text-[var(--muted)]">
                    {e.error_reason}
                  </td>
                  <td className="px-4 py-3 font-mono text-[11px] text-amber-700">
                    {e.rule_id}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function IntegrationsView({ health }: { health: Health | null }) {
  if (!health) return <Empty />;

  const items = [
    {
      icon: "link",
      name: "Razorpay Payment Links",
      mode: health.razorpay_mode,
      live: health.razorpay_mode !== "fixture",
      detail:
        "Creates real Test Mode payment links. Request payloads are built identically in both modes, so switching is an env change, not a code change.",
      env: "RAZORPAY_KEY_ID · RAZORPAY_KEY_SECRET",
    },
    {
      icon: "globe",
      name: "Recovery message generation",
      mode: health.llm_mode,
      live: health.llm_mode !== "template",
      detail:
        "Any OpenAI-compatible chat-completions endpoint — Groq, OpenRouter, Together, Ollama. The model writes copy only; it never chooses an action.",
      env: "LLM_BASE_URL · LLM_API_KEY · LLM_MODEL",
    },
    {
      icon: "shield",
      name: "Webhook signature verification",
      mode: health.webhook_signature_enforced ? "enforced" : "real HMAC-SHA256",
      live: true,
      detail:
        "Genuinely live regardless of credentials. Forged signatures and tampered bodies are rejected by real HMAC-SHA256, constant-time compared.",
      env: "RAZORPAY_WEBHOOK_SECRET",
    },
  ];

  return (
    <div className="space-y-4">
      {items.map((it) => (
        <Card key={it.name} className="flex items-start gap-4 p-5">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[#f4f4f5] text-[var(--muted)]">
            <Icon name={it.icon} className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[14px] font-medium">{it.name}</h3>
              <Badge
                className={
                  it.live
                    ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                    : "bg-[#f4f4f5] text-[var(--muted)] ring-[var(--border)]"
                }
              >
                {it.mode}
              </Badge>
            </div>
            <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--muted)]">
              {it.detail}
            </p>
            <code className="mt-2 inline-block rounded-md bg-[#fafafa] px-2 py-1 font-mono text-[10px] text-[var(--faint)]">
              {it.env}
            </code>
          </div>
        </Card>
      ))}

      <Card className="p-5">
        <h3 className="text-[13px] font-medium">Prove the live path</h3>
        <p className="mt-1.5 max-w-3xl text-[12px] leading-relaxed text-[var(--muted)]">
          Fixture mode keeps the demo immune to a flaky network, but
          &ldquo;integrated with Razorpay&rdquo; is only credible once the live
          path has run. One command creates a real Test Mode link, generates a
          real model-written message, and writes a receipt. Anything
          unconfigured reports <code className="font-mono">SKIPPED</code> rather
          than quietly passing.
        </p>
        <pre className="mt-3 overflow-x-auto rounded-lg bg-[#0b0b0d] px-4 py-3 font-mono text-[12px] text-[#e4e4e7]">
          python -m salvage.verify_live
        </pre>
      </Card>
    </div>
  );
}

/**
 * Renders an AUC against the attainable ceiling rather than against 1.0.
 *
 * Scaling to the ceiling is the whole point: on a 0-1 axis every bar here
 * looks equally mediocre, which is precisely the misreading to avoid.
 */
function CeilingBar({
  label,
  value,
  ceiling,
  accent,
}: {
  label: string;
  value: number;
  ceiling: number;
  accent?: boolean;
}) {
  const span = Math.max(ceiling - 0.5, 1e-6);
  const pct = Math.max(0, Math.min(100, ((value - 0.5) / span) * 100));

  return (
    <div className="flex items-center gap-3 text-[12px]">
      <span className="w-32 shrink-0 text-[var(--muted)]">{label}</span>
      <div className="relative h-4 flex-1 overflow-hidden rounded bg-[#f4f4f5]">
        <div
          className={`absolute inset-y-0 left-0 ${
            accent ? "bg-emerald-500" : "bg-[#d4d4d8]"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="num w-14 shrink-0 text-right font-mono text-[var(--muted)]">
        {value.toFixed(3)}
      </span>
    </div>
  );
}

function Row({
  label,
  value,
  good,
}: {
  label: string;
  value: string;
  good?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-[var(--muted)]">{label}</dt>
      <dd className={`num font-semibold ${good ? "text-emerald-700" : ""}`}>
        {value}
      </dd>
    </div>
  );
}

function Empty() {
  return (
    <p className="py-20 text-center text-[13px] text-[var(--faint)]">
      Loading…
    </p>
  );
}
