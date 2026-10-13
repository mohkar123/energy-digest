import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from energy_digest import claude_cli
from energy_digest.claude_cli import (
    GMAIL_TOOLS,
    SLACK_SEND_TOOL,
    SLACK_TOOLS,
    ClaudeError,
    ClaudeRequest,
    Profile,
    ToolEvent,
    build_argv,
    guard_script_path,
    run_claude,
    subprocess_runner,
)
from energy_digest.config import ClaudeCfg

SEARCH = 'mcp__claude_ai_Gmail__search_threads'
SEND = 'mcp__claude_ai_Gmail__send_message'
CFG = ClaudeCfg(disallow_mcp=['mcp__claude_ai_Atlassian'])


def _req(**kw: object) -> ClaudeRequest:
    base: dict[str, Any] = {
        'prompt': 'hi',
        'system_prompt': 'sys',
        'model': 'haiku',
        'profile': Profile.ISOLATED,
        'max_budget_usd': 0.5,
        'timeout_s': 30,
    }
    base.update(kw)
    return ClaudeRequest(**base)


class FakeRunner:
    def __init__(
        self, rc: int, payload: str | dict[str, Any], stderr: str = ''
    ) -> None:
        self.rc = rc
        self.stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.stderr = stderr
        self.argv: list[str] = []
        self.stdin = ''
        self.env: Mapping[str, str] = {}

    def __call__(
        self,
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        self.argv, self.stdin, self.env = argv, stdin_text, env
        return self.rc, self.stdout, self.stderr


def _ok(**kw: object) -> dict[str, Any]:
    return {
        'type': 'result',
        'subtype': 'success',
        'is_error': False,
        'result': 'done',
        'total_cost_usd': 0.12,
        'duration_ms': 900,
        'permission_denials': [],
    } | kw


def _flag_values(argv: list[str], flag: str) -> list[str]:
    i = argv.index(flag) + 1
    out = []
    while i < len(argv) and not argv[i].startswith('--'):
        out.append(argv[i])
        i += 1
    return out


def test_gmail_tools_count() -> None:
    assert len(GMAIL_TOOLS) == 30
    assert len(set(GMAIL_TOOLS)) == 30


def test_isolated_argv() -> None:
    argv = build_argv(_req(), CFG, Path('g.py'), 'py')
    assert '--strict-mcp-config' in argv
    assert argv[argv.index('--tools') + 1] == ''
    assert argv[argv.index('--setting-sources') + 1] == ''
    assert '--disable-slash-commands' in argv
    assert '--settings' not in argv
    assert argv[argv.index('--permission-mode') + 1] == 'dontAsk'


def test_optional_flags() -> None:
    schema = {'type': 'object'}
    argv = build_argv(_req(json_schema=schema, max_turns=3), CFG, Path('g.py'), 'py')
    assert json.loads(argv[argv.index('--json-schema') + 1]) == schema
    assert argv[argv.index('--max-turns') + 1] == '3'


def test_gmail_argv() -> None:
    req = _req(
        profile=Profile.GMAIL,
        allowed_mcp_tools=(SEARCH,),
        guard_args=('--to', 'a@b.com'),
    )
    argv = build_argv(req, CFG, Path('/x/guard.py'), '/venv/python')
    assert '--strict-mcp-config' not in argv
    assert '--setting-sources' not in argv
    assert _flag_values(argv, '--allowedTools') == [SEARCH]
    denied = _flag_values(argv, '--disallowedTools')
    assert SEND in denied
    assert SEARCH not in denied
    assert 'mcp__claude_ai_Atlassian' in denied
    assert set(SLACK_TOOLS) <= set(denied)
    settings = json.loads(argv[argv.index('--settings') + 1])
    entry = settings['hooks']['PreToolUse'][0]
    assert entry['matcher'] == 'mcp__.*'
    command = entry['hooks'][0]['command']
    assert '--allow' in command
    assert '/x/guard.py' in command
    assert 'a@b.com' in command


@pytest.mark.parametrize(
    'req',
    [
        _req(profile=Profile.GMAIL),
        _req(profile=Profile.ISOLATED, allowed_mcp_tools=(SEARCH,)),
    ],
)
def test_profile_tool_mismatch_raises(req: ClaudeRequest) -> None:
    with pytest.raises(ValueError, match='profile'):
        build_argv(req, CFG, Path('g.py'), 'py')


def test_env_strips_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        'ANTHROPIC_API_KEY',
        'ANTHROPIC_AUTH_TOKEN',
        'CLAUDE_CODE_OAUTH_TOKEN',
    ):
        monkeypatch.setenv(name, 'secret')
    monkeypatch.setenv('KEEP_ME', '1')
    runner = FakeRunner(0, _ok())
    run_claude(_req(), CFG, cwd=tmp_path, runner=runner)
    assert 'ANTHROPIC_API_KEY' not in runner.env
    assert 'ANTHROPIC_AUTH_TOKEN' not in runner.env
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in runner.env
    assert runner.env['KEEP_ME'] == '1'
    assert runner.stdin == 'hi'


