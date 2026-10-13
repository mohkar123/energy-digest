"""Command-line entry point for the digest."""

import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from energy_digest import pipeline
from energy_digest.claude_cli import (
    SLACK_SEND_TOOL,
    ClaudeError,
    ClaudeRequest,
    Profile,
    guard_script_path,
    run_claude,
)
from energy_digest.collect.feeds import collect_feeds, make_client
from energy_digest.collect.newsletters import READ_TOOLS
from energy_digest.config import (
    PLACEHOLDER_MARKER,
    AppConfig,
    ConfigError,
    load_config,
)
from energy_digest.deliver import load_guard
from energy_digest.logging_config import configure_logging
from energy_digest.schema import claude_json_schema
from energy_digest.store import Store

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_FAILED = 2
_AUTH_TIMEOUT_S = 30
_DOCTOR_BUDGET_USD = 0.10
_MIN_HEADLESS_TOOLS = 3
_SOURCES_WINDOW = timedelta(days=7)
_SATURDAY = 5
_FAILURE_STATUSES = frozenset({'failed', 'send-unknown'})
_RELATIVE_DAYS = re.compile(r'^(\d+)d$')


class _ToolCount(BaseModel):
    n: int


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='digest')
    sub = parser.add_subparsers(dest='command', required=True)

    run_p = sub.add_parser('run', help='collect, synthesize and send the digest')
    run_p.add_argument('--dry-run', action='store_true')
    run_p.add_argument('--no-send', action='store_true')
    run_p.add_argument('--since', type=datetime.fromisoformat, metavar='ISO')
    run_p.add_argument('--date', type=date.fromisoformat, metavar='YYYY-MM-DD')
    run_p.add_argument('--force', action='store_true')

    search_p = sub.add_parser('search', help='search the local archive')
    search_p.add_argument('terms', nargs='+')
    search_p.add_argument('--market')
    search_p.add_argument('--since', metavar='30d|YYYY-MM-DD')

    sub.add_parser('sources-check', help='fetch every feed and report health')
    sub.add_parser('doctor', help='verify the environment')
    return parser


def _write(line: str) -> None:
    sys.stdout.write(f'{line}\n')


def _parse_since(raw: str | None, today: date) -> date | None:
    if raw is None:
        return None
    match = _RELATIVE_DAYS.match(raw)
    if match:
        return today - timedelta(days=int(match.group(1)))
    return date.fromisoformat(raw)


def _local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def _cmd_run(args: argparse.Namespace, cfg: AppConfig, _root: Path) -> int:
    tz = ZoneInfo(cfg.settings.timezone)
    today = _local_now(tz)
    if args.date is None and today.weekday() >= _SATURDAY:
        # A Persistent=true timer fires on wake-up, which can be a weekend.
        logger.info(
            'weekend, no digest',
            extra={'digest.run_date': today.date().isoformat()},
        )
        skipped = pipeline.RunOutcome('skipped', None, None, None, 0.0)
        _write(json.dumps(dataclasses.asdict(skipped), default=str))
        return EXIT_OK
    run_date: date = args.date or today.date()
    opts = pipeline.RunOptions(
        run_date=run_date,
        dry_run=args.dry_run,
        no_send=args.no_send,
        since=args.since,
        force=args.force,
    )
    outcome = pipeline.run(opts, cfg)
    _write(json.dumps(dataclasses.asdict(outcome), default=str))
    return EXIT_FAILED if outcome.status in _FAILURE_STATUSES else EXIT_OK


def _cmd_search(args: argparse.Namespace, cfg: AppConfig, _root: Path) -> int:
    today = _local_now(ZoneInfo(cfg.settings.timezone)).date()
    since = _parse_since(args.since, today)
    store = Store(cfg.settings.data_dir / 'digest.db')
    try:
        rows = store.search(' '.join(args.terms), market=args.market, since=since)
        items = store.get_items(row['item_id'] for row in rows)
    finally:
        store.close()
    for row in rows:
        item = items.get(row['item_id'])
        url = item.url if item else ''
        _write(f'{row["published"][:10]}  {row["market"]}  {row["title"]}  {url}')
    return EXIT_OK


def _cmd_sources_check(_args: argparse.Namespace, cfg: AppConfig, _root: Path) -> int:
    # A throwaway store: the real one keeps conditional-GET validators that
    # would make the next real run skip feeds this check already fetched.
    with (
        tempfile.TemporaryDirectory() as tmp,
        make_client() as client,
    ):
        store = Store(Path(tmp) / 'check.db')
        try:
            _, health, _ = collect_feeds(
                cfg.sources, datetime.now(UTC) - _SOURCES_WINDOW, store, client
            )
        finally:
            store.close()
    _write(f'{"id":<28} {"ok":<6} {"items":>5}  error')
    for source_id, info in health.items():
        _write(
            f'{source_id:<28} {info["ok"]!s:<6} {info["items"]:>5}  '
            f'{info.get("error", "")}'
        )
    return EXIT_OK


def _check_slack_ids(cfg: AppConfig) -> str | None:
    placeholders = cfg.settings.delivery.placeholder_fields()
    if not placeholders:
        return None
    return (
        f'{", ".join(placeholders)} in config/settings.yaml still the '
        f'{PLACEHOLDER_MARKER} placeholder; set your Slack member ID and self-DM '
        'channel ID'
    )


def _check_claude_on_path(cfg: AppConfig) -> str | None:
    binary = cfg.settings.claude.binary
    return None if shutil.which(binary) else f'{binary} not found on PATH'


