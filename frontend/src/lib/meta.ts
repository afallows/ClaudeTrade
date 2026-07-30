/** Small lookup tables mapping domain string enums to display metadata
 * (label + Tailwind color token). Kept separate from React components so
 * they're trivially unit-testable and reusable inside AG Grid cell
 * renderers, which don't get the full component tree. */

export type SignalStatusKey =
  | 'actionable'
  | 'approaching'
  | 'extended'
  | 'triggered'
  | 'expired'
  | 'rejected'
  | 'unknown';

export interface StatusMeta {
  label: string;
  colorClass: string;
  dotClass: string;
}

const STATUS_META: Record<SignalStatusKey, StatusMeta> = {
  actionable: { label: 'Actionable', colorClass: 'text-good', dotClass: 'bg-good' },
  approaching: { label: 'Approaching', colorClass: 'text-warning', dotClass: 'bg-warning' },
  extended: { label: 'Extended', colorClass: 'text-serious', dotClass: 'bg-serious' },
  triggered: { label: 'Triggered', colorClass: 'text-accent', dotClass: 'bg-accent' },
  expired: { label: 'Expired', colorClass: 'text-ink-muted', dotClass: 'bg-ink-muted' },
  rejected: { label: 'Rejected', colorClass: 'text-critical', dotClass: 'bg-critical' },
  unknown: { label: 'Unknown', colorClass: 'text-ink-muted', dotClass: 'bg-ink-muted' },
};

export function statusMeta(status: string | null | undefined): StatusMeta {
  const key = (status ?? 'unknown').toLowerCase() as SignalStatusKey;
  return STATUS_META[key] ?? STATUS_META.unknown;
}

export interface RegimeMeta {
  label: string;
  colorClass: string;
}

const REGIME_LABELS: Record<string, string> = {
  bull_quiet: 'Bull — Quiet',
  bull_volatile: 'Bull — Volatile',
  neutral: 'Neutral',
  bear_volatile: 'Bear — Volatile',
  bear_quiet: 'Bear — Quiet',
  unknown: 'Unknown',
};

export function regimeMeta(regime: string | null | undefined): RegimeMeta {
  const key = (regime ?? 'unknown').toLowerCase();
  const label = REGIME_LABELS[key] ?? key;
  const colorClass = key.startsWith('bull')
    ? 'text-long'
    : key.startsWith('bear')
      ? 'text-short'
      : 'text-ink-muted';
  return { label, colorClass };
}

export function directionColorClass(direction: string | null | undefined): string {
  const d = (direction ?? '').toLowerCase();
  if (d === 'long') return 'text-long';
  if (d === 'short') return 'text-short';
  return 'text-neutral';
}
