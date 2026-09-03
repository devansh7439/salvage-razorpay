/**
 * Formatting helpers.
 *
 * Every amount crossing the API is in paise, matching Razorpay's own
 * convention. Conversion to rupees happens here and nowhere else, so a display
 * bug cannot silently become a hundredfold error in a headline figure.
 */

/** Indian digit grouping: 12,34,567 rather than 1,234,567. */
export function rupees(paise: number | null | undefined, decimals = 0): string {
  if (paise == null) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: decimals,
    minimumFractionDigits: decimals,
  }).format(paise / 100);
}

/** Compact form for headline tiles: ₹31.5L, ₹4.8Cr. */
export function rupeesCompact(paise: number | null | undefined): string {
  if (paise == null) return "—";
  const r = paise / 100;
  if (Math.abs(r) >= 1e7) return `₹${(r / 1e7).toFixed(2)}Cr`;
  if (Math.abs(r) >= 1e5) return `₹${(r / 1e5).toFixed(2)}L`;
  if (Math.abs(r) >= 1e3) return `₹${(r / 1e3).toFixed(1)}K`;
  return `₹${r.toFixed(0)}`;
}

export function percent(value: number | null | undefined, decimals = 1): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(decimals)}%`;
}

export function count(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("en-IN").format(n);
}

/**
 * Colour per action. Red is reserved for DROP, which is the one that most
 * needs to read as deliberate rather than as a failure - a DROP is the system
 * declining to waste money, not an error.
 */
export const ACTION_STYLE: Record<string, string> = {
  PAYMENT_LINK: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  RETRY_SCHEDULED: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  RETRY_NOW: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  NOTIFY: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  ESCALATE: "bg-violet-500/15 text-violet-300 ring-violet-500/30",
  DROP: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
};

export const CLASS_STYLE: Record<string, string> = {
  BANK_DOWNTIME: "text-amber-300",
  INSUFFICIENT_FUNDS: "text-orange-300",
  INSTRUMENT_INVALID: "text-emerald-300",
  AUTH_FAILURE: "text-sky-300",
  CUSTOMER_ABANDONED: "text-cyan-300",
  LIMIT_EXCEEDED: "text-yellow-300",
  RISK_BLOCKED: "text-rose-300",
  MERCHANT_CONFIG: "text-violet-300",
  ALREADY_PAID: "text-slate-400",
  UNKNOWN: "text-slate-400",
};
