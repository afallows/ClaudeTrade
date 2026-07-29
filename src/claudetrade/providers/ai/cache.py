"""Content-addressed cache for AI classification responses.

Reduces API calls and costs by caching responses keyed on task + payload +
model + prompt version. Backed by AICacheRow but with an in-memory front
for performance within a run.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from claudetrade.config import AIConfig
from claudetrade.providers.base import AIResponse
from claudetrade.utils.hashing import content_hash

log = logging.getLogger(__name__)


class AIResponseCache:
    """In-memory cache with optional database backing.

    Each entry is keyed on content_hash(task, prompt_version, model, payload).
    TTL is enforced at read time (expired entries are evicted lazily).
    """

    def __init__(self, config: AIConfig, *, db_session: Any = None):
        """Initialize the cache.

        Args:
            config: AIConfig with cache_ttl_hours.
            db_session: Optional SQLAlchemy session for persistent backing.
        """
        self.config = config
        self.db_session = db_session
        self._memory: dict[str, tuple[AIResponse, dt.datetime]] = {}

    @staticmethod
    def make_key(task: str, prompt_version: str, model: str, payload: dict[str, Any]) -> str:
        """Generate a content-addressed cache key.

        Args:
            task: Task name.
            prompt_version: Prompt version tag.
            model: Model identifier.
            payload: Request payload dict.

        Returns:
            SHA-256 hex digest.
        """
        return content_hash(
            {
                "task": task,
                "prompt_version": prompt_version,
                "model": model,
                "payload": payload,
            }
        )

    def get(self, cache_key: str) -> AIResponse | None:
        """Retrieve a cached response if present and not expired.

        Args:
            cache_key: Cache key from make_key().

        Returns:
            AIResponse if found and fresh, None otherwise.
        """
        # Check in-memory cache first
        if cache_key in self._memory:
            response, expires_at = self._memory[cache_key]
            if dt.datetime.now(tz=dt.UTC) < expires_at:
                return response
            else:
                # Expired; evict
                del self._memory[cache_key]

        # Fall through to database if configured
        if self.db_session is not None:
            try:
                from claudetrade.db.models import AICacheRow

                row = self.db_session.query(AICacheRow).filter_by(cache_key=cache_key).one_or_none()
                if row is not None and (
                    row.expires_at is None or dt.datetime.now(tz=dt.UTC) < row.expires_at
                ):
                    # Deserialize from JSON
                    response = AIResponse(
                        task=row.task,
                        provider=row.provider,
                        model=row.model,
                        prompt_version=row.prompt_version,
                        created_at=row.created_at,
                        data=row.response,
                        parsed_ok=True,
                        cache_hit=True,
                    )
                    # Refresh memory cache
                    expires_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(
                        hours=self.config.cache_ttl_hours
                    )
                    self._memory[cache_key] = (response, expires_at)
                    row.hits += 1
                    return response
            except Exception as exc:
                log.warning("database cache read failed: %s", exc)

        return None

    def put(self, cache_key: str, response: AIResponse) -> None:
        """Cache a response for future retrieval.

        Args:
            cache_key: Cache key from make_key().
            response: AIResponse to cache.
        """
        if not response.parsed_ok or response.data is None:
            return  # Don't cache failed responses

        expires_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(
            hours=self.config.cache_ttl_hours
        )
        self._memory[cache_key] = (response, expires_at)

        if self.db_session is not None:
            try:
                from claudetrade.db.models import AICacheRow

                row = AICacheRow(
                    cache_key=cache_key,
                    task=response.task,
                    provider=response.provider,
                    model=response.model,
                    prompt_version=response.prompt_version,
                    response=response.data,
                    expires_at=expires_at,
                    hits=0,
                )
                self.db_session.merge(row)
            except Exception as exc:
                log.warning("database cache write failed: %s", exc)

    def purge_expired(self) -> int:
        """Remove all expired entries from both caches.

        Returns:
            Number of entries evicted.
        """
        now = dt.datetime.now(tz=dt.UTC)
        count = 0

        # Memory cache
        expired_keys = [
            k for k, (_, expires_at) in self._memory.items() if expires_at < now
        ]
        for key in expired_keys:
            del self._memory[key]
            count += 1

        # Database cache
        if self.db_session is not None:
            try:
                from claudetrade.db.models import AICacheRow

                deleted = (
                    self.db_session.query(AICacheRow)
                    .filter(AICacheRow.expires_at.isnot(None), AICacheRow.expires_at < now)
                    .delete()
                )
                count += deleted
            except Exception as exc:
                log.warning("database cache purge failed: %s", exc)

        return count

    def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns:
            Dict with memory_entries, memory_bytes, database_entries (if applicable).
        """
        memory_bytes = sum(len(k) + len(str(v[0])) for k, v in self._memory.items())

        stats: dict[str, Any] = {
            "memory_entries": len(self._memory),
            "memory_bytes": memory_bytes,
        }

        if self.db_session is not None:
            try:
                from claudetrade.db.models import AICacheRow

                db_count = self.db_session.query(AICacheRow).count()
                stats["database_entries"] = db_count
            except Exception:
                pass

        return stats
