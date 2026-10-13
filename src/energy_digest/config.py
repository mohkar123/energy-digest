"""Configuration models and loader."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Self, get_args

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from energy_digest.models import Market

MARKET_CONTEXT_FILES: dict[Market, str] = {
    'US': 'us.md',
    'UK': 'uk.md',
    'BR': 'br.md',
    'NZ': 'nz.md',
    'OTHER': 'other.md',
}


class ConfigError(Exception):
    """Raised when a config file is missing or fails validation."""


class ModelsCfg(BaseModel):
    """Claude model alias per pipeline stage."""

    extract: str = 'haiku'
    score: str = 'haiku'
    synthesize: str = 'sonnet'
    send: str = 'haiku'


class BudgetsCfg(BaseModel):
    """Max spend in USD per pipeline stage."""

    extract: float = 0.5
    score: float = 0.5
    synthesize: float = 1.5
    send: float = 0.3


class ClaudeCfg(BaseModel):
    """How to invoke the claude CLI."""

    binary: str = 'claude'
    timeout_s: int = 600
    disallow_mcp: list[str]


PLACEHOLDER_MARKER = 'XXXXXXXXXX'


class DeliveryCfg(BaseModel):
    """Where the digest is delivered: the user's own Slack DM."""

    slack_user_id: str
    slack_dm_channel: str

    def placeholder_fields(self) -> list[str]:
        """Names of Slack ID fields still set to the example placeholder."""
        return [
            name
            for name in ('slack_user_id', 'slack_dm_channel')
            if PLACEHOLDER_MARKER in getattr(self, name).upper()
        ]


class Settings(BaseModel):
    """Top-level runtime settings."""

    delivery: DeliveryCfg
    timezone: str = 'America/Los_Angeles'
    top_n: int = 25
    newsletter_senders: list[str]
    models: ModelsCfg = ModelsCfg()
    budgets: BudgetsCfg = BudgetsCfg()
    claude: ClaudeCfg
    data_dir: Path
    reports_dir: Path
    email_max_bytes: int = 40_000


class SourceCfg(BaseModel):
    """One news source (RSS feed or Google News query)."""

    id: str
    name: str
    market: Market | None = None
    kind: Literal['rss', 'gnews']
    url: str | None = None
    query: str | None = None
    locale: Literal['en-US', 'en-GB', 'en-NZ', 'pt-BR', 'en-AU', 'en-CA'] = 'en-US'
    tier: Literal['primary', 'press', 'newsletter', 'aggregator']
    enabled: bool = True
    max_items: int = 30
    # Optional case-insensitive prefilter on title/summary.
    keywords: list[str] = []

    @model_validator(mode='after')
    def _check_kind_fields(self) -> Self:
        if self.kind == 'rss' and not self.url:
            msg = f'source {self.id!r}: rss sources require url'
            raise ValueError(msg)
        if self.kind == 'gnews' and not self.query:
            msg = f'source {self.id!r}: gnews sources require query'
            raise ValueError(msg)
        return self


class WatchEntry(BaseModel):
    """A customer, prospect, competitor or partner to flag in the news."""

    name: str
    kind: Literal['customer', 'prospect', 'competitor', 'partner']
    market: Market
    aliases: list[str] = []


ACCOUNT_ROLE_RULE = (
    'Only describe an account as a customer if its kind is customer; '
    'prospects are prospects; never infer customer status.'
)


def watchlist_prompt(watchlist: Sequence[WatchEntry]) -> str:
    """Prompt section listing each watchlist account with its role.

    Gives the model the authoritative kind of every account so it cannot guess
    (for example, call a prospect a customer).
    """
    lines = [
        f'{w.name} — {w.kind} — {w.market} — {", ".join(w.aliases) or "no aliases"}'
        for w in watchlist
    ]
    return '\n'.join(
        ['## Watchlist (name — kind — market — aliases)', *lines, '', ACCOUNT_ROLE_RULE]
    )


class AppConfig(BaseModel):
    """Fully loaded configuration."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    sources: list[SourceCfg]
    watchlist: list[WatchEntry]
    company_context: str
    market_context: dict[Market, str]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except OSError as exc:
        msg = f'{path.name}: {exc}'
        raise ConfigError(msg) from exc


def _load_yaml(path: Path) -> Any:  # noqa: ANN401
    try:
        return yaml.safe_load(_read(path))
    except yaml.YAMLError as exc:
        msg = f'{path.name}: {exc}'
        raise ConfigError(msg) from exc


def _validate[T: BaseModel](model: type[T], data: Any, path: Path) -> T:  # noqa: ANN401
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        msg = f'{path.name}: {exc}'
        raise ConfigError(msg) from exc


def load_config(root: Path) -> AppConfig:
    """Load and validate all config under ``root``/config."""
    cfg_dir = root / 'config'

    settings_path = cfg_dir / 'settings.yaml'
    settings = _validate(Settings, _load_yaml(settings_path), settings_path)
    settings = settings.model_copy(
        update={
            'data_dir': root / settings.data_dir,
            'reports_dir': root / settings.reports_dir,
        }
    )

    sources_path = cfg_dir / 'sources.yaml'
    sources_raw = _load_yaml(sources_path) or []
    watch_path = cfg_dir / 'watchlist.yaml'
    watch_raw = _load_yaml(watch_path) or []
    try:
        sources = [SourceCfg.model_validate(s) for s in sources_raw]
    except ValidationError as exc:
        msg = f'{sources_path.name}: {exc}'
        raise ConfigError(msg) from exc
    try:
        watchlist = [WatchEntry.model_validate(w) for w in watch_raw]
    except ValidationError as exc:
        msg = f'{watch_path.name}: {exc}'
        raise ConfigError(msg) from exc

    market_context: dict[Market, str] = {
        market: _read(cfg_dir / 'markets' / MARKET_CONTEXT_FILES[market])
        for market in get_args(Market)
    }
    return AppConfig(
        settings=settings,
        sources=sources,
        watchlist=watchlist,
        company_context=_read(cfg_dir / 'company_context.md'),
        market_context=market_context,
    )
