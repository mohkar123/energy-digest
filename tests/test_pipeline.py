import fcntl
import json
import re
import socket
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from energy_digest.claude_cli import Runner
from energy_digest.config import (
    AppConfig,
    ClaudeCfg,
    DeliveryCfg,
    Settings,
    SourceCfg,
)
from energy_digest.deliver import (
    Delivery,
    DeliveryError,
    DeliveryOutcomeUnknownError,
)
from energy_digest.models import (
    DeepDive,
    DigestDraft,
    Item,
    MarketLine,
    Score,
    ScoreBatch,
    ScoredItem,
    Story,
)
from energy_digest.pipeline import (
    CostMeter,
    PipelineError,
    RunOptions,
    RunOutcome,
    _collect,
    _window_start,
    neutral_cwd,
    run,
)
from energy_digest.score import score_items
from energy_digest.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
FEED_URL = 'https://regulator.example.com/feed.xml'


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public address so no test touches the network."""

    def fake_getaddrinfo(host: str, port: object, *args: object, **kw: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)


LINK = 'https://example.slack.com/archives/D0000000001/p1791584281232849'
MAIN_RESULT = json.dumps(
    {
        'message_link': LINK,
        'message_context': {
            'message_ts': '1791584281.232849',
            'channel_id': 'D0000000001',
        },
    }
)
TOOL_ERROR = 'channel_not_found'
_PART = re.compile(r'<<<PART (\d+)\n(.*?)\nPART \1>>>', re.DOTALL)


def _delivery_stream(stdin: str, main_result: str, *, main_is_error: bool) -> str:
    """Stream-json for a delivery run: every part posted, the main one as given."""
    send = 'mcp__claude_ai_Slack__slack_send_message'
    lines: list[dict[str, Any]] = []
    for index, message in _PART.findall(stdin):
        use_id = f't{index}'
        first = index == '0'
        lines += [
            {
                'type': 'assistant',
                'message': {
                    'content': [
                        {
                            'type': 'tool_use',
                            'id': use_id,
                            'name': send,
                            'input': {'channel_id': 'U1', 'message': message},
                        }
                    ]
                },
            },
            {
                'type': 'user',
                'message': {
                    'content': [
                        {
                            'type': 'tool_result',
                            'tool_use_id': use_id,
                            'content': main_result if first else MAIN_RESULT,
                            'is_error': main_is_error and first,
                        }
                    ]
                },
            },
        ]
        if main_is_error:
            break
    lines.append(
        {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'total_cost_usd': 0.01,
            'duration_ms': 5,
        }
    )
    return '\n'.join(json.dumps(line) for line in lines)


class FakeRunner:
    """Answers claude calls by the JSON schema found in argv."""

    def __init__(
        self, extraction: dict[str, Any], fail: set[str] | None = None
    ) -> None:
        self.extraction = extraction
        self.fail = fail or set()
        self.calls: list[str] = []
        self.delivery_stdin = ''
        self.send_result = MAIN_RESULT
        self.send_is_error = False

    def _kind(self, argv: list[str]) -> str:
        if '--json-schema' not in argv:
            return 'send'  # the send run has no schema: its outcome is a tool result
        schema = json.loads(argv[argv.index('--json-schema') + 1])
        props = schema['properties']
        for kind, key in (
            ('extract', 'stories'),
            ('score', 'scores'),
            ('synthesize', 'top_stories'),
        ):
            if key in props:
                return kind
        msg = f'unknown schema {list(props)}'
        raise AssertionError(msg)

    def _score(self, stdin: str) -> dict[str, Any]:
        ids = [json.loads(line)['id'] for line in stdin.splitlines() if line]
        scores = [
            Score(
                item_id=i,
                market='US',
                topic='grid-planning',
                market_relevance=4,
                forecast_relevance=4,
                forecast_lines=['LTF'],
                dated_event=None,
                is_advert=False,
            )
            for i in ids
        ]
        return ScoreBatch(scores=scores).model_dump(mode='json')

    def _draft(self, stdin: str) -> dict[str, Any]:
        ids = [
            line[1 : line.index(']')]
            for line in stdin.splitlines()
            if line.startswith('[') and ']' in line
        ]
        first = ids[:1]
        draft = DigestDraft(
            top_stories=[
                Story(item_ids=first, headline='Top', summary='S', market='US')
            ],
            by_market={'US': [MarketLine(item_ids=first, one_liner='One liner')]},
            forecast_impact=[],
            radar=[],
            upcoming=[],
            deep_dive=DeepDive(
                market='US',
                what_moved=[],
                regulatory_calendar=[],
                account_implications=[],
                open_questions=[],
                competitors_mentioned=[],
            ),
            week_in_review=None,
        )
        return draft.model_dump(mode='json')

    def __call__(
        self,
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        kind = self._kind(argv)
        self.calls.append(kind)
        if kind in self.fail:
            payload: dict[str, Any] = {
                'subtype': 'error_during_execution',
                'is_error': True,
                'errors': ['boom'],
            }
            return 1, json.dumps(payload), ''
        if kind == 'send':
            self.delivery_stdin = stdin_text
            stream = _delivery_stream(
                stdin_text, self.send_result, main_is_error=self.send_is_error
            )
            return 0, stream, ''
        structured = {
            'extract': lambda: self.extraction,
            'score': lambda: self._score(stdin_text),
            'synthesize': lambda: self._draft(stdin_text),
        }[kind]()
        payload = {
            'subtype': 'success',
            'is_error': False,
            'structured_output': structured,
            'total_cost_usd': 0.01,
            'duration_ms': 5,
        }
        return 0, json.dumps(payload), ''


@pytest.fixture(autouse=True)
def _claude_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ENERGY_DIGEST_CLAUDE_CWD', str(tmp_path / 'claude-cwd'))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    settings = Settings(
        delivery=DeliveryCfg(slack_user_id='U1', slack_dm_channel='D1'),
        newsletter_senders=['a@x.com'],
        claude=ClaudeCfg(disallow_mcp=[]),
        data_dir=tmp_path / 'data',
        reports_dir=tmp_path / 'reports',
    )
    source = SourceCfg(
        id='reg',
        name='Regulator',
        market='US',
        kind='rss',
        url=FEED_URL,
        tier='primary',
    )
    return AppConfig(
        settings=settings,
        sources=[source],
        watchlist=[],
        company_context='ctx',
        market_context={},
    )


@pytest.fixture
def client(fixtures_dir: Path) -> httpx.Client:
    rss = (fixtures_dir / 'rss_sample.xml').read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'HEAD':
            return httpx.Response(200)
        if str(request.url).startswith(FEED_URL):
            return httpx.Response(200, content=rss)
        return httpx.Response(200, text='<html><body>short</body></html>')

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def runner(fixtures_dir: Path) -> FakeRunner:
    extraction = json.loads((fixtures_dir / 'newsletter_extraction.json').read_text())
    return FakeRunner(extraction)


def _run(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> RunOutcome:
    return run(
        RunOptions(run_date=RUN_DATE, since=SINCE, dry_run=dry_run, force=force),
        cfg,
        runner=runner,
        client=client,
        now=NOW,
    )


def test_dry_run_writes_reports_and_does_not_send(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    outcome = _run(cfg, runner, client, dry_run=True)
    assert outcome.status == 'dry-run'
    assert outcome.message_id is None
    assert 'send' not in runner.calls
    reports = cfg.settings.reports_dir
    for name in ('email.html', 'email.txt', 'report.html'):
        assert (reports / f'2026-10-09-{name}').read_text()
    assert outcome.email_path == reports / '2026-10-09-email.html'
    assert outcome.report_path == reports / '2026-10-09-report.html'
    assert outcome.cost_usd == pytest.approx(0.01 * len(runner.calls))


def test_real_run_sends_then_skips_then_force_resends(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    first = _run(cfg, runner, client)
    assert (first.status, first.message_id) == ('ok', LINK)

    again = _run(cfg, runner, client)
    assert again.status == 'skipped'
    assert runner.calls.count('send') == 1

    forced = _run(cfg, runner, client, force=True)
    assert forced.status == 'ok'
    assert runner.calls.count('send') == 2


def test_sent_items_are_marked_and_not_repeated(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    _run(cfg, runner, client)
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        assert store.sent_item_ids()
    finally:
        store.close()


def test_synthesis_failure_degrades_but_still_sends(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    runner.fail = {'synthesize'}
    outcome = _run(cfg, runner, client)
    assert outcome.status == 'degraded'
    assert outcome.message_id == LINK


def test_send_failure_is_failed_with_reports_on_disk(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    runner.fail = {'send'}
    outcome = _run(cfg, runner, client)
    assert outcome.status == 'failed'
    assert outcome.message_id is None
    assert outcome.error is not None
    assert outcome.email_path is not None
    assert outcome.email_path.exists()
    assert outcome.report_path is not None
    assert outcome.report_path.exists()
    # a failed send must not block a retry on the same date
    runner.fail = set()
    assert _run(cfg, runner, client).status == 'ok'


def test_main_post_tool_error_fails_the_run_and_clears_claims(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    runner.send_result = TOOL_ERROR
    runner.send_is_error = True
    outcome = _run(cfg, runner, client)
    assert outcome.status == 'failed'
    assert _runs(cfg) == [('failed', 0)]
    assert outcome.message_id is None
    assert outcome.error is not None
    assert TOOL_ERROR in outcome.error
    assert outcome.cost_usd == pytest.approx(0.01 * len(runner.calls))
    assert not list(cfg.settings.data_dir.glob('deliver-*'))


def test_unparseable_main_post_result_is_send_unknown(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    runner.send_result = 'sent!'
    outcome = _run(cfg, runner, client)
    assert outcome.status == 'send-unknown'
    assert _runs(cfg) == [('send-unknown', 1)]
    assert _run(cfg, runner, client).status == 'skipped'


def test_missing_thread_replies_warn_but_the_run_is_ok(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def partial(*args: object, **kwargs: object) -> Delivery:
        return Delivery(LINK, '1791584281.232849', (2,))

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', partial)
    with caplog.at_level('WARNING'):
        outcome = _run(cfg, runner, client)
    assert (outcome.status, outcome.message_id) == ('ok', LINK)
    assert any(
        r.levelname == 'WARNING' and r.__dict__['digest.missing_parts'] == [2]
        for r in caplog.records
        if 'digest.missing_parts' in r.__dict__
    )


def test_slack_parts_are_archived_before_delivery(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    _run(cfg, runner, client)
    archived = (cfg.settings.reports_dir / '2026-10-09-slack.md').read_text()
    posted = [message for _, message in _PART.findall(runner.delivery_stdin)]
    assert posted
    assert archived == '\n\n---\n\n'.join(posted)


def test_unexpected_exception_marks_run_failed_and_reraises(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        msg = 'kaboom'
        raise RuntimeError(msg)

    monkeypatch.setattr('energy_digest.pipeline.render_all', boom)
    with pytest.raises(RuntimeError, match='kaboom'):
        _run(cfg, runner, client)
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        row = store._conn.execute('SELECT status FROM runs').fetchone()
        assert row['status'] == 'failed'
    finally:
        store.close()


def test_neutral_cwd_rejects_project_settings(tmp_path: Path) -> None:
    cwd = neutral_cwd()
    assert cwd == tmp_path / 'claude-cwd'
    (cwd / '.mcp.json').write_text('{}')
    with pytest.raises(PipelineError):
        neutral_cwd()


def test_neutral_cwd_defaults_to_xdg_state_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('ENERGY_DIGEST_CLAUDE_CWD')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    assert neutral_cwd() == tmp_path / 'state' / 'energy-digest' / 'claude-cwd'
    monkeypatch.delenv('XDG_STATE_HOME')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    assert neutral_cwd() == (
        tmp_path / 'home' / '.local' / 'state' / 'energy-digest' / 'claude-cwd'
    )
    assert not neutral_cwd().is_relative_to(REPO_ROOT)


def test_scoring_gets_a_deadline_from_run_start(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float | None] = []
    real = score_items

    def spy(
        items: Sequence[Item],
        cfg: AppConfig,
        *,
        cwd: Path,
        runner: Runner,
        deadline: float | None = None,
    ) -> list[ScoredItem]:
        seen.append(deadline)
        return real(items, cfg, cwd=cwd, runner=runner, deadline=deadline)

    monkeypatch.setattr('energy_digest.pipeline.score_items', spy)
    before = time.monotonic()
    _run(cfg, runner, client, dry_run=True)
    assert seen[0] is not None
    assert seen[0] - before == pytest.approx(25 * 60, abs=60)


def test_cost_meter_sums_reported_costs_and_ignores_garbage() -> None:
    outputs = iter(['{"total_cost_usd": 0.25}', 'not json', '[1]'])

    def fake(
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        return 0, next(outputs), ''

    meter = CostMeter(fake)
    for _ in range(3):
        meter([], '', Path(), {}, 1)
    assert meter.total_usd == pytest.approx(0.25)


def test_selected_gnews_items_are_resolved_before_render_and_store(
    cfg: AppConfig,
    runner: FakeRunner,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gnews = 'https://news.google.com/rss/articles/CBMiabc'
    decoded = 'https://publisher.example.com/large-load-story'
    rss = (
        (fixtures_dir / 'rss_sample.xml')
        .read_text()
        .replace(
            'https://regulator.example.com/news/large-load-tariff'
            '?utm_source=rss&amp;utm_medium=feed',
            gnews,
        )
    )
    assert gnews in rss

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(FEED_URL):
            return httpx.Response(200, content=rss.encode())
        return httpx.Response(200, text='<html><body>short</body></html>')

    monkeypatch.setattr(
        'energy_digest.collect.feeds._decode_gnews', lambda url: decoded
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    _run(cfg, runner, client, dry_run=True)
    # the report lists every selected item, so it shows the item whatever the
    # fake synthesis chose to cite
    report = (cfg.settings.reports_dir / '2026-10-09-report.html').read_text()
    assert decoded in report
    assert 'news.google.com' not in report
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        urls = {i.url for i in store.items_since(SINCE)}
    finally:
        store.close()
    assert decoded in urls


def _etag_client(fixtures_dir: Path) -> httpx.Client:
    rss = (fixtures_dir / 'rss_sample.xml').read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'HEAD':
            return httpx.Response(200)
        if str(request.url).startswith(FEED_URL):
            return httpx.Response(200, content=rss, headers={'ETag': '"v1"'})
        return httpx.Response(200, text='<html><body>short</body></html>')

    return httpx.Client(transport=httpx.MockTransport(handler))


def _feed_state(cfg: AppConfig) -> tuple[str | None, str | None]:
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        return store.get_feed_state('reg')
    finally:
        store.close()


def test_dry_run_never_writes_feed_validators(
    cfg: AppConfig, runner: FakeRunner, fixtures_dir: Path
) -> None:
    _run(cfg, runner, _etag_client(fixtures_dir), dry_run=True)
    assert _feed_state(cfg) == (None, None)


def test_failed_send_does_not_write_feed_validators(
    cfg: AppConfig, runner: FakeRunner, fixtures_dir: Path
) -> None:
    runner.fail = {'send'}
    _run(cfg, runner, _etag_client(fixtures_dir))
    assert _feed_state(cfg) == (None, None)


def test_successful_send_persists_feed_validators(
    cfg: AppConfig, runner: FakeRunner, fixtures_dir: Path
) -> None:
    assert _run(cfg, runner, _etag_client(fixtures_dir)).status == 'ok'
    assert _feed_state(cfg) == ('"v1"', None)


def _claim_main_post(once_dir: Path) -> None:
    once_dir.mkdir(parents=True, exist_ok=True)
    (once_dir / '0').touch()


def _runs(cfg: AppConfig) -> list[tuple[str, int]]:
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        rows = store._conn.execute('SELECT status, sent FROM runs').fetchall()
        return [(r['status'], r['sent']) for r in rows]
    finally:
        store.close()


def _digest_count(cfg: AppConfig) -> int:
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        return store._conn.execute('SELECT COUNT(*) FROM digests').fetchone()[0]
    finally:
        store.close()


def test_run_is_skipped_when_the_lock_is_held(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    cfg.settings.data_dir.mkdir(parents=True)
    with (cfg.settings.data_dir / 'run.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        outcome = _run(cfg, runner, client)
    assert outcome.status == 'skipped'
    assert runner.calls == []
    # once the holder lets go, the run proceeds
    assert _run(cfg, runner, client, dry_run=True).status == 'dry-run'


def test_unconfirmed_send_is_recorded_as_sent_and_blocks_a_rerun(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unknown(*args: object, **kwargs: object) -> Delivery:
        msg = 'timed out after the post was attempted'
        raise DeliveryOutcomeUnknownError(msg)

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', unknown)
    with caplog.at_level('ERROR'):
        outcome = _run(cfg, runner, client)
    assert outcome.status == 'send-unknown'
    assert _runs(cfg) == [('send-unknown', 1)]
    assert any('check your Slack DMs' in r.getMessage() for r in caplog.records)
    # no blind resend without --force
    assert _run(cfg, runner, client).status == 'skipped'


@pytest.mark.parametrize('error', [KeyboardInterrupt, RuntimeError])
def test_interrupt_after_the_guard_allowed_the_send_is_send_unknown(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    error: type[BaseException],
) -> None:
    def claim_then_die(*args: object, once_dir: Path, **kwargs: object) -> Delivery:
        _claim_main_post(once_dir)
        raise error

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', claim_then_die)
    with pytest.raises(error):
        _run(cfg, runner, client)
    assert _runs(cfg) == [('send-unknown', 1)]
    assert _run(cfg, runner, client).status == 'skipped'


def test_interrupt_before_the_guard_allowed_the_send_is_failed(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def die(*args: object, **kwargs: object) -> Delivery:
        raise KeyboardInterrupt

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', die)
    with pytest.raises(KeyboardInterrupt):
        _run(cfg, runner, client)
    assert _runs(cfg) == [('failed', 0)]


def test_window_starts_at_a_send_unknown_run(store: Store) -> None:
    unknown = datetime(2026, 10, 8, 14, tzinfo=UTC)
    _record_run(store, 'ok', datetime(2026, 10, 7, 14, tzinfo=UTC), sent=True)
    _record_run(store, 'send-unknown', unknown, sent=True)
    window = _window_start(RunOptions(run_date=RUN_DATE), store, NOW, LA)
    assert window == unknown - timedelta(minutes=30)


def test_once_dir_is_removed_after_an_ok_delivery(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed: list[Path] = []

    def claim_and_deliver(*args: object, once_dir: Path, **kwargs: object) -> Delivery:
        _claim_main_post(once_dir)
        claimed.append(once_dir)
        return Delivery(LINK, '1.2', ())

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', claim_and_deliver)
    assert _run(cfg, runner, client).status == 'ok'
    assert not claimed[0].exists()


def test_once_dir_is_kept_after_send_unknown(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed: list[Path] = []

    def claim_then_fail(*args: object, once_dir: Path, **kwargs: object) -> Delivery:
        _claim_main_post(once_dir)
        claimed.append(once_dir)
        msg = 'timed out after the post was attempted'
        raise DeliveryOutcomeUnknownError(msg)

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', claim_then_fail)
    assert _run(cfg, runner, client).status == 'send-unknown'
    assert (claimed[0] / '0').exists()


def test_empty_claim_dir_is_removed_after_a_definitive_delivery_error(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed: list[Path] = []

    def fail_with_empty_claim_dir(
        *args: object, once_dir: Path, **kwargs: object
    ) -> Delivery:
        once_dir.mkdir(parents=True)  # the guard creates the dir before claiming
        claimed.append(once_dir)
        msg = 'model did not post'
        raise DeliveryError(msg)

    monkeypatch.setattr(
        'energy_digest.pipeline.deliver_digest', fail_with_empty_claim_dir
    )
    assert _run(cfg, runner, client).status == 'failed'
    assert not claimed[0].exists()


def test_delivery_is_given_a_per_run_once_dir(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []

    def fake_deliver(*args: object, once_dir: Path, **kwargs: object) -> Delivery:
        seen.append(once_dir)
        return Delivery(LINK, '1.2', ())

    monkeypatch.setattr('energy_digest.pipeline.deliver_digest', fake_deliver)
    assert _run(cfg, runner, client).message_id == LINK
    assert seen[0].parent == cfg.settings.data_dir
    assert seen[0].name.startswith('deliver-')


@pytest.mark.parametrize('flags', [{'dry_run': True}, {'no_send': True}])
def test_unsent_runs_do_not_save_the_digest_or_index_summaries(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client, flags: dict[str, bool]
) -> None:
    run(
        RunOptions(run_date=RUN_DATE, since=SINCE, **flags),
        cfg,
        runner=runner,
        client=client,
        now=NOW,
    )
    assert _digest_count(cfg) == 0
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        assert store.search('One liner') == []
    finally:
        store.close()


def test_delivered_run_saves_digest_and_indexes_summary(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    assert _run(cfg, runner, client).status == 'ok'
    assert _digest_count(cfg) == 1


def test_interrupt_marks_run_failed_and_reraises(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr('energy_digest.pipeline.render_all', interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run(cfg, runner, client)
    assert _runs(cfg) == [('failed', 0)]


def test_failure_while_recording_delivery_keeps_the_run_delivered(
    cfg: AppConfig,
    runner: FakeRunner,
    client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        msg = 'disk full'
        raise OSError(msg)

    monkeypatch.setattr(Store, 'mark_sent', boom)
    with pytest.raises(OSError, match='disk full'):
        _run(cfg, runner, client)
    assert _runs(cfg) == [('ok', 1)]
    assert _run(cfg, runner, client).status == 'skipped'


def test_stale_running_rows_are_abandoned_at_start(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client
) -> None:
    cfg.settings.data_dir.mkdir(parents=True)
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        run_id = store.start_run(date(2026, 10, 1))
        with store._conn:
            store._conn.execute(
                'UPDATE runs SET started = ? WHERE run_id = ?',
                ('2026-10-01T00:00:00+00:00', run_id),
            )
    finally:
        store.close()
    _run(cfg, runner, client, dry_run=True)
    assert ('abandoned', 0) in _runs(cfg)


class _WeekReviewRunner(FakeRunner):
    """Synthesis cites a stored item from earlier in the week in week_in_review."""

    def _draft(self, stdin: str) -> dict[str, Any]:
        draft = super()._draft(stdin)
        draft['week_in_review'] = {
            'themes': [{'item_ids': ['weekitem01'], 'one_liner': 'Earlier theme'}],
            'trend_notes': 'Trend.',
        }
        return draft


def test_friday_week_review_links_resolve_to_stored_items(
    cfg: AppConfig, fixtures_dir: Path, client: httpx.Client
) -> None:
    cfg.settings.data_dir.mkdir(parents=True)
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        store.upsert_items(
            [
                Item(
                    id='weekitem01',
                    url='https://stored.example/earlier',
                    canonical_url='https://stored.example/earlier',
                    title='Earlier story',
                    source_id='s',
                    source_name='Stored Src',
                    source_kind='rss',
                    tier='press',
                    market_hint='US',
                    published=datetime(2026, 10, 6, tzinfo=UTC),
                )
            ]
        )
    finally:
        store.close()
    extraction = json.loads((fixtures_dir / 'newsletter_extraction.json').read_text())
    outcome = _run(cfg, _WeekReviewRunner(extraction), client, dry_run=True)
    assert outcome.email_path is not None
    email = outcome.email_path.read_text()
    assert 'Earlier theme' in email
    assert 'https://stored.example/earlier' in email


LA = ZoneInfo('America/Los_Angeles')


def _record_run(store: Store, status: str, started: datetime, *, sent: bool) -> None:
    run_id = store.start_run(RUN_DATE)
    store.finish_run(
        run_id, status, sent=sent, message_id=None, source_health={}, cost_usd=0.0
    )
    with store._conn:
        store._conn.execute(
            'UPDATE runs SET started = ? WHERE run_id = ?',
            (started.isoformat(), run_id),
        )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / 'digest.db')
    yield s
    s.close()


def test_window_defaults_to_24h_without_history(store: Store) -> None:
    opts = RunOptions(run_date=RUN_DATE)
    assert _window_start(opts, store, NOW, LA) == NOW - timedelta(hours=24)


def test_window_starts_at_last_delivered_run_start_minus_margin(store: Store) -> None:
    started = datetime(2026, 10, 8, 14, tzinfo=UTC)
    _record_run(store, 'ok', started, sent=True)
    window = _window_start(RunOptions(run_date=RUN_DATE), store, NOW, LA)
    assert window == started - timedelta(minutes=30)


def test_window_ignores_later_dry_run_and_failed_run(store: Store) -> None:
    delivered = datetime(2026, 10, 8, 14, tzinfo=UTC)
    _record_run(store, 'ok', delivered, sent=True)
    _record_run(store, 'dry-run', datetime(2026, 10, 9, 10, tzinfo=UTC), sent=False)
    _record_run(store, 'failed', datetime(2026, 10, 9, 11, tzinfo=UTC), sent=False)
    window = _window_start(RunOptions(run_date=RUN_DATE), store, NOW, LA)
    assert window == delivered - timedelta(minutes=30)


def test_window_uses_dry_run_only_when_nothing_was_delivered(store: Store) -> None:
    dry = datetime(2026, 10, 9, 10, tzinfo=UTC)
    _record_run(store, 'dry-run', dry, sent=False)
    window = _window_start(RunOptions(run_date=RUN_DATE), store, NOW, LA)
    assert window == dry - timedelta(minutes=30)


def test_window_interprets_naive_since_in_configured_timezone(store: Store) -> None:
    opts = RunOptions(
        run_date=RUN_DATE, since=datetime.fromisoformat('2026-10-08T09:00')
    )
    # 09:00 PDT is 16:00 UTC
    assert _window_start(opts, store, NOW, LA) == datetime(2026, 10, 8, 16, tzinfo=UTC)


def test_window_caps_explicit_since_and_warns(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    opts = RunOptions(run_date=RUN_DATE, since=NOW - timedelta(days=10))
    with caplog.at_level('WARNING'):
        window = _window_start(opts, store, NOW, LA)
    assert window == NOW - timedelta(hours=96)
    assert [r.levelname for r in caplog.records] == ['WARNING']


def test_window_within_cap_does_not_warn(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    opts = RunOptions(run_date=RUN_DATE, since=NOW - timedelta(days=2))
    with caplog.at_level('WARNING'):
        assert _window_start(opts, store, NOW, LA) == NOW - timedelta(days=2)
    assert not caplog.records


def test_collect_drops_items_whose_canonical_url_was_already_sent(
    cfg: AppConfig, runner: FakeRunner, client: httpx.Client, store: Store
) -> None:
    # Same story, new id: e.g. a Google News item resolved to the publisher URL.
    sent = Item(
        id='oldgnewsid1',
        url='https://regulator.example.com/news/large-load-tariff',
        canonical_url='https://regulator.example.com/news/large-load-tariff',
        title='Earlier headline',
        source_id='gnews',
        source_name='G',
        source_kind='gnews',
        tier='aggregator',
        market_hint='US',
        published=datetime(2026, 10, 8, tzinfo=UTC),
    )
    store.upsert_items([sent])
    store.mark_sent([sent.id], date(2026, 10, 8))
    opts = RunOptions(run_date=RUN_DATE)
    items, _, _ = _collect(cfg, opts, store, client, SINCE, cwd=Path(), runner=runner)
    assert items
    assert all('large-load-tariff' not in i.canonical_url for i in items)
    forced = RunOptions(run_date=RUN_DATE, force=True)
    again, _, _ = _collect(cfg, forced, store, client, SINCE, cwd=Path(), runner=runner)
    assert any('large-load-tariff' in i.canonical_url for i in again)
