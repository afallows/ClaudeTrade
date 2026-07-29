"""Data layer: ingestion, universe selection, quality checks and context building.

This is the seam between the provider adapters and the decision engines. It owns
three responsibilities that must not leak elsewhere:

* Persisting provider output into the database in a normalised, append-friendly
  shape.
* Detecting and recording data-quality defects, and refusing to let a
  high-confidence signal rest on stale or incomplete inputs.
* Assembling ``StrategyContext`` objects that contain **only** information
  observable at their decision session.
"""

from claudetrade.data.context import ContextBuilder, DatabaseContextProvider
from claudetrade.data.ingest import IngestReport, DataIngestor
from claudetrade.data.quality import DataQualityChecker, QualityReport
from claudetrade.data.snapshot import SnapshotManifest, build_snapshot
from claudetrade.data.universe import UniverseSelector

__all__ = [
    "ContextBuilder",
    "DataIngestor",
    "DataQualityChecker",
    "DatabaseContextProvider",
    "IngestReport",
    "QualityReport",
    "SnapshotManifest",
    "UniverseSelector",
    "build_snapshot",
]
