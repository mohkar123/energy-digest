import html
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from energy_digest.models import (
    CalendarEntry,
    DeepDive,
    Digest,
    ForecastImpact,
    Item,
    MarketLine,
    RadarRow,
    Score,
    ScoredItem,
    Story,
    UpcomingEvent,
    WeekInReview,
)
from energy_digest.render import RenderContext, render_all

SENTINEL = '— end of digest · 3 items —'


def make_item(n: int) -> Item:
    return Item(
        id=f'item{n:08d}',
        url=f'https://example.com/story-{n}?a=1&b=2',
        canonical_url=f'https://example.com/story-{n}',
        title=f'Story title {n}',
        source_id='src',
        source_name=f'Source {n}',
        source_kind='rss',
        tier='press',
        market_hint='US',
        published=datetime(2026, 10, 8, 12, tzinfo=UTC),
        also_reported_by=['Other Wire'],
    )


def make_scored(item: Item, hits: list[str]) -> ScoredItem:
    return ScoredItem(
        item=item,
        score=Score(
            item_id=item.id,
            market='US',
            topic='rate-case-tariff',
            market_relevance=4,
            forecast_relevance=3,
            forecast_lines=['STF', 'LTF'],
            dated_event=None,
            is_advert=False,
        ),
        watchlist_hits=hits,
        rank=1.0,
    )


def make_digest(items: list[Item], *, n_by_market: int = 3, **kw: object) -> Digest:
    ids = [i.id for i in items]
    line = MarketLine(item_ids=ids[:2], one_liner='A market line ' + 'x' * 120)
    data: dict[str, object] = {
        'top_stories': [
            Story(
                item_ids=ids[:1],
                headline='FERC finalises large-load rule',
                summary='Summary of the story.',
                market='US',
            )
        ],
        'by_market': {m: [line] * n_by_market for m in ('US', 'UK', 'BR', 'NZ')},
        'forecast_impact': [
            ForecastImpact(
                item_ids=ids[:1],
                product_lines=['STF', 'CP-Alerts'],
                impact_type='opportunity',
                so_what='Larger loads need better forecasts.',
                suggested_action='Ask product to scope a pilot.',
                accounts=['Acme Utility'],
            )
        ],
        'radar': [
            RadarRow(
                item_ids=ids[:1],
                market='US',
                signal='Large-load tariff',
                signal_type='large-load-data-centres',
                next_milestone_date=date(2026, 11, 1),
                forecast_lens=['LTF'],
                opportunity='High',
                confidence='C',
                next_step='Draft a one-pager.',
            )
        ],
        'upcoming': [
            UpcomingEvent(
                item_ids=ids[:1],
                date=date(2026, 11, 1),
                market='US',
                what='Comments due',
                confidence='R',
            )
        ],
        'deep_dive': DeepDive(
            market='US',
            what_moved=[line],
            regulatory_calendar=[
                CalendarEntry(
                    date=date(2026, 11, 1),
                    what='Final rule',
                    item_ids=ids[:1],
                    confidence='C',
                )
            ],
            account_implications=[line],
            open_questions=['Does it apply to BTM?'],
            competitors_mentioned=[line],
        ),
        'week_in_review': None,
        'run_date': date(2026, 10, 9),
        'deep_dive_market': 'US',
    }
    data.update(kw)
    return Digest.model_validate(data)


def make_ctx(digest: Digest, items: list[Item], **kw: object) -> RenderContext:
    scored = [make_scored(items[0], ['Acme Utility']), make_scored(items[1], [])]
    scored.append(make_scored(items[2], []))
    base: dict[str, object] = {
        'digest': digest,
        'items': {i.id: i for i in items},
        'scored': scored,
        'source_health': {'rss-a': {'ok': True}, 'rss-b': {'ok': False}},
        'items_scanned': 120,
        'report_path': Path('/var/reports/2026-10-09.html'),
    }
    base.update(kw)
    return RenderContext(**base)  # type: ignore[arg-type]


@pytest.fixture
def items() -> list[Item]:
    return [make_item(n) for n in range(3)]


def hrefs(markup: str) -> set[str]:
    return {html.unescape(h) for h in re.findall(r'href="([^"]+)"', markup)}


def test_no_script_and_hrefs_come_from_items(items: list[Item]) -> None:
    ctx = make_ctx(make_digest(items), items)
    out = render_all(ctx, 40_000)
    assert '<script' not in out.email_html.lower()
    found = hrefs(out.email_html)
    assert found
    assert found <= {i.url for i in items}


