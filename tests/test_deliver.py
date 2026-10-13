import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from energy_digest import claude_cli
from energy_digest.claude_cli import SLACK_SEND_TOOL
from energy_digest.config import AppConfig, ClaudeCfg, DeliveryCfg, Settings
from energy_digest.deliver import (
    DeliveryError,
    DeliveryOutcomeUnknownError,
    deliver_digest,
    load_guard,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
USER_ID = 'U0000000001'
DM = 'D0000000001'
LINK = 'https://example.slack.com/archives/D0000000001/p1791584281232849'
TS = '1791584281.232849'
OK_RESULT = json.dumps(
    {'message_link': LINK, 'message_context': {'message_ts': TS, 'channel_id': DM}}
)
PARTS = ('Main post\r\nbody\n', 'Reply one', 'Reply two')


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    settings = Settings(
        delivery=DeliveryCfg(slack_user_id=USER_ID, slack_dm_channel=DM),
        newsletter_senders=[],
        claude=ClaudeCfg(disallow_mcp=[]),
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


class FakeRunner:
    def __init__(self, stdout: str, rc: int = 0) -> None:
        self.stdout = stdout
        self.rc = rc
        self.argv: list[str] = []
        self.stdin = ''

    def __call__(
        self,
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        self.argv = argv
        self.stdin = stdin_text
        return self.rc, self.stdout, ''


Post = tuple[str, str | list[dict[str, str]] | None, bool]  # message, result, is_error


def _stream(
    posts: list[Post],
    *,
    subtype: str = 'success',
    denials: list[Any] | None = None,
    model_text: str = 'done',
    thread_ts: Mapping[str, str] | None = None,
) -> str:
    """JSONL as ``claude --output-format stream-json --verbose`` prints it.

    Replies carry the main post's ts as thread_ts unless ``thread_ts`` maps
    their message to another value.
    """
    thread_ts = thread_ts or {}
    lines: list[dict[str, Any]] = [{'type': 'system', 'subtype': 'init'}]
    for i, (message, result, is_error) in enumerate(posts):
        use_id = f'toolu_{i}'
        lines.append(
            {
                'type': 'assistant',
                'message': {
                    'content': [
                        {
                            'type': 'tool_use',
                            'id': use_id,
                            'name': SLACK_SEND_TOOL,
                            'input': {'channel_id': USER_ID, 'message': message}
                            | (
                                {'thread_ts': thread_ts.get(message, TS)}
                                if message != PARTS[0]
                                else {}
                            ),
                        }
                    ]
                },
            }
        )
        if result is not None:  # None: the run ended before the tool answered
            lines.append(
                {
                    'type': 'user',
                    'message': {
                        'content': [
                            {
                                'type': 'tool_result',
                                'tool_use_id': use_id,
                                'content': result,
                                **({'is_error': True} if is_error else {}),
                            }
                        ]
                    },
                }
            )
    lines.append(
        {
            'type': 'result',
            'subtype': subtype,
            'is_error': subtype != 'success',
            'result': model_text,
            'total_cost_usd': 0.01,
            'permission_denials': denials or [],
        }
    )
    return '\n'.join(json.dumps(line) for line in lines)


def _all_ok() -> list[Post]:
    return [(p, OK_RESULT, False) for p in PARTS]


def _deliver(cfg: AppConfig, tmp_path: Path, runner: FakeRunner) -> Any:  # noqa: ANN401
    return deliver_digest(
        PARTS, cfg, cwd=tmp_path, once_dir=tmp_path / 'once', runner=runner
    )


def _after(argv: list[str], flag: str) -> list[str]:
    out: list[str] = []
    for token in argv[argv.index(flag) + 1 :]:
        if token.startswith('--'):
            break
        out.append(token)
    return out


def _guard_command(runner: FakeRunner) -> str:
    settings = json.loads(runner.argv[runner.argv.index('--settings') + 1])
    return settings['hooks']['PreToolUse'][0]['hooks'][0]['command']


def test_all_parts_posted_returns_link_and_ts(cfg: AppConfig, tmp_path: Path) -> None:
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(_all_ok())))
    assert delivery.message_link == LINK
    assert delivery.main_ts == TS
    assert delivery.missing_parts == ()


def test_failed_thread_part_is_reported_missing(cfg: AppConfig, tmp_path: Path) -> None:
    posts = [*_all_ok()[:2], (PARTS[2], 'channel_not_found', True)]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert delivery.missing_parts == (2,)


def test_unposted_thread_parts_are_reported_missing(
    cfg: AppConfig, tmp_path: Path
) -> None:
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(_all_ok()[:1])))
    assert delivery.missing_parts == (1, 2)


