"""Render the digest into the email (HTML and text) and the local report."""

import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, get_args

from jinja2 import Environment, PackageLoader, StrictUndefined

from energy_digest.dedup import display_source
from energy_digest.models import (
    Digest,
    ForecastImpact,
    Item,
    Market,
    MarketLine,
    ScoredItem,
)

logger = logging.getLogger(__name__)

MARKET_NAMES: Mapping[str, str] = {
    'US': 'US',
    'UK': 'UK',
    'BR': 'Brazil',
    'NZ': 'New Zealand',
    'OTHER': 'Other',
}
CONFIDENCE_LABELS: Mapping[str, str] = {
    'C': 'Confirmed',
    'R': 'Reported',
    'I': 'Inferred',
}
_BY_MARKET_CAPS = (20, 10, 8, 6, 5, 4, 3, 2)
_RADAR_CAPS = (8, 6)
_UPCOMING_CAP = 6
_DEEP_DIVE_CAP = 3
_FORECAST_IMPACT_CAP = 5
_HEADLINE_CHARS = 70
_EMAIL_CITES_PER_ENTRY = 2
_FINAL_BY_MARKET_CAP = 1
_MAIN_STORIES = 3
_MAIN_IMPACTS = 3
_IMPACT_ACCOUNTS = 2
_RADAR_ROWS = 6
_MARKET_LINES = 3
_WEEK_THEMES = 3
_MAIN_TEXT_CHARS = 280
_FRIDAY = 4
_BLANK_RUNS = re.compile(r'\n{3,}')
_URL_SCHEME = re.compile(r'(\w+)://')
_BARE_WWW = re.compile(r'\bwww\.', re.IGNORECASE)
_DOTTED_HOST = re.compile(r'(?:[a-z0-9-]+\.)+[a-z]{2,24}\b', re.IGNORECASE)
# look-alikes for characters that would open bold or code spans in Slack
_MARKDOWN_LOOKALIKES = str.maketrans({'*': '\u2217', '`': '\u02cb'})


@dataclass(frozen=True)
class RenderContext:
    """Everything the templates need besides the digest's own text."""

    digest: Digest
    items: Mapping[str, Item]  # every id the digest may cite
    scored: Sequence[ScoredItem]  # selected items (for watchlist + counts)
    source_health: Mapping[str, Mapping[str, Any]]
    items_scanned: int
    report_path: Path


@dataclass(frozen=True)
class Rendered:
    """The rendered artefacts of one run."""

    email_html: str
    email_text: str
    report_html: str
    subject: str
    slack_parts: tuple[str, ...] = ()


def _datefmt(value: date | datetime | None, fmt: str = '%-d %b %Y') -> str:
    return '' if value is None else value.strftime(fmt)


def _defang_scheme(match: re.Match[str]) -> str:
    scheme = re.sub('http', 'hxxp', match.group(1), flags=re.IGNORECASE)
    return f'{scheme}[:]//'


def _md(value: str) -> str:
    """Neutralise link and formatting syntax in model-written text.

    ``](`` is what turns bracketed text into a markdown hyperlink, and Slack
    also autolinks ``<url|label>``, URLs with any scheme and bare domains.
    Splitting or defanging these keeps every clickable link coming from stored
    items. Upstream validation already strips URLs, so this is defence in
    depth. ``*`` and backticks become look-alike characters so model text
    cannot break the bold or code spans the templates wrap around it.
    """
    value = value.replace('<', '\u2039').replace('>', '\u203a')
    value = _URL_SCHEME.sub(_defang_scheme, value)
    value = _BARE_WWW.sub('www[.]', value)
    value = _DOTTED_HOST.sub(lambda m: m.group(0).replace('.', '[.]'), value)
    # last, so earlier rewrites cannot assemble a new ``](``
    value = value.replace('](', '] (')
    return value.translate(_MARKDOWN_LOOKALIKES)


def _clip(value: str, limit: int = _MAIN_TEXT_CHARS) -> str:
    """Cut `value` to `limit` characters at a word boundary, ending with an ellipsis."""
    if len(value) <= limit:
        return value
    head = value[: limit - 1]
    if not value[limit - 1].isspace() and ' ' in head:
        word_head = head.rsplit(' ', 1)[0]
        # keep the hard cut when the word boundary would drop most of the text
        if len(word_head) >= limit // 2:
            head = word_head
    return head.rstrip() + '\u2026'


