"""Locked-down wrapper around the headless ``claude -p`` CLI."""

import contextlib
import enum
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from energy_digest.config import ClaudeCfg

logger = logging.getLogger(__name__)

_GMAIL_PREFIX = 'mcp__claude_ai_Gmail__'
_GMAIL_TOOL_NAMES = (
    'apply_sensitive_message_label',
    'apply_sensitive_thread_label',
    'create_draft',
    'create_label',
    'delete_draft',
    'delete_label',
    'forward',
    'get_draft',
    'get_message',
    'get_thread',
    'label_message',
    'label_thread',
    'list_drafts',
    'list_labels',
    'mark_message_spam',
    'mark_thread_spam',
    'reply',
    'search_threads',
    'send_message',
    'trash_message',
    'trash_thread',
    'unlabel_message',
    'unlabel_thread',
    'unmark_message_spam',
    'unmark_thread_spam',
    'untrash_message',
    'untrash_thread',
    'update_draft',
    'update_label',
    'update_message_labels',
)
SEND_TOOL = f'{_GMAIL_PREFIX}send_message'
GMAIL_TOOLS: tuple[str, ...] = tuple(f'{_GMAIL_PREFIX}{n}' for n in _GMAIL_TOOL_NAMES)

_SLACK_PREFIX = 'mcp__claude_ai_Slack__'
_SLACK_TOOL_NAMES = (
    'slack_create_canvas',
    'slack_read_canvas',
    'slack_read_channel',
    'slack_read_thread',
    'slack_read_user_profile',
    'slack_schedule_message',
    'slack_search_channels',
    'slack_search_public',
    'slack_search_public_and_private',
    'slack_search_users',
    'slack_send_message',
    'slack_send_message_draft',
    'slack_update_canvas',
)
SLACK_SEND_TOOL = f'{_SLACK_PREFIX}slack_send_message'
SLACK_TOOLS: tuple[str, ...] = tuple(f'{_SLACK_PREFIX}{n}' for n in _SLACK_TOOL_NAMES)

# Credentials that would switch claude off the claude.ai login (which the
# Gmail connector needs) or bill a stray key.
_STRIPPED_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN')

_TIMEOUT_RC = 124
_KILL_GRACE_S = 10
_INTERRUPT_GRACE_S = 5
_FINAL_READ_S = 10


class Profile(enum.Enum):
    """How much of the claude environment a run may see."""

    ISOLATED = 'isolated'  # no MCP, no user settings, no built-in tools
    GMAIL = 'gmail'  # claude.ai Gmail connector only, guarded


@dataclass(frozen=True)
class ClaudeRequest:
    """One claude invocation."""

    prompt: str  # sent on stdin
    system_prompt: str
    model: str
    profile: Profile
    max_budget_usd: float
    timeout_s: int
    json_schema: dict[str, Any] | None = None
    allowed_mcp_tools: tuple[str, ...] = ()  # GMAIL only
    guard_args: tuple[str, ...] = ()  # extra args for hooks/guard_send.py
    max_turns: int | None = None
    # stream-json output, so tool calls and their results are observable
    stream: bool = False


@dataclass(frozen=True)
class ToolEvent:
    """One tool call paired with the result the tool returned."""

    name: str
    input: dict[str, Any]
    is_error: bool
    content_text: str


@dataclass(frozen=True)
class ClaudeResult:
    """Parsed successful claude response."""

    structured: dict[str, Any] | None
    text: str
    cost_usd: float
    duration_ms: int
    permission_denials: list[dict[str, Any]]
    raw: dict[str, Any]
    tool_events: list[ToolEvent] = field(default_factory=list)


class ClaudeError(RuntimeError):
    """A claude run failed; carries the result subtype and raw payload if known."""

    def __init__(
        self,
        message: str,
        *,
        subtype: str | None = None,
        raw: dict[str, Any] | None = None,
        tool_events: list[ToolEvent] | None = None,
    ) -> None:
        """Store the failure subtype, raw payload and observed tool events."""
        super().__init__(message)
        self.subtype = subtype
        self.raw = raw
        self.tool_events = tool_events or []


# (argv, stdin_text, cwd, env, timeout_s) -> (returncode, stdout, stderr)
Runner = Callable[[list[str], str, Path, Mapping[str, str], int], tuple[int, str, str]]