def test_unknown_item_ids_never_become_links(items: list[Item]) -> None:
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(update={'item_ids': ['nope', 'zzz']})
    digest = digest.model_copy(update={'top_stories': [story]})
    out = render_all(make_ctx(digest, items), 40_000)
    assert 'nope' not in out.email_html
    assert hrefs(out.email_html) <= {i.url for i in items}


def test_model_written_urls_are_escaped_not_linked(items: list[Item]) -> None:
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(
        update={'summary': '<a href="https://evil.test">x</a>'}
    )
    digest = digest.model_copy(update={'top_stories': [story]})
    out = render_all(make_ctx(digest, items), 40_000)
    assert 'https://evil.test' not in hrefs(out.email_html)
    assert '&lt;a href=' in out.email_html


def test_section_headings_in_order(items: list[Item]) -> None:
    digest = make_digest(
        items,
        week_in_review=WeekInReview(themes=[], trend_notes='Trend.'),
    )
    out = render_all(make_ctx(digest, items), 40_000)
    headings = [
        'Top stories',
        'Forecast impact',
        'Watchlist highlights',
        'Market research',
        'Upcoming dates',
        'By market',
        'Week in review',
    ]
    positions = [out.email_html.index(h) for h in headings]
    assert positions == sorted(positions)
    text_positions = [out.email_text.index(h.upper()) for h in headings]
    assert text_positions == sorted(text_positions)


