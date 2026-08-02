/** Display formatting helpers -- the TS equivalents of ui/formatting.py's
 * number/date rules, so the two UIs read consistently during the parity
 * window (Streamlit stays available via `--classic`). */

export function formatCurrency(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatPrice(value: number | null | undefined): string {
  return formatCurrency(value, value !== null && value !== undefined && value < 1 ? 4 : 2);
}

export function formatPercent(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toFixed(decimals)}%`;
}

export function formatRatio(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toFixed(decimals)}:1`;
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return value.toLocaleString('en-US');
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  return value;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'never';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(0)}%`;
}

export function daysToEarningsLabel(days: number | null | undefined): string {
  if (days === null || days === undefined) return '—';
  if (days < 0) return `${Math.abs(days)}d ago`;
  if (days === 0) return 'today';
  return `${days}d`;
}
