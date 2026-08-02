import type { LucideIcon } from 'lucide-react';
import { Inbox } from 'lucide-react';

interface EmptyStateProps {
  message: string;
  command?: string;
  icon?: LucideIcon;
}

/** Every empty state in the app names the exact command/action that fixes
 * it -- never a bare "no data". Mirrors ui.components.tables.empty_state's
 * rule verbatim. */
export function EmptyState({ message, command, icon: Icon = Inbox }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-gridline bg-page/40 px-6 py-8 text-center">
      <Icon className="h-6 w-6 text-ink-muted" strokeWidth={1.5} aria-hidden="true" />
      <p className="max-w-md text-sm text-ink-secondary">{message}</p>
      {command && (
        <code className="rounded bg-page px-2 py-1 font-mono text-xs text-accent">{command}</code>
      )}
    </div>
  );
}
