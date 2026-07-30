import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { TrendingUp, TrendingDown, Minus, HelpCircle, AlertTriangle } from 'lucide-react';
import { api, ApiError } from '../api/client';
import type { DashboardData, PaperAccountResponse, Performance, SignalRow } from '../api/types';
import { Card } from '../components/Card';
import { EmptyState } from '../components/EmptyState';
import { SkeletonCard, SkeletonRows } from '../components/Skeleton';
import { DirectionBadge } from '../components/DirectionBadge';
import { StatusChip } from '../components/StatusChip';
import { Sparkline } from '../charts/Sparkline';
import { formatCurrency, formatDateTime, formatPrice } from '../lib/format';

const REGIME_ICON: Record<string, typeof TrendingUp> = {
  bull_quiet: TrendingUp,
  bull_volatile: TrendingUp,
  neutral: Minus,
  bear_volatile: TrendingDown,
  bear_quiet: TrendingDown,
  unknown: HelpCircle,
};

export function Dashboard() {
  const navigate = useNavigate();
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [account, setAccount] = useState<PaperAccountResponse | null>(null);
  const [performance, setPerformance] = useState<Performance | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .dashboard()
      .then(setDashboard)
      .catch((e: unknown) => setError(e instanceof ApiError ? e.message : String(e)));
    api.paperAccount().then(setAccount).catch(() => setAccount(null));
    api.paperPerformance().then(setPerformance).catch(() => setPerformance(null));
  }, []);

  const goToSymbol = (symbol: string) => navigate(`/tickers/${encodeURIComponent(symbol)}`);

  return (
    <div className="flex flex-col gap-4 p-6">
      <div>
        <h1 className="text-lg font-semibold text-ink">Dashboard</h1>
        <p className="text-sm text-ink-muted">Market state, candidates, and paper-account performance.</p>
      </div>

      {error && <EmptyState message={`Could not load the dashboard: ${error}`} />}

      <StatusRibbon dashboard={dashboard} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[220px_1fr_1fr]">
        <RegimeCard dashboard={dashboard} />
        <CandidateTable
          title="Top Long"
          rows={dashboard?.top_longs}
          loaded={dashboard !== null}
          onRowClick={goToSymbol}
        />
        <CandidateTable
          title="Top Short"
          rows={dashboard?.top_shorts}
          loaded={dashboard !== null}
          onRowClick={goToSymbol}
        />
      </div>

      <PaperAccountSection account={account} />

      <PerformanceSection performance={performance} />

      <ProviderStatus dashboard={dashboard} />
    </div>
  );
}

function StatusRibbon({ dashboard }: { dashboard: DashboardData | null }) {
  if (!dashboard) return <SkeletonRows rows={1} className="h-8 w-full" />;
  return (
    <div className="flex flex-wrap gap-x-6 gap-y-1 border-b border-gridline pb-2 text-xs text-ink-secondary">
      <span>
        Last refresh: <b className="tabular-nums text-ink">{formatDateTime(dashboard.status.last_refresh)}</b>
      </span>
      <span>
        Last scan: <b className="tabular-nums text-ink">{formatDateTime(dashboard.status.last_scan)}</b>
      </span>
      <span>
        Symbols with data: <b className="tabular-nums text-ink">{dashboard.status.symbols_with_data}</b>
      </span>
    </div>
  );
}

function RegimeCard({ dashboard }: { dashboard: DashboardData | null }) {
  if (!dashboard) return <SkeletonCard lines={1} />;
  const Icon = REGIME_ICON[dashboard.regime.regime] ?? HelpCircle;
  const colorClass = dashboard.regime.regime.startsWith('bull')
    ? 'text-long'
    : dashboard.regime.regime.startsWith('bear')
      ? 'text-short'
      : 'text-ink-muted';
  return (
    <Card title="Regime">
      <div className={`flex items-center gap-2 text-base font-semibold ${colorClass}`}>
        <Icon className="h-5 w-5" strokeWidth={2.25} aria-hidden="true" />
        {dashboard.regime.label}
      </div>
      <p className="mt-1 text-xs text-ink-muted">
        {dashboard.regime.has_data && dashboard.regime.as_of_session
          ? `as of the ${dashboard.regime.as_of_session} scan`
          : 'No scan has run yet'}
      </p>
    </Card>
  );
}