def test_friday_without_review_shows_placeholder(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    for body in (out.email_html, out.report_html):
        assert 'Week in review' in body
        assert 'Nothing notable this week.' in body
    assert 'WEEK IN REVIEW' in out.email_text
    assert 'Nothing notable this week.' in out.email_text


def test_friday_with_review_renders_themes(items: list[Item]) -> None:
    review = WeekInReview(
        themes=[MarketLine(item_ids=[items[0].id], one_liner='Theme one')],
        trend_notes='Trend.',
    )
    out = render_all(make_ctx(make_digest(items, week_in_review=review), items), 40_000)
    for body in (out.email_html, out.email_text, out.report_html):
        assert 'Theme one' in body
        assert 'Nothing notable this week.' not in body


def test_non_friday_without_review_has_no_week_heading(items: list[Item]) -> None:
    digest = make_digest(items, run_date=date(2026, 10, 8))
    out = render_all(make_ctx(digest, items), 40_000)
    assert 'Week in review' not in out.email_html
    assert 'Week in review' not in out.report_html
    assert 'WEEK IN REVIEW' not in out.email_text


def test_sentinel_in_html_and_text(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    assert SENTINEL in out.email_html
    assert out.email_text.rstrip().endswith(SENTINEL)


def test_footer_shows_report_path_failed_sources_and_legend(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    assert 'Full report: /var/reports/2026-10-09.html' in out.email_html
    assert 'rss-b unavailable today' in out.email_html
    assert 'rss-a' not in out.email_html
    assert 'confirmed from a primary source' in out.email_html


def test_watchlist_highlights_show_hits(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    section = out.email_html.split('Watchlist highlights', 1)[1].split(
        'Market research', 1
    )[0]
    assert 'class="mg">Acme Utility</strong>' in section
    assert 'Story title 0' in section
    assert 'Story title 1' not in section


def test_large_by_market_is_trimmed_within_budget(items: list[Item]) -> None:
    digest = make_digest(items, n_by_market=50)
    assert sum(len(v) for v in digest.by_market.values()) == 200
    out = render_all(make_ctx(digest, items), 40_000)
    assert len(out.email_html.encode()) <= 40_000
    assert SENTINEL in out.email_html


def test_trim_keeps_at_least_one_per_market_as_last_resort(
    items: list[Item],
) -> None:
    digest = make_digest(items, n_by_market=50)
    out = render_all(make_ctx(digest, items), 1)
    by_market_section = out.email_html.split('By market', 1)[1]
    assert by_market_section.count('A market line') == 4


def test_over_budget_after_trimming_logs_warning(
    items: list[Item], caplog: pytest.LogCaptureFixture
) -> None:
    digest = make_digest(items)
    with caplog.at_level(logging.WARNING, logger='energy_digest.render'):
        out = render_all(make_ctx(digest, items), 100)
    assert 'over budget' in caplog.text
    assert SENTINEL in out.email_html


def test_degraded_banner_and_subject(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items, degraded=True), items), 40_000)
    assert 'Headlines only.' in out.email_html
    assert out.subject == 'Market reading — Fri 9 Oct · headlines only'


def test_normal_subject_uses_truncated_headline(items: list[Item]) -> None:
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(update={'headline': 'H' * 100})
    digest = digest.model_copy(update={'top_stories': [story]})
    out = render_all(make_ctx(digest, items), 40_000)
    assert out.subject == f'Market reading — Fri 9 Oct · {"H" * 70}'
    assert 'Headlines only.' not in out.email_html


def test_empty_forecast_impact_shows_placeholder(items: list[Item]) -> None:
    digest = make_digest(items, forecast_impact=[])
    out = render_all(make_ctx(digest, items), 40_000)
    section = out.email_html.split('Forecast impact', 1)[1].split(
        'Watchlist highlights', 1
    )[0]
    assert 'Nothing notable today.' in section


def test_report_is_untrimmed_and_lists_every_scored_item(items: list[Item]) -> None:
    digest = make_digest(items, n_by_market=50)
    radar = [
        digest.radar[0].model_copy(update={'signal': f'Signal {n}'}) for n in range(12)
    ]
    digest = digest.model_copy(update={'radar': radar})
    out = render_all(make_ctx(digest, items), 5_000)
    for item in items:
        assert item.title in out.report_html
    for n in range(12):
        assert f'Signal {n}<' in out.report_html
    assert 'Other Wire' in out.report_html
    assert 'Signal 11<' not in out.email_html


def test_report_has_no_script_and_hrefs_come_from_items(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    assert '<script' not in out.report_html.lower()
    found = hrefs(out.report_html)
    assert found
    assert found <= {i.url for i in items}


def test_report_sections_and_source_health(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    for heading in (
        'Radar',
        'What moved',
        'Regulatory calendar',
        'Account implications',
        'Open questions to confirm',
        'Competitors in play',
        'Forecast impact',
        'All scored items',
        'Source health',
    ):
        assert heading in out.report_html
    assert 'unavailable today' in out.report_html


def test_text_email_is_not_html_escaped(items: list[Item]) -> None:
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(
        update={'summary': "Utility's rate > expected"}
    )
    digest = digest.model_copy(update={'top_stories': [story]})
    out = render_all(make_ctx(digest, items), 40_000)
    assert "Utility's rate > expected" in out.email_text
    assert '&#39;' not in out.email_text
    assert '&gt;' not in out.email_text
    assert 'Utility&#39;s rate &gt; expected' in out.email_html


def test_text_citations_are_on_their_own_indented_lines(items: list[Item]) -> None:
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(
        update={'item_ids': [items[0].id, items[1].id]}
    )
    digest = digest.model_copy(update={'top_stories': [story]})
    out = render_all(make_ctx(digest, items), 40_000)
    lines = out.email_text.splitlines()
    summary = lines.index('  Summary of the story.')
    assert lines[summary + 1] == f'    - Source 0 ({items[0].url})'
    assert lines[summary + 2] == f'    - Source 1 ({items[1].url})'
    assert '' not in lines[summary + 1 : summary + 3]
    assert out.email_text.rstrip().endswith(SENTINEL)


def test_text_forecast_citations_follow_without_blank_line(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    lines = out.email_text.splitlines()
    assert lines[0] == 'MARKET READING'
    accounts = lines.index('  Accounts: Acme Utility')
    assert lines[accounts + 1] == f'    - Source 0 ({items[0].url})'


def _cite_everything(node: object, ids: list[str]) -> object:
    """Copy a dumped digest, citing every id in each entry."""
    if isinstance(node, dict):
        return {
            k: ids if k == 'item_ids' else _cite_everything(v, ids)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_cite_everything(v, ids) for v in node]
    return node


def test_many_citations_are_capped_in_email_but_not_in_report() -> None:
    many = [make_item(n) for n in range(40)]
    ids = [i.id for i in many]
    base = make_digest(many, n_by_market=20)
    dumped = _cite_everything(base.model_dump(mode='json'), ids)
    digest = Digest.model_validate(dumped)
    ctx = make_ctx(digest, many)
    full_email = render_all(ctx, 10_000_000).email_html
    assert len(full_email.encode()) > 40_000, 'fixture must exceed the budget'

    out = render_all(ctx, 40_000)

    assert len(out.email_html.encode()) <= 40_000
    assert '+38 more in full report' in out.email_html
    assert '+38 more in full report' in out.email_text
    assert 'more in full report' not in out.report_html
    assert len(hrefs(out.report_html)) == 40


def test_email_styles_live_in_head_classes_not_inline(items: list[Item]) -> None:
    out = render_all(make_ctx(make_digest(items), items), 40_000)
    assert out.email_html.count('style="') < 60
    head = out.email_html.split('</head>', 1)[0]
    for selector in ('.ca{', '.lbl{', '.hd{', '.bdg{', '.cf{', '.tc{'):
        assert selector in head
    assert 'class="ca"' in out.email_html
    assert 'class="bdg bdg-hi"' in out.email_html
    assert '.ca{' in out.report_html.split('</head>', 1)[0]
