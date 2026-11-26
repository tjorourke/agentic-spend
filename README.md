# Agentic Spend

A local-only dashboard for Claude Code / Grok / Codex / OpenCode / etc. spend, built on top of
[`ccusage`](https://ccusage.com). Branded for mastertheagent.com but fully generic — no
hardcoded personal data lives in the code, only in gitignored local data files.

Desktop-only by design: it binds to `127.0.0.1` and reads logs that only exist on the machine
you're running these CLI tools from, so it's meant to be opened on that same laptop/desktop, not
browsed from a phone — there's no mobile layout, and none is planned.

## Screenshots

(Figures below are sample data, with example names and paths, not a live account.)

**Overview** — what's actually costing money vs. covered by a subscription:

![Overview](screenshots/overview.png?v=3)

**Days:** a calendar heatmap. Click a day to show that day:

![Days](screenshots/days.png?v=3)

**Accounts** — real plan-limit usage per Claude account, live from the status line:

![Accounts](screenshots/accounts.png?v=3)

**Settings** — every source Agentic Spend found, theme toggle, live-usage setup:

![Settings](screenshots/settings.png?v=3)

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
agentic-spend --no-open  # starts the server without opening a browser
agentic-spend stop     # stops it
agentic-spend login on # start at login (also: off, status)
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
billing parser, the Grok Bot cache reader, the plan-usage-history writer/reader, and the token-source classifier) — the
logic most likely to silently produce a wrong figure.

### Upgrading

```bash
brew update && brew upgrade agentic-spend
```

(Maintainer only, to cut a new release: tag `vX.Y.Z`, push it, download the tagged
`.tar.gz` from GitHub, `shasum -a 256` it, and update `Formula/agentic-spend.rb`'s
`url`/`sha256`/`version` to match before committing and pushing the formula.)

## Forecasts, comparisons, notifications and the weekly digest

- **Forecast.** Each plan with a weekly (or other fixed) window shows a straight-line
  forecast next to its row on Overview and its card on Accounts: "on track for 74% by
  Sun 09:06", or "would hit 100% around Sat 14:00" in red. Usage is 0% at a reset, so the pace
  is measured from the window start; once this app has a day or more of readings in the
  window it uses the slope of those readings instead. Fewer than 24 hours of readings is
  marked "(early estimate)". Sources: Grok Bot (its saved history), Claude's 7-day window
  (status line history), Codex's primary window (its rollout files).
- **Compare.** The list-price and Tokens tiles show "+12% vs last week" (or vs yesterday, or
  vs the previous N days for other ranges). Plan rows show this window's peak against the
  previous window's peak, in percentage points. Both are hidden when there is no earlier data.
- **Click-through.** A Daily spend bar (and a Grok Bot day bar) opens that day on the Days
  tab. A model bar on a single-day chart opens that model. An account or agent in the
  Spend by account legend filters the dashboard to it (click again to clear). Bars and
  legend items work with the keyboard (Tab, then Enter or Space).
- **Notifications.** Settings, "Background and notifications": thresholds per plan (default
  80% and 95%), an alert when paid extra credit starts being used, and a "Send test
  notification" button. The server checks every 10 minutes and shows each threshold once per
  plan window (state in `~/.agentic-spend/notification-state.json`). Delivered with
  `osascript`, or by the menu bar app when it's running.
- **Weekly digest.** `/?tab=digest` (and `/api/digest`): last complete Monday-to-Sunday week,
  list-price and real spend, tokens, each plan's highest reading, top projects and models,
  and the change on the week before. A notification points to it on Mondays at 09:00 while
  the server is running.
- **Start at login.** Settings toggle, or `agentic-spend login on|off|status`. Writes
  `~/Library/LaunchAgents/com.agenticspend.server.plist`, which runs
  `agentic-spend serve` once at login (RunAtLoad, no KeepAlive), so readings and
  notifications carry on without the browser open.

## Live Claude usage (optional, recommended)

Self-service from the UI: Settings → "Live Claude usage" → **Turn on**. No file
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
  Hover a row for the full explanation. A tool only appears in the dashboard when it is installed
  on this machine (its CLI is on `PATH`, a known app bundle is present, or a data directory it
  already uses exists) or when historical data for it is already on disk. A $0 row with no
  activity in the selected range is hidden, unless it is a plan-limit reading such as the Grok Bot
  weekly share or a Claude or Codex plan window. Claude-specific banners and plan tiles are
  hidden when Claude Code is not detected. Settings lists what was found, and tucks anything
  missing into a collapsed "Not found on this Mac" section. `/api/usage` includes a `detection`
  list with `installed` and `hasData` for each supported tool (`claude`, `codex`, `opencode`,
  `grok-cli`, `grok-bot`). The existing `sources` array is still the cost and plan catalog, with
  absent tools removed and the same flags copied onto each remaining row.