def _check_auth(cfg: AppConfig) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cfg.settings.claude.binary, 'auth', 'status'],
            capture_output=True,
            text=True,
            timeout=_AUTH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f'claude auth status failed: {exc}'
    if proc.returncode != 0:
        return f'claude auth status exited {proc.returncode}'
    # The output format is not guaranteed: accept any output naming claude.ai.
    if 'claude.ai' not in proc.stdout.lower().replace('claudeai', 'claude.ai'):
        return 'not logged in with a claude.ai account'
    return None


def _check_api_key(_cfg: AppConfig) -> str | None:
    if os.environ.get('ANTHROPIC_API_KEY'):
        _write('WARN  ANTHROPIC_API_KEY is set; it is stripped from claude runs')
    return None


def _check_connector_tools(cfg: AppConfig) -> str | None:
    settings = cfg.settings
    request = ClaudeRequest(
        prompt=(
            'Count the tools available to you whose names are exactly or end with '
            '"search_threads", "get_thread" or "slack_send_message". '
            'Do not call any tools. Reply with {"n": <count>}.'
        ),
        system_prompt='You report facts about your own environment. Be brief.',
        model=settings.models.extract,
        profile=Profile.GMAIL,
        allowed_mcp_tools=(*READ_TOOLS, SLACK_SEND_TOOL),
        json_schema=claude_json_schema(_ToolCount),
        max_budget_usd=_DOCTOR_BUDGET_USD,
        timeout_s=settings.claude.timeout_s,
        max_turns=2,
    )
    try:
        cwd = pipeline.neutral_cwd()
        result = run_claude(request, settings.claude, cwd=cwd)
        count = _ToolCount.model_validate(result.structured).n
    except (ClaudeError, ValidationError, OSError, pipeline.PipelineError) as exc:
        return f'headless connector call failed: {exc}'
    if count < _MIN_HEADLESS_TOOLS:
        return (
            f'only {count} of the Gmail read and Slack send tools are visible '
            f'headless (need {_MIN_HEADLESS_TOOLS})'
        )
    return None


def _check_guard_denies_forgery(cfg: AppConfig) -> str | None:
    try:
        # Prefer the hook path claude itself is configured with.
        if guard_script_path().exists():
            guard = load_guard()
        else:
            guard = load_guard(Path(__file__).resolve().parents[2])
    except Exception as exc:  # noqa: BLE001 - any load failure is a FAIL
        return f'cannot load guard hook: {exc}'
    target = cfg.settings.delivery
    with tempfile.TemporaryDirectory() as tmp:
        args = argparse.Namespace(
            allow=SLACK_SEND_TOOL,
            slack_channels=f'{target.slack_user_id},{target.slack_dm_channel}',
            part_sha256=guard._sha256('the digest'),  # noqa: SLF001
            once_dir=str(Path(tmp) / 'once'),
        )
        forged = {
            'tool_name': SLACK_SEND_TOOL,
            'tool_input': {'channel_id': 'C000000000', 'message': 'the digest'},
        }
        if guard.evaluate(forged, args) is None:
            return 'guard ALLOWED a Slack post to channel C000000000'
        mismatch = {
            'tool_name': SLACK_SEND_TOOL,
            'tool_input': {'channel_id': target.slack_dm_channel, 'message': 'x'},
        }
        if guard.evaluate(mismatch, args) is None:
            return 'guard ALLOWED a Slack post with an unexpected payload'
    return None


def _check_writable(cfg: AppConfig) -> str | None:
    for directory in (cfg.settings.data_dir, cfg.settings.reports_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory):
                pass
        except OSError as exc:
            return f'{directory} not writable: {exc}'
    return None


_DOCTOR_CHECKS: tuple[tuple[str, Callable[[AppConfig], str | None]], ...] = (
    ('Slack IDs configured', _check_slack_ids),
    ('claude on PATH', _check_claude_on_path),
    ('claude.ai login', _check_auth),
    ('ANTHROPIC_API_KEY', _check_api_key),
    ('Gmail read and Slack send tools visible headless', _check_connector_tools),
    ('guard denies forged Slack posts', _check_guard_denies_forgery),
    ('data and reports dirs writable', _check_writable),
)


def _cmd_doctor(_args: argparse.Namespace, cfg: AppConfig, _root: Path) -> int:
    ok = True
    for name, check in _DOCTOR_CHECKS:
        problem = check(cfg)
        ok &= problem is None
        _write(
            f'{"PASS" if problem is None else "FAIL"}  {name}'
            + (f': {problem}' if problem else '')
        )
    return EXIT_OK if ok else EXIT_FAILED


_COMMANDS: dict[str, Callable[[argparse.Namespace, AppConfig, Path], int]] = {
    'run': _cmd_run,
    'search': _cmd_search,
    'sources-check': _cmd_sources_check,
    'doctor': _cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    """Run the digest CLI and return the process exit code."""
    args = _build_parser().parse_args(argv)
    configure_logging()
    repo_root = Path(os.environ.get('ENERGY_DIGEST_ROOT') or Path.cwd())
    try:
        cfg = load_config(repo_root)
        return _COMMANDS[args.command](args, cfg, repo_root)
    except ConfigError:
        logger.exception('invalid configuration')
        return EXIT_UNEXPECTED
    except Exception:
        logger.exception('command failed', extra={'digest.command': args.command})
        return EXIT_UNEXPECTED
