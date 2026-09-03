"use client";

import { useEffect, useState } from "react";
import { api, EventDetail } from "@/lib/api";
import { ACTION_STYLE, CLASS_STYLE, count, percent, rupees } from "@/lib/format";
import { Badge } from "./MetricCard";

/**
 * Slide-out panel answering "why did the system do that?" for one payment.
 *
 * The panel deliberately shows the rejected alternatives alongside the chosen
 * action. A dashboard that displays only the winner asks to be trusted; showing
 * the arithmetic that ruled out every other option lets a reviewer check the
 * decision instead. This is also the view that caught INC-003 during
 * development.
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

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      <aside className="relative flex h-full w-full max-w-2xl flex-col overflow-y-auto border-l border-slate-800 bg-[#0b1120] shadow-2xl">
        <header className="sticky top-0 z-10 flex items-start justify-between border-b border-slate-800 bg-[#0b1120]/95 px-6 py-5 backdrop-blur">
          <div>
            <div className="font-mono text-xs text-slate-500">{eventId}</div>
            <div className="num mt-1 text-2xl font-semibold text-slate-100">
              {ev ? rupees(ev.amount, 2) : "—"}
            </div>
            {ev && (
              <div className="mt-1 text-xs text-slate-500">
                {ev.customer_name} · {ev.method?.toUpperCase()}
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-400 transition hover:bg-slate-800 hover:text-slate-200"
          >
            Close
          </button>
        </header>

        {loading && (
          <div className="p-6 text-sm text-slate-500">Loading decision…</div>
        )}

        {d && (
          <div className="space-y-6 p-6">
            {/* 1. Diagnosis, traceable to Razorpay's own documentation. */}
            <section>
              <SectionTitle>1 · Diagnosis</SectionTitle>
              <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                <div className="flex items-center gap-2">
                  <span
                    className={`font-mono text-sm font-semibold ${
                      CLASS_STYLE[d.failure_class] ?? "text-slate-300"
                    }`}
                  >
                    {d.failure_class}
                  </span>
                  {d.diagnosis_confident ? (
                    <Badge className="bg-emerald-500/10 text-emerald-300 ring-emerald-500/30">
                      confident
                    </Badge>
                  ) : (
                    <Badge className="bg-amber-500/10 text-amber-300 ring-amber-500/30">
                      exception
                    </Badge>
                  )}
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                  <Field label="error_reason" value={ev?.error_reason} mono />
                  <Field label="error_code" value={ev?.error_code} mono />
                  <Field label="error_source" value={ev?.error_source} mono />
                  <Field label="error_step" value={ev?.error_step} mono />
                </dl>
                <p className="mt-3 text-xs leading-relaxed text-slate-400">
                  {d.diagnosis_note}
                </p>
              </div>
            </section>

            {/* 2. The arithmetic, shown rather than asserted. */}
            <section>
              <SectionTitle>2 · The maths</SectionTitle>
              <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
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
                <div className="num mt-4 rounded-lg bg-slate-950/60 p-3 font-mono text-[11px] leading-relaxed text-slate-400">
                  {rupees(ev?.amount, 2)} × lift ={" "}
                  {rupees(d.gross_expected, 2)}
                  <br />
                  − {rupees(d.mdr, 2)} MDR − {rupees(d.action_cost, 2)} action
                  cost
                  <br />
                  <span className="text-emerald-300">
                    = {rupees(d.net_ev, 2)} net expected value
                  </span>
                </div>
              </div>
            </section>

            {/* 3. What was rejected, and by how much. */}
            {d.considered.length > 0 && (
              <section>
                <SectionTitle>3 · Alternatives considered</SectionTitle>
                <div className="overflow-hidden rounded-xl border border-slate-800">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-900/60 text-slate-500">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium">action</th>
                        <th className="px-3 py-2 text-right font-medium">fit</th>
                        <th className="px-3 py-2 text-right font-medium">cost</th>
                        <th className="px-3 py-2 text-right font-medium">net EV</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/70">
                      {d.considered.map((c, i) => (
                        <tr
                          key={c.action}
                          className={i === 0 ? "bg-emerald-500/5" : ""}
                        >
                          <td className="px-3 py-2 font-mono text-slate-300">
                            {c.action}
                            {i === 0 && (
                              <span className="ml-2 text-[10px] text-emerald-400">
                                chosen
                              </span>
                            )}
                          </td>
                          <td className="num px-3 py-2 text-right text-slate-400">
                            {percent(c.effectiveness, 0)}
                          </td>
                          <td className="num px-3 py-2 text-right text-slate-400">
                            {rupees(c.cost, 2)}
                          </td>
                          <td
                            className={`num px-3 py-2 text-right ${
                              c.net_ev > 0 ? "text-emerald-300" : "text-rose-300"
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
              <SectionTitle>4 · Decision</SectionTitle>
              <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                <div className="flex items-center gap-2">
                  <Badge className={ACTION_STYLE[d.action] ?? ""}>
                    {d.action}
                  </Badge>
                  <span className="font-mono text-[11px] text-slate-500">
                    {d.rule_id}
                  </span>
                </div>
                <p className="mt-3 text-xs leading-relaxed text-slate-300">
                  {d.rationale}
                </p>
                {d.constraints.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {d.constraints.map((c) => (
                      <Badge
                        key={c}
                        className="bg-slate-800/60 text-slate-400 ring-slate-700"
                      >
                        ✓ {c}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            </section>

            {/* 5. What was actually executed. */}
            {ex && (
              <section>
                <SectionTitle>5 · Execution</SectionTitle>
                <div className="space-y-3 rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-slate-500">status</span>
                    <span className="font-mono text-slate-300">{ex.status}</span>
                    <span className="text-slate-600">·</span>
                    <span className="font-mono text-slate-500">
                      {ex.provider}
                    </span>
                  </div>
                  {ex.payment_link_url && (
                    <a
                      href={ex.payment_link_url}
                      target="_blank"
                      rel="noreferrer"
                      className="block break-all rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 font-mono text-xs text-emerald-300 transition hover:bg-emerald-500/20"
                    >
                      {ex.payment_link_url}
                    </a>
                  )}
                  {ex.message_text && (
                    <div className="rounded-lg bg-[#075e54]/20 p-3">
                      <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
                        WhatsApp · generated
                      </div>
                      <p className="text-xs leading-relaxed text-slate-200">
                        {ex.message_text}
                      </p>
                    </div>
                  )}
                  {ex.scheduled_for && (
                    <div className="text-xs text-slate-400">
                      Retry scheduled for{" "}
                      <span className="font-mono text-slate-300">
                        {new Date(ex.scheduled_for).toLocaleString("en-IN")}
                      </span>
                    </div>
                  )}
                </div>
              </section>
            )}

            {/* 6. The measured result, with credit honestly assigned. */}
            {out && (
              <section>
                <SectionTitle>6 · Measured outcome</SectionTitle>
                <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
                  <div className="grid grid-cols-2 gap-3 text-xs">
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
                    <p className="mt-3 rounded-lg bg-amber-500/10 p-2 text-[11px] leading-relaxed text-amber-300/90">
                      This customer would have returned unprompted. The revenue
                      arrived, but Salvage claims no credit for it.
                    </p>
                  )}
                </div>
              </section>
            )}

            {/* 7. The immutable trail. */}
            {detail && detail.audit_trail.length > 0 && (
              <section>
                <SectionTitle>7 · Audit trail</SectionTitle>
                <ol className="space-y-2">
                  {detail.audit_trail.map((row) => (
                    <li
                      key={row.id}
                      className="flex gap-3 rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-2"
                    >
                      <span className="num shrink-0 font-mono text-[10px] text-slate-600">
                        {new Date(row.timestamp).toLocaleTimeString("en-IN", {
                          hour12: false,
                        })}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] font-semibold text-slate-400">
                        {row.stage}
                      </span>
                      <span className="text-[11px] leading-relaxed text-slate-400">
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

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-widest text-slate-500">
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
      <div className="text-[10px] uppercase tracking-wider text-slate-600">
        {label}
      </div>
      <div
        className={`num mt-0.5 font-semibold ${
          accent === "emerald" ? "text-emerald-300" : "text-slate-200"
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
      <dt className="text-[10px] uppercase tracking-wider text-slate-600">
        {label}
      </dt>
      <dd className={`text-slate-300 ${mono ? "font-mono" : ""}`}>
        {value ?? "—"}
      </dd>
    </div>
  );
}
