# Security

## No new credentials

This tool creates no OAuth apps, API keys or tokens. It uses your existing
Claude Code login (a claude.ai account) and the claude.ai Gmail and Slack
connectors you have already authorised. `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` are stripped from the
environment of every `claude` subprocess, so a stray key can never switch
billing or identity.

## Least privilege per run

Every `claude` run is headless, with built-in tools disabled (`--tools ''`)
and `--permission-mode dontAsk`. Anything not explicitly allowed is denied.

| Stage | Profile | Allowed tools | Budget (USD, default) |
|---|---|---|---|
| Newsletter extraction | Gmail | `search_threads` and `get_thread`, read-only. The query limits results to the configured senders. | 0.50 |
| Scoring | Isolated | none | 0.50 |
| Synthesis | Isolated | none | 1.50 |
| Slack delivery | Gmail profile (connector access) | Slack `slack_send_message` only, to your own DM | 0.30 |
| `digest doctor` check | Gmail profile | `search_threads`, `get_thread` and `slack_send_message`, none of them called | 0.10 |

Isolated runs see no MCP servers and no user settings. Connector runs have the
other claude.ai connectors stripped (`claude.disallow_mcp`), and every Gmail
and Slack tool not on the run's allowlist is denied by name. You can change the
budgets in `config/settings.yaml`.

Gmail is strictly read-only. The tool uses only `search_threads` and
`get_thread` on the configured newsletter senders, and it never sends email
(Gmail `send_message` is always denied). The run that reads untrusted email
cannot post anything, and the run that can post sees only output rendered by
Python.

## Delivery guard hook

A PreToolUse hook (`hooks/guard_send.py`) wraps the delivery run. It fails
closed, so any error denies the call. It denies a Slack post unless all of
these hold:

- The tool is on the run's allowlist, which holds only `slack_send_message`.
- `channel_id` is one of the configured self-DM ids: your user id or your DM
  channel.
- The only keys are `channel_id`, `message` and `thread_ts`, so no broadcast,
  unfurl or draft options can be set.
- The SHA-256 of `message` equals the hash of one of the parts Python rendered,
  so the model cannot change the content.
- Parts go out in order and each part only once. One atomically created claim
  file per part (`data/deliver-<run>/<part>`) enforces this.
- Part 0 (the main post) has no `thread_ts`, and every later part has one.

Delivery success is judged only from the Slack tool results, never from model
text. Gmail `send_message` is denied unconditionally.

The same hook wraps the newsletter read run, with `--gmail-senders`. It allows
`search_threads` only when the query is exactly
`from:(<sender> OR <sender> ...)` over the configured senders (matched
case-insensitively), optionally followed by `after:YYYY/MM/DD`. Any other
operator or sender is denied. The hook cannot restrict `get_thread` by sender,
because thread ids come from search results. The guard therefore relies on the
read run finding threads only through the guarded search. That run cannot
write, because `send_message` is denied for it.

`digest doctor` evaluates the guard policy locally to confirm that it denies a
forged Slack post to another channel and a post with an unexpected payload. It
posts nothing. Links in the digest come only from stored items, never from
model text.

## Data handling

- **Sent to Claude:** newsletter text, public news items,
  `config/company_context.md` and the watchlist. Keep revenue, pricing,
  contract figures and personal names out of the context file.
- **Posted to Slack:** the digest summaries and links, as a DM thread to
  yourself. Nothing else is posted.
- **Kept local:** `data/` (the SQLite archive and cache) and `reports/` (the
  full report and the email-style HTML), both gitignored. Nothing is pushed or
  uploaded. `claude` subprocesses run from a neutral directory outside the repo
  (`${XDG_STATE_HOME:-~/.local/state}/energy-digest/claude-cwd`), so they do
  not pick up project files or settings.

## Network egress

The tool makes anonymous HTTPS GETs, using the system TLS trust store, to:

- the public hosts listed in `config/sources.yaml`
- news.google.com, for Google News RSS searches
- the article pages of the top-ranked items, to enrich them

Every fetch goes through `dedup.safe_url`. It allows only http(s) on standard
ports, the host must resolve to public addresses only, and each redirect hop is
checked again.

Claude traffic goes through the `claude` CLI. Newsletters are read through the
Gmail connector, and the digest is posted through the Slack connector.

## Residual risks

- **DNS rebinding.** `safe_url` resolves the host before the connection is
  made, so a hostile DNS server could answer differently at connect time
  (TOCTOU). Only anonymous GETs of public pages are at stake.
- **`get_thread` is not sender-limited.** The hook cannot restrict it to a
  sender. Threads can only be found through the guarded `search_threads`,
  which is limited to the configured senders.
- **Leftover `data/deliver-<run>/` directory.** After a `send-unknown` run
  this directory stays on purpose: it shows the guard allowed the main post.
  Check your Slack DMs before you rerun with `--force`.
- **Failed thread replies.** Some replies can fail after the main post
  succeeded. The run is still recorded as delivered, and a warning lists the
  missing parts.

## Reporting a vulnerability

This is a personal project. Please report security issues privately through
the repository's security advisory feature rather than a public issue.

## Template: note for an IT or security reviewer

If you run this tool on a managed machine, you can adapt this text:

> This is a local, scheduled Claude Code job that runs on my workstation under
> my Claude login. It reads a few named newsletters through the claude.ai Gmail
> connector. That run is read-only: it cannot send, and a hook limits its Gmail
> search query to the named senders. It posts one summary a day as a Slack DM
> to me only, through the claude.ai Slack connector. A separate hook enforces
> that channel and the exact content, and the run can use no other Slack tool.
> The job also reads public news RSS feeds and article pages. It creates no new
> OAuth apps, API keys or credentials, and stores nothing off the local machine
> except the summaries posted to my own Slack DM.
