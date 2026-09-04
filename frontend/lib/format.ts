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
 * Colour per action, tuned for a light surface.
 *
 * Kept deliberately desaturated. On a page this quiet, a fully saturated badge
 * pulls the eye away from the numbers, which are the point. DROP is the one
 * case that gets a warm tint - not because it is an error, but because "we
 * chose to spend nothing here" is the decision most worth noticing.
 */
export const ACTION_STYLE: Record<string, string> = {
  PAYMENT_LINK: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  RETRY_SCHEDULED: "bg-amber-50 text-amber-700 ring-amber-200",
  RETRY_NOW: "bg-amber-50 text-amber-700 ring-amber-200",
  NOTIFY: "bg-sky-50 text-sky-700 ring-sky-200",
  ESCALATE: "bg-violet-50 text-violet-700 ring-violet-200",
  DROP: "bg-rose-50 text-rose-700 ring-rose-200",
};

export const CLASS_STYLE: Record<string, string> = {
  BANK_DOWNTIME: "text-amber-700",
  INSUFFICIENT_FUNDS: "text-orange-700",
  INSTRUMENT_INVALID: "text-emerald-700",
  AUTH_FAILURE: "text-sky-700",
  CUSTOMER_ABANDONED: "text-cyan-700",
  LIMIT_EXCEEDED: "text-yellow-700",
  RISK_BLOCKED: "text-rose-700",
  MERCHANT_CONFIG: "text-violet-700",
  ALREADY_PAID: "text-zinc-500",
  UNKNOWN: "text-zinc-500",
};
