interface SkeletonProps {
  className?: string;
}

/** A loading placeholder -- never a spinner. Matches the shape of whatever
 * it's standing in for via `className` (height/width/rounding). */
export function Skeleton({ className = 'h-4 w-full' }: SkeletonProps) {
  return <div className={`skeleton rounded-md bg-surface-2 ${className}`} aria-hidden="true" />;
}

/** A stack of skeleton rows, for tables/lists still loading. */
export function SkeletonRows({ rows = 5, className = 'h-10 w-full' }: { rows?: number; className?: string }) {
  return (
    <div className="flex flex-col gap-2" role="status" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={className} />
      ))}
    </div>
  );
}

/** A card-shaped skeleton, for panels still loading their data. */
export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <div className="rounded-xl border border-gridline bg-surface p-4" role="status" aria-label="Loading">
      <Skeleton className="mb-3 h-4 w-1/3" />
      <div className="flex flex-col gap-2">
        {Array.from({ length: lines }).map((_, i) => (
          <Skeleton key={i} className="h-3 w-full" />
        ))}
      </div>
    </div>
  );
}
