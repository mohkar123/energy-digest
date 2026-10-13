import argparse
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from energy_digest.collect.newsletters import build_prompt

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / 'hooks' / 'guard_send.py'

_spec = importlib.util.spec_from_file_location('guard_send', GUARD_PATH)
assert _spec is not None
assert _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

SEND = 'mcp__claude_ai_Gmail__send_message'
SLACK = 'mcp__claude_ai_Slack__slack_send_message'
SEARCH = 'mcp__claude_ai_Gmail__search_threads'
FORWARD = 'mcp__claude_ai_Gmail__forward'
CHANNEL = 'D0000000001'
PARTS = ['main post\nwith table', 'thread part one', 'thread part two']
THREAD_TS = '1760000000.123456'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _args(**kw: str | None) -> argparse.Namespace:
    base: dict[str, str | None] = {
        'allow': SLACK,
        'gmail_senders': None,
        'slack_channels': CHANNEL,
        'part_sha256': ','.join(_sha(p) for p in PARTS),
        'once_dir': None,
    }
    return argparse.Namespace(**(base | kw))


def _slack(index: int = 0, **overrides: object) -> dict[str, Any]:
    tool_input: dict[str, object] = {'channel_id': CHANNEL, 'message': PARTS[index]}
    if index:
        tool_input['thread_ts'] = THREAD_TS
    return {'tool_name': SLACK, 'tool_input': tool_input | overrides}


def _post(index: int, args: argparse.Namespace, **overrides: object) -> str | None:
    return guard.evaluate(_slack(index, **overrides), args)


@pytest.fixture
def slack_args(tmp_path: Path) -> argparse.Namespace:
    return _args(once_dir=str(tmp_path / 'claims'))


def test_exact_parts_posted_in_order_allowed(slack_args: argparse.Namespace) -> None:
    for index in range(len(PARTS)):
        assert _post(index, slack_args) is None


def test_part_allowed_with_crlf_and_trailing_whitespace(
    slack_args: argparse.Namespace,
) -> None:
    message = PARTS[0].replace('\n', '\r\n') + '\r\n'
    assert _post(0, slack_args, message=message) is None


@pytest.mark.parametrize(
    'message',
    [
        'main post  \nwith table\t\n',
        'main post\r\nwith table  \r\n\r\n',
        '\n\nmain post\rwith table',
    ],
)
def test_part_matches_after_line_wise_normalisation(
    slack_args: argparse.Namespace, message: str
) -> None:
    assert _post(0, slack_args, message=message) is None


def test_part_with_an_altered_word_does_not_match(
    slack_args: argparse.Namespace,
) -> None:
    reason = _post(0, slack_args, message='main post  \nwith tables\n')
    assert reason is not None
    assert 'payload does not match' in reason


def test_claim_dir_created_private(
    slack_args: argparse.Namespace, tmp_path: Path
) -> None:
    assert _post(0, slack_args) is None
    claims = tmp_path / 'claims'
    assert (claims / '0').exists()
    assert claims.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ('overrides', 'fragment'),
    [
        ({'channel_id': 'C0OTHER'}, 'wrong channel'),
        ({'reply_broadcast': True}, 'unknown key'),
        ({'unfurl_links': True}, 'unknown key'),
        ({'draft_id': 'd1'}, 'unknown key'),
        ({'message': 'tampered'}, 'payload does not match'),
        ({'message': None}, 'payload mismatch'),
        ({'thread_ts': THREAD_TS}, 'thread_ts rule'),
    ],
)
def test_bad_part_zero_denied_without_claim(
    slack_args: argparse.Namespace,
    tmp_path: Path,
    overrides: dict[str, Any],
    fragment: str,
) -> None:
    reason = _post(0, slack_args, **overrides)
    assert reason is not None
    assert fragment in reason
    assert not (tmp_path / 'claims' / '0').exists()
    assert _post(0, slack_args) is None


@pytest.mark.parametrize('thread_ts', [None, ''])
def test_part_zero_with_empty_thread_ts_allowed(
    slack_args: argparse.Namespace, thread_ts: str | None
) -> None:
    assert _post(0, slack_args, thread_ts=thread_ts) is None


