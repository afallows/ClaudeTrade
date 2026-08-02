"""The two social axes in ``signals.scoring``: polarity and attention.

Both defects covered here had the same shape -- evidence being counted in a
place it did not belong -- but in opposite directions:

* **QA #8.** ``score_candidate`` handed the combined ``"all"`` snapshot to
  BOTH the ``reddit_sentiment`` and the ``x_sentiment`` slot, because that was
  the only snapshot a context carried. One sample filled two independently
  weighted slots and the score read as two sources agreeing when only one had
  ever been consulted. Each slot now scores from its own stored row, and a
  source that did not report earns no weight instead of a placeholder 50.
* **QA #5.** ApeWisdom's aggregate mention counts were kept out of *every*
  axis. Out of polarity that is correct and load-bearing -- the source has no
  direction to report. Out of ATTENTION it discarded the widest mention-volume
  corpus in the system. They now feed the attention axis and nothing else,
  after being ranked against their own history so their ~100x count scale
  never reaches the blend.

The tests are written against ``score_candidate`` rather than the private
helpers, because what matters is the WEIGHTED OUTCOME: two slots showing the
same number is only a bug if both were paid for.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from claudetrade.config import AppConfig
from claudetrade.data.context import ContextBuilder, SymbolData
from claudetrade.domain import (
    Bar,
    Direction,
    MarketRegime,
    RegimeState,
    SecurityInfo,
    SymbolSentiment,
)
from claudetrade.signals.scoring import score_candidate
from claudetrade.strategies.base import StrategyContext, StrategyProposal

SESSION = dt.date(2024, 3, 15)
SYMBOL = "TEST"


@pytest.fixture
def cfg(tmp_app_config: AppConfig) -> AppConfig:
    return tmp_app_config


def _bars(n: int = 220, close: float = 100.0) -> list[Bar]:
    bars: list[Bar] = []
    day = SESSION
    for _ in range(n):
        bars.append(
            Bar(
                symbol=SYMBOL,
                session=day,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000_000,
                adj_close=close,
            )
        )
        day -= dt.timedelta(days=1)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
    bars.reverse()
    return bars


def _sentiment(
    source: str,
    *,
    polarity: float,
    session: dt.date = SESSION,
    post_count: int = 40,
    unique_authors: int = 15,
    confidence: float = 0.7,
    #: Chosen so every NON-polarity component this snapshot feeds lands on
    #: exactly the value it would take with no snapshot at all: catalyst 0.5
    #: -> 50, mention growth 0.35 -> 50 on the -0.3..1.0 attention scale,
    #: manipulation risk 0.2 -> 80, the no-sentiment default. Without that,
    #: comparing "some sentiment" against "no sentiment" would silently be
    #: comparing four axes at once, and a polarity assertion could pass or
    #: fail on the catalyst component's default.
    catalyst_quality: float = 0.5,
    mention_acceleration: float = 0.35,
    manipulation_risk: float = 0.2,
    **overrides,
) -> SymbolSentiment:
    """A polarity-bearing snapshot whose four polarity measures all agree.

    ``_sentiment_score`` blends raw/engagement/credibility/unique-author
    means, so setting all four to the same value makes the resulting 0-100
    component a clean function of one number.
    """
    return SymbolSentiment(
        symbol=SYMBOL,
        session=session,
        source=source,
        post_count=post_count,
        unique_authors=unique_authors,
        raw_sentiment=polarity,
        engagement_weighted=polarity,
        credibility_weighted=polarity,
        unique_author_sentiment=polarity,
        catalyst_quality=catalyst_quality,
        mention_acceleration=mention_acceleration,
        manipulation_risk=manipulation_risk,
        confidence=confidence,
        **overrides,
    )


def _attention(
    source: str, *, mention_acceleration: float, session: dt.date = SESSION, mentions: int = 500
) -> SymbolSentiment:
    """An ApeWisdom-shaped row: mention volume, and nothing else.

    Mirrors exactly what ``data.ingest.ingest_attention`` writes -- no
    polarity, no authors, no confidence, plus the ``attention_only`` label.
    """
    return SymbolSentiment(
        symbol=SYMBOL,
        session=session,
        source=source,
        post_count=mentions,
        unique_authors=0,
        mention_acceleration=mention_acceleration,
        labels={"attention_only": 1.0},
    )


def _ctx(
    cfg: AppConfig,
    *,
    combined: SymbolSentiment | None = None,
    by_source: dict[str, SymbolSentiment] | None = None,
    attention: dict[str, SymbolSentiment] | None = None,
    attention_history: dict[str, list[SymbolSentiment]] | None = None,
) -> StrategyContext:
    sources = dict(by_source or {})
    if combined is not None:
        sources.setdefault("all", combined)
    return StrategyContext(
        session=SESSION,
        symbol=SYMBOL,
        bars=_bars(),
        features={
            "atr_14": 2.0,
            "avg_dollar_volume_20": 50_000_000.0,
            "rel_volume_20": 1.5,
            "roc_10": 2.0,
            "roc_20": 3.0,
            "rs_percentile": 60.0,
        },
        security=SecurityInfo(symbol=SYMBOL, exchange="NASDAQ", market_cap_usd=5e9),
        regime=RegimeState(session=SESSION, regime=MarketRegime.BULL_QUIET),
        sentiment=combined,
        sentiment_by_source=sources,
        attention_by_source=dict(attention or {}),
        attention_history={k: list(v) for k, v in (attention_history or {}).items()},
        config=cfg,
    )


def _proposal() -> StrategyProposal:
    return StrategyProposal(
        strategy="sentiment_breakout",
        strategy_version="v3",
        direction=Direction.LONG,
        entry_low=99.0,
        entry_high=101.0,
        stop_loss=95.0,
        targets=[110.0],
        setup_score=60.0,
    )


def _score(cfg: AppConfig, ctx: StrategyContext):
    return score_candidate(
        ctx=ctx,
        proposal=_proposal(),
        config=cfg,
        security=ctx.security,
        regime=ctx.regime,
        requires_sentiment=False,
    )


# --------------------------------------------------------------------------
# QA #8 -- one piece of evidence, one slot
# --------------------------------------------------------------------------


class TestPerSourcePolarity:
    def test_reddit_only_scores_reddit_and_leaves_x_unweighted(self, cfg: AppConfig):
        """The bug in its simplest form: with only a Reddit sample, X's slot
        must show no reading AND collect no weight. Previously the combined
        row filled both, so Reddit's opinion was paid for twice."""
        ctx = _ctx(cfg, by_source={"reddit": _sentiment("reddit", polarity=0.5)})
        result = _score(cfg, ctx)

        assert result.components.reddit_sentiment > 50.0
        assert result.components.x_sentiment == 50.0
        assert any("scored from reddit alone" in n for n in result.notes)

    def test_x_only_scores_x_and_leaves_reddit_unweighted(self, cfg: AppConfig):
        ctx = _ctx(cfg, by_source={"x": _sentiment("x", polarity=0.5)})
        result = _score(cfg, ctx)

        assert result.components.x_sentiment > 50.0
        assert result.components.reddit_sentiment == 50.0
        assert any("scored from x alone" in n for n in result.notes)

    def test_both_sources_agreeing_beats_one_source_alone(self, cfg: AppConfig):
        """Two independent samples saying the same thing IS stronger evidence
        than one -- and this is the comparison the old code faked by copying
        one row into both slots."""
        one = _score(cfg, _ctx(cfg, by_source={"reddit": _sentiment("reddit", polarity=0.5)}))
        both = _score(
            cfg,
            _ctx(
                cfg,
                by_source={
                    "reddit": _sentiment("reddit", polarity=0.5),
                    "x": _sentiment("x", polarity=0.5),
                },
            ),
        )
        assert both.overall > one.overall

    def test_both_sources_disagreeing_nets_out_instead_of_confirming(self, cfg: AppConfig):
        """Reddit bullish, X bearish: a contested signal, which the old
        single-row copy could not represent at all."""
        result = _score(
            cfg,
            _ctx(
                cfg,
                by_source={
                    "reddit": _sentiment("reddit", polarity=0.6),
                    "x": _sentiment("x", polarity=-0.6),
                },
            ),
        )
        assert result.components.reddit_sentiment > 50.0
        assert result.components.x_sentiment < 50.0
        # Two-source evidence is complete, so there is nothing to caveat:
        # the notes reach ``Signal.data_warnings`` and must not fill up with
        # informational chatter.
        assert not any("alone" in n for n in result.notes)
        assert not any("weighted as a single source" in n for n in result.notes)

    def test_combined_only_is_weighted_as_one_source_not_two(self, cfg: AppConfig):
        """The fallback. With no per-source breakdown stored, the combined row
        is real evidence and must not be discarded -- but it is ONE
        unattributed sample, so it earns half the axis's two-source budget.
        Scored against a synthetic two-source context that reports the same
        polarity, it must move the overall score strictly less.
        """
        polarity = 0.6
        combined_only = _score(cfg, _ctx(cfg, combined=_sentiment("all", polarity=polarity)))
        two_sources = _score(
            cfg,
            _ctx(
                cfg,
                combined=_sentiment("all", polarity=polarity),
                by_source={
                    "reddit": _sentiment("reddit", polarity=polarity),
                    "x": _sentiment("x", polarity=polarity),
                },
            ),
        )

        # Both display slots show the combined reading (it genuinely pools
        # whatever platforms contributed) ...
        assert combined_only.components.reddit_sentiment == pytest.approx(
            combined_only.components.x_sentiment
        )
        assert combined_only.components.reddit_sentiment > 50.0
        # ... but it is paid for once, at half weight, so it cannot reach the
        # score of two genuinely independent sources agreeing.
        assert combined_only.overall < two_sources.overall
        assert any("weighted as a single source" in n for n in combined_only.notes)

    def test_combined_only_still_beats_no_sentiment_at_all(self, cfg: AppConfig):
        """The fallback must not collapse to "discard the evidence": zeroing
        both slots when no per-source row exists would throw away the only
        polarity reading the system has."""
        nothing = _score(cfg, _ctx(cfg))
        combined = _score(cfg, _ctx(cfg, combined=_sentiment("all", polarity=0.6)))
        assert combined.overall > nothing.overall

    def test_a_missing_source_is_renormalised_away_not_scored_neutral(self, cfg: AppConfig):
        """A placeholder 50 is an opinion ("the crowd is undecided") that
        nobody expressed, and weighting it drags a strongly-evidenced
        candidate toward the middle. With the weight dropped instead, adding a
        second BEARISH source is the only thing that can pull the score down.
        """
        reddit_only = _score(
            cfg, _ctx(cfg, by_source={"reddit": _sentiment("reddit", polarity=0.8)})
        )
        with_bearish_x = _score(
            cfg,
            _ctx(
                cfg,
                by_source={
                    "reddit": _sentiment("reddit", polarity=0.8),
                    "x": _sentiment("x", polarity=-0.8),
                },
            ),
        )
        assert with_bearish_x.overall < reddit_only.overall

    def test_per_source_rows_never_substitute_for_one_another(self, cfg: AppConfig):
        """A source with a stored row and a source without must not produce
        the same component value when their polarities differ."""
        result = _score(
            cfg,
            _ctx(
                cfg,
                combined=_sentiment("all", polarity=-0.7),
                by_source={"reddit": _sentiment("reddit", polarity=0.7)},
            ),
        )
        # Reddit scores from Reddit's own row (bullish), and X -- which has no
        # row -- does NOT quietly inherit the combined row's bearish reading.
        assert result.components.reddit_sentiment > 50.0
        assert result.components.x_sentiment == 50.0


