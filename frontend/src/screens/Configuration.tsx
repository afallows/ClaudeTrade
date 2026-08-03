import { useEffect, useState } from 'react';
import { Activity, KeyRound, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import type { AIConfig, CredentialStatus, SignalWeights } from '../api/types';

/** How far the raw weight sum may drift from 1.00 before the caption warns.
 * Weights are normalised at use (see the section's own caption), so a small
 * drift is harmless -- this only flags a sum far enough off to suggest a
 * mistake (e.g. a component left at 0 or fat-fingered). */
const WEIGHT_SUM_DRIFT_WARNING = 0.15;

/** "Signal Weightings" section: one numeric input per scoring component
 * (`config.signals.component_weights`), a live sum readout, and a Save
 * button against `PUT /api/system/weights`. Rendered first on this screen,
 * before any credential -- weights shape every score the app produces, so
 * they lead. See `webapi.routers.system.update_signal_weights` (Python) for
 * the honest-persistence contract this section's note text quotes. */
function SignalWeightingsSection() {
  const [data, setData] = useState<SignalWeights | null>(null);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void api.weights().then((d) => { setData(d); setWeights(d.weights); });
  }, []);

  if (!data) return null;

  const componentNames = Object.keys(weights).sort();
  const sum = Object.values(weights).reduce((total, value) => total + (Number.isFinite(value) ? value : 0), 0);
  const sumDrifted = Math.abs(sum - 1) > WEIGHT_SUM_DRIFT_WARNING;

  async function save() {
    setSaving(true);
    setMessage('');
    try {
      const result = await api.updateWeights(weights);
      setWeights(result.weights);
      setMessage(result.note);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Unable to update signal weightings.');
    } finally {
      setSaving(false);
    }
  }

  return <section className="rounded-xl border border-gridline bg-surface p-4">
    <div className="mb-3"><h2 className="font-medium text-ink">Signal Weightings</h2>
      <p className="text-xs text-ink-secondary">
        Relative contribution of each scoring component to a signal&rsquo;s overall score. Weights are
        normalised at use, so the sum need not be exactly 1.00 -- but a sum far from 1.00 usually means a
        component was set by mistake.
      </p>
    </div>

    <div className="grid gap-2 sm:grid-cols-2">
      {componentNames.map((name) => (
        <label key={name} htmlFor={`weight-${name}`} className="flex items-center justify-between gap-3 text-sm text-ink">
          <span className="text-ink-secondary">{name.replace(/_/g, ' ')}</span>
          <input
            id={`weight-${name}`}
            type="number"
            step={0.01}
            min={0}
            max={1}
            value={weights[name]}
            onChange={(e) => setWeights((w) => ({ ...w, [name]: Number.isNaN(e.target.valueAsNumber) ? 0 : e.target.valueAsNumber }))}
            className="w-24 rounded-lg border border-gridline bg-background px-2 py-1 text-right text-sm text-ink"
          />
        </label>
      ))}
    </div>

    <div className="mt-3 flex items-center justify-between gap-3">
      <p className={`text-xs ${sumDrifted ? 'text-warning' : 'text-ink-secondary'}`}>
        Sum: {sum.toFixed(2)}{sumDrifted ? ' -- far from 1.00' : ''}
      </p>
      <button onClick={() => void save()} disabled={saving}
        className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-40">
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>

    {message && <div role="status" className="mt-3 rounded-lg border border-gridline px-3 py-2 text-sm text-ink">{message}</div>}
  </section>;
}

/** "AI Analysis" section: provider dropdown, model field, and per-provider
 * setup instructions -- see docs/ai-setup.md for the full walkthrough this
 * section summarises inline. Its connectivity test lives on the Diagnostics
 * screen, alongside every other connector's -- see Diagnostics.tsx. */
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
  </section>;
}

/** One credential card, styled to match Diagnostics.tsx's pipeline rows:
 * an icon + name header, a status pill in the same colours Diagnostics
 * uses for "configured" states, and a muted detail line -- see
 * Diagnostics.tsx's pipeline `<section>` for the layout this mirrors. */
function CredentialCard({ item, value, onChange, onSave, onRemove }: {
  item: CredentialStatus;
  value: string;
  onChange: (value: string) => void;
  onSave: () => void;
  onRemove: () => void;
}) {
  return <section className="rounded-xl border border-gridline bg-surface p-4">
    <div className="flex items-start justify-between gap-2">
      <div className="flex items-center gap-2"><KeyRound className="h-4 w-4 text-accent shrink-0" /><h2 className="font-medium text-ink">{item.label}</h2></div>
      <span className={`shrink-0 rounded-full px-2 py-1 text-xs font-medium ${item.configured ? 'bg-good/15 text-good' : 'bg-surface-2 text-ink-secondary'}`}>
        {item.configured ? 'Configured' : 'Not configured'}
      </span>
    </div>
    <p className="mt-1 text-xs text-ink-secondary">{item.pipeline.replace('_', ' ')} pipeline</p>
    <p className="mt-3 text-xs text-ink-secondary">
      {item.configured ? `${item.masked} via ${item.source}` : 'No value stored yet.'}
    </p>
    <div className="mt-3 flex gap-2">
      <input aria-label={`${item.label} value`} type="password" autoComplete="new-password" value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={item.configured ? 'Enter replacement value' : 'Enter credential'}
        className="min-w-0 flex-1 rounded-lg border border-gridline bg-background px-3 py-2 text-sm text-ink" />
      <button onClick={onSave} disabled={!value}
        className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-40">
        Save
      </button>
      {item.configured && <button aria-label={`Remove ${item.label}`} onClick={onRemove}
        className="rounded-lg border border-gridline p-2 text-critical">
        <Trash2 className="h-4 w-4" />
      </button>}
    </div>
  </section>;
}

const PIPELINE_GROUPS: { key: CredentialStatus['pipeline']; heading: string }[] = [
  { key: 'sentiment', heading: 'Sentiment sources' },
  { key: 'stock_price', heading: 'Stock price sources' },
];

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

    <SignalWeightingsSection />

    <AIAnalysisSection onChanged={load} />

    {loading ? <p className="text-sm text-ink-secondary">Loading credentials…</p> : <div className="flex flex-col gap-4">
      {PIPELINE_GROUPS.map(({ key, heading }) => {
        const group = items.filter((item) => item.pipeline === key);
        if (group.length === 0) return null;
        return <div key={key} className="flex flex-col gap-3">
          <h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-ink-secondary">
            <Activity className="h-3.5 w-3.5" />{heading}
          </h3>
          <div className="grid gap-3 md:grid-cols-2">
            {group.map((item) => <CredentialCard key={item.name} item={item}
              value={values[item.name] ?? ''}
              onChange={(value) => setValues((v) => ({ ...v, [item.name]: value }))}
              onSave={() => void save(item)}
              onRemove={() => void remove(item)} />)}
          </div>
        </div>;
      })}
    </div>}
  </div>;
}
