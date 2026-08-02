/** Inline 0-100 score bar -- used both as an AG Grid cell renderer and as a
 * standalone stat. The number is always printed alongside the bar so the
 * value is never bar-length-only (accessibility, and precision). */
export function ScoreBar({ value, max = 100 }: { value: number; max?: number }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="flex h-full min-w-[92px] items-center gap-2">
      <div className="h-1.5 w-14 overflow-hidden rounded-full bg-surface-2">
        <div
          className="h-full rounded-full bg-accent"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs tabular-nums text-ink-secondary">{value.toFixed(0)}</span>
    </div>
  );
}

/** Confidence variant: same shape, traffic-light coloured by band (matches
 * ui.formatting.format_confidence's thresholds: >=80 good, >=60 warning,
 * below that serious). */
export function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const colorClass = pct >= 80 ? 'bg-good' : pct >= 60 ? 'bg-warning' : 'bg-serious';
  return (
    <div className="flex h-full min-w-[92px] items-center gap-2">
      <div className="h-1.5 w-14 overflow-hidden rounded-full bg-surface-2">
        <div className={`h-full rounded-full ${colorClass}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-ink-secondary">{pct.toFixed(0)}%</span>
    </div>
  );
}
