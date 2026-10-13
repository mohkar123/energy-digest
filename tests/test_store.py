from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from energy_digest.models import Item, Score, ScoredItem
from energy_digest.store import Store


def _item(item_id: str = 'abc123', title: str = 'Ofgem price cap update') -> Item:
    return Item(
        id=item_id,
        url=f'https://example.com/{item_id}',
        canonical_url=f'https://example.com/{item_id}',
        title=title,
        source_id='s1',
        source_name='Source One',
        source_kind='rss',
        tier='press',
        market_hint='UK',
        published=datetime(2026, 10, 8, 12, tzinfo=UTC),
        snippet='Regulator raises the cap',
    )


def _scored(item: Item) -> ScoredItem:
    score = Score(
        item_id=item.id,
        market='UK',
        topic='rate-case-tariff',
        market_relevance=4,
        forecast_relevance=3,
        forecast_lines=['STF'],
        dated_event=None,
        is_advert=False,
    )
    return ScoredItem(item=item, score=score, watchlist_hits=[], rank=1.0)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    db = Store(tmp_path / 'nested' / 'digest.db')
    yield db
    db.close()


def _finish(store: Store, run_date: date, status: str, *, sent: bool) -> str:
    run_id = store.start_run(run_date)
    store.finish_run(
        run_id, status, sent=sent, message_id=None, source_health={}, cost_usd=0.1
    )
    return run_id


def test_upsert_reports_new_ids_once(store: Store) -> None:
    item = _item()
    assert store.upsert_items([item]) == ['abc123']
    assert store.upsert_items([item]) == []
    assert store.get_items(['abc123', 'missing']) == {'abc123': item}


def test_upsert_keeps_first_seen(store: Store) -> None:
    store.upsert_items([_item()])
    store.upsert_items([_item(title='Updated')])
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert [i.title for i in store.items_since(since)] == ['Updated']
    assert store.items_since(datetime.now(UTC) + timedelta(minutes=1)) == []


def test_already_sent_flips_after_finish(store: Store) -> None:
    today = date(2026, 10, 9)
    run_id = store.start_run(today)
    assert not store.already_sent(today)
    store.finish_run(
        run_id, 'ok', sent=True, message_id='m1', source_health={}, cost_usd=1.0
    )
    assert store.already_sent(today)


def test_search_finds_indexed_item_and_survives_quotes(store: Store) -> None:
    item = _item()
    store.upsert_items([item])
    store.save_scores([_scored(item)])
    store.index_summary(item.id, 'cap rises for households')
    hits = store.search('ofgem')
    assert [h['item_id'] for h in hits] == ['abc123']
    assert store.search('households')[0]['summary'] == 'cap rises for households'
    assert store.search('ofgem', market='US') == []
    assert store.search('ofgem', since=date(2026, 10, 9)) == []
    assert store.search('ofgem', since=date(2026, 10, 8), market='UK')
    assert store.search('a"b') == []
    assert store.search('   ') == []


def test_rescoring_preserves_summary(store: Store) -> None:
    item = _item()
    store.save_scores([_scored(item)])
    store.index_summary(item.id, 'kept summary')
    store.save_scores([_scored(item)])
    assert store.search('kept')[0]['item_id'] == item.id


def test_last_success_started_ignores_failures(store: Store) -> None:
    assert store.last_success_started() is None
    _finish(store, date(2026, 10, 7), 'failed', sent=False)
    assert store.last_success_started() is None
    _finish(store, date(2026, 10, 8), 'ok', sent=True)
    delivered = store.last_success_started()
    assert delivered is not None
    _finish(store, date(2026, 10, 9), 'dry-run', sent=False)
    assert store.last_success_started() == delivered


def test_last_success_started_counts_send_unknown(store: Store) -> None:
    _finish(store, date(2026, 10, 8), 'send-unknown', sent=True)
    assert store.last_success_started() is not None


def test_dry_run_used_only_when_nothing_else(store: Store) -> None:
    _finish(store, date(2026, 10, 9), 'dry-run', sent=False)
    assert store.last_success_started() is not None


def test_mark_sent(store: Store) -> None:
    store.upsert_items([_item('a'), _item('b')])
    store.mark_sent(['a'], date(2026, 10, 9))
    assert store.sent_item_ids() == {'a'}


def test_redirect_and_feed_state_round_trip(store: Store) -> None:
    assert store.get_redirect('u') is None
    store.put_redirect('u', 'https://x.test')
    assert store.get_redirect('u') == 'https://x.test'
    assert store.get_feed_state('f') == (None, None)
    store.put_feed_state('f', 'etag1', 'Mon')
    assert store.get_feed_state('f') == ('etag1', 'Mon')


def test_abandon_stale_runs_only_touches_old_running_rows(store: Store) -> None:
    stale = store.start_run(date(2026, 10, 7))
    fresh = store.start_run(date(2026, 10, 8))
    done = _finish(store, date(2026, 10, 6), 'ok', sent=True)
    long_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with store._conn:
        for run_id in (stale, done):
            store._conn.execute(
                'UPDATE runs SET started = ? WHERE run_id = ?', (long_ago, run_id)
            )
    assert store.abandon_stale_runs(timedelta(hours=2)) == 1
    statuses = {
        r['run_id']: r['status']
        for r in store._conn.execute('SELECT run_id, status FROM runs')
    }
    assert statuses == {stale: 'abandoned', fresh: 'running', done: 'ok'}


def test_sent_canonical_urls_and_first_seen(store: Store) -> None:
    a, b = _item('a'), _item('b')
    store.upsert_items([a, b])
    store.mark_sent(['a'], date(2026, 10, 9))
    assert store.sent_canonical_urls() == {a.canonical_url}
    assert store.first_seen('a') is not None
    assert store.first_seen('missing') is None
