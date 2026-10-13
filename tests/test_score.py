import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from energy_digest.config import (
    AppConfig,
    ClaudeCfg,
    DeliveryCfg,
    Settings,
    WatchEntry,
)
from energy_digest.models import Item, Market, Score, ScoredItem
from energy_digest.score import (
    keyword_score,
    score_items,
    select,
    watchlist_hits,
)

WATCH = [
    WatchEntry(name='Northwind', kind='customer', market='NZ'),
    WatchEntry(
        name='Highland Networks',
        kind='customer',
        market='UK',
        aliases=['HighlandNetworks'],
    ),
]


def _item(
    id_: str,
    title: str = 'Title',
    *,
    market_hint: Market | None = 'US',
    snippet: str = '',
) -> Item:
    base: dict[str, Any] = {
        'id': id_,
        'url': f'https://x.example/{id_}',
        'canonical_url': f'https://x.example/{id_}',
        'title': title,
        'source_id': 's',
        'source_name': 'Src',
        'source_kind': 'rss',
        'tier': 'press',
        'market_hint': market_hint,
        'snippet': snippet,
        'published': datetime(2026, 10, 8, tzinfo=UTC),
    }
    return Item(**base)


def _score(
    id_: str,
    market: Market = 'US',
    mr: int = 3,
    fr: int = 3,
    *,
    is_advert: bool = False,
) -> Score:
    return Score(
        item_id=id_,
        market=market,
        topic='grid-planning',
        market_relevance=mr,
        forecast_relevance=fr,
        forecast_lines=[],
        dated_event=None,
        is_advert=is_advert,
    )


def _scored(
    id_: str,
    market: Market = 'US',
    rank: float = 4.0,
    *,
    mr: int = 3,
    is_advert: bool = False,
) -> ScoredItem:
    return ScoredItem(
        item=_item(id_, market_hint=market),
        score=_score(id_, market, mr=mr, is_advert=is_advert),
        watchlist_hits=[],
        rank=rank,
    )


def _cfg(timeout_s: int = 600) -> AppConfig:
    settings = Settings(
        delivery=DeliveryCfg(slack_user_id='U1', slack_dm_channel='D1'),
        newsletter_senders=[],
        claude=ClaudeCfg(disallow_mcp=[], timeout_s=timeout_s),
        data_dir=Path('d'),
        reports_dir=Path('r'),
    )
    return AppConfig(
        settings=settings,
        sources=[],
        watchlist=WATCH,
        company_context='CTX',
        market_context={},
    )


class FakeRunner:
    def __init__(self, scores: list[Score] | None, rc: int = 0) -> None:
        self.scores = scores
        self.rc = rc
        self.argvs: list[list[str]] = []
        self.stdins: list[str] = []
        self.timeouts: list[int] = []

    def __call__(
        self,
        argv: list[str],
        stdin_text: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
    ) -> tuple[int, str, str]:
        self.argvs.append(argv)
        self.stdins.append(stdin_text)
        self.timeouts.append(timeout_s)
        if self.scores is None:
            return 1, 'not json', 'boom'
        payload = {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'result': '',
            'total_cost_usd': 0.01,
            'structured_output': {
                'scores': [s.model_dump(mode='json') for s in self.scores]
            },
        }
        return self.rc, json.dumps(payload), ''


def test_watchlist_word_boundary() -> None:
    assert watchlist_hits(_item('a', 'Northwind files AMP'), WATCH) == ['Northwind']
    assert watchlist_hits(_item('b', 'Northwindoling expands'), WATCH) == []


def test_watchlist_aliases_deduped_in_order() -> None:
    item = _item('a', 'HighlandNetworks and Highland Networks', snippet='northwind too')
    assert watchlist_hits(item, WATCH) == ['Northwind', 'Highland Networks']


def test_score_items_maps_scores_and_ignores_unknown_ids(tmp_path: Path) -> None:
    runner = FakeRunner([_score('a', mr=5, fr=4), _score('zzz')])
    out = score_items(
        [_item('a', 'Northwind files AMP'), _item('b')],
        _cfg(),
        cwd=tmp_path,
        runner=runner,
    )
    by_id = {s.item.id: s for s in out}
    assert by_id['a'].score.market_relevance == 5
    assert by_id['a'].watchlist_hits == ['Northwind']
    assert by_id['a'].rank == 0.5 * 5 + 0.5 * 4 + 1 + 1
    # b got no model score so it falls back to keywords.
    assert by_id['b'].score == keyword_score(_item('b'))
    assert 'zzz' not in by_id


def test_score_items_batches_of_25(tmp_path: Path) -> None:
    runner = FakeRunner([])
    items = [_item(f'i{n}') for n in range(30)]
    out = score_items(items, _cfg(), cwd=tmp_path, runner=runner)
    assert len(out) == 30
    assert len(runner.argvs) == 2
    assert len(runner.stdins[0].splitlines()) == 25


def test_claude_error_falls_back_to_keywords(tmp_path: Path) -> None:
    runner = FakeRunner(None)
    items = [_item('a', 'New tariff filed'), _item('b', 'Free playbook download')]
    out = score_items(items, _cfg(), cwd=tmp_path, runner=runner)
    assert out[0].score.topic == 'rate-case-tariff'
    assert out[1].score.is_advert


def test_argv_is_isolated(tmp_path: Path) -> None:
    runner = FakeRunner([])
    score_items([_item('a')], _cfg(), cwd=tmp_path, runner=runner)
    argv = runner.argvs[0]
    assert '--strict-mcp-config' in argv
    assert argv[argv.index('--model') + 1] == 'haiku'
    assert argv[argv.index('--tools') + 1] == ''


