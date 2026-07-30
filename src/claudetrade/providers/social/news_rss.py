"""RSS/Atom news provider: a lawful, credential-free social-sentiment source.

Reads a configurable list of RSS/Atom feed URLs that publishers explicitly
serve for syndication -- exchange/regulator press releases, wire-service
category feeds, public-broadcaster business sections. This is not scraping:
these feeds exist for exactly this purpose, no authentication is bypassed, no
paywall is defeated, and no vendor rate limit or ToS is tested. That is also
what makes this source able to default to *on* with no credentials, unlike
Reddit and X.

Parsing uses only the stdlib (``xml.etree.ElementTree``); no new dependency is
introduced (``feedparser`` is not installed in this project and is not added
here).

Idiom matched to ``providers/social/reddit.py``:

* Raw title+summary is scored for injection risk *before* sanitisation --
  sanitisation rewrites the tell-tale phrase to ``[filtered]``, so scoring the
  sanitised copy would always read ~0.
* Only the sanitised text is ever stored.
* The "author" of a wire story is the publisher, not a person: the author
  hash is a salted digest of the feed's domain, never anything derived from a
  byline (bylines are not parsed out of item text at all).
* Every failure mode -- unreachable feed, malformed XML, a single malformed
  item, an unparseable date -- degrades that one feed or that one item. A bad
  feed never takes down the refresh for the others, and a bad item never
  takes down its feed.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from claudetrade.config import NewsConfig
from claudetrade.domain import SocialPost, SocialSource
from claudetrade.providers.base import (
    NotConfiguredError,
    ProviderStatus,
    RateLimiter,
    RateLimitError,
)
from claudetrade.utils.hashing import pseudonymise, text_hash
from claudetrade.utils.text import injection_risk_score, sanitize_social_text

log = logging.getLogger(__name__)

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass(slots=True)
class _RawItem:
    """One feed entry after format-specific parsing, before sanitisation."""

    title: str
    summary: str
    link: str
    guid: str | None
    created_at: dt.datetime  # timezone-aware UTC


class NewsRssProvider:
    """Publisher-syndicated RSS/Atom adapter for social/news posts.

    Fetches each configured feed URL, parses whichever of RSS 2.0 or Atom it
    turns out to be, and maps entries onto sanitised ``SocialPost`` records
    with ``source=SocialSource.NEWS``. Ticker relevance is deliberately *not*
    resolved here -- headlines rarely carry cashtags, and the existing
    ``sentiment.entity_resolution.TickerResolver`` company-name/alias path is
    what the ingest pipeline already runs over every post's text, news
    included.
    """

    name: str = "news_rss"
    source: SocialSource = SocialSource.NEWS

    def __init__(self, config: NewsConfig):
        """Initialise the provider.

        Args:
            config: ``NewsConfig`` with the feed list, rate limits, etc.

        Raises:
            NotConfiguredError: if no feed URLs are configured. RSS needs no
                credentials, so this is the only way this source disables
                itself -- an empty list is a deliberate "off" switch.
        """
        self.config = config
        if not config.feed_urls:
            raise NotConfiguredError(
                "no RSS/Atom feed URLs configured (news.feed_urls is empty)",
                provider="news_rss",
            )
        self._rate_limiter = RateLimiter(
            config.rate_limit_per_minute,
            name="news_rss",
            max_wait_s=config.request_timeout_s,
        )

    def status(self) -> ProviderStatus:
        """Report provider status."""
        return ProviderStatus(
            name=self.name,
            kind="social",
            available=True,
            configured=True,
            message=f"RSS/Atom news ({len(self.config.feed_urls)} feeds)",
            supports_point_in_time=False,
            rate_limit_per_minute=self.config.rate_limit_per_minute,
            licence_note=(
                "Publisher-syndicated RSS/Atom feeds only; no scraping, no ToS "
                "bypass, no paywalled content. Engagement metrics are "
                "structurally absent (score/comments/reposts/replies are "
                "always 0) -- these are wire stories, not social posts with "
                "votes or replies."
            ),
        )

    def fetch_posts(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime | None = None,
        symbols: list[str] | None = None,  # noqa: ARG002
        limit: int | None = None,
    ) -> list[SocialPost]:
        """Fetch and parse all configured feeds, newest first.

        Args:
            since: Start timestamp; items published before this are dropped.
            until: End timestamp; defaults to now. Items published after this
                are dropped too (feeds have no concept of point-in-time query,
                so this is enforced client-side).
            symbols: Unused -- headlines are matched against the universe by
                the shared entity-resolution path downstream, not here.
            limit: Maximum posts to return, applied after dedup and sorting.

        Returns:
            Sanitised, deduplicated ``SocialPost`` records, newest first.
        """
        if until is None:
            until = dt.datetime.now(tz=dt.UTC)

        collected: list[SocialPost] = []
        seen_external_ids: set[str] = set()

        for feed_url in self.config.feed_urls:
            try:
                self._rate_limiter.acquire()
            except RateLimitError as exc:
                log.warning("rate limit reached before fetching %s: %s", feed_url, exc)
                continue

            xml_text = self._fetch_feed_xml(feed_url)
            if xml_text is None:
                continue

            try:
                items = _parse_feed(xml_text)
            except Exception as exc:
                log.warning("failed to parse feed %s: %s; skipping", feed_url, exc)
                continue

            for item in items:
                if item.created_at < since or item.created_at > until:
                    continue
                try:
                    post = self._to_post(item, feed_url)
                except Exception as exc:
                    log.debug("skipping malformed item from %s: %s", feed_url, exc)
                    continue
                if post.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(post.external_id)
                collected.append(post)

        deduped = _dedupe_by_text_hash(collected)
        deduped.sort(key=lambda p: p.created_at, reverse=True)
        if limit is not None:
            deduped = deduped[:limit]
        return deduped

    def _fetch_feed_xml(self, feed_url: str) -> str | None:
        """GET one feed, returning its body or ``None`` on any failure.

        A single unreachable feed degrades that feed only -- the loop in
        ``fetch_posts`` continues with the rest.
        """
        try:
            # Publisher feed URLs occasionally move.  Follow ordinary HTTP
            # redirects so an existing user configuration keeps working when
            # a publisher replaces a category endpoint (PR Newswire did this
            # in 2026).  httpx deliberately defaults this to False.
            with httpx.Client(
                timeout=self.config.request_timeout_s,
                follow_redirects=True,
            ) as client:
                response = client.get(
                    feed_url,
                    headers={"User-Agent": self.config.user_agent},
                )
                response.raise_for_status()
                return response.text
        except Exception as exc:
            log.warning("failed to fetch feed %s: %s", feed_url, exc)
            return None

    def _to_post(self, item: _RawItem, feed_url: str) -> SocialPost:
        """Map one parsed feed entry onto a sanitised ``SocialPost``."""
        raw_text = f"{item.title}\n{item.summary}"
        sanitised = sanitize_social_text(raw_text)
        # Score the RAW text -- sanitisation has already rewritten any
        # injection phrase to "[filtered]", so scoring the sanitised copy
        # would always read ~0 (same rule as reddit.py).
        injection_risk = injection_risk_score(raw_text)

        domain = urlparse(feed_url).netloc or feed_url
        external_id = item.guid or (f"link:{text_hash(item.link)}" if item.link else None)
        if external_id is None:
            raise ValueError("item has neither guid nor link; cannot form a stable id")

        return SocialPost(
            source=SocialSource.NEWS,
            external_id=external_id,
            created_at=item.created_at,
            text=sanitised,
            community=domain,
            score=0,
            num_comments=0,
            num_reposts=0,
            num_replies=0,
            # The "author" of a wire story is the publisher, not a person --
            # pseudonymise the feed's own domain, never anything parsed out
            # of the item as a byline.
            author_hash=pseudonymise(domain, salt=self.config.author_salt),
            author_age_days=None,
            author_karma=None,
            author_followers=None,
            is_comment=False,
            parent_id=None,
            is_removed=False,
            is_crosspost=False,
            crosspost_parent=None,
            text_hash=text_hash(sanitised),
            duplicate_group=None,
            injection_risk=injection_risk,
            fetched_at=dt.datetime.now(tz=dt.UTC),
            raw_ref=item.link or None,
        )


# --------------------------------------------------------------------------
# Format-agnostic parsing (stdlib only)
# --------------------------------------------------------------------------


def _parse_feed(xml_text: str) -> list[_RawItem]:
    """Parse RSS 2.0 or Atom into a common item shape.

    Malformed XML raises ``ValueError`` immediately below the caught
    ``ET.ParseError`` (that caller-level catch is what makes a whole bad feed
    degrade rather than crash the refresh) -- a single malformed *item* inside
    an otherwise well-formed feed is instead skipped item-by-item.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"malformed feed XML: {exc}") from exc

    tag = root.tag
    if tag == "rss":
        return _parse_rss(root)
    if tag == f"{_ATOM_NS}feed" or tag == "feed":
        return _parse_atom(root)
    raise ValueError(f"unrecognised feed root element: {tag!r}")


