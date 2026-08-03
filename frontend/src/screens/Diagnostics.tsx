import { useEffect, useState } from 'react';
import { Activity, CheckCircle2, XCircle } from 'lucide-react';
import { api } from '../api/client';
import type { CredentialTestResult, DiagnosticsResponse } from '../api/types';

/** Diagnostics is the single home for connection tests: any row whose
 * `test_source` is non-null gets a "Test connection" button that calls
 * `POST /api/system/credentials/{source}/test` and shows the verdict inline.
 * This mirrors the Configuration screen's former `ConnectivityTest` widget's
 * exact button/result styling -- only moved here so a new connector needs no
 * frontend rebuild, just a `test_source` on its diagnostics row. */
function ConnectivityTestButton({ source }: { source: string }) {
  const [result, setResult] = useState<CredentialTestResult | null>(null);
  const [testing, setTesting] = useState(false);
  async function run() {
    setTesting(true);
    setResult(null);
    try { setResult(await api.testCredential(source)); }
    catch (e) { setResult({ ok: false, mode: null, status_detail: e instanceof Error ? e.message : 'Test failed' }); }
    finally { setTesting(false); }
  }
  return <div className="mt-3 flex flex-col items-start gap-2">
    <button onClick={() => void run()} disabled={testing} className="shrink-0 rounded-lg border border-gridline px-3 py-2 text-sm font-medium text-ink disabled:opacity-40">{testing ? 'Testing…' : 'Test connection'}</button>
    {result && <div role="status" className={`flex items-start gap-2 rounded-lg px-3 py-2 text-sm ${result.ok ? 'bg-good/15 text-good' : 'bg-critical/15 text-critical'}`}>
      {result.ok ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <XCircle className="mt-0.5 h-4 w-4 shrink-0" />}
      <span>{result.mode ? <strong className="mr-1">[{result.mode}]</strong> : null}{result.status_detail}</span>
    </div>}
  </div>;
}

export function Diagnostics() {
  const [data, setData] = useState<DiagnosticsResponse | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { api.diagnostics().then(setData).catch((e) => setError(e instanceof Error ? e.message : 'Diagnostics unavailable')); }, []);
  return <div className="flex flex-col gap-5 p-6"><header><h1 className="text-lg font-semibold text-ink">Diagnostics</h1><p className="text-sm text-ink-secondary">Configuration and availability of data pipelines.</p></header>
    {error && <p role="alert" className="text-critical">{error}</p>}
    {!data ? !error && <p className="text-sm text-ink-secondary">Checking pipelines…</p> : <><div className="grid gap-3 md:grid-cols-2">{data.pipelines.map((p) => <section key={p.name} className="rounded-xl border border-gridline bg-surface p-4"><div className="flex justify-between"><div className="flex items-center gap-2"><Activity className="h-4 w-4 text-accent"/><h2 className="font-medium text-ink">{p.name}</h2></div><span className={`rounded-full px-2 py-1 text-xs font-medium ${p.status === 'reachable' ? 'bg-good/15 text-good' : p.status === 'configured' ? 'bg-warning/15 text-warning' : 'bg-surface-2 text-ink-secondary'}`}>{p.status.replace('_', ' ')}</span></div><dl className="mt-3 text-sm"><div className="flex justify-between"><dt className="text-ink-secondary">Pipeline</dt><dd className="text-ink">{p.kind.replace('_', ' ')}</dd></div><div className="flex justify-between"><dt className="text-ink-secondary">Provider</dt><dd className="text-ink">{p.provider}</dd></div></dl><p className="mt-3 text-xs text-ink-secondary">{p.detail}</p>{p.test_source && <ConnectivityTestButton source={p.test_source} />}</section>)}</div><p className="text-xs text-ink-secondary">{data.probe_note}</p></>}
  </div>;
}
