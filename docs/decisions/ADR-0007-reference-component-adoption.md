# ADR-0007: Component adoption from reference repositories

Date: 2026-07-29
Status: Accepted

## Context

Five reference repositories were cloned (as user forks under `afallows/`) and
analysed by independent review agents against ClaudeTrade's four diagnosed
gaps: (1) strategy thresholds so strict the scanner fired 1 proposal in 230
evaluations; (2) a backtest that emitted 0 trades with degenerate metrics
(Sharpe = -9.2e16) and no explanation of why nothing traded; (3) no live-broker
execution boundary; (4) no Windows packaging. Analyses (with file-level
evidence and, for ROT, targeted test runs) are archived in the session
scratchpad under `refs/ANALYSIS-*.md`.

A rule applied throughout: a reference feature counts as REAL only when
working code plus a test or executable entry point was found; README claims
alone count for nothing.

## Decision 1 — Keep ClaudeTrade's own scaffolding; adopt patterns, never merge

**Decision.** ClaudeTrade remains its own codebase. No reference repository
becomes a dependency or a base framework.

**Alternatives considered.** (a) Rebase onto lumibot as the strategy/broker
engine; (b) import ROT's sentiment modules; (c) adopt openbb-adanos as the
social-data provider.

**Reasons.** lumibot's LICENSE file is GPL-3.0 despite its README/setup.py
claiming MIT — an unresolved conflict that makes it unusable as a dependency
for this project; its Strategy/Broker classes are also 3,000–6,000-line
monoliths fusing execution, options math and telemetry. gr8monk3ys/trading-bot
has the same GPL/MIT conflict. ROT's relevant modules are well-tested in
isolation but orphaned — nothing in its own app calls them — and its
ingestion/entity/paper layers are weaker than ours (its reviewer's own
finding). openbb-adanos is a thin client for a hosted API capped at 90 days of
history, which cannot feed honest backtests. Independent review confirmed our
Reddit/sentiment stack is stronger than every reference equivalent.

**Risks.** We forgo battle-tested execution code; reimplementation may
reintroduce solved bugs.

**Reversal plan.** The broker ABC (Decision 4) is the only seam a framework
would replace; if lumibot resolves its licensing, an adapter implementing our
ABC over lumibot brokers is a bounded change.

## Decision 2 — Strategy calibration: weighted score accumulation with history-relative thresholds (gap 1)

**Decision.** Convert the five strategies from AND-chains of absolute hard
gates to weighted score accumulation: each condition contributes to a score,
a small number of non-negotiable conditions remain vetoes (earnings window,
liquidity, manipulation risk), and entry thresholds are expressed relative to
the symbol's own trailing distribution (percentile / z-score) and regime, not
as absolute constants. Tier labels are applied after ranking, so a scan
always yields a ranked list even when nothing is actionable.

**Sources.** trading-bot `strategies/momentum/signals.py::_generate_signal`
(±1 contributions, soft ±0.5 adjustments, late veto-only filters, score ≥
threshold) — pattern only, GPL/MIT conflict. finresearch
`peer_comparison.py:431-457`, `llm_insight_engine.py:209-232`,
`anomaly_detector.py` (percentile vs own 5-year history; rolling z-scores;
tiers after ranking) — MIT, reusable. ROT `analytics/iv_rank.py` (52-week
rank/percentile shape) and `strategy/regime.py` — MIT.

**Alternatives.** Hand-loosening the existing absolute thresholds — rejected
as curve-fitting by another name, and it keeps the all-or-nothing gate shape
that produced 1-in-230.

**Risks.** A scoring model can hide a single disqualifying fact inside an
averaged number; mitigated by keeping the veto list hard.

**Reversal.** Score weights and veto lists live in config; setting a weight
to ∞-like gate behaviour restores the old semantics per condition.

## Decision 3 — Backtest honesty: degenerate-metrics guard, funnel diagnostics, significance gate (gaps 2, 6)

**Decision.** Three changes. (a) Metrics computed over an insufficient sample
(fewer than a floor of trades/return-days, or ~zero return variance) return
an explicit unavailable value with a machine-readable reason — never a number
like Sharpe = -9.2e16. (b) Every scan/backtest reports a rejection funnel:
per-stage counts of why candidates fell out, so "0 trades" is always
attributable and distinguishable from a silently broken pipeline. A
regression test asserts a known-trigger fixture produces >0 trades. (c) A
minimum-trade-count significance gate lives inside the metrics calculator
(count floor AND statistical test), not as caller-optional decoration — the
same principle as our compute_metrics warnings fix.

