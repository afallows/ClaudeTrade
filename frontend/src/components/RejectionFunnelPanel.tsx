import type { ScanFunnel } from '../api/types';
import { Card } from './Card';
import { formatConfidence } from '../lib/format';

interface RejectionFunnelPanelProps {
  funnel: ScanFunnel;
}

function reasonRows(funnel: ScanFunnel): [string, number][] {
  return Object.entries(funnel.by_reason).sort((a, b) => b[1] - a[1]);
}

function componentList(components: [string, number][]): string {
  if (components.length === 0) return '—';
  return components.map(([label, value]) => `${label} ${value >= 0 ? '+' : ''}${value.toFixed(1)}`).join(', ');
}

/** The Screener's "why zero (or few) signals" panel: the last scan's
 * rejection funnel (reason -> count, per strategy) plus its closest
 * near-misses, so an empty grid is answerable instead of a dead end. Reuses
 * the same `Card`/table chrome as the rest of the app (see the near-miss
 * `<details>` table further down `Screener.tsx`). */
export function RejectionFunnelPanel({ funnel }: RejectionFunnelPanelProps) {
  if (funnel.total_rejections === 0) return null;

  return (
    <Card
      title="Why no signals?"
      subtitle={`${funnel.total_rejections} candidate evaluation(s) rejected in the last scan on this server`}
    >
      <div className="flex flex-col gap-5">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
            Rejection reasons
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[420px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
                  <th className="py-1.5 pr-4 font-medium">Reason</th>
                  <th className="py-1.5 font-medium">Count</th>
                </tr>
              </thead>
              <tbody>
                {reasonRows(funnel).map(([reason, count]) => (
                  <tr key={reason} className="border-b border-gridline/60">
                    <td className="py-1.5 pr-4 text-ink-secondary">{reason}</td>
                    <td className="py-1.5 tabular-nums text-ink">{count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {funnel.near_misses.length > 0 && (
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
              Closest near-misses
            </h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-gridline text-left text-xs uppercase tracking-wide text-ink-muted">
                    <th className="py-1.5 pr-4 font-medium">Symbol</th>
                    <th className="py-1.5 pr-4 font-medium">Strategy</th>
                    <th className="py-1.5 pr-4 font-medium">Reason</th>
                    <th className="py-1.5 pr-4 font-medium">Score / Threshold</th>
                    <th className="py-1.5 pr-4 font-medium">Confidence</th>
                    <th className="py-1.5 font-medium">Weakest components</th>
                  </tr>
                </thead>
                <tbody>
                  {funnel.near_misses.map((nm, i) => (
                    <tr key={`${nm.symbol}-${nm.strategy}-${i}`} className="border-b border-gridline/60">
                      <td className="py-1.5 pr-4 font-medium text-ink">{nm.symbol}</td>
                      <td className="py-1.5 pr-4 text-ink-secondary">{nm.strategy}</td>
                      <td className="py-1.5 pr-4 text-ink-secondary">{nm.reason_code}</td>
                      <td className="py-1.5 pr-4 tabular-nums text-ink-secondary">
                        {nm.metric.toFixed(1)} / {nm.threshold.toFixed(1)}
                      </td>
                      <td className="py-1.5 pr-4 tabular-nums text-ink-secondary">
                        {formatConfidence(nm.confidence)}
                      </td>
                      <td className="py-1.5 text-ink-secondary">
                        {componentList(nm.weakest_components)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
