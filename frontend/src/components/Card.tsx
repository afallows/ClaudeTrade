import type { ReactNode } from 'react';

interface CardProps {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
}

/** The one card chrome every panel in the app uses: surface, gridline
 * border, 8px-multiple padding, optional header row with a right-aligned
 * action slot. */
export function Card({ title, subtitle, action, children, className = '', padded = true }: CardProps) {
  return (
    <section
      className={`rounded-xl border border-gridline bg-surface ${padded ? 'p-4' : ''} ${className}`}
    >
      {(title || action) && (
        <div className="mb-3 flex items-start justify-between gap-4">
          <div>
            {title && <h2 className="text-sm font-semibold tracking-wide text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
