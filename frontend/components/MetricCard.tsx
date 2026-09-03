import { ReactNode } from "react";

/**
 * A headline figure with its supporting context.
 *
 * `note` exists because every number on this dashboard has a caveat that
 * matters. A recovery figure without its organic baseline stated is misleading,
 * so the caveat is part of the component rather than something a viewer has to
 * go looking for.
 */
export function MetricCard({
  label,
  value,
  note,
  accent = "slate",
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  accent?: "slate" | "emerald" | "amber" | "rose" | "sky";
}) {
  const accents: Record<string, string> = {
    slate: "text-slate-100",
    emerald: "text-emerald-300",
    amber: "text-amber-300",
    rose: "text-rose-300",
    sky: "text-sky-300",
  };

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-5">
      <div className="text-[11px] font-medium uppercase tracking-widest text-slate-500">
        {label}
      </div>
      <div className={`num mt-2 text-3xl font-semibold ${accents[accent]}`}>
        {value}
      </div>
      {note && (
        <div className="mt-2 text-xs leading-relaxed text-slate-500">{note}</div>
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