def test_keyword_score_defaults_and_portuguese() -> None:
    plain = keyword_score(_item('a', 'Something', market_hint=None))
    assert (plain.market, plain.market_relevance, plain.forecast_relevance) == (
        'OTHER',
        2,
        1,
    )
    pt = keyword_score(_item('b', 'Aneel anuncia bandeira amarela', market_hint='BR'))
    assert pt.topic == 'rate-case-tariff'
    assert pt.market_relevance == 3
    dc = keyword_score(_item('c', 'New data center load'))
    assert dc.topic == 'large-load-data-centres'


def test_select_drops_adverts_and_low_relevance() -> None:
    scored = [
        _scored('a', rank=9),
        _scored('b', rank=8, is_advert=True),
        _scored('c', rank=7, mr=1),
    ]
    assert [s.item.id for s in select(scored, 10)] == ['a']


def test_select_market_floor() -> None:
    scored = [_scored(f'us{n}', 'US', rank=9 - n * 0.1) for n in range(5)]
    scored += [_scored(f'nz{n}', 'NZ', rank=1 + n * 0.1) for n in range(3)]
    out = select(scored, 3)
    ids = [s.item.id for s in out]
    assert ids[:3] == ['us0', 'us1', 'us2']
    assert sorted(i for i in ids if i.startswith('nz')) == ['nz1', 'nz2']
    assert [s.rank for s in out] == sorted((s.rank for s in out), reverse=True)


def test_select_is_deterministic_on_ties() -> None:
    scored = [_scored(i, rank=5) for i in ('c', 'a', 'b')]
    first = select(scored, 2)
    assert [s.item.id for s in first] == ['a', 'b']
    assert select(list(reversed(scored)), 2) == first


def test_scoring_system_prompt_lists_watchlist_roles(tmp_path: Path) -> None:
    runner = FakeRunner([])
    score_items([_item('a')], _cfg(), cwd=tmp_path, runner=runner)
    argv = runner.argvs[0]
    system = argv[argv.index('--system-prompt') + 1]
    assert 'Northwind — customer — NZ' in system
    assert 'Highland Networks — customer — UK — HighlandNetworks' in system


def test_scoring_timeout_follows_settings_but_is_capped_at_300(tmp_path: Path) -> None:
    for configured, expected in ((600, 300), (120, 120)):
        runner = FakeRunner([])
        score_items([_item('a')], _cfg(configured), cwd=tmp_path, runner=runner)
        assert runner.timeouts == [expected]


def test_expired_deadline_skips_the_model_and_uses_keywords(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner([])
    items = [_item(f'i{n}', 'New tariff filed') for n in range(30)]
    with caplog.at_level('WARNING'):
        out = score_items(
            items,
            _cfg(),
            cwd=tmp_path,
            runner=runner,
            deadline=time.monotonic() - 1,
        )
    assert runner.argvs == []
    assert [s.score for s in out] == [keyword_score(i) for i in items]
    assert len([r for r in caplog.records if 'deadline' in r.getMessage()]) == 1


def test_deadline_passing_mid_run_only_degrades_remaining_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner([])
    clock = iter([0.0, 100.0])  # first batch is in time, the second is not
    monkeypatch.setattr(time, 'monotonic', lambda: next(clock))
    items = [_item(f'i{n}') for n in range(30)]
    out = score_items(items, _cfg(), cwd=tmp_path, runner=runner, deadline=50.0)
    assert len(out) == 30
    assert len(runner.argvs) == 1


def test_far_deadline_does_not_change_scoring(tmp_path: Path) -> None:
    runner = FakeRunner([_score('a', mr=5)])
    out = score_items(
        [_item('a')],
        _cfg(),
        cwd=tmp_path,
        runner=runner,
        deadline=time.monotonic() + 3600,
    )
    assert out[0].score.market_relevance == 5


def test_keyword_matching_respects_word_boundaries() -> None:
    # 'bill' is inside 'billion' and 'irp' inside 'chirp': neither is a topic hit
    for title in ('Billion dollar merger announced', 'Chirp of the week'):
        assert keyword_score(_item('a', title)).topic == 'policy-legislation'
        assert keyword_score(_item('a', title)).market_relevance == 2
    bill = keyword_score(_item('b', 'Senate passes energy bill'))
    assert bill.topic == 'policy-legislation'
    assert bill.market_relevance == 3
    assert keyword_score(_item('c', 'New BESS tariffs filed')).topic == (
        'rate-case-tariff'
    )
    assert keyword_score(_item('d', 'Home batteries surge')).topic == 'der-vpp-bess'
    assert keyword_score(_item('e', 'Storage shortage')).topic == 'der-vpp-bess'
    assert keyword_score(_item('f', 'Overstorage')).topic == 'policy-legislation'


def test_select_floor_tops_up_to_two_per_market_never_beyond() -> None:
    scored = [_scored(f'us{n}', 'US', rank=9 - n * 0.1) for n in range(4)]
    scored += [_scored('nz_top', 'NZ', rank=8.5)]  # already chosen via top_n
    scored += [_scored(f'nz{n}', 'NZ', rank=1 + n * 0.1) for n in range(3)]
    ids = [s.item.id for s in select(scored, 3)]
    nz = [i for i in ids if i.startswith('nz')]
    assert len(nz) == 2
    assert 'nz_top' in nz
