import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from energy_digest.config import AppConfig, ClaudeCfg, DeliveryCfg, Settings, WatchEntry
from energy_digest.models import (
    DeepDive,
    Digest,
    DigestDraft,
    ForecastImpact,
    Item,
    Market,
    Score,
    ScoredItem,
    Story,
)
from energy_digest.schema import claude_json_schema
from energy_digest.store import Store
from energy_digest.synthesize import (
    MAX_WEEK_ITEMS,
    build_prompt,
    deep_dive_market,
    degraded_digest,
    digest_summary,
    synthesize,
    validate_draft,
    week_items,
)

MONDAY = date(2026, 10, 5)
FRIDAY = date(2026, 10, 9)
IDS = {f'a{n}' for n in range(1, 7)}


def _scored(id_: str, market: Market = 'US', title: str = 'Headline') -> ScoredItem:
    item = Item(
        id=id_,
        url=f'https://x.example/{id_}',
        canonical_url=f'https://x.example/{id_}',
        title=title,
        source_id='s',
        source_name='Src',
        source_kind='rss',
        tier='primary',
        market_hint=market,
        published=datetime(2026, 10, 8, tzinfo=UTC),
        snippet='snippet text',
        text='full article text ' * 400,
        headline_only=False,
    )
    score = Score(
        item_id=id_,
        market=market,
        topic='grid-planning',
        market_relevance=4,
        forecast_relevance=3,
        forecast_lines=[],
        dated_event=None,
        is_advert=False,
    )
    return ScoredItem(item=item, score=score, watchlist_hits=['Northwind'], rank=5)


SELECTED = [
    _scored(f'a{n}', 'NZ' if n % 2 else 'US', f'Title {n}') for n in range(1, 7)
]


def _cfg() -> AppConfig:
    settings = Settings(
        delivery=DeliveryCfg(slack_user_id='U1', slack_dm_channel='D1'),
        newsletter_senders=[],
        claude=ClaudeCfg(disallow_mcp=[]),
        data_dir=Path('d'),
        reports_dir=Path('r'),
    )
    return AppConfig(
        settings=settings,
        sources=[],
        watchlist=[],
        company_context='COMPANY-CTX',
        market_context={m: f'CTX-{m}' for m in ('US', 'UK', 'BR', 'NZ', 'OTHER')},
    )


def _draft(fixtures_dir: Path) -> DigestDraft:
    return DigestDraft.model_validate_json(
        (fixtures_dir / 'synthesis_response.json').read_text()
    )


class FakeRunner:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload
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
        self.argv, self.stdin = argv, stdin_text
        if self.payload is None:
            return 1, 'garbage', 'boom'
        out = {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'result': '',
            'total_cost_usd': 0.2,
            'structured_output': self.payload,
        }
        return 0, json.dumps(out), ''


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / 'digest.db')
    yield s
    s.close()


def test_deep_dive_market_full_week() -> None:
    days = [date(2026, 10, 5 + n) for n in range(7)]
    assert [deep_dive_market(d) for d in days] == [
        'US',
        'UK',
        'BR',
        'NZ',
        'OTHER',
        'US',
        'US',
    ]


def test_validate_draft_drops_unknown_and_filters_mixed(fixtures_dir: Path) -> None:
    clean, dropped = validate_draft(_draft(fixtures_dir), IDS)
    assert [r.item_ids for r in clean.radar] == [['a4']]
    assert [s.item_ids for s in clean.top_stories][1] == ['a2']
    assert dropped >= 2


def test_validate_draft_strips_urls(fixtures_dir: Path) -> None:
    clean, _ = validate_draft(_draft(fixtures_dir), IDS)
    so_what = clean.forecast_impact[0].so_what
    assert 'http' not in so_what
    assert 'evil.example' not in so_what
    assert so_what.startswith('Large-load growth')


def test_validate_draft_clamps_lists(fixtures_dir: Path) -> None:
    draft = _draft(fixtures_dir)
    story = Story(item_ids=['a1'], headline='h', summary='s', market='US')
    draft = draft.model_copy(update={'top_stories': [story] * 9})
    clean, _ = validate_draft(draft, IDS)
    assert len(clean.top_stories) == 5


