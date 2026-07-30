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
