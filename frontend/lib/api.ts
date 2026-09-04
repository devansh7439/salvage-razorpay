/** Typed client for the Salvage API. */

const BASE = process.env.NEXT_PUBLIC_API ?? "http://127.0.0.1:8099";

/**
 * Live merchant controls. Mirrors Razorpay's own named autonomy limits:
 * review-first mode and an immediate kill switch.
 */
export interface AgentControls {
  enabled: boolean;
  mode: "review_first" | "autonomous";
  status: "disabled" | "review_first" | "autonomous";
  executes: boolean;
  disabled_reason: string | null;
  changed_at: string;
  changed_by: string;
}

export interface Health {
  status: string;
  model_loaded: boolean;
  agent: AgentControls;
  razorpay_mode: string;
  llm_mode: string;
  webhook_signature_enforced: boolean;
}

export interface Metrics {
  events: number;
  revenue_at_risk_paise: number;
  expected_recoverable_paise: number;
  gross_recovered_paise: number;
  organic_recovered_paise: number;
  incremental_recovered_paise: number;
  action_spend_paise: number;
  blind_retry_spend_paise: number;
  spend_avoided_paise: number;
  action_breakdown: Record<string, number>;
  exceptions: number;
  outcomes_recorded: number;
}

export interface QueueRow {
  id: string;
  amount: number;
  method: string | null;
  customer_name: string | null;
  error_code: string | null;
  error_reason: string | null;
  error_source: string | null;
  failure_class: string | null;
  base_propensity: number | null;
  action: string | null;
  net_ev: number | null;
  action_probability: number | null;
  rule_id: string | null;
  is_exception: number | null;
  recovered: number | null;
  incremental_paise: number | null;
  payment_link_url: string | null;
}

export interface Considered {
  action: string;
  probability: number;
  effectiveness: number;
  gross_expected: number;
  cost: number;
  net_ev: number;
}

export interface EventDetail {
  event: Record<string, any>;
  decision: {
    failure_class: string;
    diagnosis_note: string;
    diagnosis_confident: number;
    base_propensity: number;
    action: string;
    rule_id: string;
    rationale: string;
    action_probability: number | null;
    gross_expected: number | null;
    mdr: number | null;
    action_cost: number | null;
    net_ev: number | null;
    considered: Considered[];
    constraints: string[];
    retry_after_hours: number | null;
    is_exception: number;
  } | null;
  execution: {
    action: string;
    status: string;
    payment_link_url: string | null;
    message_text: string | null;
    scheduled_for: string | null;
    provider: string | null;
  } | null;
  outcome: {
    recovered: number;
    recovered_paise: number;
    organic: number;
    incremental_paise: number;
  } | null;
  audit_trail: {
    id: number;
    timestamp: string;
    stage: string;
    summary: string;
    detail: any;
  }[];
  policy: Record<string, number>;
}

export interface Strategy {
  name: string;
  actions_taken: number;
  action_cost_paise: number;
  gross_recovered_paise: number;
  organic_recovered_paise: number;
  incremental_recovered_paise: number;
  net_value_paise: number;
  wasted_actions: number;
  actions_on_unrecoverable: number;
}

export interface Evaluation {
  batch_size: number;
  revenue_at_risk_paise: number;
  strategies: { do_nothing: Strategy; blind_retry: Strategy; salvage: Strategy };
  delta_vs_blind: {
    net_value_paise: number;
    actions_saved: number;
    cost_saved_paise: number;
    wasted_actions_avoided: number;
    unrecoverable_actions_avoided: number;
  };
  exceptions: number;
  model: {
    roc_auc: number;
    oracle_auc: number;
    signal_captured_pct: number;
    brier: number;
    brier_baseline: number;
    oracle_brier: number;
    n_test: number;
    calibration_bins: { range: string; n: number; predicted: number; observed: number }[];
  } | null;
}

export interface ExceptionReport {
  total: number;
  value_paise: number;
  by_rule: Record<string, number>;
  exceptions: {
    id: string;
    amount: number;
    error_reason: string | null;
    error_code: string | null;
    customer_name: string | null;
    failure_class: string;
    rule_id: string;
    rationale: string;
  }[];
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export const api = {
  health: () => get<Health>("/health"),
  metrics: () => get<Metrics>("/api/metrics"),
  events: (limit = 200) => get<{ total: number; events: QueueRow[] }>(`/api/events?limit=${limit}`),
  eventDetail: (id: string) => get<EventDetail>(`/api/events/${id}`),
  evaluate: () => get<Evaluation>("/api/evaluate"),
  exceptions: () => get<ExceptionReport>("/api/exceptions"),
  load: async () => {
    const res = await fetch(`${BASE}/api/simulate/load`, { method: "POST" });
    if (!res.ok) throw new Error(`load -> ${res.status}`);
    return res.json();
  },

  controls: () => get<AgentControls>("/api/controls"),

  /** Change agent authority at runtime. Takes effect without a restart. */
  setControls: async (opts: {
    enabled?: boolean;
    mode?: "review_first" | "autonomous";
    reason?: string;
  }): Promise<AgentControls> => {
    const params = new URLSearchParams();
    if (opts.enabled !== undefined) params.set("enabled", String(opts.enabled));
    if (opts.mode) params.set("mode", opts.mode);
    if (opts.reason) params.set("reason", opts.reason);

    const res = await fetch(`${BASE}/api/controls?${params}`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`controls -> ${res.status}`);
    return res.json();
  },
};