function CandidateTable({
  title,
  rows,
  loaded,
  onRowClick,
}: {
  title: string;
  rows: SignalRow[] | undefined;
  loaded: boolean;
  onRowClick: (symbol: string) => void;
}) {
  return (
    <Card title={title}>
      {!loaded && <SkeletonRows rows={3} className="h-8 w-full" />}
      {loaded && (!rows || rows.length === 0) && (
        <EmptyState message="No candidates in the current signal set." command="claudetrade scan" />
      )}
      {loaded && rows && rows.length > 0 && (
        <ul className="flex flex-col divide-y divide-gridline/60">
          {rows.map((s) => (
            <li key={s.signal_id}>
              <button
                type="button"
                onClick={() => onRowClick(s.symbol)}
                className="flex w-full items-center justify-between gap-2 py-2 text-left hover:text-accent"
              >
                <span className="flex items-center gap-2">
                  <span className="font-semibold text-ink">{s.symbol}</span>
                  <DirectionBadge direction={s.direction} />
                </span>
                <span className="flex items-center gap-3 text-xs text-ink-secondary">
                  <span className="tabular-nums">{s.overall_score.toFixed(0)}</span>
                  <StatusChip status={s.status} />
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function PaperAccountSection({ account }: { account: PaperAccountResponse | null }) {
  return (
    <Card title="Paper Account">
      {!account && <SkeletonCard lines={3} />}
      {account && (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-[repeat(3,minmax(0,1fr))_2fr]">
            <Tile label="Equity" value={formatCurrency(account.account.equity)} />
            <Tile label="Cash" value={formatCurrency(account.account.cash)} />
            <Tile label="Realised P&L" value={formatCurrency(account.account.realised_pnl)} />
            <div className="rounded-lg border border-gridline bg-page/40 px-3 py-2">
              <p className="text-xs text-ink-muted">Equity, recorded history</p>
              {account.equity_curve.length > 0 ? (
                <Sparkline
                  sessions={account.equity_curve.map((p) => p.session)}
                  values={account.equity_curve.map((p) => p.equity)}
                />
              ) : (
                <p className="mt-2 text-xs text-ink-muted">{account.equity_curve_note}</p>
              )}
            </div>
          </div>

          {account.account.kill_switch_engaged && (
            <div className="flex items-center gap-2 rounded-lg bg-critical/15 px-3 py-2 text-sm font-semibold text-critical">
              <AlertTriangle className="h-4 w-4" strokeWidth={2.25} aria-hidden="true" />
              Kill switch engaged -- no new entries will be accepted
            </div>
          )}

          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Open positions
            </p>
            {account.positions.length === 0 ? (
              <EmptyState message="No open paper positions." command="claudetrade paper open <signal-id>" />
            ) : (
              <PositionsTable account={account} />
            )}
          </div>

          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Recent wins / losses
            </p>
            {account.closed_trades.length === 0 ? (
              <EmptyState
                message="No closed paper trades yet -- outcomes appear here once a position hits its stop, target, or time stop."
                command="claudetrade paper process"
              />
            ) : (
              <ClosedTradesTable account={account} />
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

function PositionsTable({ account }: { account: PaperAccountResponse }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
            <th className="py-2 pr-4 font-medium">Symbol</th>
            <th className="py-2 pr-4 font-medium">Direction</th>
            <th className="py-2 pr-4 font-medium">Shares</th>
            <th className="py-2 pr-4 font-medium">Entry</th>
            <th className="py-2 pr-4 font-medium">Last</th>
            <th className="py-2 pr-4 font-medium">Unreal. P&L</th>
            <th className="py-2 font-medium">Needs Attention</th>
          </tr>
        </thead>
        <tbody>
          {account.positions.map((p) => (
            <tr key={p.trade_id} className="border-b border-gridline/60">
              <td className="py-2 pr-4 font-medium text-ink">{p.symbol}</td>
              <td className="py-2 pr-4">
                <DirectionBadge direction={p.direction} />
              </td>
              <td className="py-2 pr-4 tabular-nums text-ink-secondary">{p.shares}</td>
              <td className="py-2 pr-4 tabular-nums text-ink-secondary">{formatPrice(p.entry_price)}</td>
              <td className="py-2 pr-4 tabular-nums text-ink-secondary">{formatPrice(p.last_price)}</td>
              <td
                className={`py-2 pr-4 tabular-nums font-medium ${p.unrealised_pnl >= 0 ? 'text-long' : 'text-short'}`}
              >
                {formatCurrency(p.unrealised_pnl)}
              </td>
              <td className="py-2 text-ink-secondary">{p.needs_attention.join('; ') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ClosedTradesTable({ account }: { account: PaperAccountResponse }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
            <th className="py-2 pr-4 font-medium">Symbol</th>
            <th className="py-2 pr-4 font-medium">Exit</th>
            <th className="py-2 pr-4 font-medium">Outcome</th>
            <th className="py-2 pr-4 font-medium">Net P&L</th>
            <th className="py-2 font-medium">R-Multiple</th>
          </tr>
        </thead>
        <tbody>
          {account.closed_trades.map((t) => (
            <tr key={t.trade_id} className="border-b border-gridline/60">
              <td className="py-2 pr-4 font-medium text-ink">{t.symbol}</td>
              <td className="py-2 pr-4 text-ink-secondary">{t.exit_session}</td>
              <td className="py-2 pr-4 uppercase text-ink-secondary">{t.outcome ?? '—'}</td>
              <td className={`py-2 pr-4 tabular-nums font-medium ${t.net_pnl >= 0 ? 'text-long' : 'text-short'}`}>
                {formatCurrency(t.net_pnl)}
              </td>
              <td className="py-2 tabular-nums text-ink-secondary">{t.r_multiple.toFixed(2)}R</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PerformanceSection({ performance }: { performance: Performance | null }) {
  return (
    <Card title="Performance Snapshot (paper account)">
      {!performance && <SkeletonCard lines={2} />}
      {performance && (
        <div className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Tile label="Win/Loss Ratio" value={performance.win_loss_display} />
            <Tile
              label="Expectancy"
              value={performance.expectancy !== null ? formatCurrency(performance.expectancy) : '—'}
            />
            <Tile
              label="Max Drawdown"
              value={performance.max_drawdown_pct !== null ? `${performance.max_drawdown_pct.toFixed(2)}%` : '—'}
              unavailableReason={performance.max_drawdown_note}
            />
            <Tile label="Closed Trades" value={String(performance.closed_trades)} />
          </div>
          {!performance.is_significant && performance.significance_reason && (
            <p className="rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">
              Not yet statistically significant: {performance.significance_reason}.
            </p>
          )}
          {performance.warnings.map((w) => (
            <p key={w} className="text-xs text-serious">
              {w}
            </p>
          ))}
          {performance.average_win !== null && performance.average_loss !== null && (
            <p className="text-xs text-ink-muted">
              Avg win {formatCurrency(performance.average_win)} · Avg loss{' '}
              {formatCurrency(performance.average_loss)} · Profit factor{' '}
              {performance.profit_factor_display}
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

function ProviderStatus({ dashboard }: { dashboard: DashboardData | null }) {
  return (
    <Card title="Data Provider Status">
      {!dashboard && <SkeletonRows rows={2} className="h-8 w-full" />}
      {dashboard && dashboard.providers.length === 0 && (
        <EmptyState message="No provider status available." />
      )}
      {dashboard && dashboard.providers.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
                <th className="py-2 pr-4 font-medium">Provider</th>
                <th className="py-2 pr-4 font-medium">Kind</th>
                <th className="py-2 pr-4 font-medium">Available</th>
                <th className="py-2 pr-4 font-medium">Configured</th>
                <th className="py-2 font-medium">Message</th>
              </tr>
            </thead>
            <tbody>
              {dashboard.providers.map((p) => (
                <tr key={p.name} className="border-b border-gridline/60">
                  <td className="py-2 pr-4 font-medium text-ink">{p.name}</td>
                  <td className="py-2 pr-4 text-ink-secondary">{p.kind}</td>
                  <td className={`py-2 pr-4 font-medium ${p.available ? 'text-good' : 'text-critical'}`}>
                    {p.available ? 'yes' : 'no'}
                  </td>
                  <td className="py-2 pr-4 text-ink-secondary">{p.configured ? 'yes' : 'no'}</td>
                  <td className="py-2 text-ink-secondary">{p.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Tile({
  label,
  value,
  unavailableReason,
}: {
  label: string;
  value: string;
  unavailableReason?: string | null;
}) {
  return (
    <div className="rounded-lg border border-gridline bg-page/40 px-3 py-2">
      <p className="text-xs text-ink-muted">{label}</p>
      <p className="mt-0.5 text-base font-semibold tabular-nums text-ink">
        {unavailableReason && value === '—' ? 'n/a' : value}
      </p>
      {unavailableReason && value === '—' && <p className="mt-0.5 text-[11px] text-ink-muted">{unavailableReason}</p>}
    </div>
  );
}
