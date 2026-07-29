"""Feature engineering for technical analysis and market regime.

Modules:
  - indicators: Causal technical-indicator primitives (SMA, EMA, RSI, MACD, ATR, etc.)
  - patterns: Price-action pattern detection (swings, breakouts, support/resistance)
  - relative_strength: Relative strength and sector ranking
  - feature_builder: High-level orchestration of all feature computations
"""

from __future__ import annotations

from claudetrade.features.feature_builder import (
    FEATURE_VERSION,
    REQUIRED_FEATURES,
    FeatureBuilder,
    build_features,
)
from claudetrade.features.indicators import assert_causal
from claudetrade.features.relative_strength import (
    beta,
    correlation,
    relative_strength,
    relative_strength_score,
    sector_relative_strength,
)

__all__ = [
    "FEATURE_VERSION",
    "REQUIRED_FEATURES",
    "FeatureBuilder",
    "assert_causal",
    "beta",
    "build_features",
    "correlation",
    "relative_strength",
    "relative_strength_score",
    "sector_relative_strength",
]