@pytest.mark.parametrize('key', ['reply_broadcast', 'unfurl_app_links', 'unfurl_links'])
@pytest.mark.parametrize('value', [False, None])
def test_falsy_optional_args_allowed(
    slack_args: argparse.Namespace, key: str, value: object
) -> None:
    assert _post(0, slack_args, **{key: value}) is None


@pytest.mark.parametrize('key', ['reply_broadcast', 'unfurl_app_links', 'unfurl_links'])
def test_truthy_optional_args_denied(slack_args: argparse.Namespace, key: str) -> None:
    reason = _post(0, slack_args, **{key: True})
    assert reason is not None
    assert key in reason


@pytest.mark.parametrize('value', [None, False, ''])
def test_draft_id_always_denied(slack_args: argparse.Namespace, value: object) -> None:
    reason = _post(0, slack_args, draft_id=value)
    assert reason is not None
    assert 'draft_id' in reason


RETRY_HINT = 'fix the arguments and retry this part once'


@pytest.mark.parametrize(
    'overrides',
    [
        {'channel_id': 'C0OTHER'},
        {'reply_broadcast': True},
        {'draft_id': 'd1'},
        {'thread_ts': THREAD_TS},
    ],
)
def test_bad_argument_denials_allow_one_corrected_retry(
    slack_args: argparse.Namespace, overrides: dict[str, Any]
) -> None:
    reason = _post(0, slack_args, **overrides)
    assert reason is not None
    assert RETRY_HINT in reason
    assert 'do not retry' not in reason


def test_out_of_order_and_bad_thread_ts_allow_retry(
    slack_args: argparse.Namespace,
) -> None:
    out_of_order = _post(1, slack_args)
    assert out_of_order is not None
    assert RETRY_HINT in out_of_order
    assert _post(0, slack_args) is None
    bad_ts = _post(1, slack_args, thread_ts='abc')
    assert bad_ts is not None
    assert RETRY_HINT in bad_ts


def test_payload_mismatch_and_already_posted_say_do_not_retry(
    slack_args: argparse.Namespace,
) -> None:
    mismatch = _post(0, slack_args, message='tampered')
    assert mismatch is not None
    assert 'do not retry' in mismatch
    assert RETRY_HINT not in mismatch
    assert _post(0, slack_args) is None
    again = _post(0, slack_args)
    assert again is not None
    assert 'do not retry' in again
    assert RETRY_HINT not in again


def test_part_one_before_part_zero_denied(slack_args: argparse.Namespace) -> None:
    reason = _post(1, slack_args)
    assert reason is not None
    assert 'out of order' in reason


def test_part_zero_twice_denied(slack_args: argparse.Namespace) -> None:
    assert _post(0, slack_args) is None
    reason = _post(0, slack_args)
    assert reason is not None
    assert 'already posted' in reason


def test_part_two_before_part_one_denied(slack_args: argparse.Namespace) -> None:
    assert _post(0, slack_args) is None
    reason = _post(2, slack_args)
    assert reason is not None
    assert 'out of order' in reason


@pytest.mark.parametrize('thread_ts', [None, 'abc', '123', '1760000000.', 12.5])
def test_later_part_needs_valid_thread_ts(
    slack_args: argparse.Namespace, thread_ts: object
) -> None:
    assert _post(0, slack_args) is None
    payload = _slack(1)
    del payload['tool_input']['thread_ts']
    if thread_ts is not None:
        payload['tool_input']['thread_ts'] = thread_ts
    reason = guard.evaluate(payload, slack_args)
    assert reason is not None
    assert 'thread_ts rule' in reason


@pytest.mark.parametrize('missing', ['slack_channels', 'part_sha256', 'once_dir'])
def test_missing_guard_args_denied(tmp_path: Path, missing: str) -> None:
    args = _args(**({'once_dir': str(tmp_path / 'claims')} | {missing: None}))
    reason = _post(0, args)
    assert reason is not None
    assert 'requires' in reason
    assert not (tmp_path / 'claims').exists()


def test_slack_post_not_allowlisted_denied(slack_args: argparse.Namespace) -> None:
    slack_args.allow = SEARCH
    assert _post(0, slack_args) == f'tool {SLACK} not allowlisted'


def test_gmail_send_always_denied(slack_args: argparse.Namespace) -> None:
    slack_args.allow = SEND
    payload = {
        'tool_name': SEND,
        'tool_input': {'to': ['me@example.com'], 'subject': 's', 'body': 'b'},
    }
    assert guard.evaluate(payload, slack_args) == 'email sending is disabled'


