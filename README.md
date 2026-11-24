# Agentic Spend

A local-only dashboard for Claude Code / Grok / Codex / OpenCode / etc. spend, built on top of
[`ccusage`](https://ccusage.com). Branded for mastertheagent.com but fully generic — no
hardcoded personal data lives in the code, only in gitignored local data files.

Desktop-only by design: it binds to `127.0.0.1` and reads logs that only exist on the machine
you're running these CLI tools from, so it's meant to be opened on that same laptop/desktop, not
browsed from a phone — there's no mobile layout, and none is planned.

## Screenshots

(Figures below are from a real run with the account/email placeholdered out.)

**Overview** — what's actually costing money vs. covered by a subscription:

![Overview](screenshots/overview.png?v=2)

**Days** — a calendar heatmap, click a day to filter:

![Days](screenshots/days.png?v=2)

**Accounts** — real plan-limit usage per Claude account, live from the status line:

![Accounts](screenshots/accounts.png?v=2)

**Settings** — every source Agentic Spend found, theme toggle, live-usage setup:

![Settings](screenshots/settings.png?v=2)

## Prerequisites

- **macOS** (the Homebrew formula and the `agentic-spend` launcher script are macOS-only).
- **Node.js**, for `npx` — Agentic Spend shells out to [`ccusage`](https://ccusage.com) via
  `npx` to read your local tool-usage logs. Installing via Homebrew pulls Node in automatically
  (the formula declares `depends_on "node"`); running from a git clone (`python3 server.py`)
  needs it on your PATH already: `brew install node`, or get it from
  [nodejs.org](https://nodejs.org). If it's missing, the dashboard shows exactly this with a
  Retry button rather than a blank page.

## Install

```bash
brew tap tjorourke/agentic-spend
brew install agentic-spend
```

If Homebrew refuses to load the formula ("untrusted tap"), run
`brew trust --formula tjorourke/agentic-spend/agentic-spend` and install again (same as
[flightbay](https://github.com/tjorourke/flightbay)).

## Run it

```bash
agentic-spend          # starts the server (if not already running) and opens the browser
```

Binds to `127.0.0.1:8787` only — nothing here leaves the machine except the optional OpenAI
Costs API call (see below) and `npx` fetching `ccusage` itself on first run. For local
development instead of the brew install: `python3 server.py` from this repo, then open
`http://127.0.0.1:8787/`.

### Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`, no pip installs, consistent with the rest of the project. Covers the pure
data-transformation functions (`compute_accounts`, `build_hourly`, `build_sources`, the Grok
billing parser, the plan-usage-history writer/reader, and the token-source classifier) — the
logic most likely to silently produce a wrong figure.

### Upgrading

```bash
brew update && brew upgrade agentic-spend
```

(Maintainer only, to cut a new release: tag `vX.Y.Z`, push it, download the tagged
`.tar.gz` from GitHub, `shasum -a 256` it, and update `Formula/agentic-spend.rb`'s
`url`/`sha256`/`version` to match before committing and pushing the formula.)

## Live Claude usage (optional, recommended)

Self-service from the UI: Settings → "Live Claude usage" → **Enable**. No file
editing needed. Under the hood it's Anthropic's own documented status line mechanism
(https://code.claude.com/docs/en/statusline): `statusline.py` (in this repo) gets wired into
`~/.claude/settings.json`'s `"statusLine"` — this is global, every Claude Code session on the
machine, not just this project — and Claude Code feeds it real, server-reported
`rate_limits.five_hour`/`seven_day` (% used + reset time) on every reply, for Pro/Max/Ultra
accounts. The script prints a useful status line (cwd/model/5h%/7d%) AND, as a side effect,
writes that data to `~/.claude/cc-usage-live/<accountUuid>.json` (mode 0600). `account_usage.py`'s
`get_max_usage_snapshots()` reads that directory and prefers it over the cached
`cachedUsageUtilization` snapshot whenever it's present — shown in the UI as a green "live, as
of \<time\>" pill instead of the amber "cached \<time\>" one.

**If you already have your own status line configured**, Enable wraps it rather than replacing
it — your existing status line keeps showing exactly as before (via
`AGENTIC_SPEND_WRAP_STATUSLINE`), with the live-usage side effect just added on top. The
original config is backed up to `.statusline-backup.json` (gitignored) and restored exactly by
clicking **Disable**, which never touches a status line this project didn't set.

No credentials are read and no network calls are made by the script — it only reads what Claude
Code already pipes to it on stdin. Limitation: it only updates while that account's Claude Code
session is active and getting replies; an idle/switched-away account falls back to the cached
snapshot (or nothing, if neither exists).

## Live Codex usage (automatic, no setup)

Same trust tier as Claude — fully official and documented, shown automatically whenever `codex`
spend exists, no toggle or key needed. `provider_usage.get_codex_live_snapshot()` spawns
`codex app-server` and calls its documented `account/rateLimits/read` JSON-RPC method (confirmed
against OpenAI's own `codex app-server generate-json-schema` output), one-shot, no persistent
session. Cached 60s server-side (spawning a process has real overhead) and bypassed on a manual
refresh. Field names are camelCase (`usedPercent`/`windowDurationMins`/`resetsAt`) — note this if
cross-referencing OpenAI's docs, some third-party write-ups get this wrong.

## Live Grok usage (optional, unofficial)

Off by default — enable from the same Settings page. Unlike Claude/Codex, no
documented API exists for Grok's SuperGrok plan at all. `provider_usage.fetch_grok_billing()`
tries `grok agent stdio`'s undocumented `_x.ai/billing` method first (the `grok` CLI handles its
own login, so this code never reads a token), falling back to a direct HTTP call using the token
in `~/.grok/auth.json` if that's unavailable. Returns weekly on-demand/overage billing and plan
tier — **not** a weekly included-pool %, which doesn't appear to be exposed anywhere. Never
polled automatically, only on an explicit "Refresh now" click. If the reported plan tier doesn't
match a manual plan note you've saved, the UI flags it rather than silently preferring either one.

## Other things on Overview

- **"What's actually costing you money"** lists every source as one compact row — a provenance
  chip (`LIVE` / `CACHED` / `MANUAL` / `ESTIMATE`) so every figure says where it came from at a
  glance, an inline mini-bar wherever there's a plan-usage %, and the amount. Rows sort with real
  cost and anything genuinely unverified (an unlabeled account, sessions with no owner record) on
  top and confirmed-covered sources at the bottom, rather than in whatever order they were found.
  Hover a row for the full explanation.
- The KPI row includes **Plan limit** — the session/week usage percentage your subscription is
  actually bound by, shown next to the list-price dollar figures precisely because the two are
  easy to mistake for the same thing (a subscription caps usage by a session/week multiplier, not
  by a dollar amount). Avg/day and Peak day hide themselves on the 1-day range, where they'd just
  repeat Spend/Today back at you.
- **Hourly spend** — Claude's bars come from `ccusage blocks -n 1`, grouped by each request's own
  timestamp, so a past hour is final rather than drifting forward as a long session keeps running.
  Every other tool only has a session's last-active moment to go on, so those bars can still move.
- **Cache efficiency** and **Where tokens went today** sit side by side. Cache efficiency is
  uncached input / cache write / cache read / output, straight from fields ccusage already
  provides in `modelBreakdowns`. Where tokens went today splits main thread vs. subagents vs.
  compaction vs. auto-review, for Claude Code and Codex (the only two tools with enough local
  transcript detail for this) — reads the raw transcripts directly, not ccusage, tokens only since
  there's no pricing per-bucket.
- **Days** has a token count alongside cost for every date, with a breakdown (real vs. including
  cache) on hover, and a native date picker to jump straight to one.
- **Projects** (its own tab, next to Accounts) — spend per repo, using each session's real working
  directory as recorded in its own transcript. Claude-only, same scope as Accounts.
- **Plan usage over time** (on the Accounts page) — a chart of the 5h/weekly/30-day window
  percentage, not just the current reading. Claude's history accumulates going forward from the
  status line; Codex's is backfilled for real from its own rollout files, which already carry a
  rate-limit sample on nearly every reply.

The idea for several of these came from skimming
[boristopalov/tokenspend](https://github.com/boristopalov/tokenspend) and
[taranek/tokenspend](https://github.com/taranek/tokenspend) — no code from either was copied
(neither repo carries a license, and the implementations here are a different language and
structure throughout), just the observation that the data was worth showing.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
