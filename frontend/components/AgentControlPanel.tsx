"use client";

import { AgentControls } from "@/lib/api";
import { Icon } from "./ui";

/**
 * The merchant's authority over the agent, in the sidebar rather than buried
 * in a settings page.
 *
 * Razorpay's published position names an "immediate kill switch" among the
 * controls a merchant must always hold. A switch is only immediate if it is
 * reachable at the moment you need it - mid-incident, from whatever screen you
 * happen to be on. Behind navigation it would be a setting, not a control.
 *
 * Kept deliberately compact. The first version carried a mode toggle, a kill
 * button and an explanatory paragraph, which came to roughly 320px - enough to
 * push half the navigation below the fold on a laptop viewport. A control that
 * costs you the rest of the interface is the wrong trade, so the explanation
 * moved to the Integrations view and what remains is the state, the switch,
 * and one line of consequence.
 */
export function AgentControlPanel({
  controls,
  onChange,
  busy,
}: {
  controls: AgentControls | null;
  onChange: (opts: {
    enabled?: boolean;
    mode?: "review_first" | "autonomous";
    reason?: string;
  }) => void;
  busy?: boolean;
}) {
  if (!controls) return null;

  const killed = !controls.enabled;
  const reviewing = controls.enabled && controls.mode === "review_first";
  const live = controls.executes;

  const dot = killed
    ? "bg-rose-500"
    : reviewing
      ? "bg-amber-500"
      : "bg-emerald-500";

  return (
    <div className="rounded-xl border border-[var(--border)] bg-white px-3 py-2.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--faint)]">
          Agent
        </span>
        <span className="flex items-center gap-1.5 text-[11px] font-medium">
          <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
          {killed ? "Disabled" : reviewing ? "Review-first" : "Autonomous"}
        </span>
      </div>

      {/* Segmented control: the two modes are a single choice, not two
          buttons, so they read as mutually exclusive at a glance. */}
      <div className="mt-2 flex rounded-lg border border-[var(--border)] p-0.5">
        <button
          disabled={busy || killed}
          onClick={() => onChange({ enabled: true, mode: "review_first" })}
          title="Decide and record everything; execute nothing"
          className={`flex-1 rounded-md px-2 py-1 text-[11px] font-medium transition disabled:opacity-40 ${
            reviewing
              ? "bg-[var(--ink)] text-white"
              : "text-[var(--muted)] hover:bg-[#fafafa]"
          }`}
        >
          Review
        </button>
        <button
          disabled={busy || killed}
          onClick={() => onChange({ enabled: true, mode: "autonomous" })}
          title="Execute approved decisions within the merchant guardrails"
          className={`flex-1 rounded-md px-2 py-1 text-[11px] font-medium transition disabled:opacity-40 ${
            live
              ? "bg-[var(--ink)] text-white"
              : "text-[var(--muted)] hover:bg-[#fafafa]"
          }`}
        >
          Auto
        </button>
      </div>

      <button
        disabled={busy}
        onClick={() =>
          killed
            ? onChange({ enabled: true })
            : onChange({
                enabled: false,
                reason: "Kill switch used from the dashboard",
              })
        }
        className={`mt-1.5 flex w-full items-center justify-center gap-1.5 rounded-lg border px-2 py-1.5 text-[11px] font-medium transition disabled:opacity-50 ${
          killed
            ? "border-transparent bg-rose-600 text-white hover:bg-rose-700"
            : "border-rose-200 bg-white text-rose-700 hover:bg-rose-50"
        }`}
      >
        <Icon name="pause" className="h-3 w-3" />
        {killed ? "Re-enable" : "Kill switch"}
      </button>

      <p className="mt-1.5 text-[10px] leading-snug text-[var(--faint)]">
        {killed
          ? "No action of any kind. Payments still ingested and diagnosed."
          : reviewing
            ? "Records the full trail. Executes nothing."
            : "Acts within guardrails. Risk blocks and opt-outs still apply."}
      </p>
    </div>
  );
}