def test_validate_draft_strips_urls_from_short_fields() -> None:
    draft = DigestDraft(
        top_stories=[
            Story(
                item_ids=['a1'],
                headline='h',
                summary='s',
                market='US',
                headline_short='Rule https://evil.example/x done',
                why_short='See www.evil.example now',
            )
        ],
        by_market={},
        forecast_impact=[
            ForecastImpact(
                item_ids=['a1'],
                product_lines=['LTF'],
                impact_type='risk',
                so_what='x',
                suggested_action='y',
                accounts=[],
                so_what_short='Bad https://evil.example',
                action_short='Go www.evil.example',
            )
        ],
        radar=[],
        upcoming=[],
        deep_dive=DeepDive(
            market='US',
            what_moved=[],
            regulatory_calendar=[],
            account_implications=[],
            open_questions=[],
            competitors_mentioned=[],
        ),
        week_in_review=None,
    )
    clean, dropped = validate_draft(draft, IDS)
    story, impact = clean.top_stories[0], clean.forecast_impact[0]
    texts = (
        story.headline_short,
        story.why_short,
        impact.so_what_short,
        impact.action_short,
    )
    assert all('evil' not in t for t in texts)
    assert dropped == 4


def test_prompt_asks_for_short_fields_with_word_limits() -> None:
    system, _ = build_prompt(SELECTED, _cfg(), MONDAY, [], None)
    for needle in (
        'headline_short is 12 words or fewer',
        'why_short is 20 words or fewer',
        'so_what_short is 12 words or fewer',
        'action_short is 8 words or fewer',
        'one_liner at 20 words or fewer',
        'signal at 10 words or fewer',
    ):
        assert needle in system.replace('\n', ' ')


def test_schema_requires_the_short_fields() -> None:
    props = claude_json_schema(DigestDraft)['properties']
    story = props['top_stories']['items']
    impact = props['forecast_impact']['items']
    assert {'headline_short', 'why_short'} <= set(story['required'])
    assert {'so_what_short', 'action_short'} <= set(impact['required'])


def test_digests_without_short_fields_still_validate() -> None:
    story = Story.model_validate(
        {'item_ids': ['a1'], 'headline': 'h', 'summary': 's', 'market': 'US'}
    )
    assert story.headline_short == ''
    assert story.why_short == ''


def test_build_prompt_friday_includes_week(fixtures_dir: Path) -> None:
    monday_digest = Digest(
        **_draft(fixtures_dir).model_dump(),
        run_date=MONDAY,
        deep_dive_market='US',
    )
    system, user = build_prompt(
        SELECTED, _cfg(), FRIDAY, [(MONDAY, 'Old story')], [monday_digest]
    )
    assert system.startswith('COMPANY-CTX')
    assert 'CTX-OTHER' in system
    assert 'Week so far' in user
    assert 'PUC approves rate case' in user
    assert 'Deep-dive market: OTHER' in user
    assert 'Old story' in user
    assert '[a1] (NZ, primary, Src, 2026-10-08, watchlist: Northwind) Title 1' in user


def test_build_prompt_monday_has_no_week_and_caps_text() -> None:
    system, user = build_prompt(SELECTED, _cfg(), MONDAY, [], None)
    assert 'Week so far' not in user
    assert 'Recent digests' not in user
    assert 'CTX-US' in system
    assert max(len(block) for block in user.split('\n\n')) < 2600


def test_synthesize_validates_and_uses_isolated_profile(
    fixtures_dir: Path, store: Store, tmp_path: Path
) -> None:
    payload = json.loads((fixtures_dir / 'synthesis_response.json').read_text())
    runner = FakeRunner(payload)
    digest = synthesize(SELECTED, _cfg(), store, MONDAY, cwd=tmp_path, runner=runner)
    assert not digest.degraded
    assert digest.dropped_entries >= 2
    assert digest.deep_dive_market == 'US'
    assert [r.item_ids for r in digest.radar] == [['a4']]
    assert '--strict-mcp-config' in runner.argv
    assert runner.argv[runner.argv.index('--model') + 1] == 'sonnet'


def test_synthesize_error_gives_degraded_digest(store: Store, tmp_path: Path) -> None:
    digest = synthesize(
        SELECTED, _cfg(), store, MONDAY, cwd=tmp_path, runner=FakeRunner(None)
    )
    assert digest.degraded
    assert digest.top_stories == []
    assert set(digest.by_market) == {'US', 'NZ'}
    assert digest.by_market['NZ'][0].one_liner == 'Title 1'


def test_synthesize_invalid_payload_gives_degraded_digest(
    store: Store, tmp_path: Path
) -> None:
    digest = synthesize(
        SELECTED, _cfg(), store, MONDAY, cwd=tmp_path, runner=FakeRunner({'x': 1})
    )
    assert digest.degraded


