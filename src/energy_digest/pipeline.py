"""End-to-end orchestration: collect, score, synthesize, render, deliver."""

import contextlib
import fcntl
import json
import logging
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from energy_digest.claude_cli import Runner, subprocess_runner
from energy_digest.collect import article
from energy_digest.collect.feeds import (
    Validators,
    collect_feeds,
    make_client,
    resolve_gnews,
)
from energy_digest.collect.newsletters import collect_newsletters
from energy_digest.config import AppConfig
from energy_digest.dedup import cluster
from energy_digest.deliver import (
    Delivery,
    DeliveryError,
    DeliveryOutcomeUnknownError,
    deliver_digest,
)
from energy_digest.models import Digest, Item, ScoredItem
from energy_digest.render import RenderContext, Rendered, render_all
from energy_digest.score import score_items, select
from energy_digest.store import Store
from energy_digest.synthesize import (
    WeekItem,
    degraded_digest,
    digest_summary,
    synthesize,
    week_items,
)

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW = timedelta(hours=24)
_MAX_WINDOW = timedelta(hours=96)
# Overlap with the previous run's window; harmless because sent items are deduped.
_WINDOW_MARGIN = timedelta(minutes=30)
# A run still 'running' after this long was killed without bookkeeping.
_STALE_RUN_AGE = timedelta(hours=2)
_LOCK_NAME = 'run.lock'
_SLACK_PART_SEPARATOR = '\n\n---\n\n'
# Scoring is the unbounded stage; past this it degrades to keyword scoring.
_SCORE_DEADLINE_S = 25 * 60
_CWD_ENV = 'ENERGY_DIGEST_CLAUDE_CWD'
_GNEWS_LIMIT = 40
_FORBIDDEN_CWD_ENTRIES = ('.claude', '.mcp.json')

Status = Literal['ok', 'degraded', 'skipped', 'failed', 'dry-run', 'send-unknown']


@dataclass(frozen=True)
class RunOptions:
    """What the caller asked for."""

    run_date: date
    dry_run: bool = False
    no_send: bool = False
    since: datetime | None = None
    force: bool = False


@dataclass(frozen=True)
class RunOutcome:
    """Result of one pipeline run."""

    status: Status
    email_path: Path | None
    report_path: Path | None
    message_id: str | None  # permalink of the main Slack post once delivered
    cost_usd: float
    error: str | None = None  # why delivery failed


class PipelineError(RuntimeError):
    """The pipeline cannot run safely."""


@dataclass(frozen=True)
class _Env:
    """Everything a run needs besides its options."""

    cfg: AppConfig
    store: Store
    client: httpx.Client
    meter: 'CostMeter'
    cwd: Path