def build_argv(
    req: ClaudeRequest, cfg: ClaudeCfg, guard_script: Path, python_exe: str
) -> list[str]:
    """Build the claude argv for ``req`` under the least-privilege profile."""
    if req.profile is Profile.GMAIL and not req.allowed_mcp_tools:
        msg = 'GMAIL profile requires allowed_mcp_tools'
        raise ValueError(msg)
    if req.profile is Profile.ISOLATED and req.allowed_mcp_tools:
        msg = 'ISOLATED profile cannot allow MCP tools'
        raise ValueError(msg)

    argv = [
        cfg.binary,
        '-p',
        '--output-format',
        *(('stream-json', '--verbose') if req.stream else ('json',)),
        '--no-session-persistence',
        '--model',
        req.model,
        '--tools',
        '',
        '--permission-mode',
        'dontAsk',
        '--system-prompt',
        req.system_prompt,
        '--max-budget-usd',
        str(req.max_budget_usd),
    ]
    if req.max_turns is not None:
        argv += ['--max-turns', str(req.max_turns)]
    if req.json_schema is not None:
        argv += ['--json-schema', json.dumps(req.json_schema)]

    if req.profile is Profile.ISOLATED:
        argv += [
            '--strict-mcp-config',
            '--setting-sources',
            '',
            '--disable-slash-commands',
        ]
        return argv

    # GMAIL: --strict-mcp-config / --setting-sources '' would hide the claude.ai
    # connectors, so restrict through allow/deny lists and a PreToolUse hook.
    allowed = req.allowed_mcp_tools
    # The Slack server is reachable (delivery needs it), so every Slack tool
    # not explicitly allowed is denied by name.
    denied = [
        *cfg.disallow_mcp,
        *(t for t in (*GMAIL_TOOLS, *SLACK_TOOLS) if t not in allowed),
    ]
    guard_cmd = shlex.join(
        [python_exe, str(guard_script), '--allow', ','.join(allowed), *req.guard_args]
    )
    settings = {
        'hooks': {
            'PreToolUse': [
                {
                    'matcher': 'mcp__.*',
                    'hooks': [{'type': 'command', 'command': guard_cmd}],
                }
            ]
        }
    }
    argv += [
        '--allowedTools',
        *allowed,
        '--disallowedTools',
        *denied,
        '--settings',
        json.dumps(settings),
    ]
    return argv


def _killpg(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):  # group already gone
        os.killpg(pid, sig)


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value or ''


