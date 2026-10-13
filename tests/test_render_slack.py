import re
from datetime import date
from typing import Any

import pytest

from energy_digest.models import (
    Digest,
    ForecastImpact,
    Item,
    MarketLine,
    RadarRow,
    Story,
    WeekInReview,
)
from energy_digest.render import _clip, _md, render_all, render_slack
from tests.test_render import make_ctx, make_digest, make_item

LINK = re.compile(r'\]\(([^)]*)\)')
MAX = 4500


@pytest.fixture
def items() -> list[Item]:
    return [make_item(n) for n in range(3)]


def big_digest(items: list[Item]) -> Digest:
    ids = [i.id for i in items]
    lines = [
        MarketLine(item_ids=ids, one_liner=f'Line {n} ' + 'y' * 150) for n in range(50)
    ]
    radar = [
        RadarRow(
            item_ids=ids[:1],
            market='US',
            signal=f'Signal {n} ' + 'z' * 100,
            signal_type='large-load-data-centres',
            next_milestone_date=date(2026, 11, 1),
            forecast_lens=['LTF'],
            opportunity='High',
            confidence='C',
            next_step='Do it.',
        )
        for n in range(40)
    ]
    return make_digest(
        items,
        by_market=dict.fromkeys(('US', 'UK', 'BR', 'NZ'), lines),
        radar=radar,
    )


def realistic_digest(items: list[Item]) -> Digest:
    """5 stories, 6 impacts, 12 radar rows, 15 by-market lines, on a Friday."""
    stories = [
        Story(
            item_ids=[items[n].id],
            headline=f'Long headline {n} ' + 'about a large regulatory filing ' * 4,
            summary='A long summary of the story. ' * 12,
            market='US',
            headline_short=f'Short headline {n}',
            why_short=f'Why it matters {n}',
        )
        for n in range(5)
    ]
    impacts = [
        ForecastImpact(
            item_ids=[items[n].id],
            product_lines=['LTF'],
            impact_type='opportunity',
            so_what='A long so-what. ' * 20,
            suggested_action='A long suggested action. ' * 10,
            accounts=[f'Account {n}'],
            so_what_short=f'So what {n}',
            action_short=f'Do {n}',
        )
        for n in range(6)
    ]
    radar = [
        RadarRow(
            item_ids=[items[n].id],
            market='US',
            signal=f'Radar signal {n}',
            signal_type='large-load-data-centres',
            next_milestone_date=None,
            forecast_lens=['LTF'],
            opportunity='Med',
            confidence='R',
            next_step='Watch.',
        )
        for n in range(12)
    ]
    by_market = {
        m: [
            MarketLine(item_ids=[items[n].id], one_liner=f'{m} line {n} ' + 'w' * 200)
            for n in range(start, start + 5)
        ]
        for m, start in (('US', 0), ('UK', 5), ('BR', 10))
    }
    review = WeekInReview(
        themes=[
            MarketLine(item_ids=[items[n].id], one_liner=f'Theme {n}') for n in range(5)
        ],
        trend_notes='Long trend notes. ' * 30,
    )
    return make_digest(
        items,
        top_stories=stories,
        forecast_impact=impacts,
        radar=radar,
        by_market=by_market,
        week_in_review=review,
    )


@pytest.fixture
def many_items() -> list[Item]:
    return [make_item(n) for n in range(15)]


def test_realistic_digest_is_concise(many_items: list[Item]) -> None:
    parts = render_slack(make_ctx(realistic_digest(many_items), many_items))
    main = parts[0]
    assert len(main) <= 1500
    assert main.count('• [US]') == 3
    assert main.count('`LTF`') == 3
    assert sum(len(p) for p in parts) <= 6000
    assert not any(p.startswith('**Watchlist highlights**') for p in parts)
    footer = parts[-1]
    assert len(footer.splitlines()) == 1
    assert footer.endswith('— end of digest · 3 items —')
    week = next(p for p in parts if p.startswith('**Week in review**'))
    assert week.count('\n• ') == 3
    assert 'trend notes' not in week.lower()


def test_part_zero_items_are_not_repeated(many_items: list[Item]) -> None:
    parts = render_slack(make_ctx(realistic_digest(many_items), many_items))
    shown = [i for i in many_items if i.url in parts[0]]
    assert len(shown) == 3
    later = [p for p in parts[1:] if not p.startswith('**Week in review**')]
    for item in shown:
        assert all(item.url not in p for p in later)
    research = next(p for p in parts if p.startswith('**Market research**'))
    rows = [ln for ln in research.splitlines() if ln.startswith('| US')]
    assert len(rows) == 6
    assert all(f'Radar signal {n}' not in research for n in range(3))
    by_market = next(p for p in parts if p.startswith('**By market**'))
    assert by_market.count('\n• ') == 8  # US 3-4, then three each for UK and BR
    assert 'US line 0' not in by_market
    assert all(len(ln) < 300 for ln in by_market.splitlines())


