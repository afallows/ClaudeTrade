import { Microscope } from 'lucide-react';

/** Small pill marking a signal whose score has been re-ranked by an accepted
 * MCP research revision -- follows the same rounded-full/15%-tint pattern as
 * `DirectionBadge`/strategy pills rather than inventing a new badge shape.
 * `engineScore` (the original, unadjusted `overall_score`) is surfaced as a
 * native tooltip and muted secondary text, never hidden -- research re-ranks
 * a signal, it never replaces the engine's own number. */
export function ResearchBadge({ engineScore }: { engineScore: number }) {
  return (
    <span
      title={`Research-adjusted -- engine score: ${engineScore.toFixed(0)}`}
      className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent"
    >
      <Microscope className="h-3 w-3" strokeWidth={2.25} aria-hidden="true" />
      Research
    </span>
  );
}
