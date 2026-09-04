"use client";

import { useEffect, useState } from "react";
import { api, EventDetail } from "@/lib/api";
import { ACTION_STYLE, CLASS_STYLE, percent, rupees } from "@/lib/format";
import { Badge } from "./MetricCard";
import { Icon } from "./ui";

/**
 * Slide-out panel answering "why did the system do that?" for one payment.
 *
 * The panel deliberately shows the rejected alternatives alongside the chosen
 * action. A dashboard that displays only the winner asks to be trusted;
 * showing the arithmetic that ruled out every other option lets a reviewer
 * check the decision instead. This is also the view that caught INC-003
 * during development.
 */
export function DecisionInspector({
  eventId,
  onClose,
}: {
  eventId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<EventDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!eventId) {
      setDetail(null);
      return;
    }
    setLoading(true);
    api
      .eventDetail(eventId)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [eventId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!eventId) return null;

  const d = detail?.decision;
  const ev = detail?.event;
  const ex = detail?.execution;
  const out = detail?.outcome;

  // Hard-constraint decisions - a risk block, an opt-out, a high-value
  // escalation - are resolved before any valuation is computed, so there is no
  // arithmetic to show. Rendering the panel anyway produced a row of em-dashes
  // that looked like a data bug rather than a deliberate refusal to price
  // something the guardrails already settled.
  const hasMaths = d != null && d.net_ev != null;

  // Sections are numbered as they render. Fixed numbers skipped a step
  // whenever a section was conditionally absent, so the panel read 1, 2, 4.
  let step = 0;
  const n = () => ++step;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/20" onClick={onClose} />
      <aside className="relative flex h-full w-full max-w-2xl flex-col overflow-y-auto border-l border-[var(--border)] bg-white shadow-2xl">
        <header className="sticky top-0 z-10 flex items-start justify-between border-b border-[var(--border)] bg-white/95 px-6 py-5 backdrop-blur">
          <div>
            <div className="font-mono text-[11px] text-[var(--faint)]">
              {eventId}
            </div>
            <div className="num mt-1 text-[22px] font-semibold">
              {ev ? rupees(ev.amount, 2) : "—"}
            </div>
            {ev && (
              <div className="mt-0.5 text-[12px] text-[var(--muted)]">
                {ev.customer_name} · {ev.method?.toUpperCase()}
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-[12px] text-[var(--muted)] transition hover:bg-[#fafafa]"
          >
            Close
          </button>
        </header>

        {loading && (
          <div className="p-6 text-[13px] text-[var(--faint)]">
            Loading decision…
          </div>
        )}

        {d && (
          <div className="space-y-6 p-6">
            {/* 1. Diagnosis, traceable to Razorpay's own documentation. */}
            <section>
              <SectionTitle>{n()} · Diagnosis</SectionTitle>
              <Panel>
                <div className="flex items-center gap-2">
                  <span
                    className={`font-mono text-[13px] font-semibold ${
                      CLASS_STYLE[d.failure_class] ?? "text-[var(--text)]"
                    }`}
                  >
                    {d.failure_class}
                  </span>
                  {d.diagnosis_confident ? (
                    <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">
                      confident
                    </Badge>
                  ) : (
                    <Badge className="bg-amber-50 text-amber-700 ring-amber-200">
                      exception
                    </Badge>
                  )}
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[12px]">
                  <Field label="error_reason" value={ev?.error_reason} mono />
                  <Field label="error_code" value={ev?.error_code} mono />
                  <Field label="error_source" value={ev?.error_source} mono />
                  <Field label="error_step" value={ev?.error_step} mono />
                </dl>
                <p className="mt-3 text-[12px] leading-relaxed text-[var(--muted)]">
                  {d.diagnosis_note}
                </p>
              </Panel>
            </section>

            {/* The arithmetic, shown rather than asserted - and omitted
                entirely when a guardrail settled the decision before any
                pricing happened. */}
            {hasMaths && (
            <section>
              <SectionTitle>{n()} · The maths</SectionTitle>
              <Panel>
                <div className="grid grid-cols-2 gap-3 text-[12px] sm:grid-cols-4">
                  <Stat label="propensity" value={percent(d.base_propensity)} />
                  <Stat
                    label="P(recovery|action)"
                    value={percent(d.action_probability)}
                  />
                  <Stat
                    label="incremental"
                    value={rupees(d.gross_expected, 2)}
                  />
                  <Stat
                    label="net EV"
                    value={rupees(d.net_ev, 2)}
                    accent="emerald"
                  />
                </div>
                <div className="num mt-4 rounded-lg border border-[var(--border)] bg-[#fbfbfc] p-3 font-mono text-[11px] leading-relaxed text-[var(--muted)]">
                  {rupees(ev?.amount, 2)} × lift = {rupees(d.gross_expected, 2)}
                  <br />− {rupees(d.mdr, 2)} MDR − {rupees(d.action_cost, 2)}{" "}
                  action cost
                  <br />
                  <span className="font-semibold text-emerald-700">
                    = {rupees(d.net_ev, 2)} net expected value
                  </span>
                </div>
              </Panel>
            </section>
            )}

            {/* What was rejected, and by how much. */}
            {d.considered.length > 0 && (
              <section>
                <SectionTitle>{n()} · Alternatives considered</SectionTitle>
                <div className="overflow-hidden rounded-xl border border-[var(--border)]">
                  <table className="w-full text-[12px]">
                    <thead className="border-b border-[var(--border)] text-[var(--faint)]">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">
                          action
                        </th>
                        <th className="px-3 py-2 text-right font-medium">fit</th>
                        <th className="px-3 py-2 text-right font-medium">
                          cost
                        </th>
                        <th className="px-3 py-2 text-right font-medium">
                          net EV
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.considered.map((c, i) => (
                        <tr
                          key={c.action}
                          className={`row-sep ${
                            i === 0 ? "bg-emerald-50/50" : ""
                          }`}
                        >
                          <td className="px-3 py-2 font-mono">
                            {c.action}
                            {i === 0 && (
                              <span className="ml-2 text-[10px] text-emerald-700">
                                chosen
                              </span>
                            )}
                          </td>
                          <td className="num px-3 py-2 text-right text-[var(--muted)]">
                            {percent(c.effectiveness, 0)}
                          </td>
                          <td className="num px-3 py-2 text-right text-[var(--muted)]">
                            {rupees(c.cost, 2)}
                          </td>
                          <td
                            className={`num px-3 py-2 text-right font-medium ${
                              c.net_ev > 0 ? "text-emerald-700" : "text-rose-700"
                            }`}
                          >
                            {rupees(c.net_ev, 2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            {/* 4. The rule that fired, named. */}
            <section>
              <SectionTitle>{n()} · Decision</SectionTitle>
              <Panel>
                <div className="flex items-center gap-2">
                  <Badge className={ACTION_STYLE[d.action] ?? ""}>
                    {d.action}
                  </Badge>
                  <span className="font-mono text-[11px] text-[var(--faint)]">
                    {d.rule_id}
                  </span>
                </div>
                <p className="mt-3 text-[12px] leading-relaxed text-[var(--text)]">
                  {d.rationale}
                </p>
                {d.constraints.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {d.constraints.map((c) => (
                      <Badge
                        key={c}
                        className="bg-[#f4f4f5] text-[var(--muted)] ring-[var(--border)]"
                      >
                        ✓ {c}
                      </Badge>
                    ))}
                  </div>
                )}
              </Panel>
            </section>

            {/* 5. What was actually executed. */}
            {ex && (
              <section>
                <SectionTitle>{n()} · Execution</SectionTitle>
                <Panel className="space-y-3">
                  <div className="flex items-center gap-2 text-[12px]">
                    <span className="text-[var(--faint)]">status</span>
                    <span className="font-mono text-[var(--text)]">
                      {ex.status}
                    </span>
                    <span className="text-[var(--faint)]">·</span>
                    <span className="font-mono text-[var(--muted)]">
                      {ex.provider}
                    </span>
                  </div>
                  {ex.payment_link_url && (
                    <a
                      href={ex.payment_link_url}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-2 break-all rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 font-mono text-[12px] text-emerald-800 transition hover:bg-emerald-100"
                    >
                      <Icon name="link" className="h-3.5 w-3.5 shrink-0" />
                      {ex.payment_link_url}
                    </a>
                  )}
                  {ex.message_text && (
                    <div className="rounded-lg border border-[var(--border)] bg-[#f0f7f2] p-3">
                      <div className="mb-1 text-[10px] uppercase tracking-wider text-[var(--faint)]">
                        WhatsApp · generated
                      </div>
                      <p className="text-[12px] leading-relaxed text-[var(--text)]">
                        {ex.message_text}
                      </p>
                    </div>
                  )}
                  {ex.scheduled_for && (
                    <div className="text-[12px] text-[var(--muted)]">
                      Retry scheduled for{" "}
                      <span className="font-mono text-[var(--text)]">
                        {new Date(ex.scheduled_for).toLocaleString("en-IN")}
                      </span>
                    </div>
                  )}
                </Panel>
              </section>
            )}

            {/* 6. The measured result, with credit honestly assigned. */}
            {out && (
              <section>
                <SectionTitle>{n()} · Measured outcome</SectionTitle>
                <Panel>
                  <div className="grid grid-cols-2 gap-3 text-[12px]">
                    <Stat
                      label="recovered"
                      value={out.recovered ? rupees(out.recovered_paise) : "no"}
                      accent={out.recovered ? "emerald" : undefined}
                    />
                    <Stat
                      label="credited to Salvage"
                      value={rupees(out.incremental_paise)}
                      accent={out.incremental_paise > 0 ? "emerald" : undefined}
                    />
                  </div>
                  {out.organic === 1 && (
                    <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[11px] leading-relaxed text-amber-900">
                      This customer would have returned unprompted. The revenue
                      arrived, but Salvage claims no credit for it.
                    </p>
                  )}
                </Panel>
              </section>
            )}

            {/* 7. The immutable trail. */}
            {detail && detail.audit_trail.length > 0 && (
              <section>
                <SectionTitle>{n()} · Audit trail</SectionTitle>
                <ol className="space-y-2">
                  {detail.audit_trail.map((row) => (
                    <li
                      key={row.id}
                      className="flex gap-3 rounded-lg border border-[var(--border)] bg-white px-3 py-2"
                    >
                      <span className="num shrink-0 font-mono text-[10px] text-[var(--faint)]">
                        {new Date(row.timestamp).toLocaleTimeString("en-IN", {
                          hour12: false,
                        })}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] font-semibold text-[var(--muted)]">
                        {row.stage}
                      </span>
                      <span className="text-[11px] leading-relaxed text-[var(--muted)]">
                        {row.summary}
                      </span>
                    </li>
                  ))}
                </ol>
              </section>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}

function Panel({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-xl border border-[var(--border)] bg-white p-4 ${className}`}
    >
      {children}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
      {children}
    </h3>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "emerald";
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--faint)]">
        {label}
      </div>
      <div
        className={`num mt-0.5 font-semibold ${
          accent === "emerald" ? "text-emerald-700" : "text-[var(--text)]"
        }`}
      >
        {value}
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: any;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-[var(--faint)]">
        {label}
      </dt>
      <dd className={`text-[var(--text)] ${mono ? "font-mono" : ""}`}>
        {value ?? "—"}
      </dd>
    </div>
  );
}
