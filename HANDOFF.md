# HANDOFF: Institutional/Insider Sentiment Signal

## Status: PAUSED by usage guard (95% threshold) before any code was written

This task was in the **research/exploration phase only**. No source files have
been created or modified yet. Safe to resume from scratch by re-reading the
plan below and the reference commit.

## Task

Implement a weighted insider/hedge-fund ("institutional") sentiment signal,
mirroring the architecture of the just-landed analyst-sentiment feature
(commit `7b1dc36`). Full original task spec (payload fields, build steps,
scoring principles, deliverables) was provided by the coordinator — re-read
it from the coordinator/user if resuming in a new session, since it is not
duplicated here in full. Key points:

- Zero new API calls: parse more fields out of the TipRanks `dataForTicker`
  `overview` payload already being fetched/cached.
- New fields: `corporateInsiderTransactions[]`, `insiderslast3MonthsSum`,
  `insidrConfidenceSignal`, `insiders[]`, `hedgeFundData` (sentiment, trend,
  `holdingsByTime[]`, `institutionalHoldings[]`), `numOfInsiders`,
  `marketCapUSD`.
- Build: parser -> `InstitutionalSnapshot` domain object, pure
  `institutional_score()` function (documented weights/half-lives/damping),
  `institutional_snapshots` table + migration 012, `data/institutional.py`
  (batched read + pure delta), ingest wiring, Streamlit block, MCP tool
  `get_institutional_sentiment`, tests, docs.
- Constraints: do NOT touch `src/claudetrade/webapi/**` or `frontend/**`; do
  NOT run `git commit` (coordinator commits); ignore pre-existing
  `test_providers.py` failures (live credentials).

## Research completed (read in full, do not re-read unless verifying details)

Read commit `7b1dc36` end to end as the architecture template:

- `src/claudetrade/providers/market/tipranks_analyst.py` — parser module.
  Pattern: module-level docstring documents every fixture cross-reference
  (which vendor codes mean what, confirmed vs. unconfirmed), small `_maybe_float`/
  `_maybe_int`/`_parse_date` helpers, row-selection helpers with documented
  fallback order, a `parse_analyst_snapshot(overview, symbol, as_of_session,
  fetched_at) -> AnalystSnapshot | None` entry point that returns `None` when
  there's no real coverage (never an all-None object). Module-level constants
  for list caps (`CONSENSUS_OVER_TIME_MAX`, `RECENT_RATING_ACTIONS_MAX`) with
  rationale comments.
- `src/claudetrade/domain.py` — `AnalystConsensusPoint`, `AnalystRatingAction`
  (frozen slots dataclasses), `AnalystSnapshot` (mutable slots dataclass,
  since it holds lists). All heavily docstringed with fixture provenance.
  New institutional dataclasses should go in the same file, same style,
  near the analyst classes (~line 275-407).
- `src/claudetrade/data/analyst.py` — mirrors what `data/institutional.py`
  needs: `_row_to_snapshot` (row->domain), `snapshot_to_row_fields`
  (domain->row dict, write-side mirror), `latest_and_previous_snapshots`
  (TWO queries total via GROUP BY + self-join subqueries — F26 discipline,
  never a per-symbol loop), `AnalystDelta` dataclass + `analyst_delta` pure
  function (None fields when either side missing, never fabricated 0).
- `src/claudetrade/db/models.py` — `AnalystSnapshotRow` (~line 510-578):
  `UniqueConstraint("session","symbol")`, `Index` on (symbol, session),
  JSON columns for list-shaped sub-data, upsert-not-append (no immutability
  trigger, unlike `signals`/`signal_revisions`).
- `src/claudetrade/db/migrations.py`: migration 011 = `_m011_analyst_snapshots`,
  pattern is `AnalystSnapshotRow.__table__.create(session.get_bind(),
  checkfirst=True)` (checkfirst=True makes it a no-op on a fresh DB since
  `create_all` already made it; real work only on pre-existing DBs).
  `LATEST_VERSION = max(m.version for m in MIGRATIONS)` — migration 012 must be
  appended to the `MIGRATIONS` tuple, never edit existing entries.
- `src/claudetrade/data/ingest.py`:
  - `IngestReport` dataclass has `analyst_snapshots_upserted: int = 0`
    (~line 120) and it's included in the report dict (~line 140). Need an
    `institutional_snapshots_upserted` counter analogously.
  - `_tipranks_analyst_provider()` helper (~line 1299) picks whichever of
    `self.earnings`/`self.market` is the tipranks-backed instance exposing
    `get_analyst_snapshots` — an `institutional` ingest should reuse the same
    provider-selection helper or a near-identical one for
    `get_institutional_snapshots`.
  - `ingest_analyst_snapshots(symbols, session_date, report)` (~line 1324-1402):
    fetch via provider method (degrade to `report.provider_failures` on
    exception, return 0), then upsert in chunks (`PERSIST_CHUNK_ROWS`) inside
    `self.db.session()`, checking `symbol in known` (from `Security` table)
    before writing, catching per-row exceptions without aborting the batch.
  - Wired into `run_full_refresh` right after `ingest_earnings` /
    `ingest_analyst_snapshots(symbols, current_trading_session(), report)`
    (~line 2033). Institutional ingest should be called immediately after
    that line.