def test_budget_error_carries_subtype(tmp_path: Path) -> None:
    payload = _ok(subtype='error_max_budget_usd', is_error=True, errors=['over budget'])
    with pytest.raises(ClaudeError, match='over budget') as exc:
        run_claude(_req(), CFG, cwd=tmp_path, runner=FakeRunner(1, payload))
    assert exc.value.subtype == 'error_max_budget_usd'
    assert exc.value.raw == payload


def test_missing_structured_output_raises(tmp_path: Path) -> None:
    with pytest.raises(ClaudeError, match='missing structured_output'):
        run_claude(
            _req(json_schema={'type': 'object'}),
            CFG,
            cwd=tmp_path,
            runner=FakeRunner(0, _ok()),
        )


def test_non_json_stdout_raises(tmp_path: Path) -> None:
    with pytest.raises(ClaudeError, match='claude exited 1: boom'):
        run_claude(_req(), CFG, cwd=tmp_path, runner=FakeRunner(1, 'nope', 'boom'))


def test_happy_path(tmp_path: Path) -> None:
    payload = _ok(
        structured_output={'a': 1},
        permission_denials=[{'tool_name': 'x'}],
    )
    result = run_claude(
        _req(json_schema={'type': 'object'}),
        CFG,
        cwd=tmp_path,
        runner=FakeRunner(0, payload),
    )
    assert result.structured == {'a': 1}
    assert result.cost_usd == 0.12
    assert result.duration_ms == 900
    assert result.text == 'done'
    assert result.permission_denials == [{'tool_name': 'x'}]


def test_missing_cost_defaults_to_zero(tmp_path: Path) -> None:
    payload = _ok()
    del payload['total_cost_usd']
    result = run_claude(_req(), CFG, cwd=tmp_path, runner=FakeRunner(0, payload))
    assert result.cost_usd == 0.0


def test_null_cost_defaults_to_zero(tmp_path: Path) -> None:
    payload = _ok(total_cost_usd=None)
    result = run_claude(_req(), CFG, cwd=tmp_path, runner=FakeRunner(0, payload))
    assert result.cost_usd == 0.0


@pytest.mark.parametrize('structured', ['text', [1, 2], 5])
def test_non_dict_structured_output_raises(tmp_path: Path, structured: object) -> None:
    payload = _ok(structured_output=structured)
    with pytest.raises(ClaudeError, match='structured_output is not an object'):
        run_claude(
            _req(json_schema={'type': 'object'}),
            CFG,
            cwd=tmp_path,
            runner=FakeRunner(0, payload),
        )


@pytest.mark.parametrize('stdout', ['[1, 2]', '"text"', '7'])
def test_non_dict_stdout_json_raises(tmp_path: Path, stdout: str) -> None:
    with pytest.raises(ClaudeError, match='unexpected output'):
        run_claude(_req(), CFG, cwd=tmp_path, runner=FakeRunner(0, stdout))


def test_guard_script_path_points_at_the_hook() -> None:
    path = guard_script_path()
    assert path.name == 'guard_send.py'
    assert path.is_file()


def test_send_tool_matches_guard_constant() -> None:
    spec = importlib.util.spec_from_file_location('guard_send', guard_script_path())
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SEND_TOOL == claude_cli.SEND_TOOL
    assert module.SLACK_SEND_TOOL == claude_cli.SLACK_SEND_TOOL


_PRINT_PID_THEN_SLEEP = (
    'import os, sys, time\nprint(os.getpid(), flush=True)\ntime.sleep(30)\n'
)


def _assert_group_dead(pgid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def _run_child(code: str, timeout_s: int) -> tuple[int, str, str]:
    return subprocess_runner(
        [sys.executable, '-c', code], '', Path.cwd(), os.environ, timeout_s
    )


def test_runner_timeout_returns_124_and_kills_group() -> None:
    start = time.monotonic()
    rc, stdout, stderr = _run_child(_PRINT_PID_THEN_SLEEP, timeout_s=1)
    assert rc == 124
    assert time.monotonic() - start < 8
    assert stderr.endswith('[timeout]')
    _assert_group_dead(int(stdout.split()[0]))


def test_runner_escalates_to_sigkill_when_sigint_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_cli, '_KILL_GRACE_S', 1)
    code = 'import signal\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n' + (
        _PRINT_PID_THEN_SLEEP
    )
    rc, stdout, _ = _run_child(code, timeout_s=1)
    assert rc == 124
    _assert_group_dead(int(stdout.split()[0]))


def test_runner_kills_group_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    pids: list[int] = []

    def interrupted(self: subprocess.Popen[str], *_a: object, **_k: object) -> None:
        pids.append(self.pid)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, 'communicate', interrupted)
    with pytest.raises(KeyboardInterrupt):
        _run_child(_PRINT_PID_THEN_SLEEP, timeout_s=30)
    _assert_group_dead(pids[0])