def _reported_cost(stdout: str) -> float:
    """Cost from a single JSON result or the final line of stream-json JSONL."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = json.loads(stdout.strip().splitlines()[-1])
    return float(payload.get('total_cost_usd') or 0.0)


class CostMeter:
    """Wraps a Runner and sums the ``total_cost_usd`` of every claude call."""

    def __init__(self, runner: Runner) -> None:
        """Wrap ``runner``."""
        self._runner = runner
        self.total_usd = 0.0

    def __call__(
        self,
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        """Run the wrapped runner, recording the cost it reports."""
        rc, stdout, stderr = self._runner(argv, stdin_text, cwd, env, timeout_s)
        # unparseable output carries no cost information
        with contextlib.suppress(ValueError, AttributeError, TypeError, IndexError):
            self.total_usd += _reported_cost(stdout)
        return rc, stdout, stderr


def neutral_cwd() -> Path:
    """Directory claude runs from, guaranteed free of project settings/MCP config.

    It lives outside the repository so claude cannot pick up the repo's own
    ``.claude`` settings. Override with ``ENERGY_DIGEST_CLAUDE_CWD``; otherwise
    it is ``${XDG_STATE_HOME:-~/.local/state}/energy-digest/claude-cwd``.
    """
    override = os.environ.get(_CWD_ENV)
    if override:
        cwd = Path(override)
    else:
        state = os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state'
        cwd = Path(state) / 'energy-digest' / 'claude-cwd'
    cwd.mkdir(parents=True, exist_ok=True)
    for name in _FORBIDDEN_CWD_ENTRIES:
        if (cwd / name).exists():
            msg = f'{cwd / name} must not exist: it would change claude behaviour'
            raise PipelineError(msg)
    return cwd


def _aware(moment: datetime, tz: tzinfo) -> datetime:
    """Attach ``tz`` to a naive datetime; aware values are kept as they are."""
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def _window_start(
    opts: RunOptions, store: Store, now: datetime, tz: ZoneInfo
) -> datetime:
    """Earliest publication time to collect, capped at ``_MAX_WINDOW`` back."""
    floor = now - _MAX_WINDOW
    if opts.since is not None:
        since = _aware(opts.since, tz)
        if since < floor:
            logger.warning(
                'explicit --since clamped to the maximum collection window',
                extra={
                    'digest.requested_since': since.isoformat(),
                    'digest.clamped_to': floor.isoformat(),
                },
            )
        return max(since, floor)
    last = store.last_success_started()
    since = (
        _aware(last, UTC) - _WINDOW_MARGIN
        if last is not None
        else now - _DEFAULT_WINDOW
    )
    return max(since, floor)


def _cited_ids(node: Any) -> set[str]:  # noqa: ANN401
    """Every item id cited anywhere in a dumped digest."""
    ids: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'item_ids':
                ids.update(value)
            else:
                ids |= _cited_ids(value)
    elif isinstance(node, list):
        for value in node:
            ids |= _cited_ids(value)
    return ids


def _resolve_and_enrich(
    selected: list[ScoredItem], store: Store, client: httpx.Client
) -> list[ScoredItem]:
    # Resolve Google News links first: there is no article to enrich until the
    # publisher URL is known. Only selected items are resolved, so the decode
    # budget is spent on links that actually appear in the digest.
    resolved = resolve_gnews([s.item for s in selected], store, limit=_GNEWS_LIMIT)
    enriched = article.enrich(resolved, client)
    return [
        s.model_copy(update={'item': item})
        for s, item in zip(selected, enriched, strict=True)
    ]


def _write_reports(
    cfg: AppConfig, run_date: date, rendered: Rendered
) -> tuple[Path, Path, Path]:
    reports = cfg.settings.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    html = reports / f'{run_date.isoformat()}-email.html'
    text = reports / f'{run_date.isoformat()}-email.txt'
    report = reports / f'{run_date.isoformat()}-report.html'
    slack = reports / f'{run_date.isoformat()}-slack.md'
    slack.write_text(_SLACK_PART_SEPARATOR.join(rendered.slack_parts), encoding='utf-8')
    html.write_text(rendered.email_html, encoding='utf-8')
    text.write_text(rendered.email_text, encoding='utf-8')
    report.write_text(rendered.report_html, encoding='utf-8')
    return html, text, report


def _collect(  # noqa: PLR0913
    cfg: AppConfig,
    opts: RunOptions,
    store: Store,
    client: httpx.Client,
    since: datetime,
    *,
    cwd: Path,
    runner: Runner,
) -> tuple[list[Item], dict[str, Any], dict[str, Validators]]:
    news_items, health = collect_newsletters(
        cfg, since, store, client, cwd=cwd, runner=runner
    )
    feed_items, feed_health, pending = collect_feeds(cfg.sources, since, store, client)
    items = cluster([*news_items, *feed_items])
    if not opts.force:
        sent_ids = store.sent_item_ids()
        sent_urls = store.sent_canonical_urls()
        items = [
            i
            for i in items
            if i.id not in sent_ids and i.canonical_url not in sent_urls
        ]
    store.upsert_items(items)
    return items, {**health, **feed_health}, pending


def _digest_for(  # noqa: PLR0913
    cfg: AppConfig,
    opts: RunOptions,
    store: Store,
    items: list[Item],
    client: httpx.Client,
    *,
    tz: tzinfo,
    score_deadline: float,
    cwd: Path,
    runner: Runner,
) -> tuple[Digest, list[ScoredItem], list[WeekItem]]:
    scored = score_items(items, cfg, cwd=cwd, runner=runner, deadline=score_deadline)
    selected = _resolve_and_enrich(select(scored, cfg.settings.top_n), store, client)
    store.upsert_items([s.item for s in selected])
    enriched = {s.item.id: s for s in selected}
    store.save_scores([enriched.get(s.item.id, s) for s in scored])
    # Computed once: synthesis cites these items and rendering must link them.
    stored_week = week_items(store, opts.run_date, tz)
    if selected:
        digest = synthesize(
            selected,
            cfg,
            store,
            opts.run_date,
            cwd=cwd,
            runner=runner,
            stored_week=stored_week,
        )
    else:
        logger.warning(
            'nothing selected for the digest',
            extra={'digest.stage': 'synthesize'},
        )
        digest = degraded_digest(selected, opts.run_date)
    return digest, selected, stored_week


def _record_delivery(
    store: Store, opts: RunOptions, digest: Digest, pending: dict[str, Validators]
) -> None:
    """Persist everything that must only reflect a digest that was delivered."""
    store.mark_sent(_cited_ids(digest.model_dump()), opts.run_date)
    store.save_digest(opts.run_date, digest, digest_summary(digest))
    for entries in digest.by_market.values():
        for line in entries:
            for item_id in line.item_ids:
                store.index_summary(item_id, line.one_liner)
    # Only a delivered digest may advance the conditional-GET validators:
    # otherwise the next run would get a 304 for items it never delivered.
    for source_id, validators in pending.items():
        store.put_feed_state(source_id, *validators)


def _confirm_delivery(
    delivery: Delivery, once_dir: Path, log_extra: Mapping[str, object]
) -> None:
    """Warn about missing thread replies and drop the guard's claim files."""
    if delivery.missing_parts:
        logger.warning(
            'some Slack thread replies were not posted',
            extra={**log_extra, 'digest.missing_parts': list(delivery.missing_parts)},
        )
    # the main post is confirmed, so the claims have served their purpose; they
    # are kept on send-unknown as evidence
    shutil.rmtree(once_dir, ignore_errors=True)


