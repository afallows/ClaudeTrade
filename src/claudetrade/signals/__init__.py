"""Signal generation, scoring and the immutable signal ledger."""

from claudetrade.signals.engine import NearMiss, ScanFunnel, ScanResult, SignalEngine
from claudetrade.signals.ledger import SignalLedger
from claudetrade.signals.scoring import score_candidate

__all__ = [
    "NearMiss",
    "ScanFunnel",
    "ScanResult",
    "SignalEngine",
    "SignalLedger",
    "score_candidate",
]
