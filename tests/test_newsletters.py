import base64
import ipaddress
import json
import socket
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_digest.collect import newsletters
from energy_digest.collect.newsletters import READ_TOOLS, collect_newsletters
from energy_digest.config import AppConfig, ClaudeCfg, DeliveryCfg, Settings
from energy_digest.dedup import item_id
from energy_digest.models import NewsletterExtraction, NewsletterStory
from energy_digest.store import Store

SINCE = datetime(2026, 10, 7, tzinfo=UTC)

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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / 'digest.db')
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    settings = Settings(
        delivery=DeliveryCfg(slack_user_id='U1', slack_dm_channel='D1'),
        newsletter_senders=['a@x.com', 'b@y.com'],
        claude=ClaudeCfg(disallow_mcp=['mcp__other__tool']),
        data_dir=tmp_path,
        reports_dir=tmp_path,
    )
    return AppConfig(
        settings=settings,
        sources=[],
        watchlist=[],
        company_context='',
        market_context={},
    )


@pytest.fixture
def extraction(fixtures_dir: Path) -> dict[str, Any]:
    return json.loads((fixtures_dir / 'newsletter_extraction.json').read_text())


def _ok_client(status: int = 200) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(status))
    )


class FakeRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.argv: list[str] = []

    def __call__(
        self,
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        self.argv = argv
        return 0, json.dumps(self.payload), ''


def _success(structured: dict[str, Any]) -> dict[str, Any]:
    return {
        'type': 'result',
        'subtype': 'success',
        'is_error': False,
        'result': '',
        'structured_output': structured,
        'total_cost_usd': 0.02,
        'duration_ms': 1,
        'permission_denials': [],
    }


def _story(**kw: object) -> NewsletterStory:
    base = {
        'sender': 'hi@news.energycentral.com',
        'subject': 's',
        'date': '2026-10-09T12:32:14Z',
        'title': 'A story',
        'url': 'https://www.energycentral.com/energy-biz/post/a',
        'blurb': 'b' * 700,
        'is_sponsored': False,
        'is_event': False,
        'event_date': None,
    }
    return NewsletterStory.model_validate({**base, **kw})


def test_collect_drops_sponsored_and_tracking(
    cfg: AppConfig, store: Store, extraction: dict[str, Any], tmp_path: Path
) -> None:
    runner = FakeRunner(_success(extraction))
    items, health = collect_newsletters(
        cfg, SINCE, store, _ok_client(), cwd=tmp_path, runner=runner
    )
    assert len(items) >= 5
    assert health == {'newsletters': {'ok': True, 'items': len(items)}}
    for item in items:
        assert item.source_name == 'Energy Central'
        assert not item.url.startswith('https://link.divenewsletter.com')
        assert item.source_kind == 'newsletter'
        assert item.market_hint == 'US'
    sponsored = {s['title'] for s in extraction['stories'] if s['is_sponsored']}
    assert not sponsored & {i.title for i in items}


def test_runner_argv_is_read_only(
    cfg: AppConfig, store: Store, extraction: dict[str, Any], tmp_path: Path
) -> None:
    runner = FakeRunner(_success(extraction))
    collect_newsletters(cfg, SINCE, store, _ok_client(), cwd=tmp_path, runner=runner)
    argv = runner.argv
    start = argv.index('--allowedTools') + 1
    assert tuple(argv[start : start + len(READ_TOOLS)]) == READ_TOOLS
    assert argv[start + len(READ_TOOLS)].startswith('--')
    denied = argv[argv.index('--disallowedTools') + 1 :]
    assert 'mcp__claude_ai_Gmail__send_message' in denied


def test_hook_is_limited_to_configured_senders(
    cfg: AppConfig, store: Store, extraction: dict[str, Any], tmp_path: Path
) -> None:
    runner = FakeRunner(_success(extraction))
    collect_newsletters(cfg, SINCE, store, _ok_client(), cwd=tmp_path, runner=runner)
    settings = json.loads(runner.argv[runner.argv.index('--settings') + 1])
    command = settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
    assert '--gmail-senders a@x.com,b@y.com' in command


def test_claude_error_yields_no_items(
    cfg: AppConfig, store: Store, tmp_path: Path
) -> None:
    runner = FakeRunner(
        {'subtype': 'error_during_execution', 'is_error': True, 'errors': ['boom']}
    )
    items, health = collect_newsletters(
        cfg, SINCE, store, _ok_client(), cwd=tmp_path, runner=runner
    )
    assert items == []
    assert health['newsletters']['ok'] is False
    assert 'error_during_execution' in health['newsletters']['error']


def test_invalid_extraction_is_recorded(
    cfg: AppConfig, store: Store, tmp_path: Path
) -> None:
    runner = FakeRunner(_success({'stories': [{'title': 'x'}]}))
    items, health = collect_newsletters(
        cfg, SINCE, store, _ok_client(), cwd=tmp_path, runner=runner
    )
    assert items == []
    assert health['newsletters']['ok'] is False


def test_prompt_contains_query_and_rules() -> None:
    prompt = newsletters.build_prompt(['a@x.com', 'b@y.com'], SINCE.date())
    assert 'from:(a@x.com OR b@y.com) after:2026/10/07' in prompt
    assert 'pageSize 30' in prompt
    assert 'PLAIN_TEXT' in prompt
    assert 'is_sponsored=true' in prompt
    assert 'is_event=true' in prompt


def test_unreachable_url_is_dropped(store: Store) -> None:
    story = _story(url='https://unknown.example.com/missing')
    assert newsletters.postprocess([story], store, _ok_client(404)) == []


def test_unknown_host_is_verified_and_redirect_followed(store: Store) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == 'short.example.com':
            return httpx.Response(
                301, headers={'location': 'https://news.example.com/final'}
            )
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    story = _story(url='https://short.example.com/x')
    (item,) = newsletters.postprocess([story], store, client)
    assert item.url == 'https://news.example.com/final'
    assert item.id == item_id('https://news.example.com/final')
    assert store.get_redirect('https://short.example.com/x') == item.url


def test_head_failure_falls_back_to_get(store: Store) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(405 if req.method == 'HEAD' else 200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    story = _story(url='https://unknown.example.com/ok')
    assert len(newsletters.postprocess([story], store, client)) == 1


def test_network_error_drops_story(store: Store) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        msg = 'down'
        raise httpx.ConnectError(msg)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    story = _story(url='https://unknown.example.com/ok')
    assert newsletters.postprocess([story], store, client) == []


def test_redirect_loop_is_dropped(store: Store) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={'location': str(req.url)})
        )
    )
    story = _story(url='https://loop.example.com/a')
    assert newsletters.postprocess([story], store, client) == []


