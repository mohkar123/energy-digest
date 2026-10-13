"""SQLite store: item archive, scores, runs, digests, FTS search and caches."""

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from energy_digest.models import Digest, Item, ScoredItem

logger = logging.getLogger(__name__)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS items(
        id TEXT PRIMARY KEY, url_hash TEXT UNIQUE, json TEXT NOT NULL,
        published TEXT, first_seen TEXT, sent_on TEXT)""",
    """CREATE TABLE IF NOT EXISTS scores(
        item_id TEXT PRIMARY KEY, json TEXT, scored_on TEXT)""",
    """CREATE TABLE IF NOT EXISTS runs(
        run_id TEXT PRIMARY KEY, run_date TEXT, started TEXT, finished TEXT,
        status TEXT, sent INTEGER DEFAULT 0, message_id TEXT,
        source_health TEXT, cost_usd REAL)""",
    """CREATE TABLE IF NOT EXISTS digests(
        run_date TEXT PRIMARY KEY, json TEXT, summary TEXT)""",
    """CREATE TABLE IF NOT EXISTS redirects(
        url TEXT PRIMARY KEY, resolved TEXT, ts TEXT)""",
    """CREATE TABLE IF NOT EXISTS feed_state(
        source_id TEXT PRIMARY KEY, etag TEXT, modified TEXT)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        item_id UNINDEXED, market UNINDEXED, published UNINDEXED,
        title, snippet, summary)""",
)


def _utc_iso(moment: datetime) -> str:
    """Render a datetime as ISO 8601 UTC; naive values are taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _now() -> str:
    return _utc_iso(datetime.now(UTC))


def _fts_query(terms: str) -> str:
    """Quote each token so user input can never be parsed as FTS syntax."""
    return ' '.join('"' + token.replace('"', '""') + '"' for token in terms.split())


