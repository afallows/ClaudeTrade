import { useEffect, useState } from 'react';
import { Activity } from 'lucide-react';
import { api } from '../api/client';
import type { DiagnosticsResponse } from '../api/types';

export function Diagnostics() {
  const [data, setData] = useState<DiagnosticsResponse | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { api.diagnostics().then(setData).catch((e) => setError(e instanceof Error ? e.message : 'Diagnostics unavailable')); }, []);
  return <div className="flex flex-col gap-5 p-6"><header><h1 className="text-lg font-semibold text-ink">Diagnostics</h1><p className="text-sm text-ink-secondary">Configuration and availability of data pipelines.</p></header>
    {error && <p role="alert" className="text-critical">{error}</p>}
    {!data ? !error && <p className="text-sm text-ink-secondary">Checking pipelines…</p> : <><div className="grid gap-3 md:grid-cols-2">{data.pipelines.map((p) => <section key={p.name} className="rounded-xl border border-gridline bg-surface p-4"><div className="flex justify-between"><div className="flex items-center gap-2"><Activity className="h-4 w-4 text-accent"/><h2 className="font-medium text-ink">{p.name}</h2></div><span className={`rounded-full px-2 py-1 text-xs font-medium ${p.status === 'reachable' ? 'bg-good/15 text-good' : p.status === 'configured' ? 'bg-warning/15 text-warning' : 'bg-surface-2 text-ink-secondary'}`}>{p.status.replace('_', ' ')}</span></div><dl className="mt-3 text-sm"><div className="flex justify-between"><dt className="text-ink-secondary">Pipeline</dt><dd className="text-ink">{p.kind.replace('_', ' ')}</dd></div><div className="flex justify-between"><dt className="text-ink-secondary">Provider</dt><dd className="text-ink">{p.provider}</dd></div></dl><p className="mt-3 text-xs text-ink-secondary">{p.detail}</p></section>)}</div><p className="text-xs text-ink-secondary">{data.probe_note}</p></>}
  </div>;
}
