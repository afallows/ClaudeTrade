import { useMemo } from 'react';
import type { Data, Layout } from 'plotly.js-dist-min';
import Plot from './Plot';
import type { Bar, Indicators, SentimentPoint } from '../api/types';

const LONG_COLOR = '#3987e5';
const SHORT_COLOR = '#e66767';
const ACCENT = '#3987e5';
const GOOD = '#0ca30c';
const CRITICAL = '#d03b3b';
const INK_MUTED = '#898781';
const GRIDLINE = '#2c2c2a';
const SURFACE = '#1a1a19';
const PAGE = '#0d0d0d';

// SMA line shades, index-matched to claudetrade.ui.theme.SEQUENTIAL_BLUE.
const SMA_COLORS: Record<20 | 50 | 200, string> = {
  20: '#6da7ec',
  50: '#256abf',
  200: '#0d366b',
};

interface TickerChartProps {
  symbol: string;
  bars: Bar[];
  indicators: Indicators;
  sentiment: SentimentPoint[];
  earningsDates: string[];
  entryLow?: number | null;
  entryHigh?: number | null;
  stopLoss?: number | null;
  targets?: number[];
  showRsi?: boolean;
  height?: number;
}

function computeDomains(weights: number[], gap: number): [number, number][] {
  const total = weights.reduce((a, b) => a + b, 0) || 1;
  const usable = 1 - gap * Math.max(0, weights.length - 1);
  const norm = weights.map((w) => (w / total) * usable);
  const domains: [number, number][] = [];
  let top = 1;
  for (const h of norm) {
    const bottom = Math.max(0, top - h);
    domains.push([bottom, top]);
    top = bottom - gap;
  }
  return domains;
}

const emptyLayout = (message: string): Partial<Layout> => ({
  paper_bgcolor: PAGE,
  plot_bgcolor: SURFACE,
  height: 200,
  annotations: [
    { text: message, showarrow: false, font: { color: INK_MUTED, size: 14 }, xref: 'paper', yref: 'paper', x: 0.5, y: 0.5 },
  ],
  xaxis: { visible: false },
  yaxis: { visible: false },
});

/**
 * Candlestick + volume + RSI + sentiment/mentions, entry-zone shading,
 * stop/target lines and earnings markers -- one shared time axis per row,
 * never a second y-axis overlaid on the price plot (see ui/charts.py's
 * design rules, mirrored here since the underlying series are computed
 * server-side by claudetrade.webapi and never recomputed in JS).
 */
