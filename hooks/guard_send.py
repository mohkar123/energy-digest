"""PreToolUse guard for headless claude runs (stdlib only).

Usage (as a hook command):
    guard_send.py --allow t1,t2 [--gmail-senders a,b,c]
                  [--slack-channels C1,C2] [--part-sha256 H0,H1,...]
                  [--once-dir DIR]

Reads the hook payload JSON on stdin. Exits 0 to allow, 2 to deny with the
reason on stderr. Claude Code only blocks a tool call on exit code 2, so every
failure path (including unexpected exceptions) must exit 2.

Slack posts are limited to the exact pre-rendered parts, each once and in
order: ``--part-sha256`` lists the expected part hashes and ``--once-dir``
holds one O_EXCL claim file per posted part.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SEND_TOOL = 'mcp__claude_ai_Gmail__send_message'
SLACK_SEND_TOOL = 'mcp__claude_ai_Slack__slack_send_message'
SEARCH_TOOL = 'mcp__claude_ai_Gmail__search_threads'
_ALLOWED_SLACK_FIELDS = frozenset({'channel_id', 'message', 'thread_ts'})
# optional connector flags a model may echo with their default value; truthy is denied
_FALSY_ONLY_SLACK_FIELDS = frozenset(
    {'reply_broadcast', 'unfurl_app_links', 'unfurl_links'}
)
_RETRY_HINT = 'fix the arguments and retry this part once'
_THREAD_TS = re.compile(r'\d+\.\d+')
_DENY_EXIT = 2
_ALLOW_DECISION = {
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow',
    }
}


def _normalize(text: str) -> str:
    """Unify line endings and drop trailing whitespace per line and overall."""
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    return '\n'.join(line.rstrip() for line in lines).strip()


def _sha256(text: str) -> str:
    # surrogatepass: a lone surrogate must hash (and mismatch), not crash
    return hashlib.sha256(_normalize(text).encode('utf-8', 'surrogatepass')).hexdigest()


def _split(value: str | None) -> list[str]:
    return [x.strip() for x in (value or '').split(',') if x.strip()]


def _check_slack(tool_input: dict[str, Any], args: argparse.Namespace) -> str | None:
    """Validate a Slack post against the allowed channels and ordered parts."""
    channels = _split(getattr(args, 'slack_channels', None))
    hashes = [h.lower() for h in _split(getattr(args, 'part_sha256', None))]
    once_dir = getattr(args, 'once_dir', None)
    if not channels or not hashes or not once_dir:
        return (
            'slack_send_message requires --slack-channels, --part-sha256 and '
            '--once-dir to be configured'
        )
    channel = tool_input.get('channel_id')
    if not isinstance(channel, str) or channel not in channels:
        return (
            f'slack_send_message wrong channel: must be one of {", ".join(channels)}; '
            f'{_RETRY_HINT}'
        )
    unknown = sorted(
        key
        for key in set(tool_input) - _ALLOWED_SLACK_FIELDS
        if key not in _FALSY_ONLY_SLACK_FIELDS or tool_input[key]
    )
    if unknown:
        return (
            f'slack_send_message unknown key(s) not permitted: {", ".join(unknown)}; '
            f'{_RETRY_HINT}'
        )
    picked = _pick_part(tool_input.get('message'), hashes, Path(once_dir))
    if isinstance(picked, str):
        return picked
    return _check_thread_ts(tool_input, picked) or _claim_part(Path(once_dir), picked)


def _pick_part(message: object, hashes: list[str], once_dir: Path) -> int | str:
    """Return the part index a message is for, or the denial reason.

    Identical parts map to the lowest index whose claim file does not exist yet.
    """
    if not isinstance(message, str):
        return 'slack_send_message payload mismatch: message missing'
    digest = _sha256(message)
    if digest not in hashes:
        return (
            'slack_send_message payload does not match the rendered part; '
            'do not retry, report the denial'
        )
    for index, candidate in enumerate(hashes):
        if candidate == digest and not (once_dir / str(index)).exists():
            return index
    return (
        'slack_send_message already posted: this part was sent in this run; '
        'do not retry'
    )


def _check_thread_ts(tool_input: dict[str, Any], index: int) -> str | None:
    thread_ts = tool_input.get('thread_ts')
    if index == 0:
        # None or "" means the model left it out, which is what part 0 needs
        if thread_ts is not None and thread_ts != '':
            return (
                'slack_send_message thread_ts rule: part 0 must not have thread_ts; '
                f'{_RETRY_HINT}'
            )
        return None
    if not isinstance(thread_ts, str) or _THREAD_TS.fullmatch(thread_ts) is None:
        return (
            f'slack_send_message thread_ts rule: part {index} needs a thread_ts '
            f'like 1234567890.123456; {_RETRY_HINT}'
        )
    return None


def _claim_part(once_dir: Path, index: int) -> str | None:
    """Enforce in-order posting and atomically claim part ``index``."""
    once_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for earlier in range(index):
        if not (once_dir / str(earlier)).exists():
            return (
                f'slack_send_message out of order: part {earlier} not posted yet; '
                f'{_RETRY_HINT}'
            )
    try:
        fd = os.open(once_dir / str(index), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return (
            f'slack_send_message already posted: part {index} was sent in this run; '
            'do not retry'
        )
    os.close(fd)
    return None


def _check_search(tool_input: dict[str, Any], senders_arg: str) -> str | None:
    """Allow only ``from:(S OR S ...) [after:YYYY/MM/DD]`` over the given senders."""
    senders = [x.strip() for x in senders_arg.split(',') if x.strip()]
    query = tool_input.get('query')
    if not isinstance(query, str):
        return 'search_threads query missing'
    if not senders:
        return 'search_threads is not permitted: no senders configured'
    sender = '|'.join(re.escape(x) for x in senders)
    pattern = rf'from:\(({sender})( OR ({sender}))*\)( after:\d{{4}}/\d{{2}}/\d{{2}})?'
    normalised = ' '.join(query.split())
    if re.fullmatch(pattern, normalised, re.IGNORECASE) is None:
        return (
            'search_threads query must be from:(<allowed senders joined by OR>) '
            f'with an optional after:YYYY/MM/DD; allowed senders: {", ".join(senders)}'
        )
    return None


def evaluate(payload: dict[str, Any], args: argparse.Namespace) -> str | None:
    """Return the denial reason for ``payload``, or None when it is allowed."""
    tool_name = payload.get('tool_name')
    allowed = {t for t in args.allow.split(',') if t}
    if tool_name not in allowed:
        return f'tool {tool_name} not allowlisted'
    if tool_name == SEND_TOOL:
        return 'email sending is disabled'
    if tool_name in (SLACK_SEND_TOOL, SEARCH_TOOL):
        return _check_guarded_tool(tool_name, payload, args)
    return None


def _check_guarded_tool(
    tool_name: str, payload: dict[str, Any], args: argparse.Namespace
) -> str | None:
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        return f'{tool_name} tool_input missing'
    if tool_name == SLACK_SEND_TOOL:
        return _check_slack(tool_input, args)
    senders = getattr(args, 'gmail_senders', None)
    if senders is None:
        return 'search_threads is not permitted: no senders configured'
    return _check_search(tool_input, senders)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow', required=True)
    parser.add_argument('--gmail-senders')
    parser.add_argument('--slack-channels')
    parser.add_argument('--part-sha256')
    parser.add_argument('--once-dir')
    return parser


def _say(message: str) -> None:
    """Write a denial reason to stderr; the exit code matters more than the text."""
    with contextlib.suppress(Exception):
        print(message, file=sys.stderr)


def main() -> int:
    """Read the hook payload from stdin and exit 0 (allow) or 2 (deny)."""
    try:
        args = _parser().parse_args()
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            _say('unparseable hook payload')
            return _DENY_EXIT
        reason = evaluate(payload, args)
    except BaseException as exc:  # noqa: BLE001 - fail closed on anything
        _say(f'guard error: {type(exc).__name__}: {exc}')
        return _DENY_EXIT
    if reason is None:
        # a broken stdout must not turn an allowed call into a crash
        with contextlib.suppress(Exception):
            print(json.dumps(_ALLOW_DECISION))
        return 0
    _say(reason)
    return _DENY_EXIT


if __name__ == '__main__':
    sys.exit(main())