- `src/claudetrade/providers/market/tipranks.py`:
  `get_analyst_snapshots(symbols, *, as_of_session=None)` (~line 1585-1640):
  goes through `self._resolve_map(symbols)` (the shared fetch/cache path,
  zero extra HTTP calls), calls `parse_analyst_snapshot` per symbol, skips
  `None` results, catches parse exceptions per-symbol (log + skip). An
  `get_institutional_snapshots` method should mirror this exactly, calling
  a new `parse_institutional_snapshot` from the new parser module.
- `src/claudetrade/mcp_server.py`:
  `_analyst_rating_action_payload` / `_analyst_snapshot_payload` (dict
  serializers, ~line 346-393), `get_analyst_sentiment(pipeline, symbol)`
  (~line 396-456): loads via `latest_and_previous_snapshots`, returns
  `available=False` + explanatory `note` when nothing stored (never an
  error), else `snapshot` + `delta` dicts. Tool registration block
  (~line 1177-1199) uses `@server.tool(name=..., description=...)` then an
  async wrapper calling `_call_bounded(name, config.mcp.tool_timeout_seconds,
  lambda: get_analyst_sentiment(pipeline, symbol))`. New tool
  `get_institutional_sentiment` goes right after this block, same shape.
  **Must also add `"get_institutional_sentiment"` to `EXPECTED_TOOL_NAMES`
  in `tests/test_mcp_server.py`** (not yet located — grep for
  `EXPECTED_TOOL_NAMES`).
- `src/claudetrade/ui/data_access.py`: `AnalystOverlay` dataclass (frozen
  slots, `available`/`snapshot`/`delta`) + `analyst_sentiment(db, symbol)`
  function (~line 198-228) — thin wrapper over
  `latest_and_previous_snapshots` + `analyst_delta` for a single symbol.
  Exported in `__all__` (~line 244). New `InstitutionalOverlay` +
  `institutional_sentiment()` should mirror this, added to `__all__` too.
- `src/claudetrade/ui/screens/ticker_detail.py`: `_render_analyst_sentiment
  (pipeline, symbol)` (~line 249+) called from `page_ticker_detail()`
  right after `_render_chart` (~line 82). New
  `_render_institutional_sentiment` should be called immediately after it
  (task says "next to the analyst block").

## NOT yet read / NOT yet done (pick up here)

1. **Fixture verification**: `tests/fixtures/tipranks/dataForTicker_INTC.json`
   and `..._TECK_B.json` were NOT yet opened this session. MUST read both in
   full before writing the parser (per the pattern the analyst module set —
   every field mapping needs fixture-backed confirmation or an explicit
   "unconfirmed, stored raw" note). Task description asserts INTC has real
   insider + hedge-fund data and says to check what TECK_B has (nulls path).
