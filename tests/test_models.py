from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from energy_digest.models import (
    DeepDive,
    Digest,
    DigestDraft,
    Item,
    MarketLine,
    NewsletterExtraction,
    Score,
    ScoreBatch,
    Story,
)
from energy_digest.schema import claude_json_schema


def _keys(node: Any) -> set[str]:  # noqa: ANN401
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= _keys(value)
    return found


@pytest.mark.parametrize('model', [DigestDraft, ScoreBatch, NewsletterExtraction])
def test_schema_is_self_contained(model: type[BaseModel]) -> None:
    schema = claude_json_schema(model)
    keys = _keys(schema)
    assert '$ref' not in keys
    assert '$defs' not in keys
    assert schema['type'] == 'object'


def test_schema_drops_titles_but_keeps_title_property() -> None:
    schema = claude_json_schema(NewsletterExtraction)
    story = schema['properties']['stories']['items']
    assert 'title' not in schema
    assert 'title' in story['properties']
    assert 'title' not in story['properties']['title']


def _score(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        'item_id': 'abc',
        'market': 'US',
        'topic': 'rate-case-tariff',
        'market_relevance': 3,
        'forecast_relevance': 2,
        'forecast_lines': ['STF'],
        'dated_event': None,
        'is_advert': False,
    }
    return base | overrides


def test_score_accepts_valid_and_rejects_out_of_range() -> None:
    assert Score.model_validate(_score()).market_relevance == 3
    with pytest.raises(ValidationError):
        Score.model_validate(_score(market_relevance=6))
    with pytest.raises(ValidationError):
        Score.model_validate(_score(forecast_relevance=-1))


def test_newsletter_fixture_parses(fixtures_dir: Path) -> None:
    raw = (fixtures_dir / 'newsletter_extraction.json').read_text()
    extraction = NewsletterExtraction.model_validate_json(raw)
    assert len(extraction.stories) == 24


def test_digest_round_trips_through_json() -> None:
    line = MarketLine(item_ids=['a'], one_liner='something moved')
    digest = Digest(
        top_stories=[
            Story(item_ids=['a'], headline='H', summary='S', market='US'),
        ],
        by_market={'US': [line]},
        forecast_impact=[],
        radar=[],
        upcoming=[],
        deep_dive=DeepDive(
            market='US',
            what_moved=[line],
            regulatory_calendar=[],
            account_implications=[],
            open_questions=['q'],
            competitors_mentioned=[],
        ),
        week_in_review=None,
        run_date=date(2026, 10, 9),
        deep_dive_market='US',
    )
    restored = Digest.model_validate_json(digest.model_dump_json())
    assert restored == digest
    assert 'UK' not in restored.by_market


def test_item_defaults_and_json_roundtrip() -> None:
    item = Item(
        id='0123456789ab',
        url='https://example.com/a',
        canonical_url='https://example.com/a',
        title='T',
        source_id='s',
        source_name='S',
        source_kind='rss',
        tier='press',
        market_hint=None,
        published=datetime(2026, 10, 9, tzinfo=UTC),
    )
    assert item.headline_only is True
    assert item.also_reported_by == []
    assert Item.model_validate_json(item.model_dump_json()) == item


def _item(**kw: object) -> Item:
    base = {
        'id': '0123456789ab',
        'url': 'https://example.com/a',
        'canonical_url': 'https://example.com/a',
        'title': 'T',
        'source_id': 's',
        'source_name': 'S',
        'source_kind': 'rss',
        'tier': 'press',
        'market_hint': None,
        'published': datetime(2026, 10, 9, tzinfo=UTC),
    }
    return Item.model_validate({**base, **kw})


@pytest.mark.parametrize(
    'url', ['javascript:alert(1)', 'ftp://example.com/a', 'file:///etc/passwd', '/rel']
)
def test_item_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValidationError, match='http'):
        _item(url=url)


@pytest.mark.parametrize('url', ['http://example.com/a', 'HTTPS://example.com/a'])
def test_item_accepts_http_urls(url: str) -> None:
    assert _item(url=url).url == url
