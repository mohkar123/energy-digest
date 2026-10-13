import ipaddress
import socket
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from energy_digest.collect import article, feeds
from energy_digest.config import SourceCfg
from energy_digest.dedup import item_id
from energy_digest.models import Item
from energy_digest.store import Store

SINCE = datetime(2026, 10, 1, tzinfo=UTC)
PRIVATE_HOSTS = {'internal.example.com': '10.0.0.5'}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public address so no test touches the network."""

    def fake_getaddrinfo(host: str, port: object, *args: object, **kw: object) -> list:
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = PRIVATE_HOSTS.get(host, '93.184.216.34')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / 'digest.db')
    yield s
    s.close()


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler, follow_redirects=True)


def _rss_src(**kw: object) -> SourceCfg:
    base = {
        'id': 'reg',
        'name': 'Regulator',
        'market': 'US',
        'kind': 'rss',
        'url': 'https://regulator.example.com/feed.xml',
        'tier': 'primary',
    }
    return SourceCfg.model_validate({**base, **kw})


def _gnews_src() -> SourceCfg:
    return SourceCfg.model_validate(
        {
            'id': 'ofgem',
            'name': 'Ofgem (Google News)',
            'market': 'UK',
            'kind': 'gnews',
            'query': 'Ofgem',
            'locale': 'en-GB',
            'tier': 'aggregator',
        }
    )


def _serving(path: Path, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda req: httpx.Response(200, content=path.read_bytes(), headers=headers)
    )


def _item(url: str, **kw: object) -> Item:
    base = {
        'id': item_id(url),
        'url': url,
        'canonical_url': url,
        'title': 'T',
        'source_id': 's',
        'source_name': 'S',
        'source_kind': 'gnews',
        'tier': 'aggregator',
        'market_hint': 'UK',
        'published': SINCE,
    }
    return Item.model_validate({**base, **kw})


def test_gnews_url_encodes_query_and_locale() -> None:
    url = feeds.gnews_url('Ofgem price cap', 'pt-BR')
    assert url == (
        'https://news.google.com/rss/search?q=Ofgem+price+cap+when%3A2d'
        '&hl=pt-BR&gl=BR&ceid=BR:pt-419'
    )


def test_fetch_rss_parses_filters_and_cleans(fixtures_dir: Path, store: Store) -> None:
    client = _client(_serving(fixtures_dir / 'rss_sample.xml'))
    items, _ = feeds.fetch_source(_rss_src(), client, store, SINCE)
    assert [i.title for i in items] == [
        'Commission approves large-load tariff',
        'Utility files rate case',
    ]
    first = items[0]
    assert first.canonical_url == (
        'https://regulator.example.com/news/large-load-tariff'
    )
    assert (
        first.snippet
        == 'The commission & staff approved a new tariff for data centres.'
    )
    assert first.source_kind == 'rss'
    assert first.market_hint == 'US'
    assert first.id == item_id(first.url)


def test_fetch_gnews_strips_publisher_suffix(fixtures_dir: Path, store: Store) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(
            200, content=(fixtures_dir / 'gnews_sample.xml').read_bytes()
        )

    items, _ = feeds.fetch_source(
        _gnews_src(), _client(httpx.MockTransport(handler)), store, SINCE
    )
    assert seen[0].startswith('https://news.google.com/rss/search?q=Ofgem')
    assert [(i.title, i.source_name) for i in items] == [
        ('Ofgem opens review of standing charges', 'Utility Week'),
        ('Ofgem price cap - set to fall', 'The Guardian'),
    ]


def test_fetch_source_returns_validators_without_persisting_them(
    fixtures_dir: Path, store: Store
) -> None:
    headers = {'ETag': '"abc"', 'Last-Modified': 'Thu, 08 Oct 2026 15:00:00 GMT'}
    client = _client(_serving(fixtures_dir / 'rss_sample.xml', headers))
    items, validators = feeds.fetch_source(_rss_src(), client, store, SINCE)
    assert len(items) == 2
    assert validators == feeds.Validators('"abc"', 'Thu, 08 Oct 2026 15:00:00 GMT')
    assert store.get_feed_state('reg') == (None, None)


def test_not_modified_returns_stored_in_window_items(
    fixtures_dir: Path, store: Store
) -> None:
    sent: list[httpx.Headers] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req.headers)
        if 'if-none-match' in req.headers:
            return httpx.Response(304)
        return httpx.Response(
            200, content=(fixtures_dir / 'rss_sample.xml').read_bytes()
        )

    client = _client(httpx.MockTransport(handler))
    src = _rss_src()
    fetched, _ = feeds.fetch_source(src, client, store, SINCE)
    store.upsert_items(fetched)
    old = _item(
        'https://regulator.example.com/old',
        source_id='reg',
        published=datetime(2026, 9, 1, tzinfo=UTC),
    )
    other = _item('https://other.example.com/x', source_id='other')
    store.upsert_items([old, other])
    with store._conn:
        store._conn.execute(
            'UPDATE items SET first_seen = ? WHERE id = ?',
            ('2026-09-01T00:00:00+00:00', old.id),
        )
    store.put_feed_state('reg', '"abc"', 'Thu, 08 Oct 2026 15:00:00 GMT')

    again, validators = feeds.fetch_source(src, client, store, SINCE)
    assert sent[-1]['if-none-match'] == '"abc"'
    assert sent[-1]['if-modified-since'] == 'Thu, 08 Oct 2026 15:00:00 GMT'
    assert validators is None
    assert {i.id for i in again} == {i.id for i in fetched}


def test_collect_feeds_reports_pending_validators(
    fixtures_dir: Path, store: Store
) -> None:
    client = _client(_serving(fixtures_dir / 'rss_sample.xml', {'ETag': '"abc"'}))
    _, _, pending = feeds.collect_feeds([_rss_src()], SINCE, store, client)
    assert pending == {'reg': feeds.Validators('"abc"', None)}
    assert store.get_feed_state('reg') == (None, None)


def test_keyword_prefilter_drops_non_matching(fixtures_dir: Path, store: Store) -> None:
    client = _client(_serving(fixtures_dir / 'rss_sample.xml'))
    items, _ = feeds.fetch_source(
        _rss_src(keywords=['DATA centres']), client, store, SINCE
    )
    assert [i.title for i in items] == ['Commission approves large-load tariff']


def test_max_items_caps_newest_first(fixtures_dir: Path, store: Store) -> None:
    client = _client(_serving(fixtures_dir / 'rss_sample.xml'))
    items, _ = feeds.fetch_source(_rss_src(max_items=1), client, store, SINCE)
    assert [i.title for i in items] == ['Commission approves large-load tariff']


def test_collect_feeds_isolates_failures(fixtures_dir: Path, store: Store) -> None:
    rss = (fixtures_dir / 'rss_sample.xml').read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == 'broken.example.com':
            return httpx.Response(500)
        return httpx.Response(200, content=rss)

    sources = [
        _rss_src(id='bad', url='https://broken.example.com/feed'),
        _rss_src(id='good'),
        _rss_src(id='off', enabled=False),
    ]
    items, health, _ = feeds.collect_feeds(
        sources, SINCE, store, _client(httpx.MockTransport(handler))
    )
    assert len(items) == 2
    assert health['bad']['ok'] is False
    assert health['bad']['items'] == 0
    assert 'HTTPStatusError' in health['bad']['error']
    assert health['good'] == {'ok': True, 'items': 2}
    assert 'off' not in health


def test_gnews_publisher_given_as_url_becomes_hostname(store: Store) -> None:
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <title>Kenya grid expansion announced - https://www.streamlinefeed.co.ke/</title>
    <link>https://news.google.com/rss/articles/CBMiKENYA?oc=5</link>
    <pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate>
    <source url="https://www.streamlinefeed.co.ke/">https://www.streamlinefeed.co.ke/</source>
  </item>
</channel></rss>"""
    client = _client(httpx.MockTransport(lambda req: httpx.Response(200, content=feed)))
    items, _ = feeds.fetch_source(_gnews_src(), client, store, SINCE)
    assert [i.source_name for i in items] == ['streamlinefeed.co.ke']