- The KPI row leads with **List-price estimate** (ccusage's USD total: tokens times published
  prices). Under it, real and covered reuse the same subtraction as "What's actually costing you
  money". **Plan limit** is the session/week usage percentage the subscription is actually bound
  by, for a named account, with that session's and week's reset times. It sits next to the
  list-price figures because a subscription caps usage by a session/week multiplier, which is a
  different kind of number from a dollar amount. The tile follows the header filter: a Claude
  account shows that account, Codex shows Codex's own window, and any other tool says it is not
  applicable. A cached plan-limit reading is labelled `cached`, with the time it was read. The
  Tokens tile is input plus output, excluding cache. Avg/day is list price divided by calendar
  days in the range, with the average of days that had spend as a secondary figure when that
  differs. Avg/day and Peak day hide themselves on a 1-day range, where they would repeat the
  list-price total. Picking a past day also hides the Today tile, so that number is not read as
  the day you picked.
- **Hourly spend:** Claude's bars come from `ccusage blocks -n 1`, grouped by each request's own
  timestamp, so a past hour is final rather than drifting forward as a long session keeps running.
  Every other tool only has a session's last-active moment to go on, so those bars can still move.
  Hourly data is collected for today only. Choosing an earlier day hides the hourly card.
- **Cache efficiency** and **Where tokens went** sit side by side and follow the selected range.
  Cache efficiency is uncached input / cache write / cache read / output, straight from fields
  ccusage already provides in `modelBreakdowns`, plus an estimate that cache reads cost about
  0.1x a fresh input token. Where tokens went splits main thread vs. subagents vs. compaction
  vs. auto-review, for Claude Code and Codex. The title names the window ("last 30 days", or
  "on 24 Nov 2026"). All is capped at the last 90 days so the transcript parse stays fast.
- **Days** has a token count alongside cost for every date, with a breakdown (real vs. including
  cache) on hover. Clicking a heatmap cell, a table row, the peak-day date, or a daily bar sets
  that one day as the range and shows a "Showing 24 Nov 2026" chip. Back returns to the previous
  range, including a tab or account change. Custom from/to dates stay behind a Custom control.
  Days with no spend still take a column, at $0. Spend by account uses the same header filter.
- **Projects** (its own tab, next to Accounts): spend per repo. Claude uses the working directory
  in the transcript. The Grok CLI uses the decoded folder name under `~/.grok/sessions`. Long
  paths are shortened (home becomes `~`, the middle collapses) and the full path stays on the
  tooltip. Columns sort, and there is a token count (input plus output, excluding cache).
- **Account names:** an account that still has no name can be named from its Accounts card, from
  its estimate row on Overview, or from the Settings table. A named account can still be renamed
  from its card. The name is saved on this machine in `account-labels.json` (see
  `account-labels.json.example`). It is not written into source. The sidebar has the theme toggle
  and an "updated" time taken from the server. If a refresh fails, that stamp says the figure is
  cached and a one-line banner offers Retry.
- **Grok CLI:** `~/.grok/sessions/<project>/<session>/usage.json` is read locally. Turns are
  bucketed by the local date of `endedAt`. `costUsdTicks` is 10^10 ticks per USD (Grok's own
  `grok usage` guide). Turns have no cost of their own, so the session total is split by token
  share and labelled as such. Session files do not record the account. A session updated after
  the last time this app looked is tagged with the account currently in `~/.grok/auth.json`.
  Older sessions stay "Unattributed Grok account" until you name them. The map is
  `grok-account-map.json` in the data directory (see the example). The unofficial
  `_x.ai/billing` reading is unchanged and still opt-in.
- **Grok Bot:** the weekly plan share is read automatically from the Grok Bot desktop app's
  local cache on this Mac (`~/Library/Application Support/Grok Bot/sand-client-persistence/`).
  The card shows the plan name, weekly percentage, reset time (local, with a countdown), extra
  credits, and a short history of the percentage. Overview also shows usage by day, derived
  from positive increases in that history within the same weekly window, and a today figure
  when you are looking at a single day. On a 30-day or all-time range, and on the Accounts
  card, a "Grok Bot by week" chart shows the highest weekly share reached in each weekly
  window. History is kept for 400 days. Readings from the last 14 days are stored in full
  (still dropping unchanged samples within 30 minutes); older readings are downsampled to
  one per hour, plus each window's last reading before reset, so the file stays modest. A
  compact per-window summary (reset time, peak percentage, first and last reading) is kept
  so the weekly trend survives that downsampling. A reading is taken whenever the dashboard
  loads usage, and every ten minutes while the server is running, so the day fills in even
  if the page is closed. Grok Bot reports a share of a weekly limit
  shared with other apps on the plan, not tokens or a dollar spend. This dashboard only reads
  that local file; it does not call Grok's servers or use cookies or tokens. If the cache is
  not there yet, the card explains how to get one: open the Grok Bot app so it can write the
  snapshot, then refresh. For a machine without the app, `scripts/grokbot-push.sh` can POST a
  JSON snapshot (or a pasted usage report) to `http://127.0.0.1:8787/api/grokbot`, or write
  the file directly if the server is down. Several Grok Bot accounts each keep their own
  history, keyed by the account id in the cache filename.
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