def _cell(value: str) -> str:
    """Make model text safe inside a markdown table cell."""
    # U+2223 instead of a backslash-escaped pipe: the escape may render literally
    return _md(value).replace('|', '\u2223').replace('\n', ' ')


def _link_text(value: str) -> str:
    return value.replace('[', '(').replace(']', ')')


def _link_url(value: str) -> str:
    return value.replace(' ', '%20').replace('(', '%28').replace(')', '%29')


def _make_cite_filters(
    items: Mapping[str, Item], cap: int | None
) -> tuple[
    Callable[[Sequence[str]], list[tuple[str, str, date]]],
    Callable[[Sequence[str]], int],
]:
    """Build the ``links`` and ``cites_cut`` filters; ``cap`` limits links shown."""

    def resolve(item_ids: Sequence[str]) -> list[tuple[str, str, date]]:
        seen: set[str] = set()
        out: list[tuple[str, str, date]] = []
        for item_id in item_ids:
            item = items.get(item_id)
            if item is None or item.url in seen:
                continue
            seen.add(item.url)
            label = display_source(item.source_name, item.url)
            out.append((label, item.url, item.published.date()))
        return out

    def links(item_ids: Sequence[str]) -> list[tuple[str, str, date]]:
        """Resolve ids to (source, url, date); unknown ids are skipped.

        This is the only place hrefs come from, so model-written text can
        never introduce a link.
        """
        found = resolve(item_ids)
        return found if cap is None else found[:cap]

    def cites_cut(item_ids: Sequence[str]) -> int:
        """How many links ``links`` leaves out because of the cap."""
        return 0 if cap is None else max(len(resolve(item_ids)) - cap, 0)

    return links, cites_cut


def _autoescape(name: str | None) -> bool:
    """Escape HTML templates only; the plain-text email must stay raw.

    ``_macros.j2`` holds HTML building blocks and is imported only by the
    HTML templates, so it is escaped too.
    """
    return name is not None and (name.endswith('.html.j2') or name == '_macros.j2')