def run(
    opts: RunOptions,
    cfg: AppConfig,
    *,
    runner: Runner = subprocess_runner,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> RunOutcome:
    """Run the whole digest pipeline for ``opts.run_date``.

    ``now`` exists so callers (and tests) can pin the clock. ``--force`` also
    lets items featured in an earlier digest be featured again. Only one run
    may be active at a time: a second concurrent run returns ``skipped``.
    """
    data_dir = cfg.settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / _LOCK_NAME).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning(
                'another digest run holds the lock, skipping',
                extra={'digest.run_date': opts.run_date.isoformat()},
            )
            return RunOutcome('skipped', None, None, None, 0.0)
        # The lock is released when the file is closed on leaving the block.
        return _locked_run(opts, cfg, runner=runner, client=client, now=now)


def _locked_run(
    opts: RunOptions,
    cfg: AppConfig,
    *,
    runner: Runner,
    client: httpx.Client | None,
    now: datetime | None,
) -> RunOutcome:
    settings = cfg.settings
    cwd = neutral_cwd()
    store = Store(settings.data_dir / 'digest.db')
    meter = CostMeter(runner)
    owns_client = client is None
    http = client or make_client()
    try:
        abandoned = store.abandon_stale_runs(_STALE_RUN_AGE)
        if abandoned:
            logger.warning(
                'marked stale runs as abandoned',
                extra={'digest.abandoned_runs': abandoned},
            )
        if store.already_sent(opts.run_date) and not (opts.force or opts.dry_run):
            logger.info(
                'digest already sent, skipping',
                extra={'digest.run_date': opts.run_date.isoformat()},
            )
            return RunOutcome('skipped', None, None, None, 0.0)
        env = _Env(cfg, store, http, meter, cwd)
        return _run(opts, env, now)
    finally:
        if owns_client:
            http.close()
        store.close()


def _drop_unclaimed_dir(once_dir: Path) -> None:
    """Remove the claim dir after a definitive failure that never claimed part 0."""
    if not (once_dir / '0').exists():
        shutil.rmtree(once_dir, ignore_errors=True)