def test_resolve_gnews_updates_url_and_keeps_fallback(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = _item('https://news.google.com/rss/articles/ok')
    bad = _item('https://news.google.com/rss/articles/bad')
    plain = _item('https://example.com/a')

    def fake_decode(url: str) -> str | None:
        if url.endswith('/ok'):
            return 'https://publisher.example.com/story?utm_source=x'
        msg = 'boom'
        raise RuntimeError(msg)

    monkeypatch.setattr(feeds, '_decode_gnews', fake_decode)
    out = feeds.resolve_gnews([ok, bad, plain], store)
    assert out[0].url == 'https://publisher.example.com/story?utm_source=x'
    assert out[0].canonical_url == 'https://publisher.example.com/story'
    assert out[0].id == ok.id
    assert out[1].url == bad.url
    assert out[2] == plain
    assert store.get_redirect(bad.url) == ''


def test_resolve_gnews_uses_cache_and_limit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_decode(url: str) -> str | None:
        calls.append(url)
        return 'https://publisher.example.com/' + url.rsplit('/', 1)[-1]

    monkeypatch.setattr(feeds, '_decode_gnews', fake_decode)
    items = [_item(f'https://news.google.com/rss/articles/{n}') for n in 'abc']
    out = feeds.resolve_gnews(items, store, limit=2)
    assert [i.url for i in out] == [
        'https://publisher.example.com/a',
        'https://publisher.example.com/b',
        'https://news.google.com/rss/articles/c',
    ]
    feeds.resolve_gnews(items[:2], store)
    assert len(calls) == 2


def _fake_decoder_module(result: dict[str, object]) -> types.ModuleType:
    module = types.ModuleType('googlenewsdecoder')
    module.gnewsdecoder = lambda url, interval=None: result  # type: ignore[attr-defined]
    return module


def test_decode_gnews_handles_result_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    url = 'https://news.google.com/rss/articles/z'
    ok = {'success': True, 'decoded_url': 'https://x.test/a'}
    monkeypatch.setitem(sys.modules, 'googlenewsdecoder', _fake_decoder_module(ok))
    assert feeds._decode_gnews(url) == 'https://x.test/a'
    failed = {'success': False, 'message': 'nope'}
    monkeypatch.setitem(sys.modules, 'googlenewsdecoder', _fake_decoder_module(failed))
    assert feeds._decode_gnews(url) is None


def test_resolve_gnews_survives_broken_decoder_install(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, 'googlenewsdecoder', None)  # import -> ImportError
    item = _item('https://news.google.com/rss/articles/q')
    assert feeds.resolve_gnews([item], store) == [item]


def test_fetch_text_sets_full_text(monkeypatch: pytest.MonkeyPatch) -> None:
    body = 'word ' * 200
    monkeypatch.setattr(article.trafilatura, 'extract', lambda html, **kw: body)
    client = _client(
        httpx.MockTransport(lambda req: httpx.Response(200, text='<html></html>'))
    )
    out = article.fetch_text(_item('https://example.com/a'), client)
    assert out.text == body
    assert out.headline_only is False


def test_fetch_text_leaves_short_text_and_errors_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item('https://example.com/a')
    ok = _client(httpx.MockTransport(lambda req: httpx.Response(200, text='x')))
    monkeypatch.setattr(article.trafilatura, 'extract', lambda html, **kw: 'short')
    assert article.fetch_text(item, ok) == item

    failing = _client(httpx.MockTransport(lambda req: httpx.Response(503)))
    assert article.fetch_text(item, failing) == item


def test_fetch_text_skips_google_news_links() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        msg = 'must not fetch'
        raise AssertionError(msg)

    item = _item('https://news.google.com/rss/articles/a')
    client = _client(httpx.MockTransport(handler))
    assert article.fetch_text(item, client) == item


def test_enrich_limits_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(article.trafilatura, 'extract', lambda html, **kw: 'w' * 500)
    client = _client(httpx.MockTransport(lambda req: httpx.Response(200, text='x')))
    items = [_item(f'https://example.com/{n}') for n in range(3)]
    out = article.enrich(items, client, limit=2)
    assert [i.headline_only for i in out] == [False, False, True]


_JS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Bad link</title><link>javascript:alert(1)</link>
<pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Good link</title><link>https://regulator.example.com/good</link>
<pubDate>Thu, 08 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_feed_entries_with_non_http_links_are_skipped(store: Store) -> None:
    client = _client(
        httpx.MockTransport(lambda req: httpx.Response(200, content=_JS_FEED))
    )
    items, _ = feeds.fetch_source(_rss_src(), client, store, SINCE)
    assert [i.title for i in items] == ['Good link']


def test_gnews_decoder_result_with_bad_scheme_keeps_google_url(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feeds, '_decode_gnews', lambda url: 'javascript:alert(1)')
    original = _item('https://news.google.com/rss/articles/abc')
    (out,) = feeds.resolve_gnews([original], store)
    assert out.url == original.url


def _article_client(handler: httpx.MockTransport) -> httpx.Client:
    # redirects are followed by hand in article.fetch_text, never by the client
    return httpx.Client(transport=handler)


def test_fetch_text_blocks_private_first_url_without_a_request() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, text='x')

    item = _item('https://internal.example.com/a')
    client = _article_client(httpx.MockTransport(handler))
    assert article.fetch_text(item, client) == item
    assert seen == []


