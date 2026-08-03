import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AgGridReact } from 'ag-grid-react';
import type { ColDef, ICellRendererParams, RowClickedEvent } from 'ag-grid-community';
import { ChevronDown, RefreshCw, ScanSearch, Search } from 'lucide-react';
import '../grid/register';
import { claudeTradeGridTheme } from '../grid/theme';
import { api, ApiError } from '../api/client';
import type { RejectedCandidate, ScanFunnel, SignalRow } from '../api/types';
import { Card } from '../components/Card';
import { EmptyState } from '../components/EmptyState';
import { SkeletonRows } from '../components/Skeleton';
import { DirectionBadge } from '../components/DirectionBadge';
import { StatusChip } from '../components/StatusChip';
import { ScoreBar, ConfidenceBar } from '../components/ScoreBar';
import { ResearchBadge } from '../components/ResearchBadge';
import { RejectionFunnelPanel } from '../components/RejectionFunnelPanel';
import { BuzzCell, MentionsCell, SentimentBar, TrendSparkline } from '../components/AttentionCells';
import { formatPrice, formatRatio, daysToEarningsLabel } from '../lib/format';

export const columnDefs: ColDef<SignalRow>[] = [
  {
    field: 'symbol',
    headerName: 'Symbol',
    pinned: 'left',
    width: 150,
    sortable: true,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => {
      const companyName = p.data?.company_name;
      return (
        <div className="flex h-full flex-col justify-center leading-tight">
          <span className="font-semibold text-ink">{p.data?.symbol}</span>
          {companyName && <span className="truncate text-xs text-ink-secondary">{companyName}</span>}
        </div>
      );
    },
  },
  {
    field: 'strategy',
    headerName: 'Strategy',
    width: 190,
    sortable: true,
    // corroborating_strategies (other strategies that independently agree
    // on the same symbol+direction -- see signals.dedupe, Python) shows as
    // a small muted "+other_strategy" annotation, mirroring how the Score
    // column already annotates research-adjusted rows with ResearchBadge.
    cellRenderer: (p: ICellRendererParams<SignalRow>) => {
      const corroborating = p.data?.corroborating_strategies ?? [];
      return (
        <div className="flex h-full flex-col justify-center leading-tight">
          <span className="text-ink">{p.data?.strategy}</span>
          {corroborating.length > 0 && (
            <span
              className="truncate text-[10px] text-ink-muted"
              title={`Also flagged by: ${corroborating.join(', ')}`}
            >
              {corroborating.map((s) => `+${s}`).join(', ')}
            </span>
          )}
        </div>
      );
    },
  },
  {
    field: 'direction',
    headerName: 'Direction',
    width: 120,
    sortable: true,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => <DirectionBadge direction={p.value} />,
  },
  {
    field: 'effective_score',
    headerName: 'Score',
    width: 190,
    sortable: true,
    sort: 'desc',
    // Sorts/filters by the research-adjusted score -- what the ledger's own
    // re-rank (mcp_server.get_signals) uses -- not the frozen engine number.
    cellRenderer: (p: ICellRendererParams<SignalRow>) => {
      const row = p.data;
      return (
        <div className="flex h-full items-center gap-1.5">
          <ScoreBar value={p.value} />
          {row && row.has_research && <ResearchBadge engineScore={row.overall_score} />}
        </div>
      );
    },
  },
  {
    field: 'confidence',
    headerName: 'Confidence',
    width: 130,
    sortable: true,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => <ConfidenceBar value={p.value} />,
  },
  {
    field: 'reward_risk_ratio',
    headerName: 'R:R',
    width: 90,
    sortable: true,
    valueFormatter: (p) => formatRatio(p.value),
  },
  {
    headerName: 'Entry Zone',
    width: 150,
    sortable: true,
    valueGetter: (p) => p.data?.entry_low ?? 0,
    valueFormatter: (p) => `${formatPrice(p.data?.entry_low)} – ${formatPrice(p.data?.entry_high)}`,
  },
  {
    field: 'days_to_earnings',
    headerName: 'Days to Earn.',
    width: 130,
    sortable: true,
    valueFormatter: (p) => daysToEarningsLabel(p.value),
  },
  {
    headerName: 'Buzz',
    width: 100,
    sortable: true,
    // Nulls (no Adanos data) sort below every real reading in both directions.
    valueGetter: (p) => p.data?.attention?.buzz_score ?? -1,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => <BuzzCell attention={p.data?.attention ?? null} />,
  },
  {
    headerName: 'Mentions',
    width: 140,
    sortable: true,
    valueGetter: (p) => p.data?.attention?.total_mentions ?? -1,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => (
      <MentionsCell attention={p.data?.attention ?? null} />
    ),
  },
  {
    headerName: 'Sentiment',
    width: 140,
    sortable: true,
    valueGetter: (p) => p.data?.attention?.bullish_pct ?? -1,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => (
      <SentimentBar attention={p.data?.attention ?? null} />
    ),
  },
  {
    headerName: 'Trend',
    width: 100,
    sortable: true,
    // Sorts by the most recent point in the combined buzz history.
    valueGetter: (p) => {
      const history = p.data?.attention?.trend_history;
      return history && history.length === 7 ? history[6] : -1;
    },
    cellRenderer: (p: ICellRendererParams<SignalRow>) => (
      <TrendSparkline attention={p.data?.attention ?? null} />
    ),
  },
  {
    field: 'status',
    headerName: 'Status',
    width: 140,
    sortable: true,
    cellRenderer: (p: ICellRendererParams<SignalRow>) => <StatusChip status={p.value} />,
  },
];