# --------------------------------------------------------------------------
# QA #5 -- attention data on the attention axis, and nowhere else
# --------------------------------------------------------------------------


def _attention_series(source: str, values: list[float]) -> list[SymbolSentiment]:
    """Ascending trailing history for one attention source, ending at SESSION."""
    out: list[SymbolSentiment] = []
    day = SESSION - dt.timedelta(days=len(values))
    for value in values:
        day += dt.timedelta(days=1)
        out.append(_attention(source, mention_acceleration=value, session=day))
    out[-1] = _attention(source, mention_acceleration=values[-1], session=SESSION)
    return out


class TestAttentionAxis:
    def test_aggregator_attention_moves_the_attention_component(self, cfg: AppConfig):
        """The change QA #5 asked for: the widest mention-volume corpus in the
        system stops being ignored."""
        quiet = _attention_series("apewisdom:all-stocks", [0.01] * 29 + [0.01])
        loud = _attention_series("apewisdom:all-stocks", [0.01] * 29 + [0.90])

        low = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:all-stocks": quiet[-1]},
                attention_history={"apewisdom:all-stocks": quiet},
            ),
        )
        high = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:all-stocks": loud[-1]},
                attention_history={"apewisdom:all-stocks": loud},
            ),
        )
        assert high.components.attention_acceleration > low.components.attention_acceleration

    def test_it_is_ranked_against_its_own_history_not_mixed_in_raw(self, cfg: AppConfig):
        """The scale guard. ApeWisdom counts run ~100x the local ones, so the
        reading that reaches the blend is a percentile within THIS source's
        own trailing series -- unitless, so nothing about corpus size
        survives. The same absolute value therefore scores high against a
        quiet history and low against a busy one.
        """
        value = 0.5
        against_quiet = _attention_series("apewisdom:4chan", [0.0] * 29 + [value])
        against_busy = _attention_series("apewisdom:4chan", [2.0] * 29 + [value])

        quiet = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:4chan": against_quiet[-1]},
                attention_history={"apewisdom:4chan": against_quiet},
            ),
        )
        busy = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:4chan": against_busy[-1]},
                attention_history={"apewisdom:4chan": against_busy},
            ),
        )
        assert quiet.components.attention_acceleration > busy.components.attention_acceleration

    def test_a_source_without_enough_history_is_skipped_entirely(self, cfg: AppConfig):
        """No distribution to rank against means no usable reading. Using the
        raw ratio instead would mix a wide-corpus scale into a narrow-corpus
        one -- the exact swamping the normalisation exists to prevent."""
        short = _attention_series("apewisdom:all-stocks", [0.0, 0.0, 5.0])
        assert len(short) < cfg.signals.attention_min_history_sessions

        result = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:all-stocks": short[-1]},
                attention_history={"apewisdom:all-stocks": short},
            ),
        )
        assert result.components.attention_acceleration == 50.0
        assert any("No usable attention sample" in n for n in result.notes)

    def test_attention_never_touches_any_polarity_component(self, cfg: AppConfig):
        """The single most important design rule in the sentiment subsystem.
        An attention row carries no direction at all, so however loud it is it
        must leave every polarity reading exactly where "no sample" leaves
        it."""
        loud = _attention_series("apewisdom:all-stocks", [0.0] * 29 + [9.0])

        nothing = _score(cfg, _ctx(cfg))
        with_attention = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:all-stocks": loud[-1]},
                attention_history={"apewisdom:all-stocks": loud},
            ),
        )

        assert with_attention.components.reddit_sentiment == nothing.components.reddit_sentiment
        assert with_attention.components.x_sentiment == nothing.components.x_sentiment
        assert (
            with_attention.components.sentiment_acceleration
            == nothing.components.sentiment_acceleration
        )
        assert with_attention.components.catalyst_quality == nothing.components.catalyst_quality
        # ... and it did move the axis it IS evidence about.
        assert (
            with_attention.components.attention_acceleration
            != nothing.components.attention_acceleration
        )

    def test_attention_never_touches_manipulation_or_data_confidence(self, cfg: AppConfig):
        """It has no post text, no authors and no timestamps, so it cannot
        inform duplicate/bot/coordination detection or sample adequacy. A row
        reporting 500 "mentions" must not read as 500 posts' worth of
        evidence."""
        loud = _attention_series("apewisdom:all-stocks", [0.0] * 29 + [9.0])

        nothing = _score(cfg, _ctx(cfg))
        with_attention = _score(
            cfg,
            _ctx(
                cfg,
                attention={"apewisdom:all-stocks": loud[-1]},
                attention_history={"apewisdom:all-stocks": loud},
            ),
        )
        assert with_attention.components.manipulation_risk == nothing.components.manipulation_risk
        assert with_attention.components.data_confidence == nothing.components.data_confidence
        assert with_attention.confidence == nothing.confidence

    def test_an_attention_row_mis_keyed_as_polarity_still_cannot_score(self, cfg: AppConfig):
        """Belt and braces: the separation must survive a caller putting an
        attention row in the polarity dict, because the row's own labels say
        what it is."""
        row = _attention("apewisdom:all-stocks", mention_acceleration=9.0)
        result = _score(cfg, _ctx(cfg, by_source={"reddit": row}))

        assert result.components.reddit_sentiment == 50.0
        assert "No usable sentiment sample" in " ".join(result.notes)

    def test_local_and_aggregator_readings_are_blended(self, cfg: AppConfig):
        """Both inputs contribute: the local post rate is narrow but ours, the
        aggregator's is wide but borrowed."""
        local = _sentiment("all", polarity=0.0, mention_acceleration=0.9)
        loud = _attention_series("apewisdom:all-stocks", [0.0] * 29 + [9.0])
        quiet = _attention_series("apewisdom:all-stocks", [0.0] * 30)

        local_only = _score(cfg, _ctx(cfg, combined=local))
        with_loud = _score(
            cfg,
            _ctx(
                cfg,
                combined=local,
                attention={"apewisdom:all-stocks": loud[-1]},
                attention_history={"apewisdom:all-stocks": loud},
            ),
        )
        with_quiet = _score(
            cfg,
            _ctx(
                cfg,
                combined=local,
                attention={"apewisdom:all-stocks": quiet[-1]},
                attention_history={"apewisdom:all-stocks": quiet},
            ),
        )
        assert (
            with_quiet.components.attention_acceleration
            < local_only.components.attention_acceleration
            <= with_loud.components.attention_acceleration
        )

    def test_the_aggregator_share_is_configurable_and_can_be_switched_off(
        self, cfg: AppConfig
    ):
        """Reversibility: setting the share to zero restores the local-only
        attention reading exactly."""
        local = _sentiment("all", polarity=0.0, mention_acceleration=0.9)
        loud = _attention_series("apewisdom:all-stocks", [0.0] * 29 + [9.0])
        ctx = _ctx(
            cfg,
            combined=local,
            attention={"apewisdom:all-stocks": loud[-1]},
            attention_history={"apewisdom:all-stocks": loud},
        )

        cfg.signals.attention_aggregator_weight = 0.0
        off = _score(cfg, ctx)
        local_only = _score(cfg, _ctx(cfg, combined=local))
        assert off.components.attention_acceleration == pytest.approx(
            local_only.components.attention_acceleration
        )