2. Tail of `db/migrations.py` (`_m010_adanos_snapshots`/`_m011_analyst_snapshots`
   full text) was read; need to re-verify exact `checkfirst=True` pattern
   when writing migration 012 (should be trivial copy of `_m011`'s shape).
3. `docs/api-providers.md` TipRanks section — NOT yet read. Need to find
   the analyst subsection and extend it with institutional fields + scoring
   formula summary (weights, half-lives, staleness caveats).
4. Test files NOT yet read (mirror their structure exactly):
   - `tests/test_tipranks_analyst_parsing.py` (parser tests pattern)
   - `tests/test_analyst_snapshot_storage.py` (storage/migration tests)
   - `tests/test_ingest_analyst_snapshots.py` (ingest tests)
   - `tests/test_mcp_analyst_sentiment.py` (MCP tool tests)
   - `tests/test_mcp_server.py` `EXPECTED_TOOL_NAMES` — find and add
     `get_institutional_sentiment`.
5. Nothing has been written yet: no `InstitutionalSnapshot` domain class, no
   `tipranks_institutional.py` (or extension of `tipranks_analyst.py`), no
   `institutional_score()` function, no `InstitutionalSnapshotRow`, no
   migration 012, no `data/institutional.py`, no ingest wiring, no UI block,
   no MCP tool, no tests, no docs.

## Recommended next step

1. Read both fixture files in full (`tests/fixtures/tipranks/
   dataForTicker_INTC.json`, `..._TECK_B.json`) — grep for
   `corporateInsiderTransactions`, `insidrConfidenceSignal`, `insiders`,
   `hedgeFundData`, `numOfInsiders`, `marketCapUSD` to locate exact shapes
   and confirm field presence/absence between the two fixtures.
2. Read the 4 analyst test files listed above to nail down exact test
   structure/fixtures/mocking conventions before writing new tests.
3. Read the `docs/api-providers.md` TipRanks analyst subsection.
4. Then build in this order (mirrors the analyst commit's own layering):
   domain dataclasses -> parser module -> score function (with its own
   dedicated test file since it's pure and heavily specified) -> db model +
   migration 012 -> data/institutional.py (read+delta) -> ingest wiring ->
   provider method on TipRanksProvider -> MCP payload/tool + EXPECTED_TOOL_NAMES
   -> ui data_access + ticker_detail block -> docs -> targeted tests + ruff.
5. Do NOT run `git commit`. Do NOT touch `webapi/**` or `frontend/**`.

## Files touched this session

None (research only). This HANDOFF.md is the only new file.

## Test results

Not applicable — no code written yet, no tests run.

## Full original task spec (added by coordinator so this file stands alone)

Implement a weighted insider/hedge-fund ("institutional") sentiment signal.
Mirror commit 7b1dc36 (analyst sentiment) end to end. Zero new API calls.

Payload fields (verified from tests/fixtures/tipranks/dataForTicker_INTC.json,
under "overview"; tolerate nulls everywhere):
- corporateInsiderTransactions[]: {month, year, sharesBought, insidersBuyCount,
  sharesSold, insidersSellCount, transBuyCount, transSellCount, transBuyAmount,
  transSellAmount, informativeBuyCount, informativeSellCount,
  informativeBuyAmount, informativeSellAmount} -- monthly aggregates, ~3 rows.
- insiderslast3MonthsSum: float net dollars + insidersLast3MonthsSumCurrencyTypeID.
- insidrConfidenceSignal: {stockScore, sectorScore, score} (vendor's own typo).
- insiders[]: {name, isOfficer, isDirector, isTenPercentOwner, officerTitle,
  action, amount, numberOfShares, rDate, insiderOperationDescription,
  estimatedSharesValue, link} -- keep most recent few as evidence rows.
- hedgeFundData: {sentiment (0..1), trendAction, trendValue, holdingsByTime[]:
  {date, holdingAmount, institutionHoldingPercentage, netSharesChange,
  numberOfSharesBought, numberOfSharesSold, isComplete}, institutionalHoldings[]:
  {managerName, institutionName, action, effectiveDate, value, change,
  changeAmount, percentageOfPortfolio, stars, isActive}} -- quarterly, lagged.
- numOfInsiders; marketCapUSD (for normalization).

Build steps:
1. Parser producing InstitutionalSnapshot (sibling to tipranks_analyst.py or
   inside it): monthly insider rows (informative* distinct from raw),
   insider_net_3m_usd, insider_confidence, top ~5 recent insider transactions
   by estimatedSharesValue (role flags + SEC link), hedge-fund
   sentiment/trend/holdings-by-quarter (netSharesChange series + latest quarter
   date for staleness), top ~5 notable holders by |changeAmount| (with stars),
   market_cap_usd, fetched_at. No institutional content at all -> not stored.
2. Pure institutional_score(snapshot, as_of) -> [-1, +1] or None:
   - Insider axis: net informative buy-sell dollar flow (3m) normalized by
     market cap, log-damped; blended with insidrConfidenceSignal.stockScore.
     Informative fields preferred; raw only when informative is null.
   - Hedge-fund axis: vendor sentiment (0..1 -> -1..+1) blended with latest
     quarter netSharesChange relative to holdingAmount.
   - Staleness: hedge-fund axis weight decays with latest-quarter age (near
     zero at 2 quarters); insider axis decays on newest transaction month age.
   - Blend: insider axis weighted above hedge-fund; absent axis redistributes
     weight; both absent -> None, never a fabricated 0.
   - Every constant = named module constant with rationale. Return per-axis
     subscores, applied weights, staleness ages.
3. institutional_snapshots table + migration 012 (append-only migration list;
   keep LATEST_VERSION test pattern). Same-session upsert. Store computed
   score components in the row (self-contained, diffable).
4. data/institutional.py mirroring data/analyst.py: latest_and_previous_snapshots
   (two queries total, F26 discipline) + pure institutional_delta (score change,
   net-flow change, HF sentiment change, new holder actions, new transactions).
5. Ingest wired into run_full_refresh right after ingest_analyst_snapshots;
   per-symbol degrade-not-abort; new IngestReport counter.
6. Surfacing: Streamlit ticker_detail "Institutional sentiment" block (axis
   breakdown + staleness, transactions with roles + SEC links, holder moves);
   MCP read-only get_institutional_sentiment(symbol) following
   get_analyst_sentiment exactly (add to EXPECTED_TOOL_NAMES in
   tests/test_mcp_server.py). NOT fed into ComponentScores/strategies (say so
   in docstrings).
7. Tests (targeted only; full suite 20+ min): INTC fixture parse (real insider
   + HF data), TECK_B nulls path, score function (all-data / insider-only /
   HF-only / no-data->None / staleness decay incl. near-zero at 2 quarters /
   market-cap normalization direction / clamping), storage upsert, migration
   012, delta, batched-read query-count guard, MCP tool.
8. docs/api-providers.md TipRanks section: institutional fields + formula
   summary (weights, half-lives) + staleness caveats.
9. ruff on changed files. Ignore pre-existing test_providers.py failures
   (live credentials on this machine). Do NOT git commit; do NOT touch
   webapi/** or frontend/**.
