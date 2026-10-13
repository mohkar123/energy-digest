from pathlib import Path

import pytest
import yaml

from energy_digest.config import ConfigError, SourceCfg, load_config

REPO_ROOT = Path(__file__).parent.parent


def _stub_root(tmp_path: Path) -> Path:
    shutil_root = tmp_path / 'root'
    cfg = shutil_root / 'config'
    cfg.mkdir(parents=True)
    for name in ('settings.yaml', 'sources.yaml', 'watchlist.yaml'):
        (cfg / name).write_text((REPO_ROOT / 'config' / name).read_text())
    (cfg / 'company_context.md').write_text('Company context')
    (cfg / 'markets').mkdir()
    for name in ('us', 'uk', 'br', 'nz', 'other'):
        (cfg / 'markets' / f'{name}.md').write_text(f'{name} context')
    return shutil_root


def test_real_config_loads(tmp_path: Path) -> None:
    root = REPO_ROOT
    if not (root / 'config' / 'company_context.md').exists():
        root = _stub_root(tmp_path)
    cfg = load_config(root)
    assert cfg.settings.delivery.slack_user_id == 'UXXXXXXXXXX'
    assert cfg.settings.delivery.slack_dm_channel == 'DXXXXXXXXXX'
    assert 'mcp__claude_ai_Slack' not in cfg.settings.claude.disallow_mcp
    assert len(cfg.sources) == 29
    assert {s.id for s in cfg.sources} >= {'utilitydive', 'neso'}
    assert any(w.name == 'Southern Cross Lines' for w in cfg.watchlist)
    assert set(cfg.market_context) == {'US', 'UK', 'BR', 'NZ', 'OTHER'}
    assert len({s.id for s in cfg.sources}) == len(cfg.sources)


def test_gnews_without_query_raises_config_error(tmp_path: Path) -> None:
    root = _stub_root(tmp_path)
    bad = [{'id': 'x', 'name': 'X', 'kind': 'gnews', 'tier': 'aggregator'}]
    (root / 'config' / 'sources.yaml').write_text(yaml.dump(bad))
    with pytest.raises(ConfigError, match=r'sources\.yaml'):
        load_config(root)


def test_rss_without_url_is_invalid() -> None:
    with pytest.raises(ValueError, match='require url'):
        SourceCfg(id='x', name='X', kind='rss', tier='press')


def test_relative_dirs_resolve_under_root(tmp_path: Path) -> None:
    root = _stub_root(tmp_path)
    cfg = load_config(root)
    assert cfg.settings.data_dir == root / 'data'
    assert cfg.settings.reports_dir == root / 'reports'