def test_every_part_fits_for_large_digest(items: list[Item]) -> None:
    parts = render_slack(make_ctx(big_digest(items), items), max_chars=600)
    assert all(len(p) <= 600 for p in parts)
    assert '_+' in parts[3]
    assert 'more in the local report_' in parts[3]


def test_deterministic(items: list[Item]) -> None:
    ctx = make_ctx(big_digest(items), items)
    assert render_slack(ctx) == render_slack(ctx)


def test_link_targets_come_from_items(items: list[Item]) -> None:
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(
        update={'summary': 'See [x](https://evil.example) now'}
    )
    line = MarketLine(item_ids=[], one_liner='[y](https://evil2.example)')
    digest = digest.model_copy(
        update={'top_stories': [story], 'by_market': {'US': [line]}}
    )
    parts = render_slack(make_ctx(digest, items))
    targets = {t for p in parts for t in LINK.findall(p)}
    assert targets
    assert targets <= {i.url for i in items}
    assert '[x] (hxxps[:]//evil[.]example)' in parts[0]


def test_main_post_layout(items: list[Item]) -> None:
    parts = render_slack(make_ctx(make_digest(items), items))
    main = parts[0]
    lines = main.splitlines()
    assert lines[0] == '**Market reading · Fri 9 Oct** · 120 items · 1 watchlist hits'
    order = ['**Top stories**', '**Forecast impact**']
    positions = [main.index(h) for h in order]
    assert positions == sorted(positions)
    assert (
        '• [US] FERC finalises large-load rule — Summary of the story.'
        ' ([Source 0](https://example.com/story-0?a=1&b=2))'
    ) in main
    assert (
        '• `STF` `CP-Alerts` Acme Utility — Larger loads need better forecasts.'
        ' → Ask product to scope a pilot.\n'
    ) in main
    assert 'Next:' not in main
    assert lines[-1] == '_More in thread ↓_'


def test_impact_names_the_market_without_accounts_and_links_new_items(
    items: list[Item],
) -> None:
    digest = make_digest(items)
    impact = digest.forecast_impact[0].model_copy(
        update={'accounts': [], 'item_ids': [items[0].id, items[2].id]}
    )
    digest = digest.model_copy(update={'forecast_impact': [impact]})
    main = render_slack(make_ctx(digest, items))[0]
    line = next(ln for ln in main.splitlines() if ln.startswith('• `STF`'))
    assert ' US — ' in line
    assert line.endswith('([Source 2](https://example.com/story-2?a=1&b=2))')


def test_impact_on_a_shown_story_carries_no_link(items: list[Item]) -> None:
    main = render_slack(make_ctx(make_digest(items), items))[0]
    line = next(ln for ln in main.splitlines() if ln.startswith('• `STF`'))
    assert '](' not in line


def test_old_digests_without_short_fields_fall_back_to_clipped_text(
    items: list[Item],
) -> None:
    long = ' '.join(f'word{n}' for n in range(110))
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(update={'headline': long, 'summary': long})
    impact = digest.forecast_impact[0].model_copy(
        update={'so_what': long, 'suggested_action': long}
    )
    digest = digest.model_copy(
        update={'top_stories': [story], 'forecast_impact': [impact]}
    )
    main = render_slack(make_ctx(digest, items))[0]
    assert 'word109' not in main
    assert main.count('\u2026') == 4
    assert len(main) <= 1500


def test_no_line_starts_with_whitespace(items: list[Item]) -> None:
    parts = render_slack(make_ctx(big_digest(items), items))
    indented = [
        line for p in parts for line in p.splitlines() if line[:1] in {' ', '\t'}
    ]
    assert indented == []


def test_thread_part_order(items: list[Item]) -> None:
    digest = make_digest(items)
    row = digest.radar[0].model_copy(update={'item_ids': [items[2].id]})
    digest = digest.model_copy(update={'radar': [row]})
    parts = render_slack(make_ctx(digest, items))
    heads = [p.splitlines()[0] for p in parts[1:]]
    assert heads[:4] == [
        '**Market research**',
        '**Upcoming dates**',
        '**By market**',
        '**Week in review**',
    ]
    assert heads[4].startswith('Sources: ')
    assert len(parts) == 6
    assert '| Market | Signal | Next date | Lens | Opp. | Conf. |' in parts[1]
    assert 'Deep dive' not in parts[1]
    assert '• Sun 1 Nov · [US] Comments due' in parts[2]


