import ipaddress
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from energy_digest.dedup import (
    canonicalize,
    cluster,
    decode_tracking,
    item_id,
    safe_url,
    url_hash,
)
from energy_digest.models import Item, NewsletterExtraction, Tier

PUBLIC_IP = '93.184.216.34'
# hostnames the fake resolver sends to non-public addresses
PRIVATE_HOSTS = {
    'internal.example.com': '10.0.0.5',
    'metadata.example.com': '169.254.169.254',
}


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve names offline: literal IPs to themselves, the rest to a public IP."""

    def fake_getaddrinfo(host: str, port: object, *args: object, **kw: object) -> list:
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = PRIVATE_HOSTS.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)


FIXTURE = Path(__file__).parent / 'fixtures' / 'newsletter_extraction.json'


def _item(
    url: str,
    title: str,
    *,
    tier: Tier = 'press',
    source: str = 'Src',
    hour: int = 8,
) -> Item:
    return Item(
        id=item_id(url),
        url=url,
        canonical_url=canonicalize(url),
        title=title,
        source_id=source.lower(),
        source_name=source,
        source_kind='rss',
        tier=tier,
        market_hint=None,
        published=datetime(2026, 10, 9, hour, tzinfo=UTC),
    )


def _dive_urls() -> list[str]:
    extraction = NewsletterExtraction.model_validate(json.loads(FIXTURE.read_text()))
    return [s.url for s in extraction.stories if 'divenewsletter' in s.url]


def test_decode_fixture_tracking_links() -> None:
    decoded = [decode_tracking(u) for u in _dive_urls()]
    assert all(d is not None for d in decoded)
    targets = [d or '' for d in decoded]
    assert any(t.startswith('https://www.vendor.example.com/') for t in targets)
    assert any(t.startswith('https://cdn.example.net/') for t in targets)


def test_decode_rejects_other_hosts_and_bad_payloads() -> None:
    assert decode_tracking('https://example.com/click/1/aHR0cHM6Ly94/x') is None
    assert decode_tracking('https://link.divenewsletter.com/click/1/!!!/x') is None
    # valid base64 that is not a URL
    assert decode_tracking('https://link.divenewsletter.com/click/1/aGVsbG8/x') is None
    assert decode_tracking('https://link.divenewsletter.com/other') is None


def test_canonicalize_strips_tracking_and_fragment() -> None:
    url = 'HTTPS://www.Example.com/a/b/?z=1&utm_source=x&a=2&fbclid=9#frag'
    assert canonicalize(url) == 'https://example.com/a/b?a=2&z=1'
    assert canonicalize('https://example.com/') == 'https://example.com/'


def test_canonicalize_decodes_tracking_link() -> None:
    vendor = next(
        u
        for u in _dive_urls()
        if canonicalize(u).startswith('https://vendor.example.com/')
    )
    assert '?' not in canonicalize(vendor)  # utm_* params stripped


def test_hash_and_id_are_stable() -> None:
    a = 'https://example.com/x?utm_medium=rss'
    b = 'https://www.example.com/x'
    assert url_hash(a) == url_hash(b)
    assert item_id(a) == url_hash(a)[:12]
    assert len(item_id(a)) == 12


def test_newsletter_and_rss_same_story_collapse() -> None:
    newsletter_url = next(
        u for u in _dive_urls() if 'vendor.example' in (decode_tracking(u) or '')
    )
    target = decode_tracking(newsletter_url) or ''
    rss = _item(
        target.split('?')[0] + '?utm_medium=rss',
        'Billing friction persists',
        tier='press',
        source='Utility Dive',
        hour=9,
    )
    news = _item(
        newsletter_url,
        'Billing friction persists',
        tier='newsletter',
        source='Utility Dive (newsletter)',
        hour=7,
    )
    result = cluster([news, rss])
    assert len(result) == 1
    assert result[0].source_name == 'Utility Dive'
    assert result[0].also_reported_by == ['Utility Dive (newsletter)']


def test_similar_titles_cluster_and_unrelated_do_not() -> None:
    a = _item('https://a.test/1', 'Ofgem raises the price cap for October', source='A')
    b = _item('https://b.test/2', 'Ofgem raises price cap for October!', source='B')
    c = _item('https://c.test/3', 'CAISO publishes battery storage report', source='C')
    result = cluster([a, b, c])
    assert len(result) == 2
    merged = next(i for i in result if i.title.startswith('Ofgem'))
    assert len(merged.also_reported_by) == 1
    assert c.id in {i.id for i in result}


def test_primary_tier_beats_aggregator() -> None:
    agg = _item(
        'https://news.google.com/1',
        'FERC approves new interconnection rule',
        tier='aggregator',
        source='Google News',
        hour=6,
    )
    prim = _item(
        'https://ferc.gov/1',
        'FERC approves new interconnection rule',
        tier='primary',
        source='FERC',
        hour=9,
    )
    result = cluster([agg, prim])
    assert [i.source_name for i in result] == ['FERC']
    assert result[0].also_reported_by == ['Google News']


def test_output_is_deterministic_and_dedups_sources() -> None:
    items = [
        _item('https://x.test/2', 'Alpha story', source='S2', hour=10),
        _item(
            'https://x.test/1',
            'Beta thing entirely different story',
            source='S1',
            hour=8,
        ),
        _item(
            'https://y.test/1',
            'Beta thing entirely different story',
            source='S1',
            hour=9,
        ),
    ]
    forward = cluster(items)
    assert forward == cluster(list(reversed(items)))
    assert [i.published.hour for i in forward] == [8, 10]
    assert forward[0].also_reported_by == ['S1']


@pytest.mark.parametrize(
    'url',
    [
        'https://example.com/a',
        'http://example.com/a',
        'http://example.com:80/a',
        'https://example.com:443/a',
    ],
)
def test_safe_url_accepts_public_http(url: str) -> None:
    assert safe_url(url)


@pytest.mark.parametrize(
    'url',
    [
        'javascript:alert(1)',
        'ftp://example.com/a',
        'file:///etc/passwd',
        'https:///no-host',
        'https://example.com:8443/a',
        'http://example.com:22/a',
        'http://localhost/a',
        'http://LOCALHOST:80/a',
        'http://127.0.0.1/a',
        'http://10.1.2.3/a',
        'http://172.16.0.1/a',
        'http://192.168.1.1/a',
        'http://169.254.169.254/latest/meta-data',
        'http://0.0.0.0/a',
        'http://224.0.0.1/a',
        'http://240.0.0.1/a',
        'http://[::1]/a',
        'http://[::ffff:127.0.0.1]/a',
        'http://[fe80::1]/a',
        'http://100.64.1.1/a',
        'http://[fc00::1]/a',
        'https://internal.example.com/a',
        'https://example.com:notaport/a',
    ],
)
def test_safe_url_rejects_unsafe_targets(url: str) -> None:
    assert not safe_url(url)


def test_safe_url_rejects_hosts_that_do_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> list:
        raise socket.gaierror

    monkeypatch.setattr(socket, 'getaddrinfo', fail)
    assert not safe_url('https://nonexistent.example.com/a')


def test_safe_url_requires_every_resolved_address_to_be_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mixed(*args: object, **kwargs: object) -> list:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 0)),
        ]

    monkeypatch.setattr(socket, 'getaddrinfo', mixed)
    assert not safe_url('https://rebind.example.com/a')


def test_short_titles_are_not_fuzzy_merged() -> None:
    a = _item('https://a.test/1', 'Ofgem price cap', source='A')
    b = _item('https://b.test/2', 'Ofgem price cap!', source='B')
    assert len(cluster([a, b])) == 2


def test_same_url_merges_even_for_short_titles() -> None:
    a = _item('https://a.test/1', 'Ofgem cap', source='A')
    b = _item('https://a.test/1?utm_source=x', 'Ofgem cap', source='B')
    assert len(cluster([a, b])) == 1


def test_clustering_uses_word_order_sensitive_match_not_subset() -> None:
    # token_set_ratio would call these identical (one is a subset of the other)
    a = _item('https://a.test/1', 'PUC approves Xcel rate case settlement', source='A')
    b = _item(
        'https://b.test/2',
        'PUC approves Xcel rate case settlement for Colorado wildfire costs',
        source='B',
    )
    assert len(cluster([a, b])) == 2
