import { NavLink } from 'react-router-dom';
import { LayoutDashboard, ScanSearch, LineChart, AlertTriangle, Settings, Activity } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../api/client';

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/screener', label: 'Screener', icon: ScanSearch },
  { to: '/configuration', label: 'Configuration', icon: Settings },
  { to: '/diagnostics', label: 'Diagnostics', icon: Activity },
];

/** Fixed left navigation: brand, the two phase-1 screens, and a compact
 * kill-switch indicator (the one piece of account state urgent enough to
 * belong in the nav rather than only on the Dashboard). */
export function Sidebar() {
  const [killSwitch, setKillSwitch] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .paperAccount()
      .then((r) => {
        if (!cancelled) setKillSwitch(r.account.kill_switch_engaged);
      })
      .catch(() => {
        if (!cancelled) setKillSwitch(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <nav className="flex w-56 shrink-0 flex-col border-r border-gridline bg-surface">
      <div className="flex items-center gap-2 px-4 py-4">
        <LineChart className="h-5 w-5 text-accent" strokeWidth={2.25} aria-hidden="true" />
        <span className="text-sm font-semibold tracking-wide text-ink">ClaudeTrade</span>
      </div>
      <div className="flex flex-col gap-1 px-2">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-accent/15 text-accent'
                  : 'text-ink-secondary hover:bg-surface-2 hover:text-ink'
              }`
            }
          >
            <Icon className="h-4 w-4" strokeWidth={2} aria-hidden="true" />
            {label}
          </NavLink>
        ))}
      </div>
      <div className="mt-auto px-4 py-4">
        {killSwitch === true && (
          <div className="flex items-center gap-1.5 rounded-lg bg-critical/15 px-2.5 py-1.5 text-xs font-semibold text-critical">
            <AlertTriangle className="h-3.5 w-3.5" strokeWidth={2.25} aria-hidden="true" />
            Kill switch engaged
          </div>
        )}
      </div>
    </nav>
  );
}
