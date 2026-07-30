import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ChevronDown, TrendingUp } from 'lucide-react';
import { api, ApiError } from '../api/client';
import type { SignalDetail, TickerDetail as TickerDetailData } from '../api/types';
import { Card } from '../components/Card';
import { EmptyState } from '../components/EmptyState';
import { Skeleton, SkeletonCard } from '../components/Skeleton';
import { DirectionBadge } from '../components/DirectionBadge';
import { StatusChip } from '../components/StatusChip';
import { TickerChart } from '../charts/TickerChart';
import { formatDate, formatPrice, formatConfidence } from '../lib/format';

const LOOKBACK_OPTIONS = [60, 90, 180, 365, 1000];

export function TickerDetailScreen() {
  const { symbol = '' } = useParams<{ symbol: string }>();
  const navigate = useNavigate();

  const [symbols, setSymbols] = useState<string[] | null>(null);
  const [lookbackDays, setLookbackDays] = useState(180);
  const [detail, setDetail] = useState<TickerDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openTradeMessage, setOpenTradeMessage] = useState<string | null>(null);
  const [openingTrade, setOpeningTrade] = useState(false);
  const [thesisOpen, setThesisOpen] = useState(false);

  useEffect(() => {
    api.listTickers().then(setSymbols).catch(() => setSymbols([]));
  }, []);

  const load = useCallback(() => {
    if (!symbol) return;
    setError(null);
    setDetail(null);
    api
      .tickerDetail(symbol, lookbackDays)
      .then(setDetail)
      .catch((e: unknown) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [symbol, lookbackDays]);

  useEffect(() => {
    load();
  }, [load]);

  async function openPaperTrade(sig: SignalDetail) {
    setOpeningTrade(true);
    setOpenTradeMessage(null);
    try {
      const result = await api.paperOpen(sig.signal_id);
      setOpenTradeMessage(result.message);
    } catch (e) {
      setOpenTradeMessage(e instanceof ApiError ? e.message : String(e));
    } finally {
      setOpeningTrade(false);
    }
  }

  const sig = detail?.current_signal ?? null;
  const showLevels = sig !== null && sig.status !== 'expired' && sig.status !== 'rejected';

  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-ink">Ticker Detail</h1>
          <p className="text-sm text-ink-muted">
            Full technical, signal, and sentiment picture for one symbol.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={symbol}
            onChange={(e) => navigate(`/tickers/${encodeURIComponent(e.target.value)}`)}
            className="rounded-lg border border-gridline bg-surface px-3 py-2 text-sm text-ink"
          >
            {(symbols ?? [symbol]).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            value={lookbackDays}
            onChange={(e) => setLookbackDays(Number(e.target.value))}
            className="rounded-lg border border-gridline bg-surface px-3 py-2 text-sm text-ink"
          >
            {LOOKBACK_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}d lookback
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && <EmptyState message={`Error loading ${symbol}: ${error}`} />}

      {!error && !detail && (
        <div className="flex flex-col gap-4">
          <SkeletonCard lines={3} />
          <Skeleton className="h-96 w-full" />
        </div>
      )}

      {!error && detail && (
        <>
          <Card title={`Current Signal: ${detail.symbol}`}>
            {!sig && (
              <EmptyState message={`No signals recorded for ${detail.symbol} yet.`} command="claudetrade scan" />
            )}
            {sig && (
              <div className="flex flex-col gap-4">
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                  <Stat label="Score" value={sig.overall_score.toFixed(0)} />
                  <Stat label="Confidence" value={formatConfidence(sig.confidence)} />
                  <Stat label="Status" value={<StatusChip status={sig.status} />} />
                  <Stat label="Direction" value={<DirectionBadge direction={sig.direction} />} />
                  <Stat
                    label="Entry Zone"
                    value={`${formatPrice(sig.plan.entry_low)} – ${formatPrice(sig.plan.entry_high)}`}
                  />
                  <Stat label="Stop Loss" value={formatPrice(sig.plan.stop_loss)} />
                  <Stat label="R:R" value={`${sig.plan.reward_risk_ratio.toFixed(2)}:1`} />
                  <Stat label="Next Earnings" value={formatDate(sig.next_earnings_date)} />
                </div>

                <details
                  open={thesisOpen}
                  onToggle={(e) => setThesisOpen((e.target as HTMLDetailsElement).open)}
                  className="rounded-lg border border-gridline bg-page/40 p-3"
                >
                  <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium text-ink">
                    <ChevronDown
                      className={`h-4 w-4 transition-transform ${thesisOpen ? '' : '-rotate-90'}`}
                      strokeWidth={2}
                      aria-hidden="true"
                    />
                    Thesis, invalidation and component scores
                  </summary>
                  <div className="mt-3 flex flex-col gap-3 text-sm">
                    <p>
                      <span className="font-medium text-ink-secondary">Thesis: </span>
                      {sig.thesis || 'No thesis available'}
                    </p>
                    <p>
                      <span className="font-medium text-ink-secondary">Invalidation: </span>
                      {sig.invalidation.length ? sig.invalidation.join(', ') : 'none recorded'}
                    </p>
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                      {Object.entries(sig.components).map(([name, value]) => (
                        <Stat
                          key={name}
                          label={name.replace(/_/g, ' ')}
                          value={(value as number).toFixed(0)}
                          small
                        />
                      ))}
                    </div>
                  </div>
                </details>

                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => openPaperTrade(sig)}
                    disabled={openingTrade}
                    className="flex items-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent-strong disabled:opacity-60"
                  >
                    <TrendingUp className="h-4 w-4" strokeWidth={2} aria-hidden="true" />
                    {openingTrade ? 'Submitting…' : `Open Paper Trade: ${sig.symbol}`}
                  </button>
                  {openTradeMessage && <p className="text-xs text-ink-secondary">{openTradeMessage}</p>}
                </div>
              </div>
            )}
          </Card>

          <Card title="Price Action">
            <TickerChart
              symbol={detail.symbol}
              bars={detail.bars}
              indicators={detail.indicators}
              sentiment={detail.sentiment}
              earningsDates={detail.earnings_dates}
              entryLow={showLevels ? sig?.plan.entry_low : undefined}
              entryHigh={showLevels ? sig?.plan.entry_high : undefined}
              stopLoss={showLevels ? sig?.plan.stop_loss : undefined}
              targets={showLevels ? sig?.plan.targets : []}
            />
            {detail.price_note && <EmptyState message={detail.price_note} command="claudetrade refresh" />}
            {detail.sentiment_note && (
              <p className="mt-2 text-xs text-ink-muted">{detail.sentiment_note}</p>
            )}
          </Card>

          <Card title="Signal History">
            {detail.signal_history.length === 0 ? (
              <EmptyState message="No signal history for this symbol yet." command="claudetrade scan" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
                      <th className="py-2 pr-4 font-medium">Session</th>
                      <th className="py-2 pr-4 font-medium">Strategy</th>
                      <th className="py-2 pr-4 font-medium">Direction</th>
                      <th className="py-2 pr-4 font-medium">Score</th>
                      <th className="py-2 pr-4 font-medium">Status</th>
                      <th className="py-2 font-medium">Entry Zone</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.signal_history.map((s) => (
                      <tr key={s.signal_id} className="border-b border-gridline/60">
                        <td className="py-2 pr-4 text-ink-secondary">{s.session}</td>
                        <td className="py-2 pr-4 text-ink-secondary">{s.strategy}</td>
                        <td className="py-2 pr-4">
                          <DirectionBadge direction={s.direction} />
                        </td>
                        <td className="py-2 pr-4 tabular-nums text-ink">{s.overall_score.toFixed(0)}</td>
                        <td className="py-2 pr-4">
                          <StatusChip status={s.status} />
                        </td>
                        <td className="py-2 tabular-nums text-ink-secondary">
                          {formatPrice(s.entry_low)} – {formatPrice(s.entry_high)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, small = false }: { label: string; value: ReactNode; small?: boolean }) {
  return (
    <div className="rounded-lg border border-gridline bg-page/40 px-3 py-2">
      <p className={`capitalize text-ink-muted ${small ? 'text-[11px]' : 'text-xs'}`}>{label}</p>
      <div className={`mt-0.5 font-semibold text-ink ${small ? 'text-sm' : 'text-base'}`}>{value}</div>
    </div>
  );
}