def test_friday_has_week_in_review_and_other_days_do_not(items: list[Item]) -> None:
    friday = render_slack(make_ctx(make_digest(items), items))
    assert any(p.startswith('**Week in review**') for p in friday)
    assert '_Nothing notable this week._' in friday[-2]
    thursday = make_digest(items, run_date=date(2026, 10, 8))
    assert not any(
        p.startswith('**Week in review**')
        for p in render_slack(make_ctx(thursday, items))
    )
    with_review = make_digest(
        items,
        run_date=date(2026, 10, 8),
        week_in_review=WeekInReview(
            themes=[MarketLine(item_ids=[items[1].id], one_liner='Theme one')],
            trend_notes='Trend.',
        ),
    )
    parts = render_slack(make_ctx(with_review, items))
    assert '• Theme one ([Source 1]' in parts[-2]


def test_degraded_banner(items: list[Item]) -> None:
    parts = render_slack(make_ctx(make_digest(items, degraded=True), items))
    assert '> ⚠️ Headlines only today' in parts[0]
    assert '⚠️' not in render_slack(make_ctx(make_digest(items), items))[0]


def test_footer_and_sentinel(items: list[Item]) -> None:
    parts = render_slack(make_ctx(make_digest(items), items))
    assert parts[-1] == (
        'Sources: 1 unavailable · C/R/I = confirmed/reported/inferred · '
        'Full report: /var/reports/2026-10-09.html · — end of digest · 3 items —'
    )
    healthy = make_ctx(make_digest(items), items, source_health={'a': {'ok': True}})
    assert render_slack(healthy)[-1].startswith('Sources: all OK · ')


def test_empty_sections_use_placeholder(items: list[Item]) -> None:
    digest = make_digest(
        items, top_stories=[], forecast_impact=[], radar=[], upcoming=[], by_market={}
    )
    parts = render_slack(make_ctx(digest, items, scored=[]))
    assert parts[0].count('_Nothing notable today._') == 2
    for part in parts[1:4]:
        assert '_Nothing notable today._' in part


def test_table_pipes_escaped(items: list[Item]) -> None:
    digest = make_digest(items)
    row = digest.radar[0].model_copy(
        update={'signal': 'A | B [c](https://e.example)', 'item_ids': [items[2].id]}
    )
    parts = render_slack(make_ctx(digest.model_copy(update={'radar': [row]}), items))
    assert 'A \u2223 B [c] (hxxps[:]//e[.]example)' in parts[1]


def test_render_all_fills_slack_parts(items: list[Item]) -> None:
    ctx = make_ctx(make_digest(items), items)
    out = render_all(ctx, 40_000, max_slack_chars=3000)
    assert out.slack_parts == render_slack(ctx, 3000)


def test_slack_parts_are_distinct(items: list[Item]) -> None:
    parts = render_slack(make_ctx(make_digest(items), items))
    assert len(set(parts)) == len(parts)


def test_model_text_cannot_create_slack_autolinks(items: list[Item]) -> None:
    evil = 'Go <https://evil.example|click> or https://evil.example or www.evil.example'
    digest = make_digest(items)
    story = digest.top_stories[0].model_copy(update={'summary': evil})
    line = MarketLine(item_ids=[], one_liner=evil)
    digest = digest.model_copy(
        update={'top_stories': [story], 'by_market': {'US': [line]}}
    )
    parts = render_slack(make_ctx(digest, items))
    text = '\n'.join(parts)
    assert '\u2039hxxps[:]//evil[.]example|click\u203a' in parts[0]
    assert 'www[.]evil[.]example' in parts[0]
    assert not re.search(r'<[^>\s]*>|https?://evil|www\.evil', text)
    urls = set(re.findall(r'https?://[^\s)>|]+', text))
    assert urls <= {i.url for i in items}


def test_main_post_is_capped_at_three_entries_each() -> None:
    many = [make_item(n) for n in range(6)]
    summary = ' '.join(f'word{n}' for n in range(110))[:600]
    stories = [
        Story(
            item_ids=[many[n].id],
            headline=f'FERC large-load rule {n}',
            summary=summary,
            market='US',
        )
        for n in range(5)
    ]
    impacts = [
        ForecastImpact(
            item_ids=[many[n].id, many[(n + 1) % 6].id],
            product_lines=['STF'],
            impact_type='opportunity',
            so_what=summary,
            suggested_action='Scope a pilot.',
            accounts=[],
        )
        for n in range(6)
    ]
    digest = make_digest(many, top_stories=stories, forecast_impact=impacts)
    main = render_slack(make_ctx(digest, many))[0]
    assert len(main) <= 1500
    assert main.count('• [US]') == 3
    assert main.count('• `STF`') == 3
    assert 'more in the local report' not in main
    assert 'word109' not in main
    assert main.count('](') == 4  # three stories plus the impact on item 3