export function TickerChart({
  symbol,
  bars,
  indicators,
  sentiment,
  earningsDates,
  entryLow,
  entryHigh,
  stopLoss,
  targets = [],
  showRsi = true,
  height = 760,
}: TickerChartProps) {
  const { data, layout } = useMemo(() => {
    if (bars.length === 0) {
      return { data: [] as Data[], layout: emptyLayout('No price history stored for this symbol yet') };
    }

    const dates = bars.map((b) => b.session);
    const rows: Array<'price' | 'volume' | 'rsi' | 'sentiment'> = ['price', 'volume'];
    if (showRsi) rows.push('rsi');
    if (sentiment.length > 0) rows.push('sentiment');
    const weights: Record<string, number> = { price: 0.55, volume: 0.15, rsi: 0.15, sentiment: 0.15 };
    const domains = computeDomains(
      rows.map((r) => weights[r]),
      0.04,
    );
    const axisSuffix = (i: number) => (i === 0 ? '' : String(i + 1));
    const rowIndex = Object.fromEntries(rows.map((r, i) => [r, i])) as Record<string, number>;

    const traces: Data[] = [];

    // --- price row -----------------------------------------------------
    const priceAxis = axisSuffix(rowIndex.price);
    traces.push({
      type: 'candlestick',
      x: dates,
      open: bars.map((b) => b.open),
      high: bars.map((b) => b.high),
      low: bars.map((b) => b.low),
      close: bars.map((b) => b.close),
      name: 'OHLC',
      increasing: { line: { color: LONG_COLOR } },
      decreasing: { line: { color: SHORT_COLOR } },
      xaxis: `x${priceAxis}`,
      yaxis: `y${priceAxis}`,
    } as Data);

    (
      [
        ['sma_20', 20],
        ['sma_50', 50],
        ['sma_200', 200],
      ] as const
    ).forEach(([key, window]) => {
      const series = indicators[key];
      if (!series.some((v) => v !== null)) return;
      traces.push({
        type: 'scatter',
        mode: 'lines',
        x: dates,
        y: series,
        name: `SMA ${window}`,
        line: { color: SMA_COLORS[window], width: 1.5 },
        xaxis: `x${priceAxis}`,
        yaxis: `y${priceAxis}`,
        connectgaps: false,
      } as Data);
    });

    // --- volume row ------------------------------------------------------
    const volumeAxis = axisSuffix(rowIndex.volume);
    traces.push({
      type: 'bar',
      x: dates,
      y: bars.map((b) => b.volume),
      name: 'Volume',
      marker: { color: bars.map((b) => (b.close >= b.open ? LONG_COLOR : SHORT_COLOR)), opacity: 0.75 },
      showlegend: false,
      xaxis: `x${volumeAxis}`,
      yaxis: `y${volumeAxis}`,
    } as Data);

    // --- RSI row -----------------------------------------------------------
    if (showRsi) {
      const rsiAxis = axisSuffix(rowIndex.rsi);
      traces.push({
        type: 'scatter',
        mode: 'lines',
        x: dates,
        y: indicators.rsi_14,
        name: 'RSI',
        line: { color: ACCENT, width: 1.5 },
        showlegend: false,
        xaxis: `x${rsiAxis}`,
        yaxis: `y${rsiAxis}`,
      } as Data);
    }

    // --- sentiment row -------------------------------------------------
    if (sentiment.length > 0) {
      const sentAxis = axisSuffix(rowIndex.sentiment);
      traces.push({
        type: 'bar',
        x: sentiment.map((p) => p.session),
        y: sentiment.map((p) => p.post_count),
        name: 'Mentions (blue=bullish, red=bearish)',
        marker: { color: sentiment.map((p) => (p.bull_bear_ratio >= 1.0 ? LONG_COLOR : SHORT_COLOR)) },
        showlegend: false,
        xaxis: `x${sentAxis}`,
        yaxis: `y${sentAxis}`,
      } as Data);
    }

    // --- shapes: entry zone, stop, targets, earnings --------------------
    // Plotly's axis-reference types (`XAxisName`/`YAxisName`) are template
    // literals over a fixed axis-count union; this file computes axis
    // suffixes dynamically (rows are assembled from which series are
    // present), so shapes are built loosely and cast once at the end
    // instead of fighting that template type at every push.
    const shapes: Record<string, unknown>[] = [];
    const firstDate = dates[0];
    const lastDate = dates[dates.length - 1];

    if (entryLow != null && entryHigh != null) {
      shapes.push({
        type: 'rect',
        xref: 'x',
        yref: 'y',
        x0: firstDate,
        x1: lastDate,
        y0: entryLow,
        y1: entryHigh,
        fillcolor: ACCENT,
        opacity: 0.15,
        line: { width: 0 },
      });
    }
    if (stopLoss != null) {
      shapes.push({
        type: 'line',
        xref: 'x',
        yref: 'y',
        x0: firstDate,
        x1: lastDate,
        y0: stopLoss,
        y1: stopLoss,
        line: { color: CRITICAL, width: 1.5, dash: 'dash' },
      });
    }
    targets.forEach((t) => {
      shapes.push({
        type: 'line',
        xref: 'x',
        yref: 'y',
        x0: firstDate,
        x1: lastDate,
        y0: t,
        y1: t,
        line: { color: GOOD, width: 1.5, dash: 'dash' },
      });
    });
    const chartStart = firstDate;
    const chartEnd = lastDate;
    earningsDates
      .filter((d) => d >= chartStart && d <= chartEnd)
      .forEach((d) => {
        shapes.push({
          type: 'line',
          xref: 'x',
          yref: 'paper',
          x0: d,
          x1: d,
          y0: domains[rowIndex.price][0],
          y1: domains[rowIndex.price][1],
          line: { color: INK_MUTED, width: 1, dash: 'dot' },
        });
      });

    // Built loosely (`Record<string, unknown>`) and cast once at the end:
    // subplot axis keys (`xaxis2`, `yaxis3`, ...) and their `anchor` values
    // are assembled dynamically from however many rows this chart ends up
    // with, which is exactly what Plotly's `AxisName` template-literal type
    // can't express statically.
    const layoutOut: Record<string, unknown> = {
      title: { text: `${symbol} — ${bars.length} sessions`, font: { size: 14, color: '#ffffff' } },
      paper_bgcolor: PAGE,
      plot_bgcolor: SURFACE,
      height,
      margin: { l: 56, r: 24, t: 48, b: 36 },
      showlegend: true,
      legend: { orientation: 'h', y: 1.05, yanchor: 'bottom', font: { color: '#c3c2b7', size: 10 } },
      hovermode: 'x unified',
      font: { color: '#c3c2b7' },
      shapes,
      xaxis: {
        domain: [0, 1],
        anchor: `y${priceAxis}`,
        gridcolor: GRIDLINE,
        showgrid: false,
        rangeslider: { visible: false },
        showticklabels: rows[rows.length - 1] === 'price',
      },
      yaxis: { domain: domains[rowIndex.price], anchor: `x${priceAxis}`, gridcolor: GRIDLINE, zeroline: false, title: { text: '' } },
    };

    rows.forEach((row, i) => {
      if (i === 0) return;
      const suffix = axisSuffix(i);
      const titles: Record<string, string> = { volume: 'Volume', rsi: 'RSI (14)', sentiment: 'Mentions' };
      layoutOut[`xaxis${suffix}`] = {
        domain: [0, 1],
        anchor: `y${suffix}`,
        matches: 'x',
        gridcolor: GRIDLINE,
        showgrid: false,
        showticklabels: i === rows.length - 1,
      };
      layoutOut[`yaxis${suffix}`] = {
        domain: domains[i],
        anchor: `x${suffix}`,
        gridcolor: GRIDLINE,
        zeroline: false,
        title: { text: titles[row], font: { size: 10, color: INK_MUTED } },
        range: row === 'rsi' ? [0, 100] : undefined,
      };
    });

    if (showRsi) {
      const rsiAxis = axisSuffix(rowIndex.rsi);
      [30, 70].forEach((level) => {
        shapes.push({
          type: 'line',
          xref: `x${rsiAxis}`,
          yref: `y${rsiAxis}`,
          x0: firstDate,
          x1: lastDate,
          y0: level,
          y1: level,
          line: { color: GRIDLINE, width: 1, dash: 'dot' },
        });
      });
    }

    return { data: traces, layout: layoutOut as unknown as Partial<Layout> };
  }, [symbol, bars, indicators, sentiment, earningsDates, entryLow, entryHigh, stopLoss, targets, showRsi, height]);

  return (
    <Plot
      data={data}
      layout={layout}
      config={{ displayModeBar: true, displaylogo: false, responsive: true }}
      style={{ width: '100%', height: `${height}px` }}
      useResizeHandler
    />
  );
}
