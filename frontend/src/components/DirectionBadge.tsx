import { TrendingDown, TrendingUp, Minus } from 'lucide-react';

/** LONG/SHORT/FLAT badge using the diverging blue/red pair -- never an
 * arbitrary categorical colour, matching claudetrade.ui.theme's rule that
 * direction and polarity always share one axis. Crisp lucide SVGs stand in
 * for the emoji dots the Streamlit UI used. */
export function DirectionBadge({ direction }: { direction: string }) {
  const d = direction.toLowerCase();
  if (d === 'long') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-long/15 px-2 py-0.5 text-xs font-semibold text-long">
        <TrendingUp className="h-3.5 w-3.5" strokeWidth={2.25} aria-hidden="true" />
        LONG
      </span>
    );
  }
  if (d === 'short') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-short/15 px-2 py-0.5 text-xs font-semibold text-short">
        <TrendingDown className="h-3.5 w-3.5" strokeWidth={2.25} aria-hidden="true" />
        SHORT
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-neutral/15 px-2 py-0.5 text-xs font-semibold text-neutral">
      <Minus className="h-3.5 w-3.5" strokeWidth={2.25} aria-hidden="true" />
      FLAT
    </span>
  );
}
