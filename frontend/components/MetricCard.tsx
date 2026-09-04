import { ReactNode } from "react";

/**
 * A headline figure with its supporting context.
 *
 * `note` exists because every number on this dashboard has a caveat that
 * matters. A recovery figure without its organic baseline stated is
 * misleading, so the caveat is part of the component rather than something a
 * viewer has to go looking for.
 */
export function MetricCard({
  label,
  value,
  note,
  accent = "ink",
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  accent?: "ink" | "emerald" | "amber" | "rose" | "sky";
}) {
  const accents: Record<string, string> = {
    ink: "text-[var(--ink)]",
    emerald: "text-emerald-700",
    amber: "text-amber-700",
    rose: "text-rose-700",
    sky: "text-sky-700",
  };

  return (
    <div className="rounded-xl border border-[var(--border)] bg-white p-5">
      <div className="text-[11px] font-medium uppercase tracking-[0.1em] text-[var(--faint)]">
        {label}
      </div>
      <div className={`num mt-2 text-[28px] font-semibold ${accents[accent]}`}>
        {value}
      </div>
      {note && (
        <div className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">
          {note}
        </div>
      )}
    </div>
  );
}

export function Badge({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${className}`}
    >
      {children}
    </span>
  );
}