class Store:
    """Local SQLite archive. Never leaves the machine."""

    def __init__(self, path: Path) -> None:
        """Open (creating if needed) the database at ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        with self._conn:
            for statement in _SCHEMA:
                self._conn.execute(statement)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # -- items ---------------------------------------------------------

    def upsert_items(self, items: Sequence[Item]) -> list[str]:
        """Store items; return the ids that were not seen before."""
        new_ids: list[str] = []
        now = _now()
        with self._conn:
            for item in items:
                row = self._conn.execute(
                    'SELECT 1 FROM items WHERE id = ?', (item.id,)
                ).fetchone()
                if row is None:
                    new_ids.append(item.id)
                    self._conn.execute(
                        'INSERT INTO items(id, url_hash, json, published, first_seen)'
                        ' VALUES (?, ?, ?, ?, ?)',
                        (
                            item.id,
                            item.id,
                            item.model_dump_json(),
                            _utc_iso(item.published),
                            now,
                        ),
                    )
                else:
                    self._conn.execute(
                        'UPDATE items SET json = ?, published = ? WHERE id = ?',
                        (item.model_dump_json(), _utc_iso(item.published), item.id),
                    )
        return new_ids

    def get_items(self, ids: Iterable[str]) -> dict[str, Item]:
        """Fetch items by id; unknown ids are omitted."""
        found: dict[str, Item] = {}
        for item_id in ids:
            row = self._conn.execute(
                'SELECT json FROM items WHERE id = ?', (item_id,)
            ).fetchone()
            if row is not None:
                found[item_id] = Item.model_validate_json(row['json'])
        return found

    def items_since(self, since: datetime) -> list[Item]:
        """Items first seen at or after ``since``, oldest first."""
        rows = self._conn.execute(
            'SELECT json FROM items WHERE first_seen >= ? ORDER BY first_seen, id',
            (_utc_iso(since),),
        ).fetchall()
        return [Item.model_validate_json(row['json']) for row in rows]

    def items_for_source(self, source_id: str, since: datetime) -> list[Item]:
        """Return a source's stored items published or first seen since ``since``.

        Newest first. Used when a feed answers 304 so its items are not lost.
        """
        bound = _utc_iso(since)
        rows = self._conn.execute(
            "SELECT json FROM items WHERE json_extract(json, '$.source_id') = ?"
            ' AND (published >= ? OR first_seen >= ?)'
            ' ORDER BY published DESC, id',
            (source_id, bound, bound),
        ).fetchall()
        return [Item.model_validate_json(row['json']) for row in rows]

    def scored_by_id(self, item_ids: Iterable[str]) -> dict[str, ScoredItem]:
        """Return stored scored items for ``item_ids``; never-scored ids are absent."""
        ids = list(item_ids)
        if not ids:
            return {}
        marks = ','.join('?' * len(ids))
        rows = self._conn.execute(
            f'SELECT json FROM scores WHERE item_id IN ({marks})',  # noqa: S608
            ids,
        ).fetchall()
        scored = (ScoredItem.model_validate_json(row['json']) for row in rows)
        return {s.item.id: s for s in scored}

    def mark_sent(self, item_ids: Iterable[str], run_date: date) -> None:
        """Record that items were featured in the digest for ``run_date``."""
        with self._conn:
            self._conn.executemany(
                'UPDATE items SET sent_on = ? WHERE id = ?',
                [(run_date.isoformat(), item_id) for item_id in item_ids],
            )

    def sent_item_ids(self) -> set[str]:
        """Ids of items already featured in a prior digest."""
        rows = self._conn.execute(
            'SELECT id FROM items WHERE sent_on IS NOT NULL'
        ).fetchall()
        return {row['id'] for row in rows}

    def sent_canonical_urls(self) -> set[str]:
        """Canonical URLs of items already featured in a prior digest.

        Catches the same story arriving under a new item id, for example a
        Google News link that was later resolved to the publisher URL.
        """
        rows = self._conn.execute(
            "SELECT json_extract(json, '$.canonical_url') AS u FROM items"
            ' WHERE sent_on IS NOT NULL'
        ).fetchall()
        return {row['u'] for row in rows if row['u']}

    def first_seen(self, item_id: str) -> datetime | None:
        """When an item was first stored, or None if it is unknown."""
        row = self._conn.execute(
            'SELECT first_seen FROM items WHERE id = ?', (item_id,)
        ).fetchone()
        if row is None or row['first_seen'] is None:
            return None
        return datetime.fromisoformat(row['first_seen'])

    # -- scores and FTS --------------------------------------------------

    def save_scores(self, scored: Sequence[ScoredItem]) -> None:
        """Persist scores and (re)index each item's title and snippet."""
        now = _now()
        with self._conn:
            for entry in scored:
                item = entry.item
                self._conn.execute(
                    'INSERT OR REPLACE INTO scores(item_id, json, scored_on)'
                    ' VALUES (?, ?, ?)',
                    (item.id, entry.model_dump_json(), now),
                )
                previous = self._conn.execute(
                    'SELECT summary FROM items_fts WHERE item_id = ?', (item.id,)
                ).fetchone()
                summary = previous['summary'] if previous else ''
                self._conn.execute(
                    'DELETE FROM items_fts WHERE item_id = ?', (item.id,)
                )
                self._conn.execute(
                    'INSERT INTO items_fts(item_id, market, published, title,'
                    ' snippet, summary) VALUES (?, ?, ?, ?, ?, ?)',
                    (
                        item.id,
                        entry.score.market,
                        _utc_iso(item.published),
                        item.title,
                        item.snippet,
                        summary,
                    ),
                )

    def index_summary(self, item_id: str, summary: str) -> None:
        """Attach a synthesis summary to an item's FTS row."""
        with self._conn:
            self._conn.execute(
                'UPDATE items_fts SET summary = ? WHERE item_id = ?',
                (summary, item_id),
            )

    def search(
        self,
        terms: str,
        market: str | None = None,
        since: date | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Full-text search the archive, best matches first."""
        query = _fts_query(terms)
        if not query:
            return []
        sql = (
            'SELECT item_id, market, published, title, snippet, summary'
            ' FROM items_fts WHERE items_fts MATCH ?'
        )
        params: list[Any] = [query]
        if market is not None:
            sql += ' AND market = ?'
            params.append(market)
        if since is not None:
            sql += ' AND published >= ?'
            params.append(datetime.combine(since, datetime.min.time(), UTC).isoformat())
        sql += ' ORDER BY rank LIMIT ?'
        params.append(limit)
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # -- runs ------------------------------------------------------------

    def start_run(self, run_date: date) -> str:
        """Create a run record and return its id."""
        run_id = uuid.uuid4().hex
        with self._conn:
            self._conn.execute(
                'INSERT INTO runs(run_id, run_date, started, status)'
                " VALUES (?, ?, ?, 'running')",
                (run_id, run_date.isoformat(), _now()),
            )
        logger.info('run started', extra={'digest.run_id': run_id})
        return run_id

    def finish_run(  # noqa: PLR0913
        self,
        run_id: str,
        status: str,
        *,
        sent: bool,
        message_id: str | None,
        source_health: dict[str, Any],
        cost_usd: float | None,
    ) -> None:
        """Close out a run."""
        with self._conn:
            self._conn.execute(
                'UPDATE runs SET finished = ?, status = ?, sent = ?,'
                ' message_id = ?, source_health = ?, cost_usd = ?'
                ' WHERE run_id = ?',
                (
                    _now(),
                    status,
                    int(sent),
                    message_id,
                    json.dumps(source_health),
                    cost_usd,
                    run_id,
                ),
            )

    def abandon_stale_runs(self, older_than: timedelta) -> int:
        """Mark runs stuck in ``running`` for longer than ``older_than`` abandoned.

        Returns how many runs were changed.
        """
        cutoff = _utc_iso(datetime.now(UTC) - older_than)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status = 'abandoned', finished = ?"
                " WHERE status = 'running' AND started < ?",
                (_now(), cutoff),
            )
        return cur.rowcount

    def last_success_started(self) -> datetime | None:
        """Start time of the last delivered (or possibly delivered) run.

        The start (not the finish) bounds what that run could have collected, so
        it is the safe lower edge for the next window. Dry runs only count when
        no delivered run exists at all.
        """
        for where in (
            "status IN ('ok', 'degraded', 'send-unknown') AND sent = 1",
            "status = 'dry-run'",
        ):
            row = self._conn.execute(
                f'SELECT MAX(started) AS s FROM runs WHERE {where}'  # noqa: S608
            ).fetchone()
            if row['s'] is not None:
                return datetime.fromisoformat(row['s'])
        return None

    def already_sent(self, run_date: date) -> bool:
        """Whether a digest for ``run_date`` has been delivered."""
        row = self._conn.execute(
            'SELECT 1 FROM runs WHERE run_date = ? AND sent = 1',
            (run_date.isoformat(),),
        ).fetchone()
        return row is not None

    # -- digests ---------------------------------------------------------

    def save_digest(self, run_date: date, digest: Digest, summary: str) -> None:
        """Store the digest and its short summary for ``run_date``."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO digests(run_date, json, summary)'
                ' VALUES (?, ?, ?)',
                (run_date.isoformat(), digest.model_dump_json(), summary),
            )

    def recent_digest_summaries(
        self, before: date, n: int = 5
    ) -> list[tuple[date, str]]:
        """Summaries of the ``n`` most recent digests strictly before a date."""
        rows = self._conn.execute(
            'SELECT run_date, summary FROM digests WHERE run_date < ?'
            ' ORDER BY run_date DESC LIMIT ?',
            (before.isoformat(), n),
        ).fetchall()
        return [(date.fromisoformat(r['run_date']), r['summary']) for r in rows]

    def week_digests(self, run_date: date) -> list[Digest]:
        """Digests from that week's Monday up to and including ``run_date``."""
        monday = run_date - timedelta(days=run_date.weekday())
        rows = self._conn.execute(
            'SELECT json FROM digests WHERE run_date >= ? AND run_date <= ?'
            ' ORDER BY run_date',
            (monday.isoformat(), run_date.isoformat()),
        ).fetchall()
        return [Digest.model_validate_json(row['json']) for row in rows]

    # -- caches ----------------------------------------------------------

    def get_redirect(self, url: str) -> str | None:
        """Return the cached redirect target for ``url``."""
        row = self._conn.execute(
            'SELECT resolved FROM redirects WHERE url = ?', (url,)
        ).fetchone()
        return None if row is None else row['resolved']

    def put_redirect(self, url: str, resolved: str) -> None:
        """Cache the redirect target for ``url``."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO redirects(url, resolved, ts) VALUES (?, ?, ?)',
                (url, resolved, _now()),
            )

    def get_feed_state(self, source_id: str) -> tuple[str | None, str | None]:
        """Conditional-GET validators (etag, last-modified) for a feed."""
        row = self._conn.execute(
            'SELECT etag, modified FROM feed_state WHERE source_id = ?',
            (source_id,),
        ).fetchone()
        return (None, None) if row is None else (row['etag'], row['modified'])

    def put_feed_state(
        self, source_id: str, etag: str | None, modified: str | None
    ) -> None:
        """Remember conditional-GET validators for a feed."""
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO feed_state(source_id, etag, modified)'
                ' VALUES (?, ?, ?)',
                (source_id, etag, modified),
            )