def _encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


def test_utility_dive_tracking_link_decoded_offline(store: Store) -> None:
    target = 'https://www.utilitydive.com/news/a-story/123/'
    story = _story(
        sender='newsletter@divenewsletter.com',
        url=f'https://link.divenewsletter.com/click/1.2/{_encode(target)}/abc',
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    )
    (item,) = newsletters.postprocess([story], store, client)
    assert item.url == target
    assert item.source_name == 'Utility Dive (newsletter)'


def test_marketing_and_bad_scheme_and_zoom_dropped(store: Store) -> None:
    pardot = _story(
        url='https://link.divenewsletter.com/click/1.2/'
        f'{_encode("https://go.pardot.com/l/1/2")}/abc'
    )
    ftp = _story(url='ftp://files.example.com/a')
    zoom = _story(url='https://events.zoom.us/ev/abc', is_event=False)
    assert newsletters.postprocess([pardot, ftp, zoom], store, _ok_client()) == []


def test_event_fields_and_snippet(store: Store) -> None:
    story = _story(
        url='https://events.zoom.us/ev/abc',
        is_event=True,
        event_date='2026-11-02',
        date='not a date',
    )
    (item,) = newsletters.postprocess([story], store, _ok_client())
    assert item.is_event is True
    assert item.event_date is not None
    assert item.event_date.isoformat() == '2026-11-02'
    assert item.published.tzinfo is not None
    assert len(item.snippet) == 600
    bad = _story(is_event=True, event_date='soon')
    assert newsletters.postprocess([bad], store, _ok_client())[0].event_date is None