const DIRECTIONS: Array<'long' | 'short'> = ['long', 'short'];
const FILTER_STORAGE_KEY = 'claudetrade.screener.filters';

type PersistedFilters = {
  minScore: number;
  minConfidence: number;
  maxDaysToEarnings: number | null;
};

const DEFAULT_FILTERS: PersistedFilters = {
  minScore: 0,
  minConfidence: 0,
  maxDaysToEarnings: null,
};

function loadPersistedFilters(): PersistedFilters {
  try {
    const stored = JSON.parse(localStorage.getItem(FILTER_STORAGE_KEY) ?? '{}') as Partial<PersistedFilters>;
    return {
      minScore:
        typeof stored.minScore === 'number' && stored.minScore >= 0 && stored.minScore <= 100
          ? stored.minScore
          : DEFAULT_FILTERS.minScore,
      minConfidence:
        typeof stored.minConfidence === 'number' &&
        stored.minConfidence >= 0 &&
        stored.minConfidence <= 1
          ? stored.minConfidence
          : DEFAULT_FILTERS.minConfidence,
      maxDaysToEarnings:
        stored.maxDaysToEarnings === null ||
        (typeof stored.maxDaysToEarnings === 'number' && stored.maxDaysToEarnings >= 0)
          ? stored.maxDaysToEarnings
          : DEFAULT_FILTERS.maxDaysToEarnings,
    };
  } catch {
    return DEFAULT_FILTERS;
  }
}

/** The Screener: the owner's original complaint ("I can't click and go on
 * the screener to show the details of the setups") is fixed entirely by
 * `onRowClicked` below plus the pointer cursor / hover highlight from the
 * grid theme -- there is no separate "select a row, then pick from a
 * dropdown" step the old Streamlit table needed. */
