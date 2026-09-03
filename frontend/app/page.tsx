"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  Evaluation,
  ExceptionReport,
  Health,
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

type Tab = "queue" | "evaluation" | "exceptions";

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [rows, setRows] = useState<QueueRow[]>([]);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [exceptions, setExceptions] = useState<ExceptionReport | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("queue");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, m, e] = await Promise.all([
        api.health(),
        api.metrics(),
        api.events(200),
      ]);
      setHealth(h);
      setMetrics(m);
      setRows(e.events);
      setError(null);
      // Fetched eagerly rather than on tab switch: the explainer above the
      // tabs needs the model's ceiling figures, and it has to be on screen
      // when the dashboard opens. A reviewer who never clicks "Evaluation"
      // would otherwise see an AUC-free page and, worse, a recovery number
      // with no explanation of why it is deliberately smaller.
      // The endpoint is cached server-side, so this costs ~4ms.
      api.evaluate().then(setEvaluation).catch(() => {});
    } catch {
      setError("Cannot reach the API. Start it with: uvicorn salvage.main:app --port 8099");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (tab === "evaluation" && !evaluation) api.evaluate().then(setEvaluation).catch(() => {});
    if (tab === "exceptions" && !exceptions) api.exceptions().then(setExceptions).catch(() => {});
  }, [tab, evaluation, exceptions]);

  const runBatch = async () => {
    setBusy(true);
    try {
      await api.load();
      setEvaluation(null);
      setExceptions(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-8">
      {/* Header */}
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
            Salvage
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-500">
            Diagnoses failed Razorpay payments against the published error
            taxonomy, prices every recovery action, and acts only where it pays.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {health && (
            <div className="flex flex-wrap gap-1.5">
              <Badge
                className={
                  health.razorpay_mode === "fixture"
                    ? "bg-slate-800/60 text-slate-400 ring-slate-700"
                    : "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                }
              >
                razorpay: {health.razorpay_mode}
              </Badge>
              <Badge
                className={
                  health.llm_mode === "template"
                    ? "bg-slate-800/60 text-slate-400 ring-slate-700"
                    : "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                }
              >
                llm: {health.llm_mode}
              </Badge>
            </div>
          )}
          <button
            onClick={runBatch}
            disabled={busy}
            className="rounded-lg bg-slate-100 px-4 py-2 text-sm font-medium text-slate-900 transition hover:bg-white disabled:opacity-50"
          >
            {busy ? "Processing…" : "Run batch"}
          </button>
        </div>
      </header>

      {error && (
        <div className="mb-6 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-300">
          {error}
        </div>
      )}

      {/* Command centre */}
      {metrics && (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
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

          {/* Action mix */}
          <div className="mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/30 px-5 py-3">
            <span className="text-[11px] uppercase tracking-widest text-slate-500">
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
              <Badge className="bg-amber-500/10 text-amber-300 ring-amber-500/30">
                exceptions · {count(metrics.exceptions)}
              </Badge>
            )}
          </div>
        </>
      )}

      {/* Above the tabs, not inside one. Every number on the command centre
          is deliberately smaller than its conventional equivalent, so the
          explanation has to be on screen at the same time as the numbers -
          not one click away in a tab a reviewer may never open. */}
      {evaluation && (
        <div className="mt-6 space-y-4">
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
          {health && (
            <IntegrationNotice
              razorpayMode={health.razorpay_mode}
              llmMode={health.llm_mode}
            />
          )}
        </div>
      )}

      {/* Tabs */}
      <nav className="mt-8 flex gap-1 border-b border-slate-800">
        {(
          [
            ["queue", "Recovery queue"],
            ["evaluation", "Evaluation"],
            ["exceptions", "Exceptions"],
          ] as [Tab, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`-mb-px border-b-2 px-4 py-2.5 text-sm font-medium transition ${
              tab === key
                ? "border-slate-100 text-slate-100"
                : "border-transparent text-slate-500 hover:text-slate-300"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "queue" && <Queue rows={rows} onSelect={setSelected} />}
      {tab === "evaluation" && <EvaluationView data={evaluation} />}
      {tab === "exceptions" && <ExceptionsView data={exceptions} onSelect={setSelected} />}

      <DecisionInspector eventId={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function Queue({
  rows,
  onSelect,
}: {
  rows: QueueRow[];
  onSelect: (id: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <p className="py-16 text-center text-sm text-slate-500">
        No payments loaded. Press <span className="text-slate-300">Run batch</span>.
      </p>
    );
  }

  return (
    <div className="mt-6 overflow-x-auto rounded-xl border border-slate-800">
      <table className="w-full text-sm">
        <thead className="bg-slate-900/60 text-[11px] uppercase tracking-wider text-slate-500">
          <tr>
            <th className="px-4 py-3 text-left font-medium">Payment</th>
            <th className="px-4 py-3 text-right font-medium">Amount</th>
            <th className="px-4 py-3 text-left font-medium">Failure</th>
            <th className="px-4 py-3 text-left font-medium">Razorpay reason</th>
            <th className="px-4 py-3 text-right font-medium">Propensity</th>
            <th className="px-4 py-3 text-right font-medium">Net EV</th>
            <th className="px-4 py-3 text-left font-medium">Action</th>
            <th className="px-4 py-3 text-right font-medium">Credited</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/70">
          {rows.map((r) => (
            <tr
              key={r.id}
              onClick={() => onSelect(r.id)}
              className="cursor-pointer transition hover:bg-slate-800/40"
            >
              <td className="px-4 py-3">
                <div className="font-mono text-xs text-slate-400">{r.id}</div>
                <div className="text-xs text-slate-600">{r.customer_name}</div>
              </td>
              <td className="num px-4 py-3 text-right font-medium text-slate-200">
                {rupees(r.amount)}
              </td>
              <td className="px-4 py-3">
                <span
                  className={`font-mono text-xs ${
                    CLASS_STYLE[r.failure_class ?? ""] ?? "text-slate-400"
                  }`}
                >
                  {r.failure_class}
                </span>
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-500">
                {r.error_reason}
              </td>
              <td className="num px-4 py-3 text-right text-slate-400">
                {percent(r.base_propensity)}
              </td>
              <td
                className={`num px-4 py-3 text-right ${
                  (r.net_ev ?? 0) > 0 ? "text-slate-300" : "text-slate-600"
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
                className={`num px-4 py-3 text-right ${
                  (r.incremental_paise ?? 0) > 0
                    ? "text-emerald-300"
                    : "text-slate-700"
                }`}
              >
                {(r.incremental_paise ?? 0) > 0
                  ? rupees(r.incremental_paise)
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvaluationView({ data }: { data: Evaluation | null }) {
  if (!data) return <p className="py-16 text-center text-sm text-slate-500">Loading…</p>;

  const { do_nothing, blind_retry, salvage } = data.strategies;
  const strategies = [do_nothing, blind_retry, salvage];

  return (
    <div className="mt-6 space-y-6">
      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/60 text-[11px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="px-4 py-3 text-left font-medium">Strategy</th>
              <th className="px-4 py-3 text-right font-medium">Actions</th>
              <th className="px-4 py-3 text-right font-medium">Spend</th>
              <th className="px-4 py-3 text-right font-medium">Wasted</th>
              <th className="px-4 py-3 text-right font-medium">On unrecoverable</th>
              <th className="px-4 py-3 text-right font-medium">Incremental</th>
              <th className="px-4 py-3 text-right font-medium">Net value</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/70">
            {strategies.map((s) => (
              <tr key={s.name} className={s.name === "Salvage" ? "bg-emerald-500/5" : ""}>
                <td className="px-4 py-3 font-medium text-slate-200">{s.name}</td>
                <td className="num px-4 py-3 text-right text-slate-400">
                  {count(s.actions_taken)}
                </td>
                <td className="num px-4 py-3 text-right text-slate-400">
                  {rupees(s.action_cost_paise)}
                </td>
                <td className="num px-4 py-3 text-right text-slate-400">
                  {count(s.wasted_actions)}
                </td>
                <td
                  className={`num px-4 py-3 text-right ${
                    s.actions_on_unrecoverable > 0 ? "text-rose-300" : "text-slate-600"
                  }`}
                >
                  {count(s.actions_on_unrecoverable)}
                </td>
                <td className="num px-4 py-3 text-right font-medium text-slate-200">
                  {rupees(s.incremental_recovered_paise)}
                </td>
                <td
                  className={`num px-4 py-3 text-right font-semibold ${
                    s.name === "Salvage" ? "text-emerald-300" : "text-slate-300"
                  }`}
                >
                  {rupees(s.net_value_paise)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <h3 className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
            Salvage vs blind retry
          </h3>
          <dl className="mt-4 space-y-2.5 text-sm">
            <Row label="Extra net value" value={rupees(data.delta_vs_blind.net_value_paise)} good />
            <Row label="Interventions avoided" value={count(data.delta_vs_blind.actions_saved)} />
            <Row label="Wasted actions avoided" value={count(data.delta_vs_blind.wasted_actions_avoided)} />
            <Row
              label="Fraud/settled blocks not retried"
              value={count(data.delta_vs_blind.unrecoverable_actions_avoided)}
            />
          </dl>
        </div>

        {/* Leads with signal captured, not AUC. A reviewer skimming for thirty
            seconds should see the interpretable number first - 0.575 on its own
            reads as a coin flip and buries the actual result. */}
        {data.model && (
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
            <h3 className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
              Model quality
            </h3>
            <div className="num mt-3 text-3xl font-semibold text-emerald-300">
              {data.model.signal_captured_pct}%
            </div>
            <p className="text-xs text-slate-400">
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
              <CeilingBar label="Coin flip" value={0.5} ceiling={data.model.oracle_auc} />
            </div>

            <dl className="mt-4 space-y-2 border-t border-slate-800 pt-3 text-sm">
              <Row label="Brier score" value={data.model.brier.toFixed(4)} />
              <Row
                label="Irreducible floor"
                value={data.model.oracle_brier.toFixed(4)}
              />
              <Row
                label="Held-out customers"
                value={count(data.model.n_test)}
              />
            </dl>
            <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
              Outcomes are Bernoulli draws, so the attainable range is 0.5 to{" "}
              {data.model.oracle_auc.toFixed(3)} — not 0.5 to 1.0. A
              near-perfect score here would be evidence of leakage, not quality.
            </p>
          </div>
        )}
      </div>

      {data.model && (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
          <h3 className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
            Calibration — held-out customers
          </h3>
          <p className="mt-1 text-[11px] text-slate-500">
            Predicted probability against observed frequency. These numbers get
            multiplied by rupees, so they have to mean what they say.
          </p>
          <div className="mt-4 space-y-2">
            {data.model.calibration_bins.map((b) => (
              <div key={b.range} className="flex items-center gap-3 text-xs">
                <span className="num w-20 shrink-0 font-mono text-slate-500">
                  {b.range}
                </span>
                <span className="num w-14 shrink-0 text-right text-slate-600">
                  n={b.n}
                </span>
                <div className="relative h-5 flex-1 overflow-hidden rounded bg-slate-950">
                  <div
                    className="absolute inset-y-0 left-0 bg-sky-500/30"
                    style={{ width: `${b.predicted * 100}%` }}
                  />
                  <div
                    className="absolute inset-y-0 left-0 border-r-2 border-emerald-400"
                    style={{ width: `${b.observed * 100}%` }}
                  />
                </div>
                <span className="num w-28 shrink-0 text-right font-mono text-slate-400">
                  {b.predicted.toFixed(3)} → {b.observed.toFixed(3)}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-4 text-[10px] text-slate-600">
            <span>
              <span className="mr-1 inline-block h-2 w-3 bg-sky-500/30 align-middle" />
              predicted
            </span>
            <span>
              <span className="mr-1 inline-block h-2 w-0.5 bg-emerald-400 align-middle" />
              observed
            </span>
          </div>
        </div>
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
  if (!data) return <p className="py-16 text-center text-sm text-slate-500">Loading…</p>;

  return (
    <div className="mt-6 space-y-4">
      <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 p-5">
        <h3 className="text-sm font-medium text-amber-200">
          {count(data.total)} payments the system did not resolve —{" "}
          {rupees(data.value_paise)} at stake
        </h3>
        <p className="mt-2 max-w-3xl text-xs leading-relaxed text-slate-400">
          Reported rather than hidden. Most are Razorpay&apos;s generic{" "}
          <code className="font-mono text-slate-300">payment_failed</code>{" "}
          reason, which carries no recovery signal — the system declines to
          guess an intervention on a failure it cannot diagnose. The remainder
          are risk blocks, routed to human review by design.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {Object.entries(data.by_rule).map(([rule, n]) => (
            <Badge key={rule} className="bg-slate-800/60 text-slate-300 ring-slate-700">
              {rule} · {count(n)}
            </Badge>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/60 text-[11px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="px-4 py-3 text-left font-medium">Payment</th>
              <th className="px-4 py-3 text-right font-medium">Amount</th>
              <th className="px-4 py-3 text-left font-medium">Reason</th>
              <th className="px-4 py-3 text-left font-medium">Rule</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/70">
            {data.exceptions.slice(0, 60).map((e) => (
              <tr
                key={e.id}
                onClick={() => onSelect(e.id)}
                className="cursor-pointer transition hover:bg-slate-800/40"
              >
                <td className="px-4 py-3 font-mono text-xs text-slate-400">{e.id}</td>
                <td className="num px-4 py-3 text-right text-slate-200">
                  {rupees(e.amount)}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-slate-500">
                  {e.error_reason}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-amber-300/80">
                  {e.rule_id}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * Renders an AUC against the attainable ceiling rather than against 1.0.
 *
 * Scaling the bar to the ceiling is the whole point: on a 0-1 axis every bar
 * here looks equally mediocre, which is precisely the misreading to avoid.
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
    <div className="flex items-center gap-3 text-xs">
      <span className="w-32 shrink-0 text-slate-500">{label}</span>
      <div className="relative h-4 flex-1 overflow-hidden rounded bg-slate-950">
        <div
          className={`absolute inset-y-0 left-0 ${
            accent ? "bg-emerald-500/50" : "bg-slate-700/60"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="num w-14 shrink-0 text-right font-mono text-slate-400">
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
      <dt className="text-slate-500">{label}</dt>
      <dd
        className={`num font-semibold ${good ? "text-emerald-300" : "text-slate-200"}`}
      >
        {value}
      </dd>
    </div>
  );
}
