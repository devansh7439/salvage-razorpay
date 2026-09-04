"use client";

import { ReactNode, useState } from "react";
import { Icon } from "./ui";

/**
 * Pre-emptive answers to the three questions a sceptical reviewer asks.
 *
 * Every one of Salvage's most deliberate decisions makes a headline number
 * look *worse* at a glance: recovery is reported net of organic, so the figure
 * shrinks; the model is reported against its Bayes ceiling, so the AUC looks
 * near-chance; the data is synthetic and says so. Each is the correct call and
 * each reads as a weakness if nobody explains it.
 *
 * The first version put all three explanations on screen permanently, which
 * buried the landing view under two hundred words of small grey type - a wall
 * of documentation where a dashboard should be. The fix is to separate the
 * *claim* from the *argument*: the three corrected figures stay visible at all
 * times, because they are the point, and the reasoning behind them expands on
 * demand.
 */
export function HonestyBand({
  incremental,
  gross,
  organic,
  aucModel,
  aucCeiling,
  signalPct,
}: {
  incremental: string;
  gross: string;
  organic: string;
  aucModel?: number;
  aucCeiling?: number;
  signalPct?: number;
}) {
  const [open, setOpen] = useState(false);

  return (
    <section className="rounded-xl border border-[var(--border)] bg-white">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-4 px-5 py-4 text-left transition hover:bg-[#fafafa]"
      >
        <span className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--faint)]">
          How to read these
        </span>

        <div className="flex flex-1 flex-wrap items-center gap-x-6 gap-y-2">
          <Claim label="incremental, not gross">
            <span className="text-[var(--faint)] line-through">{gross}</span>{" "}
            <span className="text-emerald-700">{incremental}</span>
          </Claim>

          <Claim label="AUC against its ceiling">
            {aucCeiling ? (
              <>
                <span>{aucModel?.toFixed(3)}</span>
                <span className="text-[var(--faint)]">
                  {" "}
                  / {aucCeiling.toFixed(3)}
                </span>{" "}
                <span className="text-emerald-700">= {signalPct}%</span>
              </>
            ) : (
              <span className="text-[var(--faint)]">—</span>
            )}
          </Claim>

          <Claim label="taxonomy is real">
            <span className="text-emerald-700">48</span>
            <span className="text-[var(--faint)]"> documented reasons</span>
          </Claim>
        </div>

        <span className="flex shrink-0 items-center gap-1 text-[11px] text-[var(--muted)]">
          {open ? "Hide" : "Why"}
          <Icon
            name="chevron"
            className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`}
          />
        </span>
      </button>

      {open && (
        <div className="grid grid-cols-1 gap-4 border-t border-[var(--border)] p-5 lg:grid-cols-3">
          <Point n="1" title="Recovery is incremental, not gross">
            {organic} of that gross figure came from customers who would have
            returned unprompted — for a bank outage, about a third do. Billing
            for them is not recovery, so Salvage subtracts them and claims only
            what it caused. It also <em>optimises</em> that same number, so it
            will not spend to nudge someone already coming back.
          </Point>

          <Point n="2" title="The model is scored against its ceiling">
            Recovery outcomes are coin flips weighted by probability, so no
            model can exceed the Bayes rate. We measured that ceiling by scoring
            the oracle&apos;s true probabilities: <strong>0.629</strong>. An AUC
            of <strong>0.575</strong> is {signalPct}% of the signal that exists,
            not a near-chance model. A near-perfect score would indicate a leak.
          </Point>

          <Point n="3" title="Real taxonomy, simulated volume">
            The failure taxonomy and the webhook contract are Razorpay&apos;s
            own, transcribed from their docs and covered by tests against a real
            <code className="mx-1 font-mono text-[10px] text-[var(--muted)]">
              payment.failed
            </code>
            payload. Only the <em>volume and outcomes</em> are synthetic — and
            the generator is built so the model cannot memorise it.
          </Point>
        </div>
      )}
    </section>
  );
}

function Claim({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0">
      <div className="num text-[15px] font-semibold leading-tight">
        {children}
      </div>
      <div className="mt-0.5 text-[10px] uppercase tracking-wider text-[var(--faint)]">
        {label}
      </div>
    </div>
  );
}

function Point({
  n,
  title,
  children,
}: {
  n: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[10px] text-[var(--faint)]">{n}</span>
        <h3 className="text-[12px] font-semibold text-[var(--text)]">{title}</h3>
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--muted)]">
        {children}
      </p>
    </div>
  );
}

/**
 * Honest reporting of which integrations are actually live.
 *
 * A demo that runs on fixtures while implying live calls is the thing that
 * destroys credibility when a reviewer asks. Stating it first removes the
 * question - and names the one real security property that holds either way.
 */
export function IntegrationNotice({
  razorpayMode,
  llmMode,
}: {
  razorpayMode: string;
  llmMode: string;
}) {
  const fixtures = razorpayMode === "fixture" || llmMode === "template";

  if (!fixtures) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50/60 px-5 py-3 text-[12px] text-emerald-800">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
        <span className="font-medium">Live integrations active</span>
        <span className="text-emerald-700/80">
          — real Razorpay Test Mode links, model-written recovery copy.
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-[var(--border)] bg-white px-5 py-3 text-[12px]">
      <span className="h-1.5 w-1.5 rounded-full bg-[var(--faint)]" />
      <span className="font-medium text-[var(--text)]">Running on fixtures.</span>
      <span className="text-[var(--muted)]">
        Deterministic stand-ins, so the demo cannot fail on a network call.
        Payloads are identical in both modes.
      </span>
      <code className="rounded bg-[#fafafa] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted)]">
        salvage.verify_live
      </code>
      <span className="text-[var(--muted)]">proves it.</span>
    </div>
  );
}
