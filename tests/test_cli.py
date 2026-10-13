import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from energy_digest import cli, pipeline
from energy_digest.claude_cli import (
    SLACK_SEND_TOOL,
    ClaudeRequest,
    ClaudeResult,
    Profile,
)
from energy_digest.collect.newsletters import READ_TOOLS
from energy_digest.models import Item, Score, ScoredItem
from energy_digest.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
USER_ID = 'U0000000001'
DM_CHANNEL = 'D0000000001'


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    # main() installs a JSON handler on the root logger; keep it test-local.
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(REPO_ROOT / 'config', tmp_path / 'config')
    settings = tmp_path / 'config' / 'settings.yaml'
    settings.write_text(
        settings.read_text()
        .replace('UXXXXXXXXXX', USER_ID)
        .replace('DXXXXXXXXXX', DM_CHANNEL)
    )
    monkeypatch.setenv('ENERGY_DIGEST_ROOT', str(tmp_path))
    monkeypatch.setenv('ENERGY_DIGEST_CLAUDE_CWD', str(tmp_path / 'claude-cwd'))
    return tmp_path


def _outcome(status: str) -> pipeline.RunOutcome:
    return pipeline.RunOutcome(
        status=status,  # type: ignore[arg-type]
        email_path=Path('reports/e.html'),
        report_path=Path('reports/r.html'),
        message_id=None,
        cost_usd=0.5,
    )


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(['--help'])
    assert exc.value.code == 0