def test_fetch_text_blocks_redirect_to_metadata_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(article.trafilatura, 'extract', lambda html, **kw: 'w' * 500)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(
            302, headers={'location': 'http://169.254.169.254/latest/meta-data'}
        )

    item = _item('https://example.com/a')
    client = _article_client(httpx.MockTransport(handler))
    assert article.fetch_text(item, client) == item
    assert seen == ['https://example.com/a']


def test_fetch_text_follows_safe_redirects_up_to_five_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(article.trafilatura, 'extract', lambda html, **kw: 'w' * 500)

    def handler(req: httpx.Request) -> httpx.Response:
        hop = int(req.url.path.removeprefix('/hop') or 0)
        if hop < int(req.url.params.get('to', 0)):
            return httpx.Response(
                302,
                headers={'location': f'/hop{hop + 1}?to={req.url.params["to"]}'},
            )
        return httpx.Response(200, text='<html></html>')

    client = _article_client(httpx.MockTransport(handler))
    five = article.fetch_text(_item('https://example.com/hop0?to=5'), client)
    assert five.headline_only is False
    six_item = _item('https://example.com/hop0?to=6')
    assert article.fetch_text(six_item, client) == six_item


def test_fetch_text_caps_the_downloaded_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[int] = []

    def extract(html: str, **kw: object) -> str:
        received.append(len(html))
        return 'w' * 500

    monkeypatch.setattr(article.trafilatura, 'extract', extract)
    big = b'a' * 5_000_000
    client = _article_client(
        httpx.MockTransport(lambda req: httpx.Response(200, content=big))
    )
    assert (
        article.fetch_text(_item('https://example.com/a'), client).headline_only
        is False
    )
    assert received == [article._MAX_BYTES]


