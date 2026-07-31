/**
 * TypeScript mirrors of every pydantic response model in
 * `claudetrade.webapi.schemas`. Field names are kept identical to the JSON
 * wire shape (snake_case) rather than re-cased to camelCase -- one fewer
 * translation layer to keep in sync as the API evolves, at the cost of
 * non-idiomatic-JS field names. See frontend/DESIGN.md.
 */

export type Direction = 'long' | 'short' | 'flat';

export interface ComponentScores {
  technical_setup: number;
  price_momentum: number;
  volume_confirmation: number;
  reddit_sentiment: number;
  x_sentiment: number;
  sentiment_acceleration: number;
  attention_acceleration: number;
  catalyst_quality: number;
  earnings_risk: number;
  liquidity: number;
  market_regime: number;
  manipulation_risk: number;
  data_confidence: number;
}

export interface TradePlan {
  entry_low: number;
  entry_high: number;
  stop_loss: number;
  targets: number[];
  reward_risk_ratio: number;
  shares: number;
  notional_usd: number;
  risk_per_share: number;
  reward_per_share: number;
  time_stop_days: number;
  expected_holding_days: number;
}

export interface SignalRow {
  signal_id: string;
  symbol: string;
  company_name: string;
  strategy: string;
  direction: Direction;
  status: string;
  regime: string;
  overall_score: number;
  confidence: number;
  reward_risk_ratio: number;
  entry_low: number;
  entry_high: number;
  stop_loss: number;
  days_to_earnings: number | null;
  session: string;
  created_at: string;
}

export interface SignalDetail extends SignalRow {
  components: ComponentScores;
  plan: TradePlan;
  thesis: string;
  invalidation: string[];
  exit_conditions: string[];
  risks: string[];
  evidence: string[];
  next_earnings_date: string | null;
  data_warnings: string[];
}

export interface SignalList {
  signals: SignalRow[];
  total: number;
}

export interface RejectedCandidate {
  symbol: string;
  strategy: string;
  stage: string;
  reasons: string[];
}

export interface RejectedResponse {
  available: boolean;
  reason: string | null;
  generated_at: string | null;
  evaluated_symbols: number;
  rejected: RejectedCandidate[];
}

export interface ScanRequest {
  session?: string | null;
  lookback_days?: number;
  generate_thesis?: boolean;
}

export interface ScanResponse {
  session: string;
  evaluated_symbols: number;
  signal_count: number;
  rejected_count: number;
  warnings: string[];
}

export interface RefreshRequest {
  start?: string | null;
  end?: string | null;
}

export interface RefreshResponse {
  universe_size: number;
  sentiment_rows: number;
  degraded_sources: Record<string, string>;
  warnings: string[];
}

export interface Bar {
  session: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  adj_close: number | null;
}

export interface SentimentPoint {
  session: string;
  post_count: number;
  unique_authors: number;
  engagement_weighted: number;
  bull_bear_ratio: number;
  manipulation_risk: number;
  confidence: number;
}

export interface Indicators {
  sma_20: (number | null)[];
  sma_50: (number | null)[];
  sma_200: (number | null)[];
  rsi_14: (number | null)[];
  bollinger_upper: (number | null)[];
  bollinger_lower: (number | null)[];
}

export interface TickerDetail {
  symbol: string;
  bars: Bar[];
  indicators: Indicators;
  sentiment: SentimentPoint[];
  earnings_dates: string[];
  current_signal: SignalDetail | null;
  signal_history: SignalRow[];
  price_note: string | null;
  sentiment_note: string | null;
}

export interface RegimeCard {
  regime: string;
  label: string;
  as_of_session: string | null;
  has_data: boolean;
}

export interface StatusRibbon {
  last_refresh: string | null;
  last_scan: string | null;
  symbols_with_data: number;
}

export interface ProviderStatusItem {
  name: string;
  kind: string;
  available: boolean;
  configured: boolean;
  supports_point_in_time: boolean;
  message: string;
}

export interface DashboardData {
  regime: RegimeCard;
  top_longs: SignalRow[];
  top_shorts: SignalRow[];
  status: StatusRibbon;
  providers: ProviderStatusItem[];
}

export interface PaperAccount {
  equity: number;
  cash: number;
  realised_pnl: number;
  kill_switch_engaged: boolean;
}

export interface PaperPosition {
  trade_id: string;
  symbol: string;
  direction: Direction;
  shares: number;
  entry_price: number;
  last_price: number;
  unrealised_pnl: number;
  unrealised_pct: number;
  days_held: number;
  needs_attention: string[];
}

export interface ClosedTrade {
  trade_id: string;
  symbol: string;
  direction: string;
  exit_session: string | null;
  outcome: string | null;
  net_pnl: number;
  r_multiple: number;
  reason: string | null;
}

export interface EquityPoint {
  session: string;
  equity: number;
}

export interface PaperAccountResponse {
  account: PaperAccount;
  positions: PaperPosition[];
  closed_trades: ClosedTrade[];
  equity_curve: EquityPoint[];
  equity_curve_note: string | null;
}

export interface Performance {
  closed_trades: number;
  open_trades: number;
  win_loss_ratio: number | null;
  win_loss_display: string;
  win_rate: number | null;
  expectancy: number | null;
  average_win: number | null;
  average_loss: number | null;
  profit_factor: number | null;
  profit_factor_display: string;
  max_drawdown_pct: number | null;
  max_drawdown_note: string | null;
  is_significant: boolean;
  significance_reason: string | null;
  warnings: string[];
}

export interface PaperOpenResponse {
  accepted: boolean;
  status: 'filled' | 'rejected' | 'not_fillable';
  order_id: string | null;
  symbol: string;
  direction: string;
  requested_shares: number;
  filled_shares: number;
  fill_price: number | null;
  fill_session: string | null;
  reasons: string[];
  message: string;
}

export interface Meta {
  code_version: string;
  disclaimer: string;
}

export interface SignalFilters {
  direction?: ('long' | 'short')[];
  min_score?: number;
  min_confidence?: number;
  strategy?: string[];
  max_days_to_earnings?: number;
  limit?: number;
}

export interface CredentialStatus {
  name: string;
  label: string;
  pipeline: 'sentiment' | 'stock_price';
  configured: boolean;
  source: string | null;
  masked: string | null;
}
export interface CredentialsResponse { credentials: CredentialStatus[]; storage: string; }
export interface PipelineDiagnostic {
  name: string;
  kind: 'sentiment' | 'stock_price';
  provider: string;
  status: 'reachable' | 'configured' | 'not_configured';
  configured: boolean;
  reachable: boolean | null;
  detail: string;
}
export interface DiagnosticsResponse { pipelines: PipelineDiagnostic[]; probe_note: string; }

export interface CredentialTestResult {
  ok: boolean;
  mode: string | null;
  status_detail: string;
}

export interface RefreshStatus {
  running: boolean;
  phase: string;
  symbols_done: number;
  symbols_total: number;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
}
