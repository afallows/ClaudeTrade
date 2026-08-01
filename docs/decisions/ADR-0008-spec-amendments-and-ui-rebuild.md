# ADR-0008: Spec amendments — personal-use scraping, UI rebuild, live universe

Date: 2026-07-30
Status: Accepted

## Context

After two rounds of real Windows trials, the project owner amended the
original specification in five ways: (1) universe = every US and Canadian
listing above $1B market cap; (2) a ground-up UI rebuild, technology at the
coordinator's discretion; (3) removal of the original no-scraping constraint
for this personal-use deployment, using the owner's own credentials, with the
official APIs retained as fallback; (4) one-script setup; (5) a broader
sentiment universe including X via the owner's personal session.

## Decision 1 — Scraping posture (amends the original operating principles)

**Decision.** The application may fetch content the owner's own logged-in
accounts can lawfully view, under these non-negotiable engineering
constraints:

* **Own credentials only.** Session cookies/passwords are the owner's, stored
  in the OS credential store, never bundled or defaulted.
* **Fail closed.** Any block, challenge, CAPTCHA, or unexpected response
  terminates that source's fetch for the cycle. The application will never
  solve CAPTCHAs, rotate fingerprints/proxies, or otherwise evade anti-bot
  systems — both as a line we don't cross and because that arms race
  produces an unmaintainable adapter.
* **Conservative, human-scale rates** with jitter; identifying user agent
  where the protocol allows.
* **Official APIs remain first-choice** wherever available and configured;
  scraped paths are fallbacks or opt-ins, never silent defaults.
* **Documented account risk.** Automating a logged-in X session violates
  X's ToS and can lead to account suspension; the owner accepts this for
  their own account. The Reddit public-JSON path is ToS-gray for automated
  use; the authenticated official API (script app, password grant — an
  official flow) is preferred the moment credentials work.

**Alternatives considered.** Keeping the API-only rule (rejected by the
owner: X's API is paywalled and Reddit's approval is pending); paid
aggregators (rejected earlier in ADR-0007 for history caps/cost).

**Risks.** Adapter fragility (mitigated: fail-closed design means breakage
degrades to "source unavailable", which the pipeline already handles);
account suspension (owner-accepted, bounded by fail-closed + rates).

**Reversal.** Each scraped source is a provider behind the existing
SocialProvider seam with an `enabled` flag; deleting the module restores the
prior posture. The spec's original wording is preserved in git history.

### Amendment 1 (2026-07-31, owner directive) — browser-TLS impersonation for the Stocktwits keyless endpoint

*The Decision 1 text above stands unchanged and remains the general rule.
This amendment records one narrow, explicitly owner-authorised exception for
this personal-use deployment. Owner's words: "This is a personal use project.
We can take any approach to stocktwits needed to make it work effectively."*

**Scope — one source, one mechanism.** `api.stocktwits.com/api/2/streams/
symbol/{SYMBOL}.json` may be fetched with a client that presents a real
browser's TLS ClientHello fingerprint (JA3), via `curl_cffi`'s browser
impersonation. Nothing else changes, and the exception extends to no other
source.

**Why this is not the line Decision 1 draws.** Decision 1's "never rotate
fingerprints ... or otherwise evade anti-bot systems" was written about
*credentialed, ToS-gated* surfaces, where the block is the vendor enforcing
an access boundary. This case is materially different on every axis:

