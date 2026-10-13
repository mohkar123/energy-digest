"""Domain models shared across the pipeline."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal['US', 'UK', 'BR', 'NZ', 'OTHER']
Topic = Literal[
    'rate-case-tariff',
    'wholesale-market-rules',
    'large-load-data-centres',
    'der-vpp-bess',
    'ev-electrification',
    'policy-legislation',
    'grid-planning',
    'weather-reliability',
    'corporate-ma',
    'competitor-vendor',
    'event-webinar',
    'advert',
]
ForecastLine = Literal[
    'STF', 'LTF', 'BTM', 'CP-Alerts', 'Load-Scheduling', 'Settlements'
]
Confidence = Literal['C', 'R', 'I']
Opportunity = Literal['High', 'Med', 'Low']
ImpactType = Literal[
    'opportunity', 'risk', 'product-implication', 'competitive', 'customer-relevant'
]
SourceKind = Literal['newsletter', 'rss', 'gnews']
Tier = Literal['primary', 'press', 'newsletter', 'aggregator']


class Item(BaseModel):
    """A collected news item."""

    model_config = ConfigDict(frozen=True)

    id: str  # first 12 hex characters of the url hash
    url: str
    canonical_url: str
    title: str
    source_id: str
    source_name: str
    source_kind: SourceKind
    tier: Tier
    market_hint: Market | None
    published: datetime  # tz-aware
    snippet: str = ''
    text: str | None = None
    headline_only: bool = True
    is_event: bool = False
    event_date: date | None = None
    also_reported_by: list[str] = []

    @field_validator('url')
    @classmethod
    def _http_url_only(cls, value: str) -> str:
        if urlsplit(value).scheme.lower() not in ('http', 'https'):
            msg = 'item url must be http or https'
            raise ValueError(msg)
        return value


class DatedEvent(BaseModel):
    """A dated milestone mentioned by an item."""

    model_config = ConfigDict(frozen=True)

    date: date
    what: str


class Score(BaseModel):
    """Relevance scoring for one item."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    market: Market
    topic: Topic
    market_relevance: int = Field(ge=0, le=5)
    forecast_relevance: int = Field(ge=0, le=5)
    forecast_lines: list[ForecastLine]
    dated_event: DatedEvent | None
    is_advert: bool


class ScoredItem(BaseModel):
    """An item together with its score, watchlist hits and final rank."""

    item: Item
    score: Score
    watchlist_hits: list[str]
    rank: float


def _require_in_schema(*names: str) -> Callable[[dict[str, Any]], None]:
    """Mark defaulted fields as required in the JSON schema sent to the model.

    The defaults only keep stored digests without these fields loadable; the
    model must still fill them in.
    """

    def update(schema: dict[str, Any]) -> None:
        required = schema.setdefault('required', [])
        required.extend(n for n in names if n not in required)

    return update


class Story(BaseModel):
    """A top story in the digest."""

    model_config = ConfigDict(
        frozen=True, json_schema_extra=_require_in_schema('headline_short', 'why_short')
    )

    item_ids: list[str]
    headline: str
    summary: str
    market: Market
    headline_short: str = ''  # 12 words or fewer, used in Slack
    why_short: str = ''  # 20 words or fewer, used in Slack


class MarketLine(BaseModel):
    """A one-line market note."""

    model_config = ConfigDict(frozen=True)

    item_ids: list[str]
    one_liner: str  # 20 words or fewer


class ForecastImpact(BaseModel):
    """Impact of news on Forecast product lines."""

    model_config = ConfigDict(
        json_schema_extra=_require_in_schema('so_what_short', 'action_short')
    )

    item_ids: list[str]
    product_lines: list[ForecastLine]
    impact_type: ImpactType
    so_what: str
    suggested_action: str
    accounts: list[str]
    so_what_short: str = ''  # 12 words or fewer, used in Slack
    action_short: str = ''  # 8 words or fewer, used in Slack


class RadarRow(BaseModel):
    """One row of the mini-radar."""

    item_ids: list[str]
    market: Market
    signal: str  # 10 words or fewer
    signal_type: Topic
    next_milestone_date: date | None
    forecast_lens: list[ForecastLine]
    opportunity: Opportunity
    confidence: Confidence
    next_step: str


class UpcomingEvent(BaseModel):
    """A dated upcoming event."""

    item_ids: list[str]
    date: date
    market: Market
    what: str
    confidence: Confidence


class CalendarEntry(BaseModel):
    """An entry in a market's regulatory calendar."""

    date: date | None
    what: str
    item_ids: list[str]
    confidence: Confidence


class DeepDive(BaseModel):
    """Deeper analysis of one market."""

    market: Market
    what_moved: list[MarketLine]
    regulatory_calendar: list[CalendarEntry]
    account_implications: list[MarketLine]
    open_questions: list[str]
    competitors_mentioned: list[MarketLine]


class WeekInReview(BaseModel):
    """Weekly themes, included on the last digest of the week."""

    themes: list[MarketLine]
    trend_notes: str


class DigestDraft(BaseModel):
    """The synthesis model output."""

    top_stories: list[Story]
    by_market: dict[Market, list[MarketLine]]
    forecast_impact: list[ForecastImpact]
    radar: list[RadarRow]
    upcoming: list[UpcomingEvent]
    deep_dive: DeepDive
    week_in_review: WeekInReview | None


class Digest(DigestDraft):
    """The validated digest that gets rendered."""

    run_date: date
    deep_dive_market: Market
    degraded: bool = False
    dropped_entries: int = 0


class NewsletterStory(BaseModel):
    """One story extracted from a newsletter email."""

    sender: str
    subject: str
    date: str
    title: str
    url: str
    blurb: str
    is_sponsored: bool
    is_event: bool
    event_date: str | None


class NewsletterExtraction(BaseModel):
    """Structured output of newsletter extraction."""

    stories: list[NewsletterStory]


class ScoreBatch(BaseModel):
    """Structured output of a scoring batch."""

    scores: list[Score]