_UNDATED_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Undated story</title>
<link>https://regulator.example.com/undated</link></item>
</channel></rss>"""


def _undated_client() -> httpx.Client:
    return _client(
        httpx.MockTransport(lambda req: httpx.Response(200, content=_UNDATED_FEED))
    )


def test_undated_entry_seen_for_the_first_time_counts_as_now(store: Store) -> None:
    items, _ = feeds.fetch_source(_rss_src(), _undated_client(), store, SINCE)
    assert [i.title for i in items] == ['Undated story']
    assert abs(items[0].published - datetime.now(UTC)).total_seconds() < 60


def test_undated_entry_uses_stored_first_seen_and_ages_out(store: Store) -> None:
    items, _ = feeds.fetch_source(_rss_src(), _undated_client(), store, SINCE)
    store.upsert_items(items)
    with store._conn:
        store._conn.execute(
            'UPDATE items SET first_seen = ?', ('2026-09-01T00:00:00+00:00',)
        )
    again, _ = feeds.fetch_source(_rss_src(), _undated_client(), store, SINCE)
    assert again == []
    # a window reaching back before first_seen still finds it, dated first_seen
    older = datetime(2026, 8, 1, tzinfo=UTC)
    (found,) = feeds.fetch_source(_rss_src(), _undated_client(), store, older)[0]
    assert found.published == datetime(2026, 9, 1, tzinfo=UTC)