SENDERS = 'a@x.com,b@y.com,c@z.org'


def _search(query: object) -> dict[str, Any]:
    return {'tool_name': SEARCH, 'tool_input': {'query': query, 'pageSize': 30}}


def _search_args() -> argparse.Namespace:
    return _args(allow=SEARCH, gmail_senders=SENDERS)


@pytest.mark.parametrize(
    'query',
    [
        'from:(a@x.com OR b@y.com OR c@z.org) after:2026/10/07',
        'from:(a@x.com)',
        'from:(C@Z.ORG OR A@x.com)',
        '  from:(a@x.com   OR b@y.com)\n after:2026/10/07 ',
    ],
)
def test_search_with_allowed_senders_passes(query: str) -> None:
    assert guard.evaluate(_search(query), _search_args()) is None


@pytest.mark.parametrize(
    'query',
    [
        'from:(a@x.com OR evil@example.com) after:2026/10/07',
        'from:(evil@example.com)',
        'from:(a@x.com) in:anywhere',
        'from:(a@x.com) subject:invoice',
        'from:(a@x.com) OR subject:invoice',
        'after:2026/10/07',
        'a@x.com',
        'from:(a@x.com) after:2026/10/07 after:2026/10/08',
        'from:(a@x.com) after:yesterday',
        'from:(a@x.com OR )',
        '',
        None,
    ],
)
def test_search_outside_grammar_denied(query: object) -> None:
    assert guard.evaluate(_search(query), _search_args()) is not None


def test_search_denial_names_allowed_senders() -> None:
    reason = guard.evaluate(_search('in:anywhere'), _search_args())
    assert reason is not None
    assert 'a@x.com' in reason


def test_newsletter_prompt_query_satisfies_the_guard() -> None:
    senders = ['a@x.com', 'b@y.com', 'c@z.org']
    prompt = build_prompt(senders, date(2026, 10, 7))
    query = re.search(r'query `([^`]+)`', prompt)
    assert query is not None
    assert guard.evaluate(_search(query.group(1)), _search_args()) is None


def test_search_denied_without_gmail_senders_arg() -> None:
    reason = guard.evaluate(_search('anything'), _args(allow=SEARCH))
    assert reason is not None
    assert 'no senders configured' in reason


def test_subprocess_enforces_gmail_senders() -> None:
    def run(query: str) -> int:
        payload = json.dumps(_search(query))
        return _run(payload, '--allow', SEARCH, '--gmail-senders', SENDERS).returncode

    assert run('from:(a@x.com) after:2026/10/07') == 0
    assert run('in:anywhere') == 2


def test_unlisted_tool_denied() -> None:
    reason = guard.evaluate({'tool_name': FORWARD, 'tool_input': {}}, _args())
    assert reason == f'tool {FORWARD} not allowlisted'


def _run(payload: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(GUARD_PATH), *argv],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def test_subprocess_denies_unlisted_tool() -> None:
    proc = _run(json.dumps({'tool_name': 'y', 'tool_input': {}}), '--allow', 'x')
    assert proc.returncode == 2
    assert 'not allowlisted' in proc.stderr


def test_subprocess_denies_with_exit_2_when_stderr_is_closed() -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(GUARD_PATH), '--allow', 'x'],
        input=json.dumps({'tool_name': 'y', 'tool_input': {}}),
        stderr=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


def test_deny_exit_survives_a_failing_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_print(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(sys, 'argv', ['guard_send.py', '--allow', 'x'])
    monkeypatch.setattr(
        sys, 'stdin', io.StringIO(json.dumps({'tool_name': 'y', 'tool_input': {}}))
    )
    monkeypatch.setattr('builtins.print', broken_print)
    assert guard.main() == 2


def test_subprocess_allows_listed_tool() -> None:
    proc = _run(json.dumps({'tool_name': 'x', 'tool_input': {}}), '--allow', 'x')
    assert proc.returncode == 0


def test_subprocess_denies_unparseable_payload() -> None:
    proc = _run('not json', '--allow', 'x')
    assert proc.returncode == 2


def _slack_argv(once_dir: Path) -> list[str]:
    return [
        '--allow',
        SLACK,
        '--slack-channels',
        CHANNEL,
        '--part-sha256',
        ','.join(_sha(p) for p in PARTS),
        '--once-dir',
        str(once_dir),
    ]


def test_subprocess_allow_prints_explicit_decision() -> None:
    proc = _run(json.dumps({'tool_name': 'x', 'tool_input': {}}), '--allow', 'x')
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'allow',
        }
    }