def _stop_group(proc: 'subprocess.Popen[str]') -> None:
    """Interrupt then kill the child's process group and reap the child."""
    _killpg(proc.pid, signal.SIGINT)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_INTERRUPT_GRACE_S)
    # always follow up: the leader may have exited while group members linger
    _killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _collect_after_timeout(proc: 'subprocess.Popen[str]') -> tuple[str, str]:
    """Interrupt a timed-out child, escalating to SIGKILL, and drain its output."""
    _killpg(proc.pid, signal.SIGINT)
    try:
        stdout, stderr = proc.communicate(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        _killpg(proc.pid, signal.SIGKILL)
        try:
            stdout, stderr = proc.communicate(timeout=_FINAL_READ_S)
        except subprocess.TimeoutExpired as exc:
            # a surviving grandchild may hold the pipes open; keep what we have
            stdout, stderr = _text(exc.stdout), _text(exc.stderr)
    _killpg(proc.pid, signal.SIGKILL)
    return stdout or '', stderr or ''


def subprocess_runner(
    argv: list[str],
    stdin_text: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: int,
) -> tuple[int, str, str]:
    """Run argv, killing the whole process group on timeout or interruption."""
    proc = subprocess.Popen(  # noqa: S603 - argv is built by build_argv
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=dict(env),
        text=True,
        encoding='utf-8',
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        stdout, stderr = _collect_after_timeout(proc)
        return _TIMEOUT_RC, stdout, f'{stderr} [timeout]'.strip()
    except BaseException:
        # KeyboardInterrupt or SIGINT from systemd must not orphan claude
        _stop_group(proc)
        raise
    return proc.returncode, stdout, stderr


def guard_script_path() -> Path:
    """Path of the PreToolUse guard hook shipped in this repository."""
    return Path(__file__).resolve().parents[2] / 'hooks' / 'guard_send.py'


def _flatten_content(content: object) -> str:
    """Join the text of a tool_result ``content`` (a string or a list of items)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(
        item['text']
        for item in content
        if isinstance(item, dict)
        and item.get('type') == 'text'
        and isinstance(item.get('text'), str)
    )


def _content_items(message: object) -> list[dict[str, Any]]:
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _record_calls(
    event: dict[str, Any], calls: dict[str, tuple[str, dict[str, Any]]]
) -> None:
    for item in _content_items(event.get('message')):
        use_id = item.get('id')
        if item.get('type') == 'tool_use' and isinstance(use_id, str):
            tool_input = item.get('input')
            calls[use_id] = (
                str(item.get('name', '')),
                tool_input if isinstance(tool_input, dict) else {},
            )


def _record_results(
    event: dict[str, Any], results: dict[str, tuple[bool, str]]
) -> None:
    for item in _content_items(event.get('message')):
        if item.get('type') == 'tool_result':
            results[str(item.get('tool_use_id'))] = (
                bool(item.get('is_error')),
                _flatten_content(item.get('content')),
            )


def _parse_stream(stdout: str) -> tuple[dict[str, Any] | None, list[ToolEvent]]:
    """Parse ``stream-json`` JSONL into the final result line and tool events.

    Lines that are not JSON objects are ignored. Each assistant ``tool_use`` is
    paired with the user ``tool_result`` carrying the same ``tool_use_id``; a
    call whose result never arrived is reported as a non-error with no content.
    """
    raw: dict[str, Any] | None = None
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    results: dict[str, tuple[bool, str]] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'result':
            raw = event
        elif kind == 'assistant':
            _record_calls(event, calls)
        elif kind == 'user':
            _record_results(event, results)
    events = [
        ToolEvent(name, tool_input, *results.get(use_id, (False, '')))
        for use_id, (name, tool_input) in calls.items()
    ]
    return raw, events


def run_claude(
    req: ClaudeRequest,
    cfg: ClaudeCfg,
    *,
    cwd: Path,
    runner: Runner = subprocess_runner,
) -> ClaudeResult:
    """Run claude headless and return the parsed result or raise ClaudeError."""
    argv = build_argv(req, cfg, guard_script_path(), sys.executable)
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    rc, stdout, stderr = runner(argv, req.prompt, cwd, env, req.timeout_s)

    tool_events: list[ToolEvent] = []
    if req.stream:
        raw, tool_events = _parse_stream(stdout)
        if raw is None:
            msg = f'claude exited {rc} without a result: {stderr[:500]}'
            raise ClaudeError(msg, tool_events=tool_events)
    else:
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            msg = f'claude exited {rc}: {stderr[:500]}'
            raise ClaudeError(msg) from exc
        if not isinstance(raw, dict):
            msg = f'claude exited {rc}: unexpected output {stdout[:200]!r}'
            raise ClaudeError(msg)

    subtype = raw.get('subtype')
    if subtype != 'success' or raw.get('is_error'):
        detail = '; '.join(str(e) for e in raw.get('errors') or [])
        msg = f'claude failed ({subtype}): {detail or raw.get("result", "")}'
        raise ClaudeError(msg, subtype=subtype, raw=raw, tool_events=tool_events)

    structured = raw.get('structured_output')
    if req.json_schema is not None and structured is None:
        msg = 'missing structured_output'
        raise ClaudeError(msg, subtype=subtype, raw=raw, tool_events=tool_events)
    if structured is not None and not isinstance(structured, dict):
        msg = f'structured_output is not an object: {type(structured).__name__}'
        raise ClaudeError(msg, subtype=subtype, raw=raw, tool_events=tool_events)

    result = ClaudeResult(
        structured=structured,
        text=raw.get('result') or '',
        cost_usd=float(raw.get('total_cost_usd') or 0),
        duration_ms=int(raw.get('duration_ms', 0)),
        permission_denials=list(raw.get('permission_denials') or []),
        raw=raw,
        tool_events=tool_events,
    )
    logger.info(
        'claude run finished',
        extra={
            'digest.model': req.model,
            'digest.profile': req.profile.value,
            'digest.cost_usd': result.cost_usd,
            'digest.duration_ms': result.duration_ms,
            'digest.permission_denials': len(result.permission_denials),
        },
    )
    return result