def _parse_rss(root: ET.Element) -> list[_RawItem]:
    items: list[_RawItem] = []
    channel = root.find("channel")
    if channel is None:
        return items
    for item_el in channel.findall("item"):
        try:
            title = (item_el.findtext("title") or "").strip()
            summary = (item_el.findtext("description") or "").strip()
            link = (item_el.findtext("link") or "").strip()
            guid_raw = item_el.findtext("guid")
            guid = guid_raw.strip() if guid_raw else None
            pub_date_raw = item_el.findtext("pubDate")
            if not pub_date_raw:
                continue
            created_at = _parse_rfc822(pub_date_raw)
            if created_at is None:
                continue
            if not title and not summary:
                continue
            items.append(
                _RawItem(title=title, summary=summary, link=link, guid=guid, created_at=created_at)
            )
        except Exception as exc:
            log.debug("skipping malformed RSS item: %s", exc)
            continue
    return items


def _parse_atom(root: ET.Element) -> list[_RawItem]:
    items: list[_RawItem] = []
    for entry in root.findall(f"{_ATOM_NS}entry") or root.findall("entry"):
        try:
            title = (entry.findtext(f"{_ATOM_NS}title") or entry.findtext("title") or "").strip()
            summary = (
                entry.findtext(f"{_ATOM_NS}summary")
                or entry.findtext(f"{_ATOM_NS}content")
                or entry.findtext("summary")
                or entry.findtext("content")
                or ""
            ).strip()
            guid_raw = entry.findtext(f"{_ATOM_NS}id") or entry.findtext("id")
            guid = guid_raw.strip() if guid_raw else None

            link_el = entry.find(f"{_ATOM_NS}link")
            if link_el is None:
                link_el = entry.find("link")
            link = (link_el.get("href") if link_el is not None else "") or ""

            date_raw = (
                entry.findtext(f"{_ATOM_NS}updated")
                or entry.findtext(f"{_ATOM_NS}published")
                or entry.findtext("updated")
                or entry.findtext("published")
            )
            if not date_raw:
                continue
            created_at = _parse_iso8601(date_raw)
            if created_at is None:
                continue
            if not title and not summary:
                continue
            items.append(
                _RawItem(title=title, summary=summary, link=link, guid=guid, created_at=created_at)
            )
        except Exception as exc:
            log.debug("skipping malformed Atom entry: %s", exc)
            continue
    return items