export function Screener() {
  const navigate = useNavigate();
  const [tickerSearch, setTickerSearch] = useState('');
  const [allSignals, setAllSignals] = useState<SignalRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [directions, setDirections] = useState<Set<string>>(new Set(DIRECTIONS));
  const [persistedFilters] = useState(loadPersistedFilters);
  const [minScore, setMinScore] = useState(persistedFilters.minScore);
  const [minConfidence, setMinConfidence] = useState(persistedFilters.minConfidence);
  const [maxDaysToEarnings, setMaxDaysToEarnings] = useState<number | null>(
    persistedFilters.maxDaysToEarnings,
  );
  const [strategies, setStrategies] = useState<Set<string> | null>(null);

  const [rejected, setRejected] = useState<{
    available: boolean;
    reason: string | null;
    rejected: RejectedCandidate[];
    funnel: ScanFunnel | null;
  } | null>(null);
  const [rejectedOpen, setRejectedOpen] = useState(false);

  const [scanning, setScanning] = useState(false);
  const [scanMessage, setScanMessage] = useState<string | null>(null);

  const loadSignals = useCallback(() => {
    setLoadError(null);
    api
      .listSignals({ limit: 2000 })
      .then((r) => {
        setAllSignals(r.signals);
        setStrategies((prev) => prev ?? new Set(r.signals.map((s) => s.strategy)));
      })
      .catch((e: unknown) => setLoadError(e instanceof ApiError ? e.message : String(e)));
  }, []);

  const loadRejected = useCallback(() => {
    api
      .rejectedCandidates()
      .then((r) => setRejected(r))
      .catch(() => setRejected(null));
  }, []);

  useEffect(() => {
    loadSignals();
    loadRejected();
  }, [loadSignals, loadRejected]);

  useEffect(() => {
    localStorage.setItem(
      FILTER_STORAGE_KEY,
      JSON.stringify({ minScore, minConfidence, maxDaysToEarnings }),
    );
  }, [minScore, minConfidence, maxDaysToEarnings]);

  const strategyOptions = useMemo(
    () => Array.from(new Set((allSignals ?? []).map((s) => s.strategy))).sort(),
    [allSignals],
  );

  const filtered = useMemo(() => {
    if (!allSignals) return [];
    return allSignals.filter((s) => {
      if (!directions.has(s.direction)) return false;
      if (s.overall_score < minScore) return false;
      if (s.confidence < minConfidence) return false;
      if (strategies && strategies.size > 0 && !strategies.has(s.strategy)) return false;
      if (
        maxDaysToEarnings !== null &&
        (s.days_to_earnings === null || s.days_to_earnings > maxDaysToEarnings)
      ) {
        return false;
      }
      return true;
    });
  }, [allSignals, directions, minScore, minConfidence, strategies, maxDaysToEarnings]);

  function toggleDirection(d: string) {
    setDirections((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });
  }

  function toggleStrategy(s: string) {
    setStrategies((prev) => {
      const base = prev ?? new Set(strategyOptions);
      const next = new Set(base);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  }

  async function runScan() {
    setScanning(true);
    setScanMessage(null);
    try {
      const result = await api.runScan({ generate_thesis: false });
      setScanMessage(
        `Scan complete: ${result.signal_count} signal(s) from ${result.evaluated_symbols} symbols evaluated (${result.rejected_count} rejected).`,
      );
      loadSignals();
      loadRejected();
    } catch (e) {
      setScanMessage(e instanceof ApiError ? `Scan failed: ${e.message}` : String(e));
    } finally {
      setScanning(false);
    }
  }

  const onRowClicked = (event: RowClickedEvent<SignalRow>) => {
    if (event.data) navigate(`/tickers/${encodeURIComponent(event.data.symbol)}`);
  };

  function searchTicker(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const symbol = tickerSearch.trim().toUpperCase();
    if (symbol) navigate(`/tickers/${encodeURIComponent(symbol)}`);
  }

  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-ink">Screener</h1>
          <p className="text-sm text-ink-muted">
            Click a row to open its full ticker detail. Sort or filter any column.
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <form onSubmit={searchTicker} className="flex items-center gap-2" role="search">
            <label htmlFor="ticker-sentiment-search" className="sr-only">
              Search ticker sentiment
            </label>
            <input
              id="ticker-sentiment-search"
              type="search"
              value={tickerSearch}
              onChange={(event) => setTickerSearch(event.target.value)}
              placeholder="Ticker (e.g. AAPL)"
              autoComplete="off"
              className="w-40 rounded-lg border border-gridline bg-surface px-3 py-2 text-sm uppercase text-ink placeholder:normal-case"
            />
            <button
              type="submit"
              disabled={!tickerSearch.trim()}
              className="flex items-center gap-2 rounded-lg border border-gridline bg-surface px-3 py-2 text-sm font-semibold text-ink hover:bg-surface-2 disabled:opacity-50"
            >
              <Search className="h-4 w-4" strokeWidth={2} aria-hidden="true" />
              View sentiment
            </button>
          </form>
          <button
            type="button"
            onClick={runScan}
            disabled={scanning}
            className="flex items-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-white hover:bg-accent-strong disabled:opacity-60"
          >
            <ScanSearch className={`h-4 w-4 ${scanning ? 'animate-pulse' : ''}`} strokeWidth={2} aria-hidden="true" />
            {scanning ? 'Scanning…' : 'Run Scan'}
          </button>
        </div>
      </div>

      {scanMessage && <p className="text-xs text-ink-secondary">{scanMessage}</p>}

      <Card padded={false} className="p-3">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="flex items-center gap-1.5">
            {DIRECTIONS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => toggleDirection(d)}
                className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide transition-colors ${
                  directions.has(d)
                    ? d === 'long'
                      ? 'bg-long/15 text-long'
                      : 'bg-short/15 text-short'
                    : 'bg-surface-2 text-ink-muted'
                }`}
              >
                {d}
              </button>
            ))}
          </div>

          <label className="flex items-center gap-2 text-xs text-ink-secondary">
            Min score
            <input
              type="range"
              min={0}
              max={100}
              value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
              className="accent-accent"
            />
            <span className="w-8 tabular-nums text-ink">{minScore}</span>
          </label>

          <label className="flex items-center gap-2 text-xs text-ink-secondary">
            Min confidence
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value))}
              className="accent-accent"
            />
            <span className="w-10 tabular-nums text-ink">{(minConfidence * 100).toFixed(0)}%</span>
          </label>

          <label className="flex items-center gap-2 text-xs text-ink-secondary">
            Max days to earnings
            <input
              type="number"
              min={0}
              placeholder="none"
              value={maxDaysToEarnings ?? ''}
              onChange={(e) => setMaxDaysToEarnings(e.target.value === '' ? null : Number(e.target.value))}
              className="w-16 rounded-md border border-gridline bg-page px-2 py-1 text-ink"
            />
          </label>

          <div className="flex flex-wrap items-center gap-1.5">
            {strategyOptions.map((s) => {
              const active = strategies ? strategies.has(s) : true;
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => toggleStrategy(s)}
                  className={`rounded-full px-2.5 py-1 text-xs font-medium transition-colors ${
                    active ? 'bg-accent/15 text-accent' : 'bg-surface-2 text-ink-muted'
                  }`}
                >
                  {s}
                </button>
              );
            })}
          </div>
        </div>
      </Card>

      {loadError && <EmptyState message={`Error loading signals: ${loadError}`} icon={RefreshCw} />}

      {!loadError && allSignals === null && <SkeletonRows rows={8} className="h-10 w-full" />}

      {!loadError && allSignals !== null && allSignals.length === 0 && (
        <>
          <EmptyState
            message="No signals generated yet -- the ledger is empty for this database."
            command="claudetrade scan"
          />
          {rejected?.available && rejected.funnel && (
            <RejectionFunnelPanel funnel={rejected.funnel} />
          )}
        </>
      )}

      {!loadError && allSignals !== null && allSignals.length > 0 && filtered.length === 0 && (
        <EmptyState message="No signals match the selected filters. Loosen a filter above to see candidates." />
      )}

      {!loadError && filtered.length > 0 && (
        <div className="ct-grid-shell" style={{ height: Math.min(720, 90 + filtered.length * 40) }}>
          <AgGridReact
            theme={claudeTradeGridTheme}
            rowData={filtered}
            columnDefs={columnDefs}
            onRowClicked={onRowClicked}
            getRowId={(p) => p.data.signal_id}
            animateRows
            rowSelection={{ mode: 'singleRow', enableClickSelection: false }}
          />
        </div>
      )}

      <details
        className="rounded-xl border border-gridline bg-surface p-4"
        open={rejectedOpen}
        onToggle={(e) => setRejectedOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-semibold text-ink">
          <ChevronDown
            className={`h-4 w-4 transition-transform ${rejectedOpen ? 'rotate-0' : '-rotate-90'}`}
            strokeWidth={2}
            aria-hidden="true"
          />
          Near-miss / rejected candidates
          {rejected?.available && (
            <span className="text-xs font-normal text-ink-muted">
              ({rejected.rejected.length})
            </span>
          )}
        </summary>
        <div className="mt-3">
          {!rejected?.available && (
            <EmptyState
              message={
                rejected?.reason ??
                'Rejected candidates are only available for the scan just run in this session.'
              }
              command="Run Scan"
            />
          )}
          {rejected?.available && rejected.rejected.length === 0 && (
            <p className="text-sm text-ink-secondary">
              No rejected candidates in the last scan -- everything evaluated cleared every gate.
            </p>
          )}
          {rejected?.available && rejected.rejected.length > 0 && (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
                  <th className="py-2 pr-4 font-medium">Symbol</th>
                  <th className="py-2 pr-4 font-medium">Strategy</th>
                  <th className="py-2 pr-4 font-medium">Stage</th>
                  <th className="py-2 font-medium">Reasons</th>
                </tr>
              </thead>
              <tbody>
                {rejected.rejected.map((r, i) => (
                  <tr key={`${r.symbol}-${r.strategy}-${i}`} className="border-b border-gridline/60">
                    <td className="py-2 pr-4 font-medium text-ink">{r.symbol}</td>
                    <td className="py-2 pr-4 text-ink-secondary">{r.strategy}</td>
                    <td className="py-2 pr-4 text-ink-secondary">{r.stage}</td>
                    <td className="py-2 text-ink-secondary">{r.reasons.join('; ') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </details>
    </div>
  );
}
