"""Full-text enrichment of top-ranked items."""

import logging
from urllib.parse import urlsplit

import httpx
import trafilatura

from energy_digest.dedup import safe_url
from energy_digest.models import Item

logger = logging.getLogger(__name__)

_MIN_TEXT_CHARS = 400
_MAX_TEXT_CHARS = 10_000
_MAX_BYTES = 2_000_000
_MAX_HOPS = 5


def _fetch_html(url: str, client: httpx.Client) -> str | None:
    """GET ``url`` following redirects by hand, refusing unsafe targets.

    Every hop (including the first URL) must pass ``safe_url``, and the body is
    streamed and truncated at ``_MAX_BYTES``. Returns None when a hop is
    unsafe or the redirect chain is too long.
    """
    current = url
    for _ in range(_MAX_HOPS + 1):
        if not safe_url(current):
            logger.info(
                'article url rejected',
                extra={'digest.host': urlsplit(current).hostname},
            )
            return None
        with client.stream('GET', current, follow_redirects=False) as resp:
            location = resp.headers.get('location')
            if resp.is_redirect and location:
                current = str(resp.url.join(location))
                continue
            resp.raise_for_status()
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) >= _MAX_BYTES:
                    break
            return bytes(body[:_MAX_BYTES]).decode(
                resp.encoding or 'utf-8', errors='replace'
            )
    return None


def fetch_text(item: Item, client: httpx.Client) -> Item:
    """Return ``item`` with its article text, or unchanged. Never raises."""
    if urlsplit(item.url).hostname == 'news.google.com':
        return item
    try:
        html = _fetch_html(item.url, client)
        text = (
            trafilatura.extract(html, include_comments=False, include_tables=False)
            if html
            else None
        )
    except Exception:
        logger.debug(
            'article fetch failed',
            extra={'digest.host': urlsplit(item.url).hostname},
            exc_info=True,
        )
        return item
    if not text or len(text) < _MIN_TEXT_CHARS:
        return item
    return item.model_copy(
        update={'text': text[:_MAX_TEXT_CHARS], 'headline_only': False}
    )


def enrich(items: list[Item], client: httpx.Client, limit: int = 30) -> list[Item]:
    """Fetch article text for the first ``limit`` items (call on ranked items)."""
    head = [fetch_text(i, client) for i in items[:limit]]
    return [*head, *items[limit:]]
