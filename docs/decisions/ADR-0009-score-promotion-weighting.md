# ADR-0009: Promoting analyst, institutional and cross-source sentiment into the composite score

**Status: PROPOSED** — awaiting owner review. Nothing in this document is
implemented; the three data layers it promotes are collected and surfaced
today but deliberately excluded from ranking (stated in their docstrings).

## Context

Three new evidence layers landed on 2026-08-02/03, each stored per symbol
and session, each currently display-only:

1. **Analyst sentiment** (`analyst_snapshots`, TipRanks): ranked-consensus
   Buy/Hold/Sell counts, price-target consensus (mean/high/low), dated
   per-analyst rating actions with analyst quality, coverage changes,
   earnings surprise. Universe-wide for covered names; zero marginal API
   cost.
2. **Institutional sentiment** (`institutional_snapshots`, TipRanks): a
   staleness-discounted insider/hedge-fund blend already normalised to
   [-1, +1], `None` when no data (never a fabricated neutral).
3. **Cross-source attention** (`adanos_snapshots`, Adanos): per-platform
   (X/Reddit/News/Polymarket) buzz score, real polarity split
   (bullish/bearish pct), trend and 7-day history — but only for names in
   the vendor's top-100 per feed, plus on-demand detail for any supported
   ticker.

The composite (`signals/scoring.py`) is a weighted average of 12
components with **per-candidate effective weights**: components whose
evidence is absent get weight 0 and the rest renormalise — absence is
never scored as neutral-50. `data_confidence` is computed but unweighted
(it feeds confidence, not rank). Current weights sum to exactly 1.00:

| Component | Weight |
|---|---|
| technical_setup | 0.20 |
| price_momentum | 0.12 |
| volume_confirmation | 0.10 |
| reddit_sentiment | 0.08 |
| sentiment_acceleration | 0.08 |
| earnings_risk | 0.08 |
| catalyst_quality | 0.07 |
| liquidity | 0.07 |
| market_regime | 0.06 |
| x_sentiment | 0.05 |
| attention_acceleration | 0.05 |
| manipulation_risk | 0.04 |

## Decision (proposed)

### D1 — Three new components, not overloads of existing ones

New 0–100 `ComponentScores` fields, computed at scan time from the latest
stored snapshots. New fields are picked up automatically by the
research-revision guardrails (`VALID_COMPONENT_NAMES` derives from the
dataclass) and remain subject to the ±`max_component_adjustment` clamp.

- **`analyst_sentiment`** — blend of: consensus tilt
  `(nB − nS) / (nB + nH + nS)` mapped to 0–100 (weight 0.40 of the
  component); price-target upside `(target_mean / close − 1)` clamped to
  ±30% and mapped to 0–100 (0.30); 10-session rating-action momentum —
  star-weighted upgrades + initiations minus downgrades, tanh-squashed
  (0.20); coverage-change kicker, ±analyst-count delta squashed (0.10).
  Evidence-absent when the symbol has no analyst snapshot, when
  `analyst_count == 0`, or when the newest snapshot is older than 5
  sessions (stale-snapshot guard).
- **`institutional_sentiment`** — direct map of the existing
  `institutional_score` s ∈ [-1, +1] to `(s + 1) × 50`. Staleness is
  already inside that score (axis decay), so no second discount here.
  Evidence-absent when the score is `None`.
- **`cross_source_attention`** — from the symbol's latest Adanos
  aggregate: buzz level relative to the symbol's own 7-day
  `trend_history` percentile (0.40), bullish-minus-bearish spread mapped
  to 0–100 (0.40), corroboration bonus scaled by the number of platforms
  agreeing on trend direction (0.20). Evidence-absent when no Adanos rows
  exist for the symbol — which is most small caps, so renormalisation is
  doing real work here; task #22 (universal enrichment) will widen
  coverage first.

### D2 — Weight table (sums to 1.00)

New components get a combined 0.11, funded from the components whose
information they partially overlap — not pro-rata — so correlated
evidence is not double-counted:

