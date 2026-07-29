"""Data snapshots for reproducibility.

Acceptance criterion 12 of this project is that an operator can *reproduce a
historical signal from its stored configuration and data snapshot*. That needs
four things pinned at generation time:

1. ``code_version``  -- which code ran (``version.CODE_VERSION``).
2. ``config_hash``   -- the effective settings (``AppConfig.config_hash``).
3. ``strategy_version`` -- the rule set (stamped by each ``Strategy``).
4. ``data_snapshot_hash`` -- **which inputs were visible**, computed here.

The snapshot stores a *manifest*, not a copy of the data: per-symbol bar counts,
the last session and a digest of the bar values, plus the social and earnings
counts and the provider identities. Combined with the append-only bar tables
that is enough to detect whether the inputs have since changed, and to rebuild
them if they have not.

Storing a full copy of every scan's inputs was rejected (see ADR-0007): the
storage cost is large, and it does not actually add reproducibility over a
manifest plus immutable source tables.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from claudetrade.db.models import DataSnapshotRow
from claudetrade.db.session import Database
from claudetrade.domain import Bar, EarningsEvent, SocialPost
from claudetrade.logging_setup import get_logger
from claudetrade.utils.hashing import content_hash
from claudetrade.utils.timeutils import utc_now

log = get_logger(__name__)

#: Bump when the manifest layout changes, so old hashes are not compared to new.
MANIFEST_VERSION = "v1"


@dataclass(slots=True)
class SnapshotManifest:
    """Description of the inputs visible to one scan or backtest."""

    session: dt.date
    manifest_version: str = MANIFEST_VERSION
    created_at: dt.datetime = field(default_factory=utc_now)
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)
    providers: dict[str, str] = field(default_factory=dict)
    earnings_count: int = 0
    post_count: int = 0
    universe_size: int = 0

    @property
    def bar_count(self) -> int:
        return sum(int(entry.get("bar_count", 0)) for entry in self.symbols.values())

    def digest_payload(self) -> dict[str, Any]:
        """Canonical structure the hash is computed over."""
        return {
            "manifest_version": self.manifest_version,
            "session": self.session.isoformat(),
            "symbols": {sym: entry for sym, entry in sorted(self.symbols.items())},
            "providers": dict(sorted(self.providers.items())),
            "earnings_count": self.earnings_count,
            "post_count": self.post_count,
            "universe_size": self.universe_size,
        }

    @property
    def snapshot_hash(self) -> str:
        return content_hash(self.digest_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.digest_payload(), "created_at": self.created_at.isoformat()}


def _bar_digest(bars: list[Bar]) -> str:
    """Digest of a symbol's bar values.

    Rounded to six decimals so that float formatting differences between
    providers do not present as data changes, but any real revision to a
    historical price does.
    """
    return content_hash(
        [
            [
                b.session.isoformat(),
                round(b.open, 6),
                round(b.high, 6),
                round(b.low, 6),
                round(b.close, 6),
                round(b.volume, 4),
            ]
            for b in bars
        ]
    )


def build_snapshot(
    *,
    session: dt.date,
    bars_by_symbol: dict[str, list[Bar]],
    earnings: dict[str, list[EarningsEvent]] | None = None,
    posts: list[SocialPost] | None = None,
    providers: dict[str, str] | None = None,
    universe_size: int | None = None,
) -> SnapshotManifest:
    """Construct the manifest for a scan's inputs.

    Only bars dated at or before ``session`` contribute. A bar dated after the
    decision session must not exist in the input at all, and excluding it here
    means the hash cannot be silently changed by later data arriving.
    """
    manifest = SnapshotManifest(session=session, providers=dict(providers or {}))
    for symbol, bars in sorted(bars_by_symbol.items()):
        visible = [b for b in bars if b.session <= session]
        if not visible:
            continue
        manifest.symbols[symbol] = {
            "bar_count": len(visible),
            "first_session": visible[0].session.isoformat(),
            "last_session": visible[-1].session.isoformat(),
            "digest": _bar_digest(visible),
        }
    if earnings:
        manifest.earnings_count = sum(len(v) for v in earnings.values())
    if posts:
        manifest.post_count = len(posts)
    manifest.universe_size = (
        universe_size if universe_size is not None else len(manifest.symbols)
    )
    return manifest


def persist_snapshot(db: Database, manifest: SnapshotManifest) -> str:
    """Store the manifest and return its hash.

    Idempotent: re-storing an identical manifest is a no-op, which is what makes
    "same inputs produce the same snapshot hash" a usable equality test.
    """
    digest = manifest.snapshot_hash
    with db.session() as session:
        if session.get(DataSnapshotRow, digest) is not None:
            return digest
        session.add(
            DataSnapshotRow(
                snapshot_hash=digest,
                created_at=manifest.created_at,
                session=manifest.session,
                manifest=manifest.to_dict(),
                symbol_count=len(manifest.symbols),
                bar_count=manifest.bar_count,
                post_count=manifest.post_count,
                providers=manifest.providers,
            )
        )
    log.info("stored data snapshot %s for %s", digest[:12], manifest.session)
    return digest


def load_snapshot(db: Database, snapshot_hash: str) -> dict[str, Any] | None:
    """Retrieve a stored manifest, or ``None`` when unknown."""
    with db.read_session() as session:
        row = session.get(DataSnapshotRow, snapshot_hash)
        return dict(row.manifest) if row else None


@dataclass(slots=True)
class ReproductionCheck:
    """Outcome of verifying that a signal's inputs still match its snapshot."""

    snapshot_hash: str
    reproducible: bool
    reason: str = ""
    differences: list[str] = field(default_factory=list)


def verify_reproduction(
    db: Database,
    *,
    snapshot_hash: str,
    current: SnapshotManifest,
) -> ReproductionCheck:
    """Compare freshly-assembled inputs against a stored snapshot.

    A mismatch is not necessarily an error -- providers legitimately restate
    history after a split or a correction -- but it does mean the original
    signal cannot be reproduced bit-for-bit, and the operator is told which
    symbols changed rather than being shown a silent pass.
    """
    stored = load_snapshot(db, snapshot_hash)
    if stored is None:
        return ReproductionCheck(
            snapshot_hash=snapshot_hash,
            reproducible=False,
            reason="snapshot not found in this database",
        )
    if current.snapshot_hash == snapshot_hash:
        return ReproductionCheck(snapshot_hash=snapshot_hash, reproducible=True)

    differences: list[str] = []
    stored_symbols: dict[str, Any] = stored.get("symbols", {})
    for symbol, entry in sorted(current.symbols.items()):
        prior = stored_symbols.get(symbol)
        if prior is None:
            differences.append(f"{symbol}: not present in the original snapshot")
        elif prior.get("digest") != entry.get("digest"):
            differences.append(
                f"{symbol}: price history has been revised "
                f"({prior.get('bar_count')} -> {entry.get('bar_count')} bars)"
            )
    for symbol in sorted(set(stored_symbols) - set(current.symbols)):
        differences.append(f"{symbol}: present originally but missing now")

    if stored.get("post_count") != current.post_count:
        differences.append(
            f"social sample changed ({stored.get('post_count')} -> {current.post_count} posts); "
            "social engagement values are mutable at the source and are not expected to match"
        )

    return ReproductionCheck(
        snapshot_hash=snapshot_hash,
        reproducible=False,
        reason="inputs differ from the stored snapshot",
        differences=differences,
    )