def test_list_content_result_is_flattened(cfg: AppConfig, tmp_path: Path) -> None:
    posts: list[Post] = [(PARTS[0], [{'type': 'text', 'text': OK_RESULT}], False)]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert delivery.message_link == LINK


def test_main_post_tool_error_is_definitive_and_clears_claims(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()  # the guard claims the part before the tool runs
    runner = FakeRunner(_stream([(PARTS[0], 'x' * 1000, True)]))
    with pytest.raises(DeliveryError, match='Slack post failed') as exc:
        _deliver(cfg, tmp_path, runner)
    assert not isinstance(exc.value, DeliveryOutcomeUnknownError)
    assert 'x' * 300 in str(exc.value)
    assert 'x' * 301 not in str(exc.value)
    assert not once.exists()


def test_error_result_after_a_successful_main_post_still_delivers(
    cfg: AppConfig, tmp_path: Path
) -> None:
    runner = FakeRunner(_stream(_all_ok()[:1], subtype='error_max_turns'), rc=1)
    assert _deliver(cfg, tmp_path, runner).missing_parts == (1, 2)


def test_no_events_with_claimed_main_post_is_outcome_unknown(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, FakeRunner(_stream([])))
    assert once.exists()


def test_claimed_main_post_without_result_is_outcome_unknown(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    runner = FakeRunner(_stream([(PARTS[0], None, False)], subtype='error_max_turns'))
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, runner)


def test_no_events_without_claim_is_a_plain_error_with_denials(
    cfg: AppConfig, tmp_path: Path
) -> None:
    runner = FakeRunner(_stream([], denials=[{'reason': 'wrong channel'}]))
    with pytest.raises(DeliveryError, match=r'did not post.*wrong channel') as exc:
        _deliver(cfg, tmp_path, runner)
    assert not isinstance(exc.value, DeliveryOutcomeUnknownError)


@pytest.mark.parametrize('content', ['sent!', '{"message_link": "x"}', '[]'])
def test_unparseable_main_result_is_outcome_unknown(
    cfg: AppConfig, tmp_path: Path, content: str
) -> None:
    runner = FakeRunner(_stream([(PARTS[0], content, False)]))
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, runner)


def test_model_text_claiming_success_is_not_trusted(
    cfg: AppConfig, tmp_path: Path
) -> None:
    runner = FakeRunner(_stream([], model_text=f'Posted! {LINK}'))
    with pytest.raises(DeliveryError):
        _deliver(cfg, tmp_path, runner)


def test_claude_failure_without_events_is_a_plain_error(
    cfg: AppConfig, tmp_path: Path
) -> None:
    runner = FakeRunner(_stream([], subtype='error_max_turns'))
    with pytest.raises(DeliveryError, match='delivery run failed'):
        _deliver(cfg, tmp_path, runner)


def test_argv_allows_only_the_slack_send_tool(cfg: AppConfig, tmp_path: Path) -> None:
    runner = FakeRunner(_stream(_all_ok()))
    _deliver(cfg, tmp_path, runner)
    assert _after(runner.argv, '--allowedTools') == [SLACK_SEND_TOOL]
    denied = _after(runner.argv, '--disallowedTools')
    assert SLACK_SEND_TOOL not in denied
    for tool in (
        'slack_read_channel',
        'slack_send_message_draft',
        'slack_search_users',
    ):
        assert f'mcp__claude_ai_Slack__{tool}' in denied
    assert set(claude_cli.SLACK_TOOLS) - {SLACK_SEND_TOOL} <= set(denied)
    assert set(claude_cli.GMAIL_TOOLS) <= set(denied)
    assert _after(runner.argv, '--max-turns') == [str(len(PARTS) + 3)]
    assert _after(runner.argv, '--output-format') == ['stream-json']


