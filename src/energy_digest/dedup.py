"""URL canonicalisation, tracking-link decoding and near-duplicate clustering."""

import base64
import binascii
import hashlib
import ipaddress
import re
import socket
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz import fuzz

from energy_digest.models import Item, Tier

_TRACKING_HOST = 'link.divenewsletter.com'
_TRACKING_SEGMENT = 3  # ['', 'click', '<id>', '<b64>', ...]
_TRACKING_PARAM_PREFIXES = ('utm_', 'mc_', 'mkt_', '_hs', 'fbclid', 'gclid')
_TIER_ORDER: dict[Tier, int] = {
    'primary': 0,
    'press': 1,
    'newsletter': 2,
    'aggregator': 3,
}
_PUNCTUATION = re.compile(r'[^\w\s]')
_ALLOWED_PORTS = frozenset({None, 80, 443})
_MIN_FUZZY_TOKENS = 4


def decode_tracking(url: str) -> str | None:
    """Decode a Utility Dive click-tracking link to its target URL, offline."""
    parts = urlsplit(url)
    if (parts.hostname or '').lower() != _TRACKING_HOST:
        return None
    segments = parts.path.split('/')
    if len(segments) <= _TRACKING_SEGMENT or segments[1] != 'click':
        return None
    encoded = segments[_TRACKING_SEGMENT]
    encoded += '=' * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded).decode('utf-8')
    except (binascii.Error, ValueError):
        return None
    return decoded if decoded.startswith('http') else None


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global treats some multicast ranges as global; those are never fetchable
    return ip.is_global and not ip.is_multicast


def safe_url(url: str) -> bool:
    """Whether ``url`` may be fetched: public http(s) on a standard port.

    The host must resolve, and every address it resolves to must be public, so
    a link cannot be used to reach loopback, link-local (cloud metadata) or
    internal-network services.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    if parts.scheme not in ('http', 'https') or not host:
        return False
    if port not in _ALLOWED_PORTS or host == 'localhost':
        return False
    try:
        infos = socket.getaddrinfo(host, port or parts.scheme)
    except OSError:
        return False
    addresses = {str(info[4][0]) for info in infos}
    return bool(addresses) and all(_is_public(a) for a in addresses)


def canonicalize(url: str) -> str:
    """Return a stable form of ``url`` for identity comparison."""
    parts = urlsplit(decode_tracking(url) or url)
    host = (parts.hostname or '').lower().removeprefix('www.')
    if parts.port:
        host = f'{host}:{parts.port}'
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    )
    path = parts.path.rstrip('/') or '/'
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ''))


def _hostname(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    return hostname.removeprefix('www.') if hostname else None


def display_source(name: str, url: str) -> str:
    """Return the publisher label to display, using a bare hostname for URLs.

    A ``name`` that is a URL is replaced by its host, and an empty ``name`` by
    the host of ``url``. Any other name is returned unchanged.
    """
    name = name.strip()
    if not name:
        return _hostname(url) or url
    if '://' not in name:
        return name
    return _hostname(name) or name


def url_hash(url: str) -> str:
    """SHA-256 hex digest of the canonical URL."""
    return hashlib.sha256(canonicalize(url).encode()).hexdigest()


def item_id(url: str) -> str:
    """Short stable item id: the first 12 hex characters of the URL hash."""
    return url_hash(url)[:12]


def _normalise_title(title: str) -> str:
    return ' '.join(_PUNCTUATION.sub(' ', title.lower()).split())


def _primary_key(item: Item) -> tuple[int, str, str]:
    return (_TIER_ORDER[item.tier], item.published.isoformat(), item.id)


def _similar(a: str, b: str, threshold: int) -> bool:
    """Near-identical titles; short titles are too ambiguous to merge by text."""
    if min(len(a.split()), len(b.split())) < _MIN_FUZZY_TOKENS:
        return False
    return fuzz.token_sort_ratio(a, b) >= threshold


def cluster(items: Sequence[Item], threshold: int = 88) -> list[Item]:
    """Collapse duplicate stories, keeping the most authoritative item of each.

    Items sharing a canonical URL are grouped first; groups whose titles are
    near-identical (and at least four words long) are then merged. The primary
    of each cluster is chosen by source tier, then earliest publication; the
    other sources are listed in ``also_reported_by``.
    """
    by_url: dict[str, list[Item]] = {}
    for item in items:
        by_url.setdefault(item.canonical_url, []).append(item)
    groups = sorted(by_url.values(), key=lambda g: min((i.published, i.id) for i in g))

    clusters: list[list[Item]] = []
    titles: list[list[str]] = []
    for group in groups:
        group_titles = [_normalise_title(i.title) for i in group]
        for members, known in zip(clusters, titles, strict=True):
            if any(_similar(a, b, threshold) for a in group_titles for b in known):
                members.extend(group)
                known.extend(group_titles)
                break
        else:
            clusters.append(list(group))
            titles.append(group_titles)

    primaries: list[Item] = []
    for members in clusters:
        primary = min(members, key=_primary_key)
        others = dict.fromkeys(m.source_name for m in members if m is not primary)
        primaries.append(primary.model_copy(update={'also_reported_by': list(others)}))
    return sorted(primaries, key=lambda i: (i.published, i.id))