def test_clip_cuts_at_word_boundary() -> None:
    assert _clip('alpha beta gamma', 11) == 'alpha beta\u2026'
    assert _clip('short', 11) == 'short'
    assert len(_clip('word ' * 200)) <= 280


def test_clip_hard_cuts_when_no_word_boundary_leaves_a_head() -> None:
    clipped = _clip(' ' + 'a' * 400, 280)
    assert clipped != '\u2026'
    assert len(clipped) == 280
    assert clipped.endswith('a\u2026')
    assert _clip('a' * 400, 280) == 'a' * 279 + '\u2026'


def test_clip_hard_cuts_when_word_boundary_would_drop_most_of_the_text() -> None:
    clipped = _clip('a ' + 'x' * 300, 50)
    assert len(clipped) == 50
    assert clipped.index('\u2026') >= 25


def test_markdown_link_syntax_cannot_be_assembled_by_defanging() -> None:
    assert '](' not in _md('see www.(//evil.ru/x)')


def test_model_bold_markers_become_lookalikes() -> None:
    out = _md('**x** and `code`')
    assert '*' not in out
    assert '`' not in out
    assert out == '\u2217\u2217x\u2217\u2217 and \u02cbcode\u02cb'


def _story_digest(items: list[Item], **story: str) -> Any:  # noqa: ANN401
    digest = make_digest(items)
    top = digest.top_stories[0].model_copy(update=story)
    return digest.model_copy(update={'top_stories': [top]})


def test_bold_uses_standard_markdown(items: list[Item]) -> None:
    parts = render_slack(make_ctx(make_digest(items), items))
    for part in parts:
        assert not re.search(r'(?<![*\w])\*[^*\n]+\*(?!\*)', part)
    assert all(p.splitlines()[0].startswith('**') for p in parts[:2])


def test_markdown_control_characters_in_headlines_cannot_break_bold(
    items: list[Item],
) -> None:
    digest = _story_digest(items, headline='Rule *big* _news_ ~x~ `y` back\\slash')
    main = render_slack(make_ctx(digest, items))[0]
    assert '• [US] Rule \u2217big\u2217 _news_ ~x~ \u02cby\u02cb back\\slash —' in main


def test_source_label_shows_publisher_host_not_raw_url(items: list[Item]) -> None:
    labelled = [
        items[0].model_copy(update={'source_name': 'https://www.example.co.uk/'}),
        *items[1:],
    ]
    main = render_slack(make_ctx(make_digest(labelled), labelled))[0]
    assert '[example.co.uk](' in main
    assert 'https://www.example.co.uk' not in main


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('see evil.com/x now', 'see evil[.]com/x now'),
        ('Visit EVIL.ORG today', 'Visit EVIL[.]ORG today'),
        ('try shop-1.io, ok', 'try shop-1[.]io, ok'),
        ('ends in the company', 'ends in the company'),
        ('v1.5 of the rule', 'v1.5 of the rule'),
        ('evil.ru', 'evil[.]ru'),
        ('ofgem.gov.uk', 'ofgem[.]gov[.]uk'),
        ('up 4.5 GW', 'up 4.5 GW'),
        ('the U.S. market, e.g. Texas', 'the U.S. market, e.g. Texas'),
    ],
)
def test_bare_domains_are_defanged(text: str, expected: str) -> None:
    assert _md(text) == expected


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('xhttps://a.example', 'xhxxps[:]//a[.]example'),
        ('ftp://a.example', 'ftp[:]//a[.]example'),
        ('slack://channel?id=1', 'slack[:]//channel?id=1'),
        ('HTTP://a.example', 'hxxp[:]//a[.]example'),
    ],
)
def test_any_url_scheme_is_defanged(text: str, expected: str) -> None:
    assert _md(text) == expected


def test_table_cell_pipe_becomes_a_lookalike_character(items: list[Item]) -> None:
    digest = make_digest(items)
    row = digest.radar[0].model_copy(
        update={'signal': 'A | B\nC', 'item_ids': [items[2].id]}
    )
    parts = render_slack(make_ctx(digest.model_copy(update={'radar': [row]}), items))
    assert '| US | A \u2223 B C |' in parts[1]
    assert '\\|' not in parts[1]