def _run(opts: RunOptions, env: _Env, now: datetime | None) -> RunOutcome:
    cfg, store, client, meter, cwd = env.cfg, env.store, env.client, env.meter, env.cwd
    score_deadline = time.monotonic() + _SCORE_DEADLINE_S
    run_id = store.start_run(opts.run_date)
    health: dict[str, Any] = {}
    log_extra = {'digest.run_id': run_id}
    delivered = False
    digest: Digest | None = None
    # the guard claims each Slack part here before it is posted; the main
    # post's claim means it may exist even if this process dies afterwards
    once_dir = cfg.settings.data_dir / f'deliver-{run_id}'

    def finish(status: Status, *, sent: bool, message_id: str | None = None) -> None:
        store.finish_run(
            run_id,
            status,
            sent=sent,
            message_id=message_id,
            source_health=health,
            cost_usd=meter.total_usd,
        )

    def finish_send_unknown() -> None:
        # sent=True so a rerun does not blindly post a duplicate; the cited items
        # are marked sent so they are not repeated tomorrow if the post arrived
        finish('send-unknown', sent=True)
        if digest is not None:
            store.mark_sent(_cited_ids(digest.model_dump()), opts.run_date)

    try:
        tz = ZoneInfo(cfg.settings.timezone)
        since = _window_start(opts, store, now or datetime.now(UTC), tz)
        items, health, pending = _collect(
            cfg, opts, store, client, since, cwd=cwd, runner=meter
        )
        digest, selected, stored_week = _digest_for(
            cfg,
            opts,
            store,
            items,
            client,
            tz=tz,
            score_deadline=score_deadline,
            cwd=cwd,
            runner=meter,
        )
        report_path = (
            cfg.settings.reports_dir / f'{opts.run_date.isoformat()}-report.html'
        )
        # Friday week-in-review entries may cite stored items from earlier days;
        # links come only from the DB, so those items must be resolvable too.
        cited_items = {w.item.id: w.item for w in stored_week}
        cited_items |= {s.item.id: s.item for s in selected}
        rendered = render_all(
            RenderContext(
                digest=digest,
                items=cited_items,
                scored=selected,
                source_health=health,
                items_scanned=len(items),
                report_path=report_path,
            ),
            cfg.settings.email_max_bytes,
        )
        email_path, _, report_path = _write_reports(cfg, opts.run_date, rendered)

        if opts.dry_run or opts.no_send:
            finish('dry-run', sent=False)
            return RunOutcome('dry-run', email_path, report_path, None, meter.total_usd)

        try:
            delivery = deliver_digest(
                rendered.slack_parts,
                cfg,
                cwd=cwd,
                once_dir=once_dir,
                runner=meter,
            )
        except DeliveryOutcomeUnknownError:
            logger.exception(
                'delivery outcome unknown: check your Slack DMs before rerunning; '
                'use --force to send again',
                extra=log_extra,
            )
            finish_send_unknown()
            return RunOutcome(
                'send-unknown', email_path, report_path, None, meter.total_usd
            )
        except DeliveryError as exc:
            logger.exception('delivery failed', extra=log_extra)
            _drop_unclaimed_dir(once_dir)
            finish('failed', sent=False)
            return RunOutcome(
                'failed',
                email_path,
                report_path,
                None,
                meter.total_usd,
                error=str(exc),
            )

        status: Status = 'degraded' if digest.degraded else 'ok'
        # The run is recorded as delivered before anything else is written, so a
        # crash while recording cannot make a rerun resend the digest.
        finish(status, sent=True, message_id=delivery.message_link)
        delivered = True
        _confirm_delivery(delivery, once_dir, log_extra)
        _record_delivery(store, opts, digest, pending)
        return RunOutcome(
            status, email_path, report_path, delivery.message_link, meter.total_usd
        )
    except BaseException:  # includes KeyboardInterrupt/SIGINT from systemd
        logger.exception('pipeline failed', extra=log_extra)
        if not delivered:
            if (once_dir / '0').exists():
                logger.exception(
                    'run interrupted after the main post was allowed: check your '
                    'Slack DMs before rerunning; use --force to send again',
                    extra=log_extra,
                )
                finish_send_unknown()
            else:
                finish('failed', sent=False)
        raise
