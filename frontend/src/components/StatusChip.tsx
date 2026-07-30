import {
  Target,
  Clock,
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  XCircle,
  HelpCircle,
} from 'lucide-react';
import { statusMeta } from '../lib/meta';

const ICONS: Record<string, typeof Target> = {
  actionable: Target,
  approaching: Clock,
  extended: AlertTriangle,
  triggered: CheckCircle2,
  expired: CircleDashed,
  rejected: XCircle,
  unknown: HelpCircle,
};

/** The "actionability chip" the Screener spec calls for: a signal's current
 * ledger status, as a crisp icon + label rather than a coloured emoji. */
export function StatusChip({ status }: { status: string }) {
  const meta = statusMeta(status);
  const Icon = ICONS[status.toLowerCase()] ?? HelpCircle;
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${meta.colorClass}`}>
      <Icon className="h-3.5 w-3.5" strokeWidth={2} aria-hidden="true" />
      {meta.label}
    </span>
  );
}