def _parse_rfc822(value: str) -> dt.datetime | None:
    """Parse an RSS ``pubDate`` (RFC 822/2822). ``None`` on any failure.

    A result with no timezone at all is treated as unparseable rather than
    guessed at -- ``created_at`` must be timezone-aware UTC everywhere in this
    codebase, and silently assuming UTC for a genuinely ambiguous timestamp
    would be exactly the kind of quiet localisation this project forbids
    elsewhere (see ``utils.timeutils``).
    """
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _parse_iso8601(value: str) -> dt.datetime | None:
    """Parse an Atom ``updated``/``published`` (ISO 8601). ``None`` on failure."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _dedupe_by_text_hash(posts: list[SocialPost]) -> list[SocialPost]:
    """Collapse the same wire story syndicated across multiple feeds.

    Wire services and public broadcasters routinely run the identical story
    (often word-for-word) under distinct per-feed ``guid``/``link`` values, so
    ``external_id`` dedup alone (done in ``fetch_posts`` as items are
    collected) is not enough. The first-seen post for a given sanitised
    ``text_hash`` is kept and marked via ``duplicate_group`` so a downstream
    consumer can see the story was corroborated by more than one outlet;
    later copies are dropped rather than double-counted by sentiment
    aggregation.
    """
    seen: dict[str, SocialPost] = {}
    out: list[SocialPost] = []
    for post in posts:
        key = post.text_hash
        if key and key in seen:
            seen[key].duplicate_group = key
            continue
        if key:
            seen[key] = post
        out.append(post)
    return out