| Component | Old | New | Rationale for change |
|---|---|---|---|
| technical_setup | 0.20 | **0.18** | Remains the single largest axis |
| price_momentum | 0.12 | **0.11** | Slight trim |
| volume_confirmation | 0.10 | **0.09** | Slight trim |
| reddit_sentiment | 0.08 | **0.07** | Adanos Reddit feed overlaps |
| sentiment_acceleration | 0.08 | **0.07** | Slight trim |
| earnings_risk | 0.08 | 0.08 | Risk axis — untouched |
| catalyst_quality | 0.07 | **0.05** | Rating actions + earnings surprise now scored explicitly in analyst_sentiment |
| liquidity | 0.07 | 0.07 | Risk axis — untouched |
| market_regime | 0.06 | 0.06 | Untouched |
| x_sentiment | 0.05 | **0.03** | Local X adapter is credential-gated (disabled on this install); Adanos's X feed carries the same population into cross_source_attention |
| attention_acceleration | 0.05 | **0.04** | Adanos buzz overlaps ApeWisdom attention |
| manipulation_risk | 0.04 | 0.04 | Risk axis — untouched |
| **analyst_sentiment** | — | **0.05** | Highest-quality new evidence: attributed, dated, professional |
| **institutional_sentiment** | — | **0.03** | Real capital, but monthly/quarterly latency |
| **cross_source_attention** | — | **0.03** | Broad but vendor-derived and top-100-skewed |

Principles: risk components (earnings_risk, liquidity, manipulation_risk)
are never diluted; technical evidence keeps plurality (0.38 across the
three price/volume components); no single new component exceeds the
weight of the axes it partially replaces.

### D3 — Direction-awareness follows existing precedent

Whatever `reddit_sentiment` does for short-direction proposals today
(the hype-failure short exists), the three new components must do
identically — implementation must locate and mirror that precedent, not
invent a second convention. Flagged as the first thing to verify at
implementation time.

### D4 — Hard gates and the plan are untouched

`apply_hard_gates` vetoes are not extended or relaxed; new components can
re-rank but never veto and never touch entry/stop/targets/size. The
research-revision clamp continues to apply on top of whatever the engine
computes.

### D5 — Shadow mode before live

New config `signals.promoted_scoring: "off" | "shadow" | "live"`,
default **"shadow"** on merge:

- **off** — current behaviour, new components computed but zero-weighted.
- **shadow** — both composites computed; the CURRENT one still ranks;
  the promoted composite, its components and the rank divergence are
  stored in the signal's detail JSON and surfaced in the funnel/UI
  ("promoted rank: #3 (+2)"), so divergence accumulates as observable
  evidence while recommendations stay unchanged.
- **live** — promoted weights rank; flipped only by the owner after
  reviewing shadow divergence and the backtest comparison below.

### D6 — Validation before "live"

- Walk-forward backtest with both weight tables over whatever history
  the snapshot tables then hold. **Honest limitation**: institutional
  and Adanos snapshots only accumulate forward from 2026-08-02, and
  analyst history is limited to the `consensusOverTime` series embedded
  in current payloads — the first meaningful comparison window is weeks
  away, not days. Shadow-mode divergence review is the interim evidence.
- Acceptance: promoted weights must not degrade expectancy or max
  drawdown on the comparison window, and shadow divergence must show the
  new components adding discrimination (not noise) — e.g. divergent
  picks' forward returns at least matching baseline picks'.

### D7 — Reproducibility side-effects, stated up front

Changing `component_weights` changes the config hash, so the first scan
after the flip mints new signal ids for the same session. The read-time
dedup (`signals/dedupe.py`, 2026-08-03) already collapses that
transition; no ledger change needed.

## Alternatives considered

- **Feed new evidence into existing components** (e.g. analyst actions
  into catalyst_quality): rejected — it hides provenance, makes research
  revisions ambiguous, and prevents independent weighting/ablation.
- **ML-fitted weights**: rejected for now — ~0 closed paper trades and
  a forward-only snapshot history is nowhere near enough data; revisit
  after the accuracy-loop groundwork in ADR-0007 Decision 5(c).
- **Equal-weight the new components at 0.05 each (0.15 total)**:
  rejected — institutional latency (45+ day holdings lag) and Adanos
  top-100 skew don't justify parity with attributed analyst actions.

## Consequences

Ranking gains three independent, provenance-tracked evidence axes with
conservative influence (11% combined), absence never penalises a symbol
(renormalisation), and every step from here is reversible: `off` restores
today's behaviour exactly; `shadow` costs one extra weighted-average per
candidate and some detail-JSON bytes.
