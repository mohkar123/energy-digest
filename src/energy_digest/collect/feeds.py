"""RSS and Google News collectors."""

import html
import logging
import re
import ssl
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import quote_plus, urlsplit

import feedparser
import httpx
from pydantic import ValidationError

from energy_digest.config import SourceCfg
from energy_digest.dedup import canonicalize, display_source, item_id
from energy_digest.models import Item
from energy_digest.store import Store

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) energy-digest/0.1 (+personal news reader)'
_TIMEOUT_S = 10.0
_SNIPPET_MAX = 600
_GNEWS_HOST = 'news.google.com'
# Locale -> (hl, gl, ceid) query parameters for Google News RSS search.
_LOCALES = {
    'en-US': ('en-US', 'US', 'US:en'),
    'en-GB': ('en-GB', 'GB', 'GB:en'),
    'en-NZ': ('en-NZ', 'NZ', 'NZ:en'),
    'pt-BR': ('pt-BR', 'BR', 'BR:pt-419'),
    'en-AU': ('en-AU', 'AU', 'AU:en'),
    'en-CA': ('en-CA', 'CA', 'CA:en'),
}
_TAG = re.compile(r'<[^>]+>')
_TITLE_PUBLISHER = re.compile(r'\s+-\s+[^-]+$')


class Validators(NamedTuple):
    """Conditional-GET validators a feed returned (``etag``, ``last-modified``)."""

    etag: str | None
    modified: str | None


def make_client() -> httpx.Client:
    """Build the shared HTTP client.

    The system trust store is used so that fetching works behind a
    TLS-inspection proxy whose CA is installed system-wide.
    """
    return httpx.Client(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        headers={'User-Agent': USER_AGENT},
        verify=ssl.create_default_context(),
    )


def gnews_url(query: str, locale: str) -> str:
    """Google News RSS search URL for ``query`` (last two days) in ``locale``."""
    hl, gl, ceid = _LOCALES[locale]
    q = quote_plus(f'{query} when:2d')
    return f'https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}'


def _clean_summary(raw: str) -> str:
    return _TAG.sub('', html.unescape(raw)).strip()[:_SNIPPET_MAX]


def _entry_time(entry: Any, store: Store) -> datetime:  # noqa: ANN401
    """Entry publication time; undated entries count from when we first saw them.

    Using the stored first-seen time (rather than now) lets an undated entry age
    out of the window instead of being re-collected and re-scored every day.
    """
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if parsed is not None:
        return datetime(*parsed[:6], tzinfo=UTC)
    link = str(entry.get('link') or '')
    seen = store.first_seen(item_id(link)) if link else None
    return seen or datetime.now(UTC)


def _entry_to_item(entry: Any, src: SourceCfg, published: datetime) -> Item | None:  # noqa: ANN401
    link = str(entry.get('link') or '')
    title = html.unescape(str(entry.get('title') or '')).strip()
    if not link or not title:
        return None
    source_name = src.name
    if src.kind == 'gnews':
        publisher = str(entry.get('source', {}).get('title') or '')
        if publisher:
            source_name = display_source(publisher, link)
            title = title.removesuffix(f' - {publisher}')
        else:
            title = _TITLE_PUBLISHER.sub('', title)
    try:
        return Item(
            id=item_id(link),
            url=link,
            canonical_url=canonicalize(link),
            title=title,
            source_id=src.id,
            source_name=source_name,
            source_kind=src.kind,
            tier=src.tier,
            market_hint=src.market,
            published=published,
            snippet=_clean_summary(str(entry.get('summary') or '')),
        )
    except ValidationError:
        logger.info(
            'feed entry skipped: invalid url',
            extra={'digest.source_id': src.id},
        )
        return None


def _validators(source_id: str, store: Store) -> dict[str, str]:
    etag, modified = store.get_feed_state(source_id)
    headers: dict[str, str] = {}
    if etag:
        headers['If-None-Match'] = etag
    if modified:
        headers['If-Modified-Since'] = modified
    return headers