def test_run_dry_run_prints_json_and_exits_zero(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[pipeline.RunOptions] = []

    def fake_run(
        opts: pipeline.RunOptions, *args: object, **kwargs: object
    ) -> pipeline.RunOutcome:
        seen.append(opts)
        return _outcome('dry-run')

    monkeypatch.setattr(pipeline, 'run', fake_run)
    code = cli.main(['run', '--dry-run', '--date', '2026-10-09'])
    assert code == 0
    assert seen[0].run_date == date(2026, 10, 9)
    assert seen[0].dry_run
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload['status'] == 'dry-run'
    assert payload['cost_usd'] == 0.5


def test_run_failed_outcome_exits_two(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, 'run', lambda *a, **k: _outcome('failed'))
    assert cli.main(['run', '--date', '2026-10-09']) == 2


def test_run_send_unknown_outcome_exits_two(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, 'run', lambda *a, **k: _outcome('send-unknown'))
    assert cli.main(['run', '--date', '2026-10-09']) == 2


@pytest.mark.parametrize(
    ('local_now', 'expected_runs'),
    [
        (datetime(2026, 10, 10, 8, tzinfo=UTC), 0),  # Saturday
        (datetime(2026, 10, 11, 8, tzinfo=UTC), 0),  # Sunday
        (datetime(2026, 10, 9, 8, tzinfo=UTC), 1),  # Friday
    ],
)
def test_run_without_date_is_skipped_on_weekends(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    local_now: datetime,
    expected_runs: int,
) -> None:
    runs: list[pipeline.RunOptions] = []

    def fake_run(
        opts: pipeline.RunOptions, *args: object, **kwargs: object
    ) -> pipeline.RunOutcome:
        runs.append(opts)
        return _outcome('dry-run')

    monkeypatch.setattr(pipeline, 'run', fake_run)
    monkeypatch.setattr(cli, '_local_now', lambda tz: local_now)
    assert cli.main(['run']) == 0
    assert len(runs) == expected_runs
    if not expected_runs:
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload['status'] == 'skipped'


def test_run_with_explicit_date_runs_on_a_weekend(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[pipeline.RunOptions] = []

    def fake_run(
        opts: pipeline.RunOptions, *args: object, **kwargs: object
    ) -> pipeline.RunOutcome:
        runs.append(opts)
        return _outcome('dry-run')

    monkeypatch.setattr(pipeline, 'run', fake_run)
    monkeypatch.setattr(
        cli, '_local_now', lambda tz: datetime(2026, 10, 10, 8, tzinfo=UTC)
    )
    assert cli.main(['run', '--date', '2026-10-09']) == 0
    assert len(runs) == 1


def test_run_unexpected_exception_exits_one(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        msg = 'kaboom'
        raise RuntimeError(msg)

    monkeypatch.setattr(pipeline, 'run', boom)
    assert cli.main(['run', '--date', '2026-10-09']) == 1


def test_missing_config_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('ENERGY_DIGEST_ROOT', str(tmp_path))
    assert cli.main(['search', 'anything']) == 1


def test_search_prints_rows(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    item = Item(
        id='abc123',
        url='https://x.example/story',
        canonical_url='https://x.example/story',
        title='Large-load tariff approved',
        source_id='s',
        source_name='Src',
        source_kind='rss',
        tier='primary',
        market_hint='US',
        published=datetime(2026, 10, 8, tzinfo=UTC),
    )
    score = Score(
        item_id='abc123',
        market='US',
        topic='large-load-data-centres',
        market_relevance=4,
        forecast_relevance=4,
        forecast_lines=[],
        dated_event=None,
        is_advert=False,
    )
    store = Store(root / 'data' / 'digest.db')
    store.upsert_items([item])
    store.save_scores([ScoredItem(item=item, score=score, watchlist_hits=[], rank=1)])
    store.close()

    assert (
        cli.main(['search', 'tariff', '--market', 'US', '--since', '2026-10-01']) == 0
    )
    out = capsys.readouterr().out
    assert '2026-10-08  US  Large-load tariff approved  https://x.example/story' in out

    cli.main(['search', 'tariff', '--market', 'UK'])
    assert 'Large-load' not in capsys.readouterr().out


def test_parse_since_accepts_relative_and_absolute() -> None:
    today = date(2026, 10, 9)
    assert cli._parse_since('30d', today) == date(2026, 9, 9)
    assert cli._parse_since('2026-10-01', today) == date(2026, 10, 1)
    assert cli._parse_since(None, today) is None


def _patch_doctor(
    monkeypatch: pytest.MonkeyPatch, *, auth_out: str = '{"authMethod": "claude.ai"}'
) -> list[ClaudeRequest]:
    requests: list[ClaudeRequest] = []

    def fake_proc(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=auth_out, stderr='')

    def fake_claude(req: ClaudeRequest, cfg: object, **kwargs: object) -> ClaudeResult:
        requests.append(req)
        return ClaudeResult({'n': 3}, '', 0.01, 1, [], {})

    monkeypatch.setattr(cli.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(cli.subprocess, 'run', fake_proc)
    monkeypatch.setattr(cli, 'run_claude', fake_claude)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    return requests


def test_doctor_passes_every_check(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = _patch_doctor(monkeypatch)
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert out.count('PASS') == 7
    assert 'FAIL' not in out
    assert requests[0].profile is Profile.GMAIL
    assert requests[0].max_budget_usd == 0.10
    assert requests[0].allowed_mcp_tools == (*READ_TOOLS, SLACK_SEND_TOOL)


def test_doctor_accepts_non_json_auth_output(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_doctor(monkeypatch, auth_out='Logged in via claude.ai as me@example.com')
    assert cli.main(['doctor']) == 0


def test_doctor_fails_when_connector_hidden(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_doctor(monkeypatch)
    monkeypatch.setattr(
        cli,
        'run_claude',
        lambda *a, **k: ClaudeResult({'n': 0}, '', 0.0, 1, [], {}),
    )
    assert cli.main(['doctor']) == 2
    assert 'FAIL  Gmail read and Slack send tools' in capsys.readouterr().out


def test_doctor_warns_when_api_key_set(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_doctor(monkeypatch)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    assert cli.main(['doctor']) == 0
    assert 'WARN' in capsys.readouterr().out


def test_doctor_fails_when_too_few_tools_are_visible(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_doctor(monkeypatch)
    monkeypatch.setattr(
        cli,
        'run_claude',
        lambda *a, **k: ClaudeResult({'n': 2}, '', 0.0, 1, [], {}),
    )
    assert cli._check_connector_tools(cli.load_config(root)) is not None


def test_guard_check_denies_forged_slack_posts(root: Path, tmp_path: Path) -> None:
    cfg = cli.load_config(root)
    assert cli._check_guard_denies_forgery(cfg) is None
    # the check only evaluates the policy: it must not claim or post anything
    assert not list(tmp_path.glob('**/once'))


def test_doctor_fails_on_placeholder_slack_ids(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(REPO_ROOT / 'config' / 'settings.yaml', root / 'config')
    _patch_doctor(monkeypatch)
    assert cli.main(['doctor']) == 2
    out = capsys.readouterr().out
    assert 'FAIL  Slack IDs configured' in out
    assert 'slack_user_id, slack_dm_channel' in out
    assert 'XXXXXXXXXX placeholder' in out


def test_slack_id_check_passes_with_real_ids(root: Path) -> None:
    assert cli._check_slack_ids(cli.load_config(root)) is None
