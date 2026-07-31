import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { api } from '../api/client';
import type { RefreshStatus } from '../api/types';

/**
 * Slim banner shown on every screen while a background data refresh is
 * running -- see `claudetrade.webapi.routers.system`'s
 * `POST /api/system/refresh` / `GET /api/system/refresh/status`. This is
 * what lets `scripts/setup.ps1` start the UI before the first data load
 * finishes: the operator sees this instead of a blank, apparently-hung
 * screen while a large universe (thousands of symbols) loads behind it.
 *
 * Polls every 3s. A failed poll (older server build without this endpoint,
 * a transient network hiccup) just hides the banner -- this is a progress
 * hint, never something the rest of the UI depends on.
 */
export function RefreshBanner() {
  const [status, setStatus] = useState<RefreshStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = () =>
      api
        .refreshStatus()
        .then((result) => { if (!cancelled) setStatus(result); })
        .catch(() => { if (!cancelled) setStatus(null); });
    void poll();
    const interval = setInterval(poll, 3000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  if (!status?.running) return null;

  const pct =
    status.symbols_total > 0 ? Math.round((status.symbols_done / status.symbols_total) * 100) : null;

  return (
    <div
      role="status"
      className="flex items-center gap-2 border-b border-gridline bg-accent/10 px-4 py-2 text-sm text-ink"
    >
      <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden="true" />
      <span>
        Loading market data ({status.phase})
        {pct !== null ? `: ${status.symbols_done}/${status.symbols_total} symbols (${pct}%)` : '…'}
      </span>
    </div>
  );
}
