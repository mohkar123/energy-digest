# Energy-markets daily digest

A daily energy-markets digest for the US, UK, Brazil and New Zealand. It
collects energy newsletters (read through the claude.ai Gmail connector,
read-only) plus public RSS feeds and Google News searches. It then scores and
synthesizes them with headless Claude Code (`claude -p`, so you need no API
key) and posts a short Slack self-DM: one main post plus a thread of replies.
A full local HTML report and a full-text-search (SQLite FTS) archive of every
item stay on your machine.

> Built with [Claude Code](https://claude.com/claude-code). The pipeline uses
> Claude Code as its runtime, and Claude Code also wrote most of the code
> (see [How it was built](#how-it-was-built)).

## Architecture

```
collect ──► dedup ──► score ──► synthesize ──► render ──► deliver
```

| Stage | Module | What it does |
|---|---|---|
| collect | `collect/feeds.py`, `collect/newsletters.py`, `collect/article.py` | Fetches RSS and Google News feeds (with conditional GET) and extracts newsletter stories through a read-only Gmail run. Decodes tracking links offline and enriches top items with article text. |
| dedup | `dedup.py` | Canonicalizes URLs, strips tracking parameters and clusters near-duplicate headlines (rapidfuzz). Merges the copies into one item with "also reported by". |
| score | `score.py` | A batched, tool-less Claude run scores market and forecast relevance against `config/company_context.md` and the watchlist. If it fails it falls back to keyword scoring. |
| synthesize | `synthesize.py` | A tool-less Claude run writes a structured digest (JSON schema) of top stories, forecast impact, radar, upcoming dates, by-market notes and a deep dive on one market (rotating by weekday). Every entry must cite stored item ids. |
| render | `render.py`, `templates/` | Jinja templates render the Slack parts, an email-style HTML page and the full report. Links are inserted only from stored items. |
| deliver | `deliver.py`, `hooks/guard_send.py` | A Claude run that can call just one tool, Slack `slack_send_message`, posts the pre-rendered parts to your own DM. A guard hook watches it. |

`pipeline.py` orchestrates the stages. `store.py` keeps the SQLite archive,
`cli.py` exposes the `digest` command and `claude_cli.py` is the only module
that spawns `claude`.

The Slack text is deliberately short. The main post is about 1,200 characters
(at most 3 top stories and 3 forecast-impact lines, with one source link each).
The thread replies follow in this order: market radar, upcoming dates, by
market, week in review on Fridays, then a footer. Each story appears only once.
The fuller report, the email-style HTML and the exact Slack text are written
to `reports/`.

## Security and least privilege

- **Per-run tool allowlists.** Every `claude` run is headless with built-in
  tools disabled (`--tools ''`) and `--permission-mode dontAsk`. Scoring and
  synthesis runs see no MCP servers and no user settings. The newsletter run
  may call only Gmail `search_threads` and `get_thread`. The delivery run may
  call only Slack `slack_send_message`. Unused claude.ai connectors are stripped
  from connector runs.
- **Fail-closed PreToolUse guard hook.** `hooks/guard_send.py` is a stdlib-only
  hook, and any error denies the call. It allows a Slack post only to your
  configured self-DM, only with the expected keys, and only when the SHA-256 of
  the message matches the next Python-rendered part in order. Each part can be
  posted once. Gmail searches must match the exact
  `from:(<configured senders>)` grammar, and Gmail `send_message` is always
  denied.
- **Links only from stored items.** Model output is treated as data. Every link
  and source name in the digest comes from the `items` table, and URLs in model
  text are defanged.
- **SSRF protection.** Every outbound fetch goes through `dedup.safe_url`. Only
  http(s) on standard ports is allowed, and the host must resolve to public
  addresses only. Redirects are checked hop by hop, and unsubscribe links are
  never followed.
- **No credentials.** The tool creates no OAuth apps or API keys, and
  `ANTHROPIC_API_KEY` and similar variables are stripped from `claude`
  subprocesses.

See [`SECURITY.md`](SECURITY.md) for the full model and residual risks.

## Setup

Prerequisites:

- [uv](https://docs.astral.sh/uv/) and Python 3.13
- [Claude Code](https://claude.com/claude-code) logged in with a claude.ai
  account (`claude auth status`)
- The claude.ai Gmail and Slack connectors connected to that account (Gmail
  needs read access only)

Steps:

1. Install dependencies:

   ```bash
   uv sync
   ```

2. Edit `config/settings.yaml`:
   - Set `delivery.slack_user_id` and `delivery.slack_dm_channel` to your Slack
     member ID and self-DM channel ID. They ship as `XXXXXXXXXX` placeholders.
   - Set `newsletter_senders` to the newsletters you subscribe to.
   - List the claude.ai connectors on your account under
     `claude.disallow_mcp`.
3. Replace `config/company_context.md` with your own product and company
   context. The shipped file describes a fictional "ExampleCo Forecast".
4. Replace `config/watchlist.yaml` with your own customers, prospects,
   competitors and partners. The shipped names are fictional.
5. Optionally edit `config/sources.yaml` (feeds and Google News queries) and
   `config/markets/*.md` (market background).
6. Check the environment:

   ```bash
   uv run digest doctor
   ```

   It checks the Slack IDs, the claude.ai login, the headless Gmail and Slack
   tools, the guard hook and the directories. It posts nothing.
7. Do a dry run, which runs the full pipeline without posting:

   ```bash
   uv run digest run --dry-run
   ```

   Then open the newest file in `reports/`.
8. Install the systemd user timer:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/* ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now energy-digest.timer
   loginctl enable-linger $USER    # optional: lets the timer run while logged out
   ```

   The timer fires Mon-Fri at 07:00 America/Los_Angeles. `Persistent=true`
   catches up after a missed run, and weekend catch-ups are skipped. The units
   assume the repo is at `~/energy-digest` and uv is at `~/.local/bin/uv`, so
   edit them if yours differ. A failed run shows a desktop notification.

## Commands

| Command | Purpose |
|---|---|
| `digest doctor` | Verify the environment. Run this first. |
| `digest run` | Collect, score, synthesize, render and post to Slack. Options: `--dry-run`, `--no-send`, `--since ISO`, `--date YYYY-MM-DD`, `--force`. |
| `digest search TERMS... [--market M] [--since 30d\|YYYY-MM-DD]` | Search the local FTS archive. |
| `digest sources-check` | Fetch every feed once and print per-source health. |

Run these as `uv run digest ...`.

## Run behaviour

- Only one run happens at a time (`data/run.lock`). A run left `running` for
  over 2 hours is marked `abandoned` at the next start.
- `digest run` without `--date` does nothing on weekends.
- Dry runs and `--no-send` runs never update the stored digest history, the
  sent marks or the feed validators.
- A run succeeds once the Slack tool result confirms the main post. If that
  post cannot be confirmed, the run ends as `send-unknown` and exits 2. Check
  your DMs before you rerun with `--force`.
- `claude` runs from a neutral directory outside the repo,
  `${XDG_STATE_HOME:-~/.local/state}/energy-digest/claude-cwd`. Override it
  with `ENERGY_DIGEST_CLAUDE_CWD`.

## Cost notes

Tool-less runs carry about 1K tokens of overhead. Gmail runs carry about 28K
tokens once unused connectors are stripped through `claude.disallow_mcp`, and
about 188K tokens without that. Each stage also has a hard USD cap under
`budgets` in `config/settings.yaml`.

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

Tests never touch the network or spawn `claude`: they use
`httpx.MockTransport` and an injected runner. Fixtures are synthetic.

## How it was built

I built the project with Claude Code using a structured workflow:

1. **Spec.** A brainstorming session turned the idea into a design spec with
   locked decisions (sources, markets, output shape, security constraints).
2. **Plan.** The spec became a bite-sized implementation plan with exact file
   paths, tests and atomic commits.
3. **Subagent implementation.** Claude Code subagents implemented the plan
   unit by unit, with one commit per task.
4. **Spec and quality reviews.** Separate review passes checked the
   implementation against the spec and for code quality and security. Their
   findings went back in as fix-up tasks.

One pivot shaped the design. The first plan delivered the digest by email
through the Gmail connector, but workspace policy blocked Gmail sending. So
delivery moved to a Slack self-DM, and Gmail became strictly read-only. That
also gave a cleaner security split: the run that reads untrusted email cannot
post anything, and the run that posts sees only Python-rendered output.

## Troubleshooting

- `claude auth status` confirms the claude.ai login.
- `journalctl --user -u energy-digest` shows logs from scheduled runs.
- `uv run digest sources-check` finds broken or empty feeds.
- If no Slack post appears, run `uv run digest doctor`. If it fails, reconnect
  the claude.ai Slack connector.