def test_degraded_digest_deep_dive_is_empty_for_day_market() -> None:
    digest = degraded_digest(SELECTED, date(2026, 10, 7))
    assert digest.deep_dive.market == 'BR'
    assert digest.deep_dive.what_moved == []
    assert digest.week_in_review is None


def test_digest_summary_joins_headlines(fixtures_dir: Path) -> None:
    digest = Digest(
        **_draft(fixtures_dir).model_dump(), run_date=MONDAY, deep_dive_market='US'
    )
    assert digest_summary(digest) == (
        'PUC approves rate case; Data-centre tariff proposed; Northwind files AMP'
    )


def test_system_prompt_grounds_account_roles_in_watchlist() -> None:
    cfg = _cfg().model_copy(
        update={
            'watchlist': [
                WatchEntry(
                    name='Harbor Power Co-op',
                    kind='customer',
                    market='US',
                    aliases=['Harbor Power'],
                ),
                WatchEntry(name='Prairie Electric', kind='prospect', market='US'),
                WatchEntry(name='Acme Analytics', kind='competitor', market='US'),
            ]
        }
    )
    system, _ = build_prompt(SELECTED, cfg, MONDAY, [], None)
    assert 'Harbor Power Co-op — customer — US — Harbor Power' in system
    assert 'Prairie Electric — prospect — US — no aliases' in system
    assert 'Acme Analytics — competitor — US — no aliases' in system
    assert 'prospects are prospects; never infer customer status.' in system


def _stored_item(id_: str, day: int, title: str = 'Stored') -> Item:
    return _scored(id_, title=title).item.model_copy(
        update={'published': datetime(2026, 10, day, tzinfo=UTC)}
    )


def test_friday_synthesis_without_earlier_digests_uses_stored_week_items(
    fixtures_dir: Path, store: Store, tmp_path: Path
) -> None:
    store.upsert_items(
        [_stored_item('w1', 6, 'Tuesday story'), _stored_item('w2', 7, 'Wed story')]
    )
    payload = json.loads((fixtures_dir / 'synthesis_response.json').read_text())
    payload['week_in_review'] = {
        'themes': [{'item_ids': ['w1', 'zz'], 'one_liner': 'Theme'}],
        'trend_notes': 'Trend',
    }
    runner = FakeRunner(payload)
    digest = synthesize(SELECTED, _cfg(), store, FRIDAY, cwd=tmp_path, runner=runner)
    assert 'Week so far' in runner.stdin
    assert '[w1] (US, Src, 2026-10-06) Tuesday story' in runner.stdin
    assert '[w2] (US, Src, 2026-10-07) Wed story' in runner.stdin
    assert digest.week_in_review is not None
    assert [t.item_ids for t in digest.week_in_review.themes] == [['w1']]


def test_non_friday_synthesis_ignores_week_items(
    fixtures_dir: Path, store: Store, tmp_path: Path
) -> None:
    store.upsert_items([_stored_item('w1', 6)])
    payload = json.loads((fixtures_dir / 'synthesis_response.json').read_text())
    runner = FakeRunner(payload)
    synthesize(SELECTED, _cfg(), store, MONDAY, cwd=tmp_path, runner=runner)
    assert 'Week so far' not in runner.stdin
    assert '[w1]' not in runner.stdin


def test_week_items_caps_and_ranks_scored_first(store: Store) -> None:
    many = [_stored_item(f'w{n:02d}', 6 + n % 3) for n in range(MAX_WEEK_ITEMS + 5)]
    store.upsert_items(many)
    store.save_scores([_scored('w00')])
    picked = week_items(store, FRIDAY, UTC)
    ids = [w.item.id for w in picked]
    assert len(ids) == MAX_WEEK_ITEMS
    assert ids[0] == 'w00'


def test_week_items_empty_off_friday(store: Store) -> None:
    store.upsert_items([_stored_item('w1', 6)])
    assert week_items(store, date(2026, 10, 8), UTC) == []


def test_week_items_start_at_monday_midnight_in_given_timezone(store: Store) -> None:
    store.upsert_items([_stored_item('early', 4), _stored_item('late', 5)])
    first_seen = {
        # Sunday 20:00 and Monday 01:00 in Los Angeles (PDT, UTC-7)
        'early': '2026-10-05T03:00:00+00:00',
        'late': '2026-10-05T08:00:00+00:00',
    }
    with store._conn:
        for item_id, moment in first_seen.items():
            store._conn.execute(
                'UPDATE items SET first_seen = ? WHERE id = ?', (moment, item_id)
            )
    picked = week_items(store, FRIDAY, ZoneInfo('America/Los_Angeles'))
    assert [w.item.id for w in picked] == ['late']
