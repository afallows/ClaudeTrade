import type { Attention } from '../api/types';

/** Cell renderers for the Screener grid's four Adanos attention columns
 * (Buzz, Mentions, Sentiment, Trend). Every cell handles `attention === null`
 * the same way -- a muted em-dash, never a fabricated zero -- matching the
 * app's "render unavailable-with-reason honestly" rule (there's no room for
 * a reason string in a grid cell, so the em-dash itself is the signal; the
 * column header plus the null being universal across all four cells makes
 * "no Adanos data for this symbol" unambiguous). */

const NO_DATA = <span className="text-ink-muted">—</span>;

const TREND_COLOR: Record<string, string> = {
  rising: 'text-good',
  falling: 'text-critical',
  stable: 'text-ink-muted',
};

const TREND_ARROW: Record<string, string> = {
  rising: '↑',
  falling: '↓',
  stable: '→',
};

function trendColorClass(trend: string): string {
  return TREND_COLOR[trend] ?? 'text-ink-muted';
}

/** BUZZ: the mention-weighted buzz score (0-100) plus a direction arrow --
 * green rising, red falling, muted stable/unknown. */
export function BuzzCell({ attention }: { attention: Attention | null }) {
  if (!attention) return NO_DATA;
  const arrow = TREND_ARROW[attention.trend] ?? '→';
  return (
    <div className="flex h-full items-center gap-1.5 tabular-nums">
      <span className="text-ink">{attention.buzz_score.toFixed(0)}</span>
      <span
        className={`text-sm font-semibold ${trendColorClass(attention.trend)}`}
        title={attention.trend ? `Buzz trend: ${attention.trend}` : 'No trend reported'}
      >
        {arrow}
      </span>
    </div>
  );
}

/** MENTIONS: total across every reporting platform, plus a source count when
 * the news feed reported one ("55 (9 sources)") -- omitted otherwise. */
export function MentionsCell({ attention }: { attention: Attention | null }) {
  if (!attention) return NO_DATA;
  return (
    <span className="tabular-nums text-ink-secondary">
      {attention.total_mentions.toLocaleString()}
      {attention.source_count !== null && (
        <span className="text-ink-muted"> ({attention.source_count} sources)</span>
      )}
    </span>
  );
}

/** SENTIMENT: a slim bullish (green) / neutral (gray) / bearish (red) split
 * bar, pure CSS -- exact percentages are in the tooltip, never bar-length-only
 * (same "never hide the number behind a shape" rule `ScoreBar` follows). */
export function SentimentBar({ attention }: { attention: Attention | null }) {
  if (!attention || (attention.bullish_pct === null && attention.bearish_pct === null)) {
    return NO_DATA;
  }
  const bull = attention.bullish_pct ?? 0;
  const bear = attention.bearish_pct ?? 0;
  const neutral = Math.max(0, 100 - bull - bear);
  const title = `Bullish ${bull.toFixed(0)}% · Neutral ${neutral.toFixed(0)}% · Bearish ${bear.toFixed(0)}%`;
  return (
    <div className="flex h-full items-center" title={title}>
      <div className="flex h-1.5 w-24 overflow-hidden rounded-full bg-surface-2">
        {bull > 0 && <div className="h-full bg-good" style={{ width: `${bull}%` }} />}
        {neutral > 0 && <div className="h-full bg-neutral/40" style={{ width: `${neutral}%` }} />}
        {bear > 0 && <div className="h-full bg-critical" style={{ width: `${bear}%` }} />}
      </div>
    </div>
  );
}

/** TREND: a tiny 7-point sparkline of the combined buzz history, hand-rolled
 * SVG (no charting library) -- stroke colour follows the same trend palette
 * as `BuzzCell`'s arrow. */
export function TrendSparkline({ attention }: { attention: Attention | null }) {
  if (!attention || attention.trend_history.length !== 7) return NO_DATA;
  const points = attention.trend_history;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const width = 64;
  const height = 22;
  const step = width / (points.length - 1);
  const coords = points
    .map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / range) * height).toFixed(1)}`)
    .join(' ');
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={trendColorClass(attention.trend)}
      aria-hidden="true"
    >
      <polyline
        points={coords}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
