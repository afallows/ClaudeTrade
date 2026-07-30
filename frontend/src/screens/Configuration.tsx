import { useEffect, useState } from 'react';
import { KeyRound, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import type { CredentialStatus } from '../api/types';

export function Configuration() {
  const [items, setItems] = useState<CredentialStatus[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const load = () => api.credentials().then((r) => setItems(r.credentials)).finally(() => setLoading(false));
  useEffect(() => { void load(); }, []);
  async function save(item: CredentialStatus) {
    const value = values[item.name] ?? '';
    if (!value) return;
    setMessage('');
    try { await api.saveCredential(item.name, value); setValues((v) => ({ ...v, [item.name]: '' })); await load(); setMessage(`${item.label} saved securely.`); }
    catch (e) { setMessage(e instanceof Error ? e.message : 'Unable to save credential.'); }
  }
  async function remove(item: CredentialStatus) {
    setMessage('');
    try { await api.deleteCredential(item.name); await load(); setMessage(`${item.label} removed.`); }
    catch (e) { setMessage(e instanceof Error ? e.message : 'Unable to remove credential.'); }
  }
  return <div className="flex flex-col gap-5 p-6">
    <header><h1 className="text-lg font-semibold text-ink">Configuration</h1><p className="text-sm text-ink-secondary">Add API credentials without storing secrets in ClaudeTrade files or its database.</p></header>
    <div className="rounded-xl border border-gridline bg-surface p-4 text-sm text-ink-secondary"><KeyRound className="mr-2 inline h-4 w-4 text-accent" />Secrets are written to your operating system credential store. Existing values are never returned to this page.</div>
    {message && <div role="status" className="rounded-lg border border-gridline px-3 py-2 text-sm text-ink">{message}</div>}
    {loading ? <p className="text-sm text-ink-secondary">Loading credentials…</p> : <div className="grid gap-3">
      {items.map((item) => <section key={item.name} className="rounded-xl border border-gridline bg-surface p-4">
        <div className="mb-3 flex items-start justify-between"><div><h2 className="font-medium text-ink">{item.label}</h2><p className="text-xs text-ink-secondary">{item.pipeline.replace('_', ' ')} pipeline · {item.configured ? `${item.masked} via ${item.source}` : 'Not configured'}</p></div><span className={`rounded-full px-2 py-1 text-xs ${item.configured ? 'bg-good/15 text-good' : 'bg-surface-2 text-ink-secondary'}`}>{item.configured ? 'Configured' : 'Not configured'}</span></div>
        <div className="flex gap-2"><input aria-label={`${item.label} value`} type="password" autoComplete="new-password" value={values[item.name] ?? ''} onChange={(e) => setValues((v) => ({...v, [item.name]: e.target.value}))} placeholder={item.configured ? 'Enter replacement value' : 'Enter credential'} className="min-w-0 flex-1 rounded-lg border border-gridline bg-background px-3 py-2 text-sm text-ink"/><button onClick={() => void save(item)} disabled={!values[item.name]} className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-40">Save</button>{item.configured && <button aria-label={`Remove ${item.label}`} onClick={() => void remove(item)} className="rounded-lg border border-gridline p-2 text-critical"><Trash2 className="h-4 w-4" /></button>}</div>
      </section>)}
    </div>}
  </div>;
}