def test_extraction_model_roundtrip(extraction: dict[str, Any]) -> None:
    assert NewsletterExtraction.model_validate(extraction).stories


def _recording_client(
    seen: list[str], response: httpx.Response | None = None
) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return response or httpx.Response(200)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_story_on_private_host_is_dropped_without_a_request(store: Store) -> None:
    seen: list[str] = []
    story = _story(url='https://internal.example.com/a')
    assert newsletters.postprocess([story], store, _recording_client(seen)) == []
    assert seen == []


def test_redirect_to_cloud_metadata_address_is_blocked(store: Store) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(
            302, headers={'location': 'http://169.254.169.254/latest/meta-data'}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    story = _story(url='https://short.example.com/x')
    assert newsletters.postprocess([story], store, client) == []
    assert seen
    assert all('169.254.169.254' not in url for url in seen)
    assert store.get_redirect('https://short.example.com/x') is None


def test_redirect_to_private_hostname_is_blocked(store: Store) -> None:
    seen: list[str] = []
    client = _recording_client(
        seen,
        httpx.Response(302, headers={'location': 'https://metadata.example.com/x'}),
    )
    story = _story(url='https://short.example.com/x')
    assert newsletters.postprocess([story], store, client) == []
    assert not any('metadata.example.com' in url for url in seen)


def test_decoded_tracking_target_on_private_network_is_dropped(store: Store) -> None:
    story = _story(
        sender='newsletter@divenewsletter.com',
        url='https://link.divenewsletter.com/click/1.2/'
        f'{_encode("http://169.254.169.254/latest")}/abc',
    )
    assert newsletters.postprocess([story], store, _ok_client()) == []


def test_trusted_host_resolving_to_private_address_is_dropped(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PRIVATE_HOSTS, 'www.energycentral.com', '10.9.9.9')
    assert newsletters.postprocess([_story()], store, _ok_client()) == []


@pytest.mark.parametrize(
    'url',
    [
        'https://www.energycentral.com/unsubscribe?u=1',
        'https://news.example.com/Email-Settings/abc',
        'https://news.example.com/manage-subscription',
        'https://news.example.com/email-preferences?id=3',
        'https://news.example.com/optout',
        'https://news.example.com/opt-out',
    ],
)
def test_unsubscribe_style_links_are_dropped_before_any_request(
    store: Store, url: str
) -> None:
    seen: list[str] = []
    assert (
        newsletters.postprocess([_story(url=url)], store, _recording_client(seen)) == []
    )
    assert seen == []


def test_redirect_to_unsubscribe_link_is_dropped_without_requesting_it(
    store: Store,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == 'short.example.com':
            return httpx.Response(
                302, headers={'location': 'https://news.example.com/unsubscribe?u=1'}
            )
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    story = _story(url='https://short.example.com/x')
    assert newsletters.postprocess([story], store, client) == []
    assert not any('unsubscribe' in url for url in seen)
    assert store.get_redirect('https://short.example.com/x') is None


def test_article_slug_containing_preferences_is_kept(store: Store) -> None:
    url = 'https://news.example.com/news/consumer-preferences-shift-to-solar'
    (item,) = newsletters.postprocess([_story(url=url)], store, _ok_client())
    assert item.url == url


def test_unsubscribe_link_hidden_behind_tracking_is_dropped(store: Store) -> None:
    story = _story(
        sender='newsletter@divenewsletter.com',
        url='https://link.divenewsletter.com/click/1.2/'
        f'{_encode("https://www.utilitydive.com/unsubscribe/123")}/abc',
    )
    assert newsletters.postprocess([story], store, _ok_client()) == []


def test_javascript_url_is_dropped(store: Store) -> None:
    story = _story(url='javascript:alert(1)')
    assert newsletters.postprocess([story], store, _ok_client()) == []