def test_subprocess_lone_surrogate_exits_2(tmp_path: Path) -> None:
    payload = json.dumps(_slack(0, message='<p>\ud800</p>'))
    proc = _run(payload, *_slack_argv(tmp_path / 'claims'))
    assert proc.returncode == 2
    assert proc.stderr


def test_subprocess_non_dict_tool_input_exits_2(tmp_path: Path) -> None:
    payload = json.dumps({'tool_name': SLACK, 'tool_input': 'nope'})
    assert _run(payload, *_slack_argv(tmp_path / 'claims')).returncode == 2


def test_subprocess_non_dict_payload_exits_2() -> None:
    assert _run('[1, 2]', '--allow', 'x').returncode == 2


def test_subprocess_invalid_json_exits_2(tmp_path: Path) -> None:
    assert _run('{"tool_name":', *_slack_argv(tmp_path / 'claims')).returncode == 2


def test_subprocess_bad_arguments_exit_2() -> None:
    assert _run('{}', '--bogus').returncode == 2


def test_subprocess_crash_in_evaluation_exits_2(tmp_path: Path) -> None:
    payload = json.dumps({'tool_name': SLACK, 'tool_input': {'channel_id': [{}]}})
    assert _run(payload, *_slack_argv(tmp_path / 'claims')).returncode == 2


def test_subprocess_unwritable_claim_dir_exits_2(tmp_path: Path) -> None:
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    payload = json.dumps(_slack(0))
    assert _run(payload, *_slack_argv(blocker / 'claims')).returncode == 2


def test_subprocess_search_without_senders_denied() -> None:
    proc = _run(json.dumps(_search('anything')), '--allow', SEARCH)
    assert proc.returncode == 2
    assert 'no senders configured' in proc.stderr


def test_subprocess_posts_parts_once_in_order(tmp_path: Path) -> None:
    argv = _slack_argv(tmp_path / 'claims')
    assert _run(json.dumps(_slack(1)), *argv).returncode == 2
    first = _run(json.dumps(_slack(0)), *argv)
    assert first.returncode == 0
    assert json.loads(first.stdout)['hookSpecificOutput']['permissionDecision'] == (
        'allow'
    )
    again = _run(json.dumps(_slack(0)), *argv)
    assert again.returncode == 2
    assert 'already posted' in again.stderr
    assert _run(json.dumps(_slack(1)), *argv).returncode == 0


def test_subprocess_gmail_send_denied() -> None:
    payload = json.dumps({'tool_name': SEND, 'tool_input': {}})
    proc = _run(payload, '--allow', SEND)
    assert proc.returncode == 2
    assert 'email sending is disabled' in proc.stderr


def test_subprocess_unknown_option_exits_2() -> None:
    assert _run('{}', '--allow', 'x', '--to', 'me@example.com').returncode == 2


def test_hash_mismatch_denial_says_do_not_retry(tmp_path: Path) -> None:
    args = _args(once_dir=str(tmp_path / 'claims'))
    reason = _post(0, args, message='something else')
    assert reason is not None
    assert 'payload does not match the rendered part; do not retry' in reason


def test_identical_parts_claim_lowest_unclaimed_index(tmp_path: Path) -> None:
    same = ['same text', 'same text']
    args = _args(
        part_sha256=','.join(_sha(p) for p in same), once_dir=str(tmp_path / 'claims')
    )
    first = {
        'tool_name': SLACK,
        'tool_input': {'channel_id': CHANNEL, 'message': 'same text'},
    }
    reply = {
        'tool_name': SLACK,
        'tool_input': {
            'channel_id': CHANNEL,
            'message': 'same text',
            'thread_ts': THREAD_TS,
        },
    }
    assert guard.evaluate(first, args) is None
    assert (tmp_path / 'claims' / '0').exists()
    assert guard.evaluate(reply, args) is None
    assert (tmp_path / 'claims' / '1').exists()
    assert 'already posted' in (guard.evaluate(reply, args) or '')
