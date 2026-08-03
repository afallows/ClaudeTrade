/**
 * Component test for the Screener's marquee interaction: clicking a row
 * navigates straight to that symbol's ticker detail. This is the exact
 * complaint from the owner's verdict ("I can't click and go on the
 * screener to show the details of the setups") that this rebuild exists to
 * fix, so it gets a dedicated regression test.
 *
 * AG Grid's real rendering is virtualised and leans on browser layout APIs
 * (ResizeObserver, getBoundingClientRect) that jsdom only partially
 * implements, which makes asserting against its real DOM output slow and
 * environment-fragile. `ag-grid-react` is stubbed here with a minimal fake
 * that renders one row per item and calls the *real* `onRowClicked` handler
 * Screener.tsx wires up -- so this test exercises the actual navigation
 * logic under test, not AG Grid's internals (which are AG Grid's own
 * responsibility to test).
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider, useParams } from 'react-router-dom';
import { describe, it, expect, vi, afterEach } from 'vitest';
import type { ICellRendererParams } from 'ag-grid-community';
import type { SignalRow } from '../api/types';

vi.mock('ag-grid-react', () => ({
  AgGridReact: (props: {
    rowData: SignalRow[];
    onRowClicked: (event: { data: SignalRow }) => void;
  }) => (
    <div data-testid="fake-grid">
      {props.rowData.map((row) => (
        <div
          key={row.signal_id}
          data-testid={`row-${row.symbol}`}
          onClick={() => props.onRowClicked({ data: row })}
        >
          {row.symbol}
        </div>
      ))}
    </div>
  ),
}));

const listSignals = vi.fn();
const rejectedCandidates = vi.fn();
const runScan = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    listSignals: (...args: unknown[]) => listSignals(...args),
    rejectedCandidates: (...args: unknown[]) => rejectedCandidates(...args),
    runScan: (...args: unknown[]) => runScan(...args),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

// Imported after the mocks above so Screener picks up the mocked client.
const { Screener, columnDefs } = await import('./Screener');

function TickerDetailStub() {
  const { symbol } = useParams<{ symbol: string }>();
  return <div data-testid="ticker-detail">detail for {symbol}</div>;
}

function renderScreener() {
  const router = createMemoryRouter(
    [
      { path: '/screener', element: <Screener /> },
      { path: '/tickers/:symbol', element: <TickerDetailStub /> },
    ],
    { initialEntries: ['/screener'] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const SIGNAL: SignalRow = {
  signal_id: 'sig-1',
  symbol: 'AAPL',
  company_name: 'Apple Inc',
  strategy: 'sentiment_breakout',
  direction: 'long',
  status: 'actionable',
  regime: 'bull_quiet',
  overall_score: 82,
  effective_score: 82,
  has_research: false,
  confidence: 0.7,
  reward_risk_ratio: 2.5,
  entry_low: 190,
  entry_high: 195,
  stop_loss: 180,
  days_to_earnings: 20,
  session: '2024-06-28',
  created_at: '2024-06-28T15:00:00Z',
  attention: null,
  corroborating_strategies: [],
  corroborating_count: 0,
  duplicates_collapsed: 0,
};

describe('Screener strategy column corroborating annotation', () => {
  function renderStrategyCell(row: SignalRow) {
    const strategyColumn = columnDefs.find((c) => c.field === 'strategy');
    const rendered = strategyColumn!.cellRenderer!({
      data: row,
    } as ICellRendererParams<SignalRow>);
    render(<>{rendered}</>);
  }

  it('shows a muted "+strategy" annotation when a corroborating strategy exists', () => {
    renderStrategyCell({ ...SIGNAL, strategy: 'volume_breakout', corroborating_strategies: ['sentiment_pullback'] });
    expect(screen.getByText('+sentiment_pullback')).toBeInTheDocument();
  });

  it('shows no annotation when there is no corroborating strategy', () => {
    renderStrategyCell({ ...SIGNAL, strategy: 'volume_breakout', corroborating_strategies: [] });
    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
  });
});

describe('Screener row click navigation', () => {
  afterEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('navigates to the ticker detail route when a row is clicked', async () => {
    listSignals.mockResolvedValue({ signals: [SIGNAL], total: 1 });
    rejectedCandidates.mockResolvedValue({
      available: false,
      reason: 'POST /api/scan to populate this list.',
      generated_at: null,
      evaluated_symbols: 0,
      rejected: [],
    });

    renderScreener();

    const row = await screen.findByTestId('row-AAPL');
    fireEvent.click(row);

    await waitFor(() => expect(screen.getByTestId('ticker-detail')).toHaveTextContent('AAPL'));
  });

  it('shows an empty state naming the fix when there are no signals yet', async () => {
    listSignals.mockResolvedValue({ signals: [], total: 0 });
    rejectedCandidates.mockResolvedValue({
      available: false,
      reason: null,
      generated_at: null,
      evaluated_symbols: 0,
      rejected: [],
      funnel: null,
    });

    renderScreener();

    expect(await screen.findByText(/ledger is empty/i)).toBeInTheDocument();
    expect(screen.getByText('claudetrade scan')).toBeInTheDocument();
  });

  it('shows the rejection funnel panel alongside the empty state when a scan ran with rejections', async () => {
    listSignals.mockResolvedValue({ signals: [], total: 0 });
    rejectedCandidates.mockResolvedValue({
      available: true,
      reason: null,
      generated_at: '2026-07-31T13:00:00Z',
      evaluated_symbols: 1673,
      rejected: [],
      funnel: {
        top_n: 20,
        total_rejections: 8365,
        by_reason: { illiquid: 4000, score_below_threshold: 4365 },
        by_strategy_reason: {
          sentiment_breakout: { illiquid: 4000 },
          sentiment_pullback: { score_below_threshold: 4365 },
        },
        near_misses: [
          {
            symbol: 'ZZZZ',
            strategy: 'sentiment_pullback',
            reason_code: 'score_below_threshold',
            metric: 46.2,
            threshold: 48.0,
            margin: -1.8,
            overall_score: 46.2,
            confidence: 0.55,
            weakest_components: [['volume_confirmation', 1.0]],
            strongest_components: [['technical_setup', 20.0]],
          },
        ],
      },
    });

    renderScreener();

    expect(await screen.findByText(/ledger is empty/i)).toBeInTheDocument();
    expect(screen.getByText('Why no signals?')).toBeInTheDocument();
    expect(screen.getByText('illiquid')).toBeInTheDocument();
    expect(screen.getByText('ZZZZ')).toBeInTheDocument();
  });

  it('opens sentiment detail for a directly entered ticker', async () => {
    listSignals.mockResolvedValue({ signals: [SIGNAL], total: 1 });
    rejectedCandidates.mockResolvedValue({ available: false, reason: null, rejected: [] });
    renderScreener();

    fireEvent.change(await screen.findByLabelText(/search ticker sentiment/i), {
      target: { value: ' msft ' },
    });
    fireEvent.click(screen.getByRole('button', { name: /view sentiment/i }));

    await waitFor(() => expect(screen.getByTestId('ticker-detail')).toHaveTextContent('MSFT'));
  });

  it('restores numeric filters after navigating away and back', async () => {
    listSignals.mockResolvedValue({ signals: [SIGNAL], total: 1 });
    rejectedCandidates.mockResolvedValue({ available: false, reason: null, rejected: [] });
    const router = renderScreener();

    const score = await screen.findByLabelText(/min score/i);
    const confidence = screen.getByLabelText(/min confidence/i);
    const earnings = screen.getByLabelText(/max days to earnings/i);
    fireEvent.change(score, { target: { value: '65' } });
    fireEvent.change(confidence, { target: { value: '0.55' } });
    fireEvent.change(earnings, { target: { value: '30' } });

    await router.navigate('/tickers/AAPL');
    await router.navigate('/screener');

    expect(await screen.findByLabelText(/min score/i)).toHaveValue('65');
    expect(screen.getByLabelText(/min confidence/i)).toHaveValue('0.55');
    expect(screen.getByLabelText(/max days to earnings/i)).toHaveValue(30);
  });
});
