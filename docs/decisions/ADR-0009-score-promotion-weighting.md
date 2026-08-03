# ADR-0009: Promoting analyst, institutional and cross-source sentiment into the composite score

**Status: ACCEPTED** (owner, 2026-08-03 — "I like the weightings you've
chosen ... to start"). Implementation follows the phased rollout below:
shadow mode first, live only after the owner reviews shadow divergence.

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

## Implementation notes (2026-08-03, task #24)

Implemented in shadow mode. Three deviations from this ADR's literal
wording were identified during implementation, reviewed, and approved
before landing; a fourth was discovered mid-implementation by running the
existing test suite and is recorded here as well. All four are additive —
none relax D4 (hard gates), and `off`/`shadow` remain byte-identical to
pre-ADR-0009 ranking (proved in `tests/test_score_promotion.py`,
`TestByteIdenticalBaseline`).

**1. Two-table config, not one field replaced.** `SignalConfig` gained a
SECOND, independent field, `promoted_component_weights` (this ADR's D2 "New"
column, sums to 1.00 — enforced by a `field_validator`), alongside the
existing `component_weights` (the "Old" column), which is untouched — not a
single field whose values move under an existing name. This is what makes
`overall` provably immune to `promoted_scoring_mode`: `component_weights` has
no entries for `analyst_sentiment`/`institutional_sentiment`/
`cross_source_attention`, so their effective weight there is zero with no
extra code, in every mode. `GET /api/system/weights` still returns
`component_weights` only; surfacing `promoted_component_weights` there is a
`webapi/`-scope follow-up, deliberately left to the coordinator (see
deviation 4).

**2. Signed/unsigned direction-awareness split, not a flat mirror of
`reddit_sentiment`.** D3 says to mirror whatever `reddit_sentiment` does for
shorts. Implementation instead splits each new component's SUB-BLENDS by
whether the sub-signal is polarity-shaped (bullish vs. bearish — flips sign
for a short, exactly like `_sentiment_score`) or attention-shaped (how much/
how many — never flips, exactly like `_attention_score`'s documented "a
crowd gathering is neither bullish nor bearish"):

- `analyst_sentiment`: consensus tilt, price-target upside and the
  rating-action tilt are SIGNED; the coverage-change kicker (more/fewer
  analysts covering the name) is UNSIGNED.
- `institutional_sentiment`: the whole component is SIGNED (it is a single
  polarity figure, no attention-shaped sub-part).
- `cross_source_attention`: the bullish-minus-bearish spread is SIGNED; the
  buzz-percentile and platform-corroboration sub-components are UNSIGNED.

**3. Rating-tilt substitute for the unconfirmable upgrade/downgrade
count.** D1 asks for "star-weighted upgrades and initiations minus
downgrades". `AnalystRatingAction.action_id`/`action_label` are confirmed
(against committed fixtures) ONLY for `action_id=3` ("upgrade") and
`action_id=5` ("reiterate") — downgrades and initiations have no confirmed
mapping, and ADR-0008 Decision 1 forbids fabricating one. Implemented
instead: each rating action's CONFIRMED `rating_label` ("buy"/"sell"; "hold"
and unmapped labels carry no tilt), star-weighted by `analyst_stars`
(default 1.0 when unrated), summed over the trailing 10 trading sessions and
`tanh`-squashed — "recent rating tilt weighted by analyst quality" rather
than literal upgrade/downgrade counting. See
`signals.scoring._rating_action_tilt`.

**4. `promoted_scoring_mode`, not `promoted_scoring` (discovered, not
planned).** The original implementation plan (HANDOFF.md) named this field
`promoted_scoring`, inferred from `webapi/routers/system.py`'s
`getattr(config.signals, "promoted_scoring", None)`. Running
`tests/test_webapi_system.py` UNEDITED (required by this task's brief)
surfaced a collision that grep alone had missed: `webapi/schemas.py`'s
`SignalWeightsOut.promoted_scoring` is already typed `dict[str, float] |
None` — a placeholder for an eventually-surfaced WEIGHTS table, not a mode
string. Naming the new config field `promoted_scoring` would make
`GET /api/system/weights` return HTTP 500 on every call (pydantic rejects a
`str` where the response model declares `dict[str, float] | None`), not
merely fail one assertion. Since `webapi/` is out of scope to edit, the
field was named `promoted_scoring_mode` instead — `getattr(config.signals,
"promoted_scoring", None)` still resolves to `None`, exactly what that
endpoint's own docstring already promised and what
`test_webapi_system.py` already asserted, so neither needed edits.
Surfacing `promoted_component_weights`/`promoted_scoring_mode` on
`GET /api/system/weights` (and the matching `ComponentScoresOut` schema,
which similarly does not yet list the three new component names) is a
`webapi/`-scope follow-up for the coordinator.

**Extras JSON shape** (`Signal.extras["promoted_scoring"]`, stamped by
`signals.engine.SignalEngine.scan` in "shadow" and "live" modes, absent in
"off"):

```json
{
  "mode": "shadow",
  "promoted_score": 63.5,
  "baseline_score": 58.0,
  "baseline_rank": 4,
  "promoted_rank": 2,
  "rank_divergence_note": "promoted scoring would rank this #2 (+2)"
}
```

Ranks are computed over the FINAL, already-truncated `ScanResult.signals`
list (what the operator actually sees), not the full pre-`max_candidates`
candidate pool.
