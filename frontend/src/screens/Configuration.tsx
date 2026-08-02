import { useEffect, useState } from 'react';
import { CheckCircle2, KeyRound, Trash2, XCircle } from 'lucide-react';
import { api } from '../api/client';
import type { AIConfig, CredentialStatus, CredentialTestResult } from '../api/types';

/** Generic "test this source" widget: mirrors the Reddit connectivity
 * probe's exact shape so Reddit/X/AI all read and behave identically. */
function ConnectivityTest({ source, label, caption }: { source: string; label: string; caption: string }) {
  const [result, setResult] = useState<CredentialTestResult | null>(null);
  const [testing, setTesting] = useState(false);
  async function run() {
    setTesting(true);
    setResult(null);
    try { setResult(await api.testCredential(source)); }
    catch (e) { setResult({ ok: false, mode: null, status_detail: e instanceof Error ? e.message : 'Test failed' }); }
    finally { setTesting(false); }
  }
  return <div className="rounded-lg border border-gridline p-3">
    <div className="mb-2 flex items-center justify-between gap-3">
      <div><h3 className="text-sm font-medium text-ink">{label}</h3><p className="text-xs text-ink-secondary">{caption}</p></div>
      <button onClick={() => void run()} disabled={testing} className="shrink-0 rounded-lg border border-gridline px-3 py-2 text-sm font-medium text-ink disabled:opacity-40">{testing ? 'Testing…' : 'Test'}</button>
    </div>
    {result && <div role="status" className={`flex items-start gap-2 rounded-lg px-3 py-2 text-sm ${result.ok ? 'bg-good/15 text-good' : 'bg-critical/15 text-critical'}`}>
      {result.ok ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <XCircle className="mt-0.5 h-4 w-4 shrink-0" />}
      <span>{result.mode ? <strong className="mr-1">[{result.mode}]</strong> : null}{result.status_detail}</span>
    </div>}
  </div>;
}

/** "AI Analysis" section: provider dropdown, model field, per-provider
 * setup instructions, and a Test button -- see docs/ai-setup.md for the
 * full walkthrough this section summarises inline. */