**Sources.** ROT `backtest/metrics.py:48-95` (None on <5 days or zero stdev;
MIT, adoptable). lumibot `create_tearsheet` degenerate short-circuit with
reason codes (pattern only). trading-bot `backtest_order_gateway.py` bug
class ("0 trades indistinguishable from broken data") and
`performance_metrics.py:459-545` significance gate (patterns only).

**Also adopted as process.** trading-bot's pre-registered-prediction habit:
before running a headline backtest, write the expected outcome down
(`docs/` note), then publish the result against it. Their bias-controlled
test underperforming SPY — documented, not buried — is the model.

**Risks.** Stricter gates make results pages emptier. That is the point.

**Reversal.** Floors are config values; the funnel report is additive.

## Decision 4 — Broker boundary: reimplement the lumibot-shaped ABC, keep the kill switch ours (gap 3)

**Decision.** Define a minimal broker ABC of our own with roughly the
lumibot surface: submit/cancel/modify order, balances, pull position(s), pull
order(s), parse broker order — plus a closed OrderStatus enum and an
`is_backtesting`-style flag consulted only at the executor seam, never inside
strategies. Our existing paper broker becomes the first implementation; a
future Alpaca adapter is the second. The kill switch and risk halts remain in
ClaudeTrade's risk layer, outside the broker interface: **no reference repo
was found to contain a real, enforced kill switch** — lumibot's is user-stub
hooks plus flatten/cancel primitives; ROT and trading-bot have none wired.

**Sources.** lumibot `brokers/broker.py:893-1038` abstract surface and
`example_broker.py` (shape only, GPL/MIT conflict); trading-bot
`broker_interface.py` dataclass shapes and the risk-outside-interface
separation (pattern only).

**Alternatives.** Depending on lumibot directly — rejected (license,
monolith). Skipping the boundary until live trading is requested — rejected:
retrofitting an interface under a working paper broker is costlier than
extracting it now.

**Risks.** Speculative generality. Mitigated by keeping the ABC to the
methods our paper broker already needs.

**Reversal.** The ABC is internal; deleting it collapses back to the
concrete paper broker.

## Decision 5 — Sentiment additions, not replacements (gap 5)

**Decision.** Keep our pipeline; add, in priority order: (a) report buzz
(volume/attention) and polarity as separate factors rather than only blended
scores — surfaced separately in scanner UI and scoring; (b) a cross-source
corroboration flag when a symbol's attention is confirmed by more than one
platform; (c) design (not yet build) a per-community and per-author
historical-accuracy loop atop our existing author hashes — the missing piece
everywhere, ROT included, is closing the loop from trade outcome back to
source credibility, which our immutable ledger makes feasible later.

**Sources.** adanos models (buzz/sentiment split, `is_validated`; MIT).
ROT `aggregator/crowd_signals.py` and `credibility/user_reputation.py`
(designs; MIT — but note ROT never wired them either).

**Risks.** Accuracy-loop features can overfit to few outcomes; deferred
until enough closed paper trades exist to test it honestly.

## Decision 6 — Windows packaging is ours alone (gap 4)

**Decision.** No reference repository has any Windows packaging, PyInstaller
spec, or Task Scheduler integration (all deploy to Docker/Railway/PyPI).
We proceed with our own plan: PyInstaller build + launcher scripts +
documented install path.

## License register

| Repo | LICENSE file | Claimed | Posture |
|---|---|---|---|
| gr8monk3ys/trading-bot | GPL-3.0 | MIT (pyproject/README) | Ideas only — conflict unresolved |
| Lumiwealth/lumibot | GPL-3.0 | MIT (badge/setup.py/README) | Ideas only — conflict unresolved |
| Mattbusel/ROT | MIT | MIT | Code reuse permitted with attribution |
| gsaini/financial-research-analyst-agent | MIT | MIT | Code reuse permitted with attribution |
| adanos-software/openbb-adanos | MIT | MIT | Wrapper reusable; hosted API unsuitable (90-day cap) |

Any code adapted from the MIT repositories must carry an attribution comment
naming the source file. Nothing may be copied from the two GPL/MIT-conflicted
repositories; only independently reimplemented ideas.
