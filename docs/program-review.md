# Program Review and Remediation Plan

Review date: 2026-07-29. This is an engineering review, not a validation of any
strategy's profitability and not investment advice.

## Executive assessment

The repository is a substantial **alpha research framework**, not a complete or
production-ready trading product. Its strongest properties are provider
interfaces, synthetic/offline fallbacks, explicit research-only positioning,
an append-oriented signal ledger, five transparent strategy modules, an
execution-cost model, and broad unit-test intent. It must remain paper-only.

The application cannot currently satisfy the final acceptance criteria. The
initial review ran 230 tests: 189 passed and 41 failed. Failures affect core
risk claims, including earnings exclusion windows, participation-limited fills,
performance metrics, position sizing, migrations, prompt-injection detection,
and risk-limit messages. A passing test that merely exercises synthetic data is
not evidence that a live vendor connection, point-in-time dataset, or strategy
has been validated.

## Connector verification

### Market data

* `StooqMarketProvider` uses the historical CSV endpoint, maps bare US symbols
  to Stooq's `.us` namespace, bounds requests by date, rejects quote-shaped and
  no-data payloads, verifies TLS, rate-limits locally, and explicitly refuses
  intraday requests. Mocked contract tests pass.
* The Stooq feed is daily-only, unadjusted in the returned domain object, has no
  point-in-time universe or reliable delisted-security coverage, and is not a
  real-time feed. It is unsuitable for survivorship-unbiased validation and its
  licensing must be checked by the operator for the intended use.
* CSV and deterministic synthetic adapters provide lawful offline fallbacks.
  CSV correctness and licensing remain the operator's responsibility.

### Reddit

* `RedditProvider` uses OAuth client credentials, an identifying user agent,
  local throttling, TLS through `httpx`, sanitised text, and salted author
  digests. Missing credentials produce `NotConfiguredError`, allowing the
  registry to degrade cleanly.
* This review repaired two correctness issues: results are now constrained by
  both `since` and `until`, and crossposts are detected by `crosspost_parent`
  rather than the unrelated `is_crosspostable` permission.
* The implementation fetches submissions only. Despite configuration exposing
  `comments_per_post`, it does **not** fetch comments, account age, karma,
  pagination, deleted-content revisions, or historical engagement snapshots.
  Documentation and UI must not claim those fields are collected.
* Client-credentials access and a `script` app may not be appropriate for every
  deployment or distribution model. Operators must obtain Reddit approval when
  required and re-check the current Data API Terms before enabling it.

### X

* `XProvider` uses the official v2 recent-search endpoint and a bearer token,
  throttles locally, sanitises text, pseudonymises authors, handles 403/429, and
  disables cleanly without credentials.
* This review migrated the hostname to `api.x.com`, requests `username`, hashes
  the stable author ID when username is omitted, and calculates account age
  from the included user record. This prevents all missing-username posts from
  collapsing into one empty-author bucket.
* It does not paginate, use `next_token`, collect quote counts, identify all
  reply relationships, snapshot mutable engagement, or support historical/full
  archive search. Query terms are configured manually; the `symbols` hint is
  ignored. Production-scale recent search generally requires paid access.
* There is no recommended unofficial “free X connector.” Scraping, browser
  automation, or proxy datasets can violate terms, licensing, privacy, or
  access controls. When authorized API access is unavailable, keep X disabled
  and show reduced data confidence.

## Architecture and implementation findings

### High priority — correctness gates

1. **Make the core suite green before strategy research.** Resolve the 41
   failures without weakening assertions. In particular, independently verify
   metrics against hand-calculated fixtures and determine whether costs are
   intended in each reported pre-/post-cost field.
2. **Repair earnings semantics.** `effective_risk_date_range` currently fails
   its own before/after-buffer tests. Define calendar-day versus trading-session
   behavior, BMO/AMC execution timing, confirmed versus estimated dates, and an
   `as_of` revision timestamp.
3. **Repair execution realism.** Participation is currently interpreted in a
   way that lets the test order fill completely, and gap-through-stop reason
   semantics disagree with the public contract. Specify shares-versus-dollar
   volume, adverse slippage, partial fills, and next-bar timing in one ADR.
4. **Repair persistence lifecycle.** Database fixtures appear pre-migrated even
   when migration tests require a fresh database. Separate engine construction
   from schema initialization and test upgrade paths from every supported
   version.
5. **Repair risk controls and injection handling.** Several tests show missing
   or incorrect user-facing risk reasons and failure to detect direct “ignore
   instructions”/“manipulate scores” content. Treat this as a release blocker
   for any LLM-enabled mode.

### Medium priority — research validity

* Add a point-in-time security master with delistings and symbol changes. Never
  market results from a current-membership-only universe as unbiased.
* Store immutable raw observations with `observed_at`, `effective_at`, vendor,
  request parameters, payload hash, and license-retention policy. Engagement and
  earnings revisions must be treated as observations, not timeless facts.
* Add walk-forward integration tests that prove the feature cutoff precedes the
  signal and execution cutoffs. Include a deliberately poisoned future column
  and assert it cannot change earlier signals.
* Separate gross and net P&L throughout the domain. Reconcile cash, positions,
  equity, fees, borrow costs, dividends, splits, and short-sale constraints on
  every bar.
* Add deterministic end-to-end fixtures covering ingestion, entity resolution,
  sentiment, signal creation, ledger revision, entry, gap exit, and final
  metrics. Record golden configuration/data/code hashes.
* Make confidence a data-quality output: a missing social provider should not
  be equivalent to neutral sentiment, and stale/partial data must cap signal
  confidence.

### Medium priority — provider robustness

* Reuse one `httpx.Client` per provider, close it explicitly, use exponential
  backoff with bounded jitter for retryable failures, and expose vendor request
  IDs without logging credentials or raw personal data.
* Parse and honor vendor rate-limit reset headers, not only local estimates.
* Add pagination with strict global limits and duplicate-ID suppression.
* Validate provider response schemas and numeric bounds. Quarantine malformed
  records rather than silently converting them into normal observations.
* Add opt-in live contract tests marked `network`; keep mocked response-shape
  tests as the deterministic CI gate.

### Windows and operations

* The Streamlit UI is a prototype, not a native Windows desktop application.
  The repository lacks a committed PyInstaller spec, Windows build workflow,
  signed installer, update path, and tested Credential Manager workflow.
* Add a locked dependency artifact with hashes, automated vulnerability and
  license scans, a Windows CI matrix, log retention/redaction tests, backup
  restore drills, and an SBOM. Broad lower bounds in `pyproject.toml` are not a
  reproducible production environment.
* Keep live brokerage code absent until a separate threat model, authorization
  ceremony, kill switch, idempotent order model, reconciliation loop, and paper
  soak test are approved.

## Recommended release gates

1. **Alpha:** all unit tests, Ruff, and mypy pass; mock pipeline and database
   restore tests pass; no credentials or local tool settings are tracked.
2. **Research beta:** point-in-time datasets and universe are licensed; golden
   end-to-end and leakage tests pass; walk-forward reports are reproducible.
3. **Windows beta:** clean Windows VM install/build/uninstall is automated;
   Credential Manager, scheduling, backups, and offline mode are exercised.
4. **Paper release:** at least one full paper-trading observation period is
   reconciled; alerts, stale-data gates, and immutable revisions are audited.
5. **Live consideration:** a separate project decision. It is not implied by
   any research or paper-trading result.

