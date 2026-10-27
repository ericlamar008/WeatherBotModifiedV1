export interface BucketDistribution {
  question?: string;
  market_id: string;
  range: [number, number];
  yes_price: number;
  no_price?: number;
  volume?: number;
  spread?: number;
  yes_token_id?: string;
  no_token_id?: string;
  model_prob: number;
  side?: string;
  ask?: number;
  market_prob?: number;
  belief_prob?: number;
  ev?: number;
  kelly?: number;
  sell_value?: number | null;
  model_prob_change?: number | null;
  model_prob_pct_change?: number | null;
  model_prob_6h_change?: number | null;
  yes_price_change?: number | null;
  yes_price_pct_change?: number | null;
  yes_price_10m_change?: number | null;
  yes_price_10m_pct_change?: number | null;
  yes_price_6h_change?: number | null;
  yes_price_6h_pct_change?: number | null;
  // Smart gated calibration fields
  smart_model_prob?: number;
  smart_model_prob_change?: number | null;
  smart_belief_prob?: number;
  smart_ev?: number;
  is_smart_main?: boolean;
}

export interface AllocationLeg {
  market_id: string;
  side: 'YES' | 'NO';
  role?: 'main_signal' | 'hedge' | 'emergency_hedge' | string;
  units?: number;
  price?: number;
  ev?: number;
  entry_key?: string;
  idx?: number | string;
  sell_value?: number | null;
  won?: boolean | null;
  pnl_units?: number | null;
  user_entered?: boolean;
}

export interface CityFormProfile {
  city: string;
  city_name: string;
  market_type: 'max' | 'min';
  unit: 'C' | 'F';
  is_us: boolean;
  // Recent 14-day stats
  recent_n: number;
  recent_hit_pct: number;
  recent_within1_pct: number;
  recent_mae: number;
  recent_bias: number;
  // Historical (older) stats
  old_n: number;
  old_hit_pct: number | null;
  old_mae: number | null;
  old_bias: number | null;
  // Trend & Form
  hit_delta_pct: number | null;
  form_status: 'hot' | 'stable' | 'cold_drop' | 'weak';
  form_label: string;
  // Calibration Gate & Sub-models
  raw_shadow_mae: number | null;
  calibrated_mae: number | null;
  raw_shadow_hit_pct: number | null;
  calibrated_hit_pct: number | null;
  gate_decision: 'use_calibrated' | 'revert_to_shadow' | 'smart_submodel_blend' | 'sign_flip_damped';
  gate_reason: string;
  sign_flip_detected: boolean;
  best_submodel: string | null;
  worst_submodel: string | null;
  submodel_maes: Record<string, number>;
  submodel_weights: Record<string, number>;
  trading_advice: string;
}

export interface WeatherMarket {
  city: string;
  city_name: string;
  date: string;
  unit: string;
  status: string;
  market_type: 'max' | 'min';
  file_key?: string;
  hours_left?: number | null;
  time_status?: string;
  forecast_mean?: number | null;
  forecast_mean_shadow?: number | null;
  model_means_raw?: Record<string, number> | null;
  actual_temp?: number | null;
  sigma?: number | null;
  full_distribution: BucketDistribution[];
  committed_allocation?: AllocationLeg[] | null;
  committed_full_distribution?: BucketDistribution[] | null;
  live_allocation?: AllocationLeg[] | null;
  graded_allocation?: AllocationLeg[] | null;
  graded_against?: string;
  best_case_pnl_units?: number | null;
  worst_case_pnl_units?: number | null;
  success_probability?: number | null;
  confidence?: number | null;
  committed_best_case_pnl_units?: number | null;
  committed_worst_case_pnl_units?: number | null;
  committed_success_probability?: number | null;
  committed_confidence?: number | null;
  balance_units?: number;
  committed_balance_units?: number;
  resolved_outcome?: 'win' | 'loss' | null;
  pnl_units?: number | null;
  my_pnl_units?: number | null;
  // Smart calibration fields populated by server
  smart_forecast_mean?: number | null;
  smart_sigma?: number | null;
  smart_main_market_id?: string | null;
  smart_gate_decision?: string;
  smart_gate_note?: string;
  city_form?: CityFormProfile | null;
}

export interface LockedSignal {
  issue_number: number;
  city: string;
  city_name: string;
  date: string;
  market_id: string;
  market_type?: 'max' | 'min';
  token_id?: string;
  side: string;
  entry_price: number;
  locked_at: string;
  status: 'open' | 'closed_manual' | 'closed_resolved';
  last_price?: number | null;
  last_pct?: number | null;
  exit_price?: number | null;
  closed_at?: string | null;
  close_reason?: string | null;
  actual_temp?: number | null;
  last_checked_at?: string | null;
  bucket_label?: string;
  unit?: string;
  hours_left?: number | null;
  time_status?: string | null;
  market_status?: string;
  forecast_mean?: number | null;
  outcome_pct?: number;
}

export interface DailyPnlGroup {
  date: string;
  avg_pct: number;
  count: number;
  items: LockedSignal[];
}

export interface CityStat {
  city: string;
  city_name: string;
  total: number;
  wins: number;
  pnl_sum: number;
  my_trades: number;
  my_pnl_sum: number;
  accuracy_pct: number | null;
  recent_accuracy_pct?: number | null;
  form_status?: string;
}

export interface DashboardDataResponse {
  meta: {
    last_update_iran: string;
    latest_scan_iso: string;
    latest_scan_kind: string;
    last_scan: { lite?: string; full?: string };
    total_markets_files: number;
    open_max_count: number;
    open_min_count: number;
    city_count: number;
  };
  summary: {
    balance_units: number;
    pnl_units: number;
    wins: number;
    losses: number;
    total_trades: number;
    win_rate: number;
    my_balance_units: number;
    my_wins: number;
    my_losses: number;
    my_total_trades: number;
    open_count: number;
  };
  open_max_markets: WeatherMarket[];
  open_min_markets: WeatherMarket[];
  locks: LockedSignal[];
  daily_pnl: DailyPnlGroup[];
  resolved_markets: WeatherMarket[];
  no_signal_markets: Partial<WeatherMarket>[];
  city_stats: CityStat[];
  accuracy_by_city: Record<string, number | null>;
  city_form_profiles: Record<string, CityFormProfile>;
  config: Record<string, any>;
  last_price_status: string;
}

export interface HistoryRow {
  date: string;
  city_name: string;
  signal_label: string;
  final_temp: string;
  buy_cents: number | null;
  sell_cents: number | null;
  outcome_pct: number | null;
}

export interface AccuracyRow {
  city_name: string;
  date: string;
  market_type: string;
  horizon_days: string;
  forecast_mean: number | null;
  sigma: number | null;
  actual_temp: number | null;
  abs_error: number | null;
  signed_bias: number | null;
  outcome: string;
  pnl_units: number | null;
  confidence: number | null;
  success_prob: number | null;
}