def test_runner_returns_output_on_success() -> None:
    rc, stdout, stderr = _run_child(
        'import sys; print("out"); print("err", file=sys.stderr)', timeout_s=10
    )
    assert (rc, stdout.strip(), stderr.strip()) == (0, 'out', 'err')


def _jsonl(*events: dict[str, Any] | str) -> str:
    return '\n'.join(e if isinstance(e, str) else json.dumps(e) for e in events)


def _use(use_id: str, name: str, **tool_input: object) -> dict[str, Any]:
    content = [{'type': 'tool_use', 'id': use_id, 'name': name, 'input': tool_input}]
    return {'type': 'assistant', 'message': {'content': content}}


def _tool_result(use_id: str, content: object, **extra: object) -> dict[str, Any]:
    item = {
        'type': 'tool_result',
        'tool_use_id': use_id,
        'content': content,
        **extra,
    }
    return {'type': 'user', 'message': {'content': [item]}}


RESULT_LINE = {
    'type': 'result',
    'subtype': 'success',
    'is_error': False,
    'result': 'done',
    'total_cost_usd': 0.02,
    'duration_ms': 7,
}


def test_stream_flag_selects_stream_json_output() -> None:
    cfg = ClaudeCfg(disallow_mcp=[])
    argv = build_argv(_req(stream=True), cfg, Path('/g.py'), 'python')
    start = argv.index('--output-format')
    assert argv[start + 1 : start + 3] == ['stream-json', '--verbose']
    plain = build_argv(_req(), cfg, Path('/g.py'), 'python')
    assert plain[plain.index('--output-format') + 1] == 'json'
    assert '--verbose' not in plain


def test_stream_pairs_tool_use_with_result_by_id(tmp_path: Path) -> None:
    stdout = _jsonl(
        {'type': 'system', 'subtype': 'init'},
        _use('t1', SEND, to=['a@b.c']),
        _use('t2', SEARCH, q='x'),
        _tool_result(
            't2', [{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}]
        ),
        _tool_result('t1', 'Insufficient scope', is_error=True),
        RESULT_LINE,
    )
    result = run_claude(
        _req(stream=True), CFG, cwd=tmp_path, runner=FakeRunner(0, stdout)
    )
    assert result.tool_events == [
        ToolEvent(SEND, {'to': ['a@b.c']}, True, 'Insufficient scope'),
        ToolEvent(SEARCH, {'q': 'x'}, False, 'one\ntwo'),
    ]
    assert result.text == 'done'
    assert result.cost_usd == pytest.approx(0.02)
    assert result.raw['subtype'] == 'success'


def test_stream_ignores_non_json_lines(tmp_path: Path) -> None:
    stdout = _jsonl(
        'warning: not json', _use('t1', SEND), '[1, 2]', RESULT_LINE, '{oops'
    )
    result = run_claude(
        _req(stream=True), CFG, cwd=tmp_path, runner=FakeRunner(0, stdout)
    )
    assert [e.name for e in result.tool_events] == [SEND]
    assert result.tool_events[0].content_text == ''


def test_stream_without_result_line_raises(tmp_path: Path) -> None:
    stdout = _jsonl(_use('t1', SEND))
    with pytest.raises(ClaudeError, match='without a result'):
        run_claude(_req(stream=True), CFG, cwd=tmp_path, runner=FakeRunner(1, stdout))


def test_stream_error_result_keeps_tool_events_on_the_error(tmp_path: Path) -> None:
    failed = {**RESULT_LINE, 'subtype': 'error_max_turns', 'is_error': True}
    stdout = _jsonl(_use('t1', SEND), _tool_result('t1', 'boom', is_error=True), failed)
    with pytest.raises(ClaudeError) as exc:
        run_claude(_req(stream=True), CFG, cwd=tmp_path, runner=FakeRunner(1, stdout))
    assert exc.value.subtype == 'error_max_turns'
    assert [e.is_error for e in exc.value.tool_events] == [True]


def test_non_stream_run_has_no_tool_events(tmp_path: Path) -> None:
    result = run_claude(_req(), CFG, cwd=tmp_path, runner=FakeRunner(0, RESULT_LINE))
    assert result.tool_events == []


def test_slack_send_tool_is_the_only_slack_tool_left_open() -> None:
    req = _req(profile=Profile.GMAIL, allowed_mcp_tools=(SLACK_SEND_TOOL,))
    denied = _flag_values(build_argv(req, CFG, Path('g.py'), 'py'), '--disallowedTools')
    assert SLACK_SEND_TOOL not in denied
    assert set(SLACK_TOOLS) - {SLACK_SEND_TOOL} <= set(denied)
    assert set(GMAIL_TOOLS) <= set(denied)