* The endpoint is **keyless and open by the vendor's own documentation**.
  There is no credential to present, none is bypassed, no paywall or
  authentication boundary exists to cross, and no account (ours or anyone
  else's) is involved.
* The block is **not an access decision about the request** — it is a
  client-shape heuristic about the *handshake*. Confirmed cause: Cloudflare
  bot management gating on the TLS fingerprint. A logged-out browser tab on
  the same machine and IP is served HTTP 200 for the identical URL; a
  stdlib-`ssl` Python client gets 403 before the request line is read.
* We are therefore **making our client look like the client the endpoint
  already serves**, not defeating a gate that says no to us on the merits.
* It is one **honest, configured, disclosed** profile (`stocktwits.impersonate`,
  default `"chrome"`), not a rotating pool. "Fingerprint rotation" — the
  thing Decision 1 forbids, and the thing that produces the unmaintainable
  arms race — is still forbidden here.

**What still applies, unchanged.** Fail-closed on any 401/403, non-JSON body,
or unexpected shape (`SourceBlockedError` ends that source's cycle: no retry
loop, no proxy rotation, no CAPTCHA solving, no profile-shuffling on a
block). Conservative, human-scale rates with jitter, well under the vendor's
published 200 requests/hour unauthenticated budget. A hard
`max_symbols_per_cycle` cap. **No Cloudflare cookie is captured, stored or
replayed** — no `cf_clearance`/`__cf_bm` harvesting, no session persistence;
each request stands alone. Impersonation is expected to make a block
unlikely, not impossible, and "Stocktwits unavailable" remains a supported,
non-degraded state for the application.

**This does NOT extend to credentialed sources.** X and Reddit keep the
original Decision 1 posture verbatim. There the block *is* an access/ToS
boundary tied to an account, so the reasoning above does not transfer — and
neither does the authorisation.

**Consequences.** `curl_cffi` becomes an **optional** runtime dependency
(lazy-imported, same pattern as the `anthropic`/`openai` SDKs): without it
the application runs unchanged and the source reports itself unavailable with
an install hint. `stocktwits.enabled` flips to `true` by default (owner
directive) — the vendor's rate budget is respected by the rate limiter and
symbol cap rather than by leaving the source switched off.

**Risks.** Cloudflare heuristics change; a profile that works today may be
challenged later (mitigated: fail-closed, plus `impersonate` is a one-line
config change and `curl_cffi` ships new profiles as browsers ship). The
impersonation profile drifts from the browser fleet as Chrome advances
(mitigated: the default is the alias `"chrome"`, which resolves to
`curl_cffi`'s newest Chrome build rather than a pinned version).

**Reversal.** Set `stocktwits.enabled = false`, or uninstall `curl_cffi` —
either leaves the source cleanly unavailable. Reverting the transport to
`httpx` restores the pre-amendment behaviour (and, on the evidence, HTTP
403).

## Decision 2 — UI rebuilt as a local web app in a desktop shell

**Decision.** Replace Streamlit with a React + TypeScript SPA (Vite build;
AG Grid for interactive tables with row click-through; Plotly for charts;
a coherent dark design system) served by a FastAPI layer over the existing
pipeline/engine code, launched in a native desktop window (pywebview) with
plain-browser fallback. Built static assets ship with the package so end
users never need Node. Streamlit remains available as `--classic` until the
new UI reaches parity, then is removed.

**Alternatives considered.** PySide6 (spec's original suggestion): rejected
on polish-per-effort — the owner's complaints are aesthetic and
interactional, where the web stack's defaults are strongest and Qt requires
the most custom work; also weakest testability in this environment.
NiceGUI/Flet: simpler but constrain the design ceiling and add less-mature
packaging risks. Electron/Tauri: heavier toolchains for no gain over
pywebview given a Python core.

**Risks.** Two-language repo; mitigated by shipping built assets and keeping
all domain logic in Python behind typed API endpoints. PyInstaller must
bundle static assets + pywebview (validated on the owner's machine like the
rest of the Windows build).

**Reversal.** The FastAPI layer is UI-agnostic; any future shell (including
PySide6 hosting a webview) consumes the same endpoints.

## Decision 3 — Universe is computed, not shipped

**Decision.** The shipped CSVs become bootstrap seeds only (expanded to
Russell-1000-scale US coverage plus the TSX Composite). The authoritative
universe is computed at refresh time on the owner's machine: all US + CA
listings for which the market-data path can establish market cap, filtered
to >= $1B (configurable floor), with the existing liquidity/price/exchange
filters unchanged. A name missing from the seeds but present in provider
data joins the universe; a seeded name that delists falls out.

**Risks.** Market-cap data quality varies by provider; the data-quality
layer must flag names whose cap cannot be established rather than silently
excluding them.

## Decision 4 — One-script setup

`setup.bat`/`setup.ps1`: verify/install Python, create venv, install
dependencies, init database, run the first 90-day refresh, launch the UI.
The UI, not the CLI, is the primary surface; the CLI remains for automation
and troubleshooting.