def test_guard_command_pins_channels_hashes_and_once_dir(
    cfg: AppConfig, tmp_path: Path
) -> None:
    runner = FakeRunner(_stream(_all_ok()))
    _deliver(cfg, tmp_path, runner)
    command = _guard_command(runner)
    guard = load_guard()
    hashes = ','.join(guard._sha256(p) for p in PARTS)
    assert f'--slack-channels {USER_ID},{DM}' in command
    assert f'--part-sha256 {hashes}' in command
    assert f'--once-dir {tmp_path / "once"}' in command


def test_prompt_gives_parts_verbatim_and_forbids_retry(
    cfg: AppConfig, tmp_path: Path
) -> None:
    runner = FakeRunner(_stream(_all_ok()))
    _deliver(cfg, tmp_path, runner)
    for i, part in enumerate(PARTS):
        assert f'<<<PART {i}\n{part}\nPART {i}>>>' in runner.stdin
    assert f'channel_id={USER_ID}' in runner.stdin
    assert 'message_context.message_ts' in runner.stdin
    assert 'Never retry' in runner.stdin
    assert 'exactly one tool call per turn' in runner.stdin.replace('\n', ' ')
    assert 'fix the arguments and retry that part once' in runner.stdin


def test_single_part_prompt_has_no_thread_step(cfg: AppConfig, tmp_path: Path) -> None:
    runner = FakeRunner(_stream(_all_ok()[:1]))
    deliver_digest(
        PARTS[:1], cfg, cwd=tmp_path, once_dir=tmp_path / 'once', runner=runner
    )
    assert 'Parts 1 to' not in runner.stdin


def test_empty_parts_are_rejected(cfg: AppConfig, tmp_path: Path) -> None:
    with pytest.raises(DeliveryError, match='no Slack parts'):
        deliver_digest(
            (), cfg, cwd=tmp_path, once_dir=tmp_path / 'once', runner=FakeRunner('')
        )


def test_guard_hook_and_tool_name_come_from_claude_cli() -> None:
    guard = load_guard()
    assert guard.SLACK_SEND_TOOL == SLACK_SEND_TOOL
    assert guard.SEND_TOOL == claude_cli.SEND_TOOL
    assert guard.__file__ == str(claude_cli.guard_script_path())


def test_load_guard_accepts_a_repo_root() -> None:
    assert load_guard(REPO_ROOT).__file__ == str(REPO_ROOT / 'hooks' / 'guard_send.py')


