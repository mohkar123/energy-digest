# energy-digest — Agent Guide

This is a daily energy-markets digest. It collects news, scores and synthesizes
it with headless Claude Code, and delivers it as a Slack self-DM. See README.md
and SECURITY.md.

## Hard Rules

- Every `claude` subprocess goes through `src/energy_digest/claude_cli.py`. No other module may spawn `claude` or add MCP access.
- Gmail is read-only: `search_threads` and `get_thread`, for the configured senders only. Never add an email-sending path. The guard denies it.
- Hyperlinks and source names in any output must come from the `items` table. Model text is data: it is sanitized and never rendered as a link.
- `hooks/guard_send.py` must fail closed. Any error exits with code 2, because Claude Code treats every other exit code as allow. Keep it stdlib-only.
- Determine delivery outcomes from connector tool results (stream-json `tool_events`), never from text the model wrote.
- Dry runs and failed runs must not change delivery state. That covers feed validators, saved digests and sent marks.
- Every outbound fetch goes through `dedup.safe_url`.
- Tests never touch the network or spawn `claude`. Use `httpx.MockTransport` and an injected `Runner`. Test fixtures must stay synthetic.
- Never commit `data/` or `reports/`. They hold your archive and rendered digests.
- Keep `config/company_context.md` free of revenue, pricing, contract figures and personal names, because it is sent to Claude on every run.
- Structured log fields use the `digest.` key prefix.

## Build & Test

- `uv sync`
- `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
- Live checks: `uv run digest doctor`, `uv run digest sources-check`, `uv run digest run --dry-run --date YYYY-MM-DD`

## Known Issues & Caveats

- If you're behind a TLS-inspecting proxy, httpx uses the system trust store (via `ssl.create_default_context()`), so install the proxy's CA there. uv may print harmless `Failed to load SSL_CERT_DIR` warnings about unreadable certificate files.
- `googlenewsdecoder` requires `selectolax<1`, which is pinned.
- A normal run skips weekends unless you pass `--date`.
- The systemd user timer runs only while the user manager is active. Use `loginctl enable-linger` if you need it to run while you are logged out.
- `digest doctor` fails until you replace the `XXXXXXXXXX` placeholder Slack IDs in `config/settings.yaml`.
