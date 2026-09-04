import { Learning } from "@/lib/api";
import { count } from "@/lib/format";
import { Card, Th } from "./ui";

/**
 * The LEARN stage, made inspectable.
 *
 * Two questions are answered here and they are not the same question, so they
 * get separate sections rather than one merged table:
 *
 *   1. Do the hand-authored effectiveness constants survive contact with
 *      observed outcomes? (the audit — reported, never auto-applied)
 *   2. What would the data say those constants are, if it were allowed to
 *      speak? (the posteriors)
 *
 * The visual carrying most of the weight is the interval bar. An assumption
 * sitting inside its own confidence interval is one the data supports; one
 * outside it is not. Drawing it rather than tabulating it means a reviewer can
 * scan a column of bars and see at once that every tick lands inside its band —
 * which is the actual claim, and is far harder to read off six decimal columns.
 */
export function LearningView({ data }: { data: Learning | null }) {
  if (!data) {
    return (
      <p className="py-20 text-center text-[13px] text-[var(--faint)]">
        Loading…
      </p>
    );
  }

  const { effectiveness } = data;
  const informed = effectiveness.posteriors.filter((p) => p.observations > 0);
  const uninformed = effectiveness.posteriors.filter(
    (p) => p.observations === 0
  );

  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-[var(--border)] bg-[#fafafa] p-5">
        <div className="flex flex-wrap items-baseline gap-x-10 gap-y-3">
          <Stat
            label="Assumptions audited"
            value={`${data.assumptions_checked} / ${data.matrix_entries}`}
          />
          <Stat
            label="Contradicted by data"
            value={count(data.drifted)}
            tone={data.drifted > 0 ? "bad" : "good"}
          />
          <Stat
            label="Too few observations"
            value={count(data.insufficient_data)}
          />
          <Stat
            label="Arms carrying evidence"
            value={`${effectiveness.informed_arms} / ${effectiveness.arms}`}
          />
        </div>
        <p className="mt-4 max-w-3xl border-t border-[var(--border)] pt-3 text-[12px] leading-relaxed text-[var(--muted)]">
          {data.policy}
        </p>
      </div>

      <Card className="p-5">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
          Effectiveness assumptions vs observed outcomes
        </h3>
        <p className="mt-1.5 max-w-3xl text-[12px] leading-relaxed text-[var(--muted)]">
          Every expected value in this system is a hand-authored effectiveness
          number multiplied by a predicted propensity. This is that number held
          to account. The band is the 95% Wilson interval on what actually
          happened; the tick is what the economics currently assume. A tick
          inside its band is an assumption the data supports — and a band that
          spans half the axis is the honest answer when {data.min_samples}{" "}
          observations have not yet accumulated.
        </p>

        <div className="mt-5">
          <IntervalAxis />
          {data.checks.map((c) => (
            <IntervalRow
              key={`${c.failure_class}:${c.action}`}
              label={c.failure_class}
              sub={c.action}
              assumed={c.assumed}
              observed={c.observed}
              low={c.ci_low}
              high={c.ci_high}
              n={c.n}
              muted={!c.sufficient}
              bad={c.drifted}
              recommendation={c.recommendation}
            />
          ))}
        </div>

        <Legend minSamples={data.min_samples} />
      </Card>

      <Card className="p-5">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
          Organic baseline — the control group
        </h3>
        <p className="mt-1.5 max-w-3xl text-[12px] leading-relaxed text-[var(--muted)]">
          Payments the system declined to act on. Nobody contacted them, so
          whatever came back came back on its own. This is the one assumption
          measurable almost directly, and it is the one deciding how much credit
          Salvage claims for itself — an organic rate set too low turns other
          people&apos;s recoveries into our results.
        </p>

        <div className="mt-5">
          <IntervalAxis />
          {data.organic_baseline.map((o) => (
            <IntervalRow
              key={o.failure_class}
              label={o.failure_class}
              sub="no intervention"
              assumed={o.assumed}
              observed={o.observed}
              low={o.ci_low}
              high={o.ci_high}
              n={o.n}
              muted={!o.sufficient}
              bad={o.sufficient && (o.assumed < o.ci_low || o.assumed > o.ci_high)}
            />
          ))}
        </div>
      </Card>

      <Card className="p-5">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
          Learned effectiveness — Beta posteriors
        </h3>
        <p className="mt-1.5 max-w-3xl text-[12px] leading-relaxed text-[var(--muted)]">
          What the data says each intervention is worth. Seeded from the
          hand-authored matrix at {effectiveness.prior_strength} pseudo-observations,
          so day one behaves exactly as it did before and evidence moves the
          number only as evidence accumulates. Exposure counts summed propensity
          rather than payments, because effectiveness is a multiplier on
          propensity and not a recovery rate.
        </p>

        <div className="mt-5 overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--border)]">
                <Th>Failure class</Th>
                <Th>Action</Th>
                <Th align="right">Authored</Th>
                <Th align="right">Learned</Th>
                <Th align="right">Moved</Th>
                <Th align="right">± sd</Th>
                <Th align="right">Payments</Th>
                <Th align="right">Exposure</Th>
              </tr>
            </thead>
            <tbody>
              {informed.map((p) => (
                <tr key={`${p.failure_class}:${p.action}`} className="row-sep">
                  <td className="px-4 py-3 text-[13px] font-medium">
                    {p.failure_class}
                  </td>
                  <td className="px-4 py-3 text-[12px] text-[var(--muted)]">
                    {p.action}
                  </td>
                  <td className="num px-4 py-3 text-right text-[13px] text-[var(--muted)]">
                    {p.prior.toFixed(3)}
                  </td>
                  <td className="num px-4 py-3 text-right text-[13px] font-medium">
                    {p.posterior_mean.toFixed(3)}
                  </td>
                  <td
                    className={`num px-4 py-3 text-right text-[13px] ${
                      Math.abs(p.moved) >= 0.05
                        ? "font-medium text-amber-700"
                        : "text-[var(--faint)]"
                    }`}
                  >
                    {p.moved > 0 ? "+" : ""}
                    {p.moved.toFixed(3)}
                  </td>
                  <td className="num px-4 py-3 text-right text-[12px] text-[var(--faint)]">
                    {p.stdev.toFixed(3)}
                  </td>
                  <td className="num px-4 py-3 text-right text-[13px] text-[var(--muted)]">
                    {count(p.observations)}
                  </td>
                  <td className="num px-4 py-3 text-right text-[12px] text-[var(--faint)]">
                    {p.exposure.toFixed(1)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Stated rather than hidden. An arm the policy never picks carries no
            evidence, and listing its prior in the same table as a learned value
            would be the system quietly overclaiming. */}
        {uninformed.length > 0 && (
          <div className="mt-5 rounded-lg border border-[var(--border)] bg-[#fafafa] p-4">
            <h4 className="text-[12px] font-medium">
              {uninformed.length} arms held at their prior — no evidence exists
            </h4>
            <p className="mt-1.5 max-w-3xl text-[12px] leading-relaxed text-[var(--muted)]">
              {effectiveness.identification}
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {uninformed.map((p) => (
                <span
                  key={`${p.failure_class}:${p.action}`}
                  className="rounded border border-[var(--border)] bg-white px-2 py-1 text-[11px] text-[var(--muted)]"
                >
                  {p.failure_class}
                  <span className="mx-1 text-[var(--faint)]">→</span>
                  {p.action}
                  <span className="num ml-1.5 font-mono text-[var(--faint)]">
                    {p.prior.toFixed(2)}
                  </span>
                </span>
              ))}
            </div>
          </div>
        )}

        <p className="mt-4 max-w-3xl border-t border-[var(--border)] pt-3 text-[11px] leading-relaxed text-[var(--muted)]">
          {effectiveness.honesty}
        </p>
      </Card>
    </div>
  );
}

/**
 * One assumption drawn on a 0–1 axis: the confidence band, the observed point,
 * and a tick where the authored constant sits.
 */
//: Where the axis is ruled. 0 and 1 are the track edges and get no line.
const TICKS = [0.25, 0.5, 0.75];

/**
 * The scale the bands are drawn against.
 *
 * Without it the bars are only comparable to each other — a reviewer can see
 * that one interval is wider than another but cannot read a value off either,
 * which makes the whole panel decorative. The column widths here have to match
 * IntervalRow exactly or the ruling lies about where the values sit.
 */
function IntervalAxis() {
  return (
    <div className="flex items-end gap-3 px-1 pb-1 text-[10px] text-[var(--faint)]">
      <span className="w-52 shrink-0" />
      <div className="relative h-4 min-w-[120px] flex-1">
        <span className="absolute bottom-0 left-0">0</span>
        {TICKS.map((t) => (
          <span
            key={t}
            className="absolute bottom-0 -translate-x-1/2"
            style={{ left: `${t * 100}%` }}
          >
            {t.toFixed(2)}
          </span>
        ))}
        <span className="absolute bottom-0 right-0">1</span>
      </div>
      <span className="w-24 shrink-0" />
      <span className="w-16 shrink-0" />
    </div>
  );
}

function IntervalRow({
  label,
  sub,
  assumed,
  observed,
  low,
  high,
  n,
  muted,
  bad,
  recommendation,
}: {
  label: string;
  sub: string;
  assumed: number;
  observed: number;
  low: number;
  high: number;
  n: number;
  muted?: boolean;
  bad?: boolean;
  recommendation?: string;
}) {
  const pct = (v: number) => Math.max(0, Math.min(100, v * 100));
  const left = pct(low);
  // A zero-width band (n small, both bounds equal) would render as nothing at
  // all, which reads as missing data rather than as a point estimate.
  const width = Math.max(pct(high) - left, 0.6);

  return (
    <div className="rounded px-1 py-1.5 hover:bg-[#fafafa]">
      <div className="flex items-center gap-3 text-[12px]">
        <span className="flex w-52 shrink-0 items-baseline gap-1.5 truncate">
          <span className={muted ? "text-[var(--muted)]" : ""}>{label}</span>
          <span className="truncate text-[11px] text-[var(--faint)]">{sub}</span>
        </span>

        <div className="relative h-5 min-w-[120px] flex-1 rounded bg-[#f4f4f5]">
          {TICKS.map((t) => (
            <div
              key={t}
              className="absolute inset-y-0 w-px bg-white"
              style={{ left: `${t * 100}%` }}
            />
          ))}
          <div
            className={`absolute inset-y-1 rounded-sm ${
              bad ? "bg-rose-200" : muted ? "bg-[#d4d4d8]" : "bg-emerald-200"
            }`}
            style={{ left: `${left}%`, width: `${width}%` }}
          />
          <div
            className={`absolute inset-y-0.5 w-[2px] ${
              bad ? "bg-rose-600" : "bg-[#3f3f46]"
            }`}
            style={{ left: `calc(${pct(assumed)}% - 1px)` }}
          />
          <div
            className="absolute top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-[#52525b]"
            style={{ left: `calc(${pct(observed)}% - 3px)` }}
          />
        </div>

        <span className="num w-24 shrink-0 text-right font-mono text-[11px] text-[var(--muted)]">
          {assumed.toFixed(2)} → {observed.toFixed(2)}
        </span>
        <span
          className={`num w-16 shrink-0 text-right text-[11px] ${
            muted ? "text-amber-700" : "text-[var(--faint)]"
          }`}
        >
          n={count(n)}
        </span>
      </div>

      {/* A contradicted assumption is the one output of this panel someone has
          to act on, so it is stated on the page rather than left in a tooltip
          nobody hovers. Supported rows stay quiet — eleven lines of "consistent
          with the data" is noise that buries the one line that is not. */}
      {bad && recommendation && (
        <p className="ml-[13.75rem] mt-1 max-w-2xl text-[11px] leading-relaxed text-rose-700">
          {recommendation}
        </p>
      )}
    </div>
  );
}

function Legend({ minSamples }: { minSamples: number }) {
  return (
    <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 border-t border-[var(--border)] pt-3 text-[10px] text-[var(--faint)]">
      <span>
        <span className="mr-1 inline-block h-2 w-4 rounded-sm bg-emerald-200 align-middle" />
        95% interval on observed
      </span>
      <span>
        <span className="mr-1 inline-block h-2.5 w-0.5 bg-[#3f3f46] align-middle" />
        authored assumption
      </span>
      <span>
        <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-[#52525b] align-middle" />
        observed rate
      </span>
      <span>
        <span className="mr-1 inline-block h-2 w-4 rounded-sm bg-[#d4d4d8] align-middle" />
        under {minSamples} observations — not yet judgeable
      </span>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "bad";
}) {
  return (
    <div>
      <div
        className={`num text-[22px] font-semibold tracking-tight ${
          tone === "good"
            ? "text-emerald-700"
            : tone === "bad"
              ? "text-rose-700"
              : ""
        }`}
      >
        {value}
      </div>
      <div className="mt-0.5 text-[11px] text-[var(--muted)]">{label}</div>
    </div>
  );
}