def test_load_guard_fails_for_a_missing_hook(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_guard(tmp_path)


def test_denied_then_successful_main_post_delivers_and_keeps_claim(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    posts: list[Post] = [
        (PARTS[0], 'hook denied: payload does not match', True),
        (PARTS[0], OK_RESULT, False),
    ]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert delivery.message_link == LINK
    assert delivery.main_ts == TS
    assert (once / '0').exists()


def test_all_main_post_attempts_erroring_is_definitive(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    posts: list[Post] = [
        (PARTS[0], 'denied', True),
        (PARTS[0], 'channel_not_found', True),
    ]
    with pytest.raises(DeliveryError, match='Slack post failed') as exc:
        _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert not isinstance(exc.value, DeliveryOutcomeUnknownError)
    assert not once.exists()


def test_error_then_empty_main_result_is_outcome_unknown(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    posts: list[Post] = [(PARTS[0], 'denied', True), (PARTS[0], '', False)]
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert once.exists()


def test_denied_then_successful_reply_part_is_not_missing(
    cfg: AppConfig, tmp_path: Path
) -> None:
    posts: list[Post] = [
        (PARTS[0], OK_RESULT, False),
        (PARTS[1], 'hook denied', True),
        (PARTS[1], OK_RESULT, False),
        (PARTS[2], OK_RESULT, False),
    ]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert delivery.missing_parts == ()


def test_all_part_zero_errors_with_a_successful_reply_is_outcome_unknown(
    cfg: AppConfig, tmp_path: Path
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    posts: list[Post] = [
        (PARTS[0], 'channel_not_found', True),
        (PARTS[1], OK_RESULT, False),
    ]
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert once.exists()


@pytest.mark.parametrize(
    'error',
    [
        'Request TIMED OUT',
        'Connection reset by peer',
        '503 Service Unavailable',
        'deadline exceeded',
        '502 bad gateway',
        '504',
    ],
)
def test_transport_style_part_zero_error_is_outcome_unknown(
    cfg: AppConfig, tmp_path: Path, error: str
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, FakeRunner(_stream([(PARTS[0], error, True)])))
    assert once.exists()


def test_reply_posted_in_the_wrong_thread_is_missing(
    cfg: AppConfig, tmp_path: Path
) -> None:
    stream = _stream(_all_ok(), thread_ts={PARTS[1]: '1.1'})
    delivery = _deliver(cfg, tmp_path, FakeRunner(stream))
    assert delivery.missing_parts == (1,)


@pytest.mark.parametrize(
    'result', ['sent!', json.dumps({'ok': False, 'error': 'x'}), '{"ok": false}']
)
def test_reply_without_ts_or_with_ok_false_is_missing(
    cfg: AppConfig, tmp_path: Path, result: str
) -> None:
    failed = json.dumps(
        {'ok': False, 'message_context': {'message_ts': TS, 'channel_id': DM}}
    )
    posts: list[Post] = [
        (PARTS[0], OK_RESULT, False),
        (PARTS[1], result, False),
        (PARTS[2], failed, False),
    ]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert delivery.missing_parts == (1, 2)


def test_part_with_trailing_whitespace_still_counts_as_delivered(
    cfg: AppConfig, tmp_path: Path
) -> None:
    padded: list[Post] = [
        ('Main post  \r\nbody \n\n', OK_RESULT, False),
        ('Reply one  \n', OK_RESULT, False),
        ('Reply two\n\n', OK_RESULT, False),
    ]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(padded)))
    assert delivery.missing_parts == ()


def test_part_with_an_altered_word_is_not_a_delivery(
    cfg: AppConfig, tmp_path: Path
) -> None:
    posts: list[Post] = [(PARTS[0], OK_RESULT, False), ('Reply won', OK_RESULT, False)]
    delivery = _deliver(cfg, tmp_path, FakeRunner(_stream(posts)))
    assert delivery.missing_parts == (1, 2)


@pytest.mark.parametrize(
    'error',
    [
        'channel_not_found: D05031234',
        'invalid_auth request id 4f504a',
        'Your Slack connection does not have permission',
        'restricted_action reset',
    ],
)
def test_known_slack_error_codes_are_definitive(
    cfg: AppConfig, tmp_path: Path, error: str
) -> None:
    once = tmp_path / 'once'
    once.mkdir()
    (once / '0').touch()
    with pytest.raises(DeliveryError, match='Slack post failed') as exc:
        _deliver(cfg, tmp_path, FakeRunner(_stream([(PARTS[0], error, True)])))
    assert not isinstance(exc.value, DeliveryOutcomeUnknownError)
    assert not once.exists()


@pytest.mark.parametrize(
    'error',
    ['Request timed out', 'HTTP 503 Service Unavailable', 'connection reset by peer'],
)
def test_transport_errors_without_slack_code_are_outcome_unknown(
    cfg: AppConfig, tmp_path: Path, error: str
) -> None:
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, FakeRunner(_stream([(PARTS[0], error, True)])))


def test_main_result_with_ok_false_is_not_a_delivery(
    cfg: AppConfig, tmp_path: Path
) -> None:
    failed = json.dumps(
        {
            'ok': False,
            'message_link': LINK,
            'message_context': {'message_ts': TS, 'channel_id': DM},
        }
    )
    with pytest.raises(DeliveryOutcomeUnknownError):
        _deliver(cfg, tmp_path, FakeRunner(_stream([(PARTS[0], failed, False)])))
