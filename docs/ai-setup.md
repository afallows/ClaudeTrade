# AI-Assisted Sentiment: Setup Guide

ClaudeTrade's sentiment pipeline is **fully functional with no AI provider
configured at all** (`ai.provider = "none"`, the default). A deterministic,
rule-based classifier (`sentiment.classifiers.RuleSentimentClassifier`)
scores every post -- lexicon, heuristics, no external calls, no cost, fully
offline. That classifier is the **mandatory floor**: it always runs,
whether or not AI is configured.

AI (Claude or ChatGPT) is an **opt-in ensemble adjunct**. When configured,
its per-post classification is blended into the sentiment ensemble with a
capped weight alongside the rule classifier's output -- it never replaces
the rule classifier, never relaxes a risk control, and never becomes the
sole basis of a trading signal. If a classification call fails for any
reason (missing credentials, a rate limit, a malformed response, the SDK
package not being installed), the pipeline silently falls back to the rule
classifier for that post -- see `sentiment/ai_classifier.py`'s module
docstring for the enforced contract ("this module must never raise into the
pipeline").

This guide covers: creating an API key for Claude or ChatGPT, connecting it
in the app, choosing a model, understanding the cost caps, and what AI does
and does not influence.

## Where to configure it

Open the desktop app's **Configuration** screen -> **AI Analysis** section.
It has:

- A **provider** dropdown: None / Claude (Anthropic) / ChatGPT (OpenAI).
- A **model** field (leave blank to use the provider's own sensible
  default -- shown as the field's placeholder).
- A **Test** button once a provider is selected: makes one minimal, real
  classification call against a canned sentence to confirm the credential,
  model, and SDK path all work end to end. It never echoes the API key.
- The credential fields themselves (`Anthropic (Claude) API key` /
  `OpenAI (ChatGPT) API key`) are further down the same Configuration
  screen, alongside every other provider's credentials -- both are always
  listed regardless of which provider is currently selected, so you can set
  up a key before (or without) flipping the provider dropdown.

Provider/model changes made here take effect **immediately for the running
session** (the Test button, diagnostics, and the next refresh/scan all see
it right away) but are **not** written back to `config.toml`. To make a
choice permanent across restarts, add it to `config.toml`'s `[ai]` table
(see the example below) or set the environment variables
`CLAUDETRADE_AI__PROVIDER` / `CLAUDETRADE_AI__MODEL`.

## Claude (Anthropic)

1. Go to **platform.claude.com** (formerly console.anthropic.com).
2. Sign in (or create an account).
3. Open **API Keys** and create a new key.
4. Copy it -- it starts with `sk-ant-`.
5. Paste it into the Configuration screen's "Anthropic (Claude) API key"
   field and save, or store it directly:

   ```bash
   claudetrade secrets set anthropic_api_key
   # or
   export CLAUDETRADE_SECRET_ANTHROPIC_API_KEY="sk-ant-..."
   ```

6. Set the AI Analysis provider dropdown to **Claude**.

Billing is **pay-per-use**, billed directly to your Anthropic account --
there is no separate subscription for API access.

**Model choice**: leave the model field blank to use the default,
`claude-opus-5` ($5.00 input / $25.00 output per million tokens). For
high-volume per-post classification, `claude-haiku-4-5` ($1.00 / $5.00 per
million tokens -- roughly a fifth of Opus 5's rate) is the economical
choice; it trades some classification nuance for a large cost reduction at
high post volume. Set `ai.model = "claude-haiku-4-5"` (or type it into the
model field) to switch. This is an operator judgment call this application
does not make for you -- pick based on your post volume and budget.

**Under the hood**: uses the official `anthropic` Python SDK
(`client.messages.create(...)` with structured outputs --
`output_config.format`) so the per-post sentiment JSON parses reliably.
`temperature`/`top_p`/`top_k` are never sent (removed on current Claude
models; sending them returns an error). Typed SDK exceptions
(`RateLimitError`, `APIStatusError`, `APIConnectionError`) are all mapped
into the same silent-degrade-to-rules contract described above --
`providers/ai/anthropic_provider.py` is the source of truth.

## ChatGPT (OpenAI)

1. Go to **platform.openai.com**.
2. Sign in (or create an account) and add a payment method.
3. Open **API keys** and create a new key.
4. Copy it -- it starts with `sk-`.
5. Paste it into the Configuration screen's "OpenAI (ChatGPT) API key"
   field and save, or store it directly:

   ```bash
   claudetrade secrets set openai_api_key
   # or
   export CLAUDETRADE_SECRET_OPENAI_API_KEY="sk-..."
   ```

6. Set the AI Analysis provider dropdown to **ChatGPT**.

Billing is **pay-per-use**, billed directly to your OpenAI account.

**Model choice**: OpenAI's model lineup and pricing change faster than this
document can reliably track -- check current model names and pricing at
platform.openai.com before relying on the built-in default
(`providers/ai/openai_provider.py`'s `DEFAULT_MODEL`). Set the model field
explicitly once you've picked one.

**Under the hood**: uses the official `openai` Python SDK
(`client.chat.completions.create(...)` with JSON-schema structured output
mode) against the same sentiment schema Claude uses. The same typed-SDK-
exception degrade contract applies (`RateLimitError`, `APIStatusError`,
`APIConnectionError` all map to a clean fallback, never a raised exception).

## Cost caps

Independent of which provider you pick, `[ai]` in `config.toml` (or the
equivalent env vars) enforces:

| Setting | Default | What it does |
|---|---|---|
| `max_calls_per_run` | 250 | Hard cap on classification calls in one pipeline run. |
| `daily_cost_limit_usd` | 5.0 | Local running-cost estimate; further calls are refused once exceeded. |
| `cache_enabled` / `cache_ttl_hours` | true / 168 (1 week) | Identical (post, symbol, model, prompt version) requests are served from cache rather than re-billed. |
| `injection_block_threshold` | 0.4 | A post scoring above this on the injection-risk heuristic is **never** sent to the AI provider at all, regardless of cost caps. |
| `input_cost_per_mtok_usd` / `output_cost_per_mtok_usd` | Claude Opus 5's published rate | Used only for the *local* running-cost estimate against `daily_cost_limit_usd` -- update these to match whichever model you actually configure (e.g. `claude-haiku-4-5` is $1.00/$5.00, not $5.00/$25.00). |

**Rough per-refresh estimate**: with the default 250-call cap and typical
post lengths, a full refresh with Claude Opus 5 costs roughly $1-$2;
`claude-haiku-4-5` for the same call volume is roughly a fifth of that. This
is an illustrative range, not a guarantee -- actual cost depends on post
length, how many symbols have eligible sentiment volume that cycle, and the
cache hit rate.

## What AI does and does not influence

- **Does**: contributes one more classification (bullish/bearish/hype/fear/
  etc. scores, each 0-1) per (post, symbol) pair, blended into the
  sentiment ensemble alongside the rule classifier's own scores.
- **Does not**: replace the rule classifier (which always runs
  regardless), decide entries/exits/position sizing on its own, relax any
  risk control (`risk.*`/`filters.*` config), or see anything beyond the
  sanitised post text and the target symbol -- usernames, author ids,
  karma, follower counts, and post history are never included in a
  request (see `sentiment/ai_classifier.py`'s module docstring).
- **Never runs at all on**: a post whose `injection_risk_score` exceeds
  `ai.injection_block_threshold` -- enforced in code, not just documented.

## Privacy note

When AI is configured, the **sanitised text of posts sent for
classification, and the target stock symbol, are sent to the chosen
provider's API** (Anthropic or OpenAI, per your selection) over that
provider's own HTTPS endpoint, subject to that provider's own API terms and
data-retention policy. No usernames, author identifiers, karma, follower
counts, or post history are ever included -- only the sanitised post text
and the symbol. If `ai.provider = "none"` (the default), nothing is ever
sent to an external AI provider; sentiment stays fully local.

## Example `config.toml` entry

```toml
[ai]
provider = "anthropic"                    # or "openai", or "none"
model = "claude-haiku-4-5"                # blank = provider's own default
anthropic_api_key_credential = "anthropic_api_key"
openai_api_key_credential = "openai_api_key"
max_calls_per_run = 250
daily_cost_limit_usd = 5.0
input_cost_per_mtok_usd = 1.0             # match claude-haiku-4-5's published rate
output_cost_per_mtok_usd = 5.0
```

Credential *values* never belong in `config.toml` -- only the credential
*names* above do. Store the actual key via `claudetrade secrets set
anthropic_api_key` / `openai_api_key`, the Configuration screen, or the
matching `CLAUDETRADE_SECRET_*` environment variable.

See also: `docs/api-providers.md`'s "AI Providers" section for the full
provider adapter reference, and `docs/windows-install.md` for the overall
first-run setup flow (`scripts/setup.ps1`/`setup.bat`).
