"""Deterministic hashing used for reproducibility, dedup and pseudonymisation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_SALT_LEN = 16


def stable_json(payload: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace, ASCII-safe."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(payload: Any) -> str:
    """SHA-256 of the canonical JSON encoding of ``payload``."""
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    """Hash of normalised text -- used for near-duplicate / copy-paste detection.

    Normalisation collapses whitespace and case so that reposts with cosmetic
    edits still collide.
    """
    normalised = " ".join(text.lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def pseudonymise(value: str, *, salt: str) -> str:
    """One-way, salted identifier for authors.

    Social-media usernames are personal data and are never persisted or sent to
    a third-party AI provider in the clear.  We keep only a salted digest, which
    is enough to count unique authors and detect repeat posters while making
    re-identification impractical from the database alone.
    """
    digest = hashlib.sha256(f"{salt}:{value.strip().lower()}".encode())
    return digest.hexdigest()[:32]


def short_hash(payload: Any, length: int = 12) -> str:
    """Truncated content hash for human-facing identifiers."""
    return content_hash(payload)[:length]