function AIAnalysisSection({ onChanged }: { onChanged: () => void }) {
  const [config, setConfig] = useState<AIConfig | null>(null);
  const [provider, setProvider] = useState<'none' | 'anthropic' | 'openai'>('none');
  const [model, setModel] = useState('');
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void api.aiConfig().then((c) => { setConfig(c); setProvider(c.provider); setModel(c.model); });
  }, []);

  async function save() {
    setSaving(true);
    setMessage('');
    try {
      const result = await api.updateAIConfig(provider, model);
      setMessage(result.note);
      onChanged();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Unable to update AI provider.');
    } finally {
      setSaving(false);
    }
  }

  if (!config) return null;
  const placeholder = provider === 'anthropic' ? config.anthropic_default_model
    : provider === 'openai' ? config.openai_default_model : '';

  return <section className="rounded-xl border border-gridline bg-surface p-4">
    <div className="mb-3"><h2 className="font-medium text-ink">AI Analysis</h2>
      <p className="text-xs text-ink-secondary">
        Optional LLM-assisted sentiment classification. Rules-based sentiment always runs and remains the
        mandatory floor -- AI is an ensemble adjunct with a capped weight, never the sole basis of a signal.
        Post text is sent to the chosen provider&rsquo;s API when enabled. See <code>docs/ai-setup.md</code> for
        the full walkthrough, cost caps, and privacy note.
      </p>
    </div>

    <div className="grid gap-3 sm:grid-cols-2">
      <label className="flex flex-col gap-1 text-sm text-ink">
        Provider
        <select value={provider} onChange={(e) => setProvider(e.target.value as typeof provider)}
          className="rounded-lg border border-gridline bg-background px-3 py-2 text-sm text-ink">
          <option value="none">None (rules only)</option>
          <option value="anthropic">Claude (Anthropic)</option>
          <option value="openai">ChatGPT (OpenAI)</option>
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm text-ink">
        Model {provider !== 'none' && <span className="text-xs text-ink-secondary">(blank = provider default)</span>}
        <input value={model} onChange={(e) => setModel(e.target.value)} disabled={provider === 'none'}
          placeholder={placeholder || 'n/a'}
          className="rounded-lg border border-gridline bg-background px-3 py-2 text-sm text-ink disabled:opacity-40" />
      </label>
    </div>

    <div className="mt-3 flex justify-end">
      <button onClick={() => void save()} disabled={saving}
        className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-40">
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>

    {message && <div role="status" className="mt-3 rounded-lg border border-gridline px-3 py-2 text-sm text-ink">{message}</div>}

    {provider === 'anthropic' && <div className="mt-3 rounded-lg bg-surface-2 p-3 text-xs text-ink-secondary">
      <p className="mb-1 font-medium text-ink">Set up Claude</p>
      <ol className="list-decimal space-y-0.5 pl-4">
        <li>Go to <code>platform.claude.com</code> (formerly console.anthropic.com)</li>
        <li>Open <strong>API Keys</strong> and create a new key</li>
        <li>Paste it into the &ldquo;Anthropic (Claude) API key&rdquo; field below and Save</li>
      </ol>
      <p className="mt-1">Keys start with <code>sk-ant-</code>. Billing is pay-per-use, billed to your Anthropic account.</p>
      <p className="mt-1">
        Rough cost per refresh: with the default 250-call cap, Claude Opus 5 ($5/$25 per MTok) runs
        roughly $1&ndash;$2 per full refresh; <code>claude-haiku-4-5</code> ($1/$5 per MTok) is the economical
        choice for high-volume per-post classification, roughly 5&times; cheaper for the same call volume.
      </p>
    </div>}
    {provider === 'openai' && <div className="mt-3 rounded-lg bg-surface-2 p-3 text-xs text-ink-secondary">
      <p className="mb-1 font-medium text-ink">Set up ChatGPT</p>
      <ol className="list-decimal space-y-0.5 pl-4">
        <li>Go to <code>platform.openai.com</code></li>
        <li>Open <strong>API keys</strong> and create a new key</li>
        <li>Paste it into the &ldquo;OpenAI (ChatGPT) API key&rdquo; field below and Save</li>
      </ol>
      <p className="mt-1">Keys start with <code>sk-</code>. Billing is pay-per-use, billed to your OpenAI account. Check current model names/pricing at platform.openai.com.</p>
    </div>}

    {provider !== 'none' && <div className="mt-3">
      <ConnectivityTest source="ai" label="AI connectivity"
        caption="Runs one minimal classification call against the configured provider using the saved API key -- no secret is ever echoed back." />
    </div>}
  </section>;
}

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
  const hasReddit = items.some((item) => item.name.startsWith('reddit'));
  const hasX = items.some((item) => item.name.startsWith('x_'));
  return <div className="flex flex-col gap-5 p-6">
    <header><h1 className="text-lg font-semibold text-ink">Configuration</h1><p className="text-sm text-ink-secondary">Add API credentials without storing secrets in ClaudeTrade files or its database.</p></header>
    <div className="rounded-xl border border-gridline bg-surface p-4 text-sm text-ink-secondary"><KeyRound className="mr-2 inline h-4 w-4 text-accent" />Secrets are written to your operating system credential store. Existing values are never returned to this page.</div>
    {message && <div role="status" className="rounded-lg border border-gridline px-3 py-2 text-sm text-ink">{message}</div>}

    <AIAnalysisSection onChanged={load} />

    {(hasReddit || hasX) && <section className="rounded-xl border border-gridline bg-surface p-4">
      <h2 className="mb-2 font-medium text-ink">Social source connectivity</h2>
      <div className="grid gap-3">
        {hasReddit && <ConnectivityTest source="reddit" label="Reddit connectivity"
          caption="Makes one small live request using whichever Reddit credentials are currently configured -- no secret is ever echoed back." />}
        {hasX && <ConnectivityTest source="x" label="X connectivity"
          caption="Makes one small live request using whichever X credentials are currently configured (official API or session cookies) -- no secret is ever echoed back." />}
      </div>
    </section>}

    {loading ? <p className="text-sm text-ink-secondary">Loading credentials…</p> : <div className="grid gap-3">
      {items.map((item) => <section key={item.name} className="rounded-xl border border-gridline bg-surface p-4">
        <div className="mb-3 flex items-start justify-between"><div><h2 className="font-medium text-ink">{item.label}</h2><p className="text-xs text-ink-secondary">{item.pipeline.replace('_', ' ')} pipeline · {item.configured ? `${item.masked} via ${item.source}` : 'Not configured'}</p></div><span className={`rounded-full px-2 py-1 text-xs ${item.configured ? 'bg-good/15 text-good' : 'bg-surface-2 text-ink-secondary'}`}>{item.configured ? 'Configured' : 'Not configured'}</span></div>
        <div className="flex gap-2"><input aria-label={`${item.label} value`} type="password" autoComplete="new-password" value={values[item.name] ?? ''} onChange={(e) => setValues((v) => ({...v, [item.name]: e.target.value}))} placeholder={item.configured ? 'Enter replacement value' : 'Enter credential'} className="min-w-0 flex-1 rounded-lg border border-gridline bg-background px-3 py-2 text-sm text-ink"/><button onClick={() => void save(item)} disabled={!values[item.name]} className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-40">Save</button>{item.configured && <button aria-label={`Remove ${item.label}`} onClick={() => void remove(item)} className="rounded-lg border border-gridline p-2 text-critical"><Trash2 className="h-4 w-4" /></button>}</div>
      </section>)}
    </div>}
  </div>;
}
