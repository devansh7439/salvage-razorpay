import { Icon } from "./ui";
import { rupeesCompact } from "@/lib/format";
import { AgentControls } from "@/lib/api";
import { AgentControlPanel } from "./AgentControlPanel";

/**
 * Left navigation, grouped by what a reviewer is trying to do rather than by
 * which table the data lives in: build the batch, understand the economics,
 * then inspect what the system refused to handle.
 *
 * The card pinned to the bottom carries the one number that answers "did this
 * work?" without navigating anywhere, plus the action that regenerates it.
 */

export type View =
  | "command"
  | "queue"
  | "evaluation"
  | "learning"
  | "exceptions"
  | "integrations";

const GROUPS: { label: string; items: { id: View; label: string; icon: string }[] }[] =
  [
    {
      label: "Build",
      items: [
        { id: "command", label: "Command Centre", icon: "grid" },
        { id: "queue", label: "Recovery Queue", icon: "list" },
      ],
    },
    {
      label: "Optimize",
      items: [
        { id: "evaluation", label: "Evaluation", icon: "chart" },
        { id: "learning", label: "Learning", icon: "sparkle" },
      ],
    },
    {
      label: "Review",
      items: [
        { id: "exceptions", label: "Exceptions", icon: "alert" },
        { id: "integrations", label: "Integrations", icon: "plug" },
      ],
    },
  ];

export function Sidebar({
  view,
  onSelect,
  atRisk,
  recovered,
  exceptions,
  busy,
  onRun,
  controls,
  onControlChange,
}: {
  view: View;
  onSelect: (v: View) => void;
  atRisk: number;
  recovered: number;
  exceptions: number;
  busy: boolean;
  onRun: () => void;
  controls: AgentControls | null;
  onControlChange: (o: {
    enabled?: boolean;
    mode?: "review_first" | "autonomous";
    reason?: string;
  }) => void;
}) {
  const pct = atRisk > 0 ? Math.min(100, (recovered / atRisk) * 100) : 0;

  return (
    <aside className="flex h-screen w-[248px] shrink-0 flex-col border-r border-[var(--border)] bg-white">
      <div className="px-5 py-6">
        <div className="flex items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-[var(--ink)] text-white">
            <Icon name="sparkle" className="h-3.5 w-3.5" />
          </span>
          <span className="text-[15px] font-semibold tracking-tight">Salvage</span>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-[var(--faint)]">
          Bounded autonomous
          <br />
          revenue recovery
        </p>
      </div>

      {/* Navigation and utility links share one scrollable region, so the
          slack collects in a single place just above the pinned cards rather
          than opening a gap in the middle of the rail. The region scrolls
          because the bottom block is now tall enough to overflow a short
          viewport. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <nav className="px-3">
        {GROUPS.map((group) => (
          <div key={group.label} className="mb-5">
            <div className="px-2 pb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--faint)]">
              {group.label}
            </div>
            <div className="space-y-0.5">
              {group.items.map((item) => {
                const active = view === item.id;
                return (
                  <button
                    key={item.id}
                    onClick={() => onSelect(item.id)}
                    className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] transition ${
                      active
                        ? "border border-[var(--border)] bg-white font-medium text-[var(--text)] shadow-[0_1px_2px_rgba(0,0,0,0.04)]"
                        : "border border-transparent text-[var(--muted)] hover:bg-[#fafafa]"
                    }`}
                  >
                    <Icon name={item.icon} className="h-4 w-4" />
                    {item.label}
                    {item.id === "exceptions" && exceptions > 0 && (
                      <span className="num ml-auto rounded-md bg-[#f4f4f5] px-1.5 py-0.5 text-[10px] text-[var(--muted)]">
                        {exceptions}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="px-3 pb-3">
        <div className="space-y-0.5 border-t border-[var(--border)] pt-3">
          <a
            href="https://razorpay.com/docs/errors/payments/list/"
            target="_blank"
            rel="noreferrer"
            className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] text-[var(--muted)] transition hover:bg-[#fafafa]"
          >
            <Icon name="link" className="h-4 w-4" />
            Razorpay error docs
          </a>
          <button className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] text-[var(--muted)] transition hover:bg-[#fafafa]">
            <Icon name="help" className="h-4 w-4" />
            Help &amp; Support
          </button>
        </div>
      </div>

      {/* All remaining slack, in one place. */}
      <div className="flex-1" />
      </div>

      <div className="shrink-0 space-y-2 border-t border-[var(--border)] p-3">
      <AgentControlPanel
        controls={controls}
        onChange={onControlChange}
        busy={busy}
      />

      {/* The one number that answers "did this work?", without navigating. */}
      <div className="rounded-xl border border-[var(--border)] bg-[#fbfbfc] p-4">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-[12px] text-[var(--muted)]">
            <Icon name="sparkle" className="h-3.5 w-3.5" />
            Recovered
          </span>
          <span className="num text-[12px] font-semibold">
            {pct.toFixed(1)}%
          </span>
        </div>
        <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-[#ebebed]">
          <div
            className="h-full rounded-full bg-[var(--ink)] transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="num mt-2 flex justify-between text-[10px] text-[var(--faint)]">
          <span>{rupeesCompact(recovered)}</span>
          <span>{rupeesCompact(atRisk)}</span>
        </div>
        <button
          onClick={onRun}
          disabled={busy}
          className="mt-3 w-full rounded-lg bg-[var(--ink)] py-2 text-[13px] font-medium text-white transition hover:bg-black disabled:opacity-50"
        >
          {busy ? "Processing…" : "Run batch"}
        </button>
      </div>
      </div>
    </aside>
  );
}
