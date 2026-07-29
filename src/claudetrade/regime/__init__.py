"""Market regime classification for adaptive position sizing and threshold adjustment.

Exports:
  - RegimeClassifier: Main classifier, produces RegimeState from market data
"""

from __future__ import annotations

from claudetrade.regime.market_regime import RegimeClassifier

__all__ = [
    "RegimeClassifier",
]