def _make_env(items: Mapping[str, Item], cite_cap: int | None = None) -> Environment:
    env = Environment(
        loader=PackageLoader('energy_digest', 'templates'),
        autoescape=_autoescape,  # noqa: S701 - HTML templates escape; text does not
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters['links'], env.filters['cites_cut'] = _make_cite_filters(items, cite_cap)
    env.filters['conf_label'] = lambda c: CONFIDENCE_LABELS.get(c, c)
    env.filters['market_name'] = lambda m: MARKET_NAMES.get(m, m)
    env.filters['datefmt'] = _datefmt
    env.filters['md'] = _md
    env.filters['cell'] = _cell
    env.filters['clip'] = _clip
    env.filters['link_text'] = _link_text
    env.filters['link_url'] = _link_url
    return env


def _covered_markets(digest: Digest) -> list[Market]:
    seen: set[Market] = {s.market for s in digest.top_stories}
    seen |= {r.market for r in digest.radar}
    seen |= {m for m, lines in digest.by_market.items() if lines}
    return [m for m in get_args(Market) if m in seen]


def _template_vars(ctx: RenderContext, digest: Digest) -> dict[str, Any]:
    watchlist = [s for s in ctx.scored if s.watchlist_hits]
    failed = sorted(
        name for name, health in ctx.source_health.items() if not health.get('ok')
    )
    return {
        'ctx': ctx,
        'd': digest,
        'market_names': MARKET_NAMES,
        'covered_markets': [MARKET_NAMES[m] for m in _covered_markets(digest)],
        'watchlist': watchlist,
        'failed_sources': failed,
        'n_items': len(ctx.scored),
        'report_path': str(ctx.report_path),
    }


def _render(env: Environment, name: str, ctx: RenderContext, digest: Digest) -> str:
    return env.get_template(name).render(**_template_vars(ctx, digest))


def _trim_steps(digest: Digest) -> Iterator[Digest]:
    """Yield progressively smaller digests, in the order content is dropped."""
    current = digest
    for cap in _BY_MARKET_CAPS:
        trimmed = {m: lines[:cap] for m, lines in current.by_market.items()}
        # Never go below two entries per market (cap floor is 2).
        if trimmed != current.by_market:
            current = current.model_copy(update={'by_market': trimmed})
            yield current
    for cap in _RADAR_CAPS:
        if len(current.radar) > cap:
            current = current.model_copy(update={'radar': current.radar[:cap]})
            yield current
    if len(current.upcoming) > _UPCOMING_CAP:
        current = current.model_copy(
            update={'upcoming': current.upcoming[:_UPCOMING_CAP]}
        )
        yield current
    dd = current.deep_dive
    capped = dd.model_copy(
        update={
            'what_moved': dd.what_moved[:_DEEP_DIVE_CAP],
            'regulatory_calendar': dd.regulatory_calendar[:_DEEP_DIVE_CAP],
            'account_implications': dd.account_implications[:_DEEP_DIVE_CAP],
            'open_questions': dd.open_questions[:_DEEP_DIVE_CAP],
            'competitors_mentioned': dd.competitors_mentioned[:_DEEP_DIVE_CAP],
        }
    )
    if capped != dd:
        current = current.model_copy(update={'deep_dive': capped})
        yield current
    if len(current.forecast_impact) > _FORECAST_IMPACT_CAP:
        current = current.model_copy(
            update={'forecast_impact': current.forecast_impact[:_FORECAST_IMPACT_CAP]}
        )
        yield current


def _subject(digest: Digest) -> str:
    day = f'{digest.run_date:%a %-d %b}'
    if digest.degraded:
        return f'Market reading — {day} · headlines only'
    headline = (
        digest.top_stories[0].headline if digest.top_stories else 'no top stories'
    )
    return f'Market reading — {day} · {headline[:_HEADLINE_CHARS]}'


def _one_per_market(digest: Digest) -> Digest:
    """Last-resort trim for the email: a single by-market entry per market."""
    capped = {m: lines[:_FINAL_BY_MARKET_CAP] for m, lines in digest.by_market.items()}
    return digest.model_copy(update={'by_market': capped})


def _fit_email(ctx: RenderContext, max_bytes: int) -> tuple[Environment, Digest, str]:
    """Pick the digest and environment for the email so it fits ``max_bytes``.

    Content is dropped in stages: lower-value sections first, then citations
    per entry (email only; the report keeps them all), then by-market lines.
    """
    env = _make_env(ctx.items)
    digest = ctx.digest
    email_html = _render(env, 'email.html.j2', ctx, digest)

    def fits() -> bool:
        return len(email_html.encode()) <= max_bytes

    if fits():
        return env, digest, email_html
    for digest in _trim_steps(ctx.digest):
        email_html = _render(env, 'email.html.j2', ctx, digest)
        if fits():
            return env, digest, email_html
    env = _make_env(ctx.items, _EMAIL_CITES_PER_ENTRY)
    email_html = _render(env, 'email.html.j2', ctx, digest)
    if fits():
        return env, digest, email_html
    digest = _one_per_market(digest)
    email_html = _render(env, 'email.html.j2', ctx, digest)
    if not fits():
        logger.warning(
            'email still over budget after trimming',
            extra={
                'digest.stage': 'render',
                'digest.bytes': len(email_html.encode()),
                'digest.max_bytes': max_bytes,
            },
        )
    return env, digest, email_html


def _render_part(
    env: Environment,
    macro: str,
    ctx: RenderContext,
    lists: Mapping[str, list[Any]],
    dropped: int,
) -> str:
    """Render one Slack macro with the given (possibly trimmed) lists."""
    module = env.get_template('slack.md.j2').make_module()
    text = getattr(module, macro)(_template_vars(ctx, ctx.digest), lists, dropped)
    return _BLANK_RUNS.sub('\n\n', str(text)).strip()


def _fit_part(
    macro: str,
    ctx: RenderContext,
    lists: Mapping[str, list[Any]],
    max_chars: int,
) -> str:
    """Render one Slack part within ``max_chars``.

    If it is too long, whole entries are dropped from the tail of the longest
    list, noting how many were left out.
    """
    env = _make_env(ctx.items)
    view = dict(lists)
    dropped = 0
    text = _render_part(env, macro, ctx, view, dropped)
    while len(text) > max_chars:
        longest = max(view, key=lambda k: len(view[k]), default=None)
        if longest is None or not view[longest]:
            logger.warning(
                'slack part still over budget after trimming',
                extra={
                    'digest.stage': 'render',
                    'digest.chars': len(text),
                    'digest.max_chars': max_chars,
                },
            )
            break
        view[longest] = view[longest][:-1]
        dropped += 1
        text = _render_part(env, macro, ctx, view, dropped)
    return text


@dataclass(frozen=True)
class _ImpactLine:
    """A forecast impact with its Slack label and the one link it adds, if any."""

    impact: ForecastImpact
    who: str  # account names, or the market when no account is named
    link_ids: list[str]  # cited items not already linked from a top story


def _item_markets(ctx: RenderContext) -> dict[str, Market]:
    markets: dict[str, Market] = {
        i.id: i.market_hint for i in ctx.items.values() if i.market_hint
    }
    markets.update({s.item.id: s.score.market for s in ctx.scored})
    return markets


def _impact_line(
    impact: ForecastImpact, story_ids: set[str], markets: Mapping[str, Market]
) -> _ImpactLine:
    who = ', '.join(impact.accounts[:_IMPACT_ACCOUNTS])
    if not who:
        market = next((markets[i] for i in impact.item_ids if i in markets), None)
        who = MARKET_NAMES[market] if market else ''
    extra = [i for i in impact.item_ids if i not in story_ids]
    return _ImpactLine(impact, who, extra)


def _unseen(lines: Sequence[MarketLine], shown: set[str], cap: int) -> list[MarketLine]:
    """Keep the first ``cap`` lines that cite something not shown yet.

    Each kept line cites only its unseen items, so the link it shows is a new
    story. Lines with no item ids are kept as is.
    """
    out: list[MarketLine] = []
    for line in lines:
        fresh = [i for i in line.item_ids if i not in shown]
        if line.item_ids and not fresh:
            continue
        out.append(line.model_copy(update={'item_ids': fresh}))
        if len(out) == cap:
            break
    return out


def _slack_specs(ctx: RenderContext) -> list[tuple[str, dict[str, list[Any]]]]:
    """Slack parts in posting order, each with the lists its macro renders.

    Entries already shown in the main post are left out of later parts, so each
    story appears once.
    """
    digest = ctx.digest
    stories = list(digest.top_stories[:_MAIN_STORIES])
    story_ids = {i for s in stories for i in s.item_ids}
    markets = _item_markets(ctx)
    impacts = [
        _impact_line(f, story_ids, markets)
        for f in digest.forecast_impact[:_MAIN_IMPACTS]
    ]
    shown = story_ids | {i for f in impacts for i in f.impact.item_ids}
    radar = [r for r in digest.radar if not r.item_ids or set(r.item_ids) - shown]
    by_market = {
        f'bm_{m}': _unseen(digest.by_market.get(m, []), shown, _MARKET_LINES)
        for m in get_args(Market)
    }
    specs: list[tuple[str, dict[str, list[Any]]]] = [
        ('main', {'top_stories': stories, 'impacts': impacts}),
        ('research', {'radar': radar[:_RADAR_ROWS]}),
        ('upcoming', {'upcoming': list(digest.upcoming)}),
        ('by_market', by_market),
    ]
    if digest.week_in_review or digest.run_date.weekday() == _FRIDAY:
        themes = digest.week_in_review.themes if digest.week_in_review else []
        specs.append(('week', {'themes': list(themes[:_WEEK_THEMES])}))
    specs.append(('footer', {}))
    return specs


def render_slack(ctx: RenderContext, max_chars: int = 4500) -> tuple[str, ...]:
    """Render the digest as ordered Slack parts: main post, then thread replies.

    Each part is at most ``max_chars`` characters and entries are never split
    across parts. Output is deterministic for a given context.
    """
    parts = [
        _fit_part(macro, ctx, lists, max_chars) for macro, lists in _slack_specs(ctx)
    ]
    return _distinct(parts)


def _distinct(parts: list[str]) -> tuple[str, ...]:
    """Make every part unique; the delivery guard identifies parts by hash."""
    seen: set[str] = set()
    out: list[str] = []
    for i, part in enumerate(parts):
        unique = f'{part} _(part {i})_' if part in seen else part
        seen.add(unique)
        out.append(unique)
    return tuple(out)


def render_all(
    ctx: RenderContext, max_email_bytes: int, max_slack_chars: int = 4500
) -> Rendered:
    """Render email, report and Slack parts; trim the email to fit its budget."""
    email_env, digest, email_html = _fit_email(ctx, max_email_bytes)
    report_env = _make_env(ctx.items)
    return Rendered(
        email_html=email_html,
        email_text=_render(email_env, 'email.txt.j2', ctx, digest),
        report_html=_render(report_env, 'report.html.j2', ctx, ctx.digest),
        subject=_subject(ctx.digest),
        slack_parts=render_slack(ctx, max_slack_chars),
    )
