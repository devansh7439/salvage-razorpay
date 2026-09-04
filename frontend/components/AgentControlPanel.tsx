"use client";

import { AgentControls } from "@/lib/api";
import { Icon } from "./ui";

/**
 * The merchant's authority over the agent, in the sidebar rather than buried
 * in a settings page.
 *
 * Razorpay's published position names an "immediate kill switch" among the
 * controls a merchant must always hold. A switch is only immediate if it is
 * reachable at the moment you need it — which is mid-incident, from whatever
 * screen you happen to be on. Putting it behind navigation would make it a
 * setting, not a control.
 *
 * The three states are deliberately distinct rather than a single on/off:
 * review-first is not a degraded autonomous mode, it is the posture a merchant
 * runs on day one, where the agent produces its full decision trail and
 * executes none of it.
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

  const label = killed
    ? "Disabled"
    : reviewing
      ? "Review-first"
      : "Autonomous";

  return (
    <div className="rounded-xl border border-[var(--border)] bg-white p-4">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--faint)]">
          Agent authority
        </span>
        <span className="flex items-center gap-1.5 text-[11px] font-medium">
          <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
          {label}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-1.5">
        <button
          disabled={busy}
          onClick={() =>
            onChange({ enabled: true, mode: "review_first" })
          }
          className={`rounded-lg border px-2 py-1.5 text-[11px] font-medium transition disabled:opacity-50 ${
            reviewing
              ? "border-transparent bg-[var(--ink)] text-white"
              : "border-[var(--border)] bg-white text-[var(--muted)] hover:bg-[#fafafa]"
          }`}
        >
          Review-first
        </button>
        <button
          disabled={busy}
          onClick={() => onChange({ enabled: true, mode: "autonomous" })}
          className={`rounded-lg border px-2 py-1.5 text-[11px] font-medium transition disabled:opacity-50 ${
            live
              ? "border-transparent bg-[var(--ink)] text-white"
              : "border-[var(--border)] bg-white text-[var(--muted)] hover:bg-[#fafafa]"
          }`}
        >
          Autonomous
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
        {killed ? "Re-enable agent" : "Kill switch"}
      </button>

      <p className="mt-2.5 text-[10px] leading-relaxed text-[var(--faint)]">
        {killed ? (
          <>
            No action of any kind. Payments are still ingested and diagnosed, so
            you can see what is arriving.
            {controls.disabled_reason && (
              <span className="block mt-1 text-rose-600">
                {controls.disabled_reason}
              </span>
            )}
          </>
        ) : reviewing ? (
          "Decides and records everything, executes nothing. The full audit trail is produced before the agent is given authority to act."
        ) : (
          "Executes approved decisions within the merchant guardrails. Risk blocks, opt-outs and attempt caps still apply."
        )}
      </p>
    </div>
  );
}