# --------------------------------------------------------------------------
# The context side: where each stored row is routed
# --------------------------------------------------------------------------


class TestContextBuilderPartitionsSources:
    """``symbol_sentiment_daily`` holds three kinds of row under one schema --
    the combined aggregate, per-source polarity breakdowns, and aggregator
    attention tallies. ``ContextBuilder`` is where they are told apart; every
    guarantee the scoring tests above rest on is only as good as this split.
    """

    def _data(self, rows: list[SymbolSentiment]) -> SymbolData:
        bars = _bars(60)
        return SymbolData(
            symbol=SYMBOL,
            security=SecurityInfo(symbol=SYMBOL),
            bars=bars,
            sentiment=rows,
        )

    def test_each_polarity_source_travels_separately(self, cfg: AppConfig):
        builder = ContextBuilder(cfg)
        data = self._data(
            [
                _sentiment("all", polarity=0.1),
                _sentiment("reddit", polarity=0.5),
                _sentiment("x", polarity=-0.5),
            ]
        )
        by_source = builder._polarity_by_source(data, SESSION)

        assert set(by_source) == {"all", "reddit", "x"}
        assert by_source["reddit"].raw_sentiment == pytest.approx(0.5)
        assert by_source["x"].raw_sentiment == pytest.approx(-0.5)
        # And the combined view is exactly the "all" row, not "whichever row
        # the query returned first".
        assert builder._sentiment_for(data, SESSION) is by_source["all"]

    def test_attention_rows_are_kept_out_of_the_polarity_dict(self, cfg: AppConfig):
        builder = ContextBuilder(cfg)
        data = self._data(
            [
                _sentiment("all", polarity=0.4),
                _attention("apewisdom:all-stocks", mention_acceleration=3.0),
            ]
        )

        assert set(builder._polarity_by_source(data, SESSION)) == {"all"}
        assert set(builder._attention_by_source(data, SESSION)) == {"apewisdom:all-stocks"}

    def test_each_polarity_source_is_sample_gated_independently(self, cfg: AppConfig):
        """A per-source row is a subset of the combined one, so a source that
        reported only a handful of posts drops out while the combined
        aggregate it fed remains usable. Its evidence is not lost -- it is
        already inside the combined row -- it simply cannot claim a slot of
        its own on a sample too thin to trust."""
        builder = ContextBuilder(cfg)
        data = self._data(
            [
                _sentiment("all", polarity=0.4),
                _sentiment("reddit", polarity=0.9, post_count=1, unique_authors=1),
            ]
        )
        assert set(builder._polarity_by_source(data, SESSION)) == {"all"}

    def test_attention_rows_are_not_gated_on_authors_they_cannot_have(
        self, cfg: AppConfig
    ):
        """The post/author minimums measure whether a POLARITY sample is big
        enough to trust. An attention tally reports no authors at all, so
        applying that gate would reject every row for a reason that does not
        apply to it."""
        builder = ContextBuilder(cfg)
        data = self._data([_attention("apewisdom:4chan", mention_acceleration=1.0, mentions=3)])
        assert set(builder._attention_by_source(data, SESSION)) == {"apewisdom:4chan"}

    def test_sentiment_history_carries_only_the_combined_series(self, cfg: AppConfig):
        """Strategies percentile-rank today's combined snapshot against this
        list. It used to carry every row for the symbol -- per-source
        breakdowns and ApeWisdom attention rows interleaved -- so a value was
        ranked against a distribution built from other sources, mostly a
        different corpus at a different scale."""
        builder = ContextBuilder(cfg)
        earlier = SESSION - dt.timedelta(days=7)
        rows = [
            _sentiment("all", polarity=0.2, session=earlier),
            _sentiment("reddit", polarity=0.9, session=earlier),
            _attention("apewisdom:all-stocks", mention_acceleration=5.0, session=earlier),
            _sentiment("all", polarity=0.3),
        ]
        data = self._data(rows)
        data.features = pd.DataFrame(
            {"atr_14": [2.0] * len(data.bars)},
            index=[b.session for b in data.bars],
        )

        ctx = builder.build(
            data,
            SESSION,
            regime=RegimeState(session=SESSION, regime=MarketRegime.BULL_QUIET),
        )
        assert ctx is not None
        assert {s.source for s in ctx.sentiment_history} == {"all"}
        assert set(ctx.attention_history) == {"apewisdom:all-stocks"}
        assert [s.session for s in ctx.attention_history["apewisdom:all-stocks"]] == [earlier]