def _matches(item: Item, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f'{item.title} {item.snippet}'.lower()
    return any(k in haystack for k in keywords)


def fetch_source(
    src: SourceCfg, client: httpx.Client, store: Store, since: datetime
) -> tuple[list[Item], Validators | None]:
    """Fetch one feed; return items newer than ``since`` and its new validators.

    The validators are NOT persisted here: saving them before the digest is
    delivered would make the next run get a 304 and lose this run's items. On a
    304 the stored items of the source inside the window are returned instead.

    Raises ``httpx.HTTPError`` or ``ValueError``; the caller records the failure.
    """
    if src.kind == 'gnews':
        url = gnews_url(src.query or '', src.locale)
    else:
        url = src.url or ''
    resp = client.get(url, headers=_validators(src.id, store))
    if resp.status_code == httpx.codes.NOT_MODIFIED:
        stored = store.items_for_source(src.id, since)
        return stored[: src.max_items], None
    resp.raise_for_status()
    validators = Validators(resp.headers.get('etag'), resp.headers.get('last-modified'))

    parsed = feedparser.parse(resp.content)
    dated = sorted(
        ((_entry_time(e, store), e) for e in parsed.entries),
        key=lambda pair: pair[0],
        reverse=True,
    )
    keywords = [k.lower() for k in src.keywords]
    items: list[Item] = []
    for published, entry in dated:
        if len(items) >= src.max_items:
            break
        if published < since:
            continue
        item = _entry_to_item(entry, src, published)
        if item is None:
            continue
        if _matches(item, keywords):
            items.append(item)
    return items, validators


def _decode_gnews(url: str) -> str | None:
    """Best-effort decode of a Google News link to the publisher URL."""
    # Imported lazily: the package pulls in an HTML parser at import time and
    # a broken install must degrade to "keep the Google URL", not crash.
    from googlenewsdecoder import gnewsdecoder  # noqa: PLC0415

    result = gnewsdecoder(url, interval=1)
    if isinstance(result, dict) and result.get('success'):
        decoded = result.get('decoded_url')
        return str(decoded) if decoded else None
    return None


def resolve_gnews(items: list[Item], store: Store, limit: int = 40) -> list[Item]:
    """Replace Google News links with publisher URLs where decoding succeeds.

    Only ``url`` and ``canonical_url`` change: the item id stays stable so
    scores, citations and store rows keep pointing at the same item. Failures
    are cached (as an empty string) so they are not retried every run; at most
    ``limit`` uncached links are decoded per run.
    """
    out: list[Item] = []
    attempts = 0
    for original in items:
        item = original
        if urlsplit(item.url).hostname != _GNEWS_HOST:
            out.append(item)
            continue
        cached = store.get_redirect(item.url)
        resolved: str | None
        if cached is not None:
            resolved = cached or None
        elif attempts >= limit:
            resolved = None
        else:
            attempts += 1
            try:
                resolved = _decode_gnews(item.url)
            except Exception:
                logger.debug(
                    'gnews decode failed',
                    extra={'digest.host': urlsplit(item.url).hostname},
                    exc_info=True,
                )
                resolved = None
            store.put_redirect(item.url, resolved or '')
        if resolved and urlsplit(resolved).scheme in ('http', 'https'):
            item = item.model_copy(
                update={
                    'url': resolved,
                    'canonical_url': canonicalize(resolved),
                }
            )
        out.append(item)
    return out


def collect_feeds(
    sources: list[SourceCfg],
    since: datetime,
    store: Store,
    client: httpx.Client,
) -> tuple[list[Item], dict[str, dict[str, Any]], dict[str, Validators]]:
    """Collect every enabled source, isolating failures in a health dict.

    The third element maps source id to the validators to persist once the
    digest has been delivered; this function never writes them.
    """
    items: list[Item] = []
    pending: dict[str, Validators] = {}
    health: dict[str, dict[str, Any]] = {}
    for src in sources:
        if not src.enabled:
            continue
        try:
            fetched, validators = fetch_source(src, client, store, since)
        except Exception as exc:
            logger.warning(
                'source failed',
                extra={'digest.stage': 'collect', 'digest.source_id': src.id},
                exc_info=True,
            )
            health[src.id] = {'ok': False, 'error': repr(exc)[:200], 'items': 0}
            continue
        items.extend(fetched)
        if validators is not None:
            pending[src.id] = validators
        health[src.id] = {'ok': True, 'items': len(fetched)}
    return items, health, pending
