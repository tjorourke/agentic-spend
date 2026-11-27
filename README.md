# Agentic Spend

A local-only dashboard for Claude Code / Grok / Codex / OpenCode / etc. spend, built on top of
[`ccusage`](https://ccusage.com). Branded for mastertheagent.com but fully generic — no
hardcoded personal data lives in the code, only in gitignored local data files.

Desktop-only by design: it binds to `127.0.0.1` and reads logs that only exist on the machine
you're running these CLI tools from, so it's meant to be opened on that same laptop/desktop, not
browsed from a phone — there's no mobile layout, and none is planned.

## Quick start

```bash
brew tap tjorourke/agentic-spend https://github.com/tjorourke/agentic-spend
brew trust tjorourke/agentic-spend         # newer Homebrew refuses untrusted taps (see below)
brew install agentic-spend                 # server + dashboard
agentic-spend                              # starts it and opens http://127.0.0.1:8787
brew install --cask agentic-spend-app      # optional menu bar app (macOS only)
open "/Applications/Agentic Spend.app"
```

On Linux it's the same first four lines, with [Homebrew on Linux](https://docs.brew.sh/Homebrew-on-Linux).
There's no menu bar app on Linux; the dashboard and `agentic-spend` work the same.

- **`brew trust`:** recent Homebrew versions refuse to install anything from a third-party tap
  until you trust it, and stop with an "untrusted tap" error. `brew trust tjorourke/agentic-spend`
  trusts this tap once (it changes your Homebrew settings, nothing else); the formula and the
  cask then install normally. Per-item alternatives are below under Install.
- **Dashboard:** http://127.0.0.1:8787. The server opens it once in your browser the first
  time it runs (set `AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1` to skip that). `agentic-spend login on`
  starts the server at login.
- **Menu bar app:** on its first launch it opens the dashboard and a short welcome. Its menu has
  **Open dashboard**.
- **macOS blocks the menu bar app at first** (and after each upgrade) because it is signed ad
  hoc, not notarised: **System Settings > Privacy & Security > Open Anyway**. If the icon still
  doesn't show, **System Settings > Menu Bar > Allow in the Menu Bar > Agentic Spend**. The app
  stays open and shows these steps if macOS hides it.
- **Icon still missing although Allow in the Menu Bar shows on?** macOS can keep a stale entry for
  the app. Quit it, `brew uninstall --cask agentic-spend-app`, open **System Settings > Menu Bar**
  once (macOS then drops the entry), `brew install --cask agentic-spend-app`, and approve it again.

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

- **macOS or Linux** (x86_64 or arm64). On Linux, install [Homebrew on Linux](https://docs.brew.sh/Homebrew-on-Linux)
  first. The menu bar app is macOS only.
- **No Node.js needed.** Agentic Spend reads your usage with [`ccusage`](https://ccusage.com),
  and the Homebrew install bundles ccusage's native binary for your OS and chip (version 20.1.2, MIT, see
  `THIRD_PARTY_NOTICES.md`) in `libexec/vendor/ccusage`. From a git clone (`python3 server.py`)
  it looks for a ccusage in this order: the `AGENTIC_SPEND_CCUSAGE` environment variable (a path),
  the bundled binary, `ccusage` on your `PATH`, and only as a last resort `npx ccusage@20.1.2`
  (which needs Node.js and the network; the server prints a message when it falls back to that).
  If none is available the dashboard says so, with a Retry button, rather than a blank page.

## Install

```bash
brew tap tjorourke/agentic-spend
brew trust tjorourke/agentic-spend
brew install agentic-spend
```

If Homebrew refuses to load the formula ("untrusted tap"), run `brew trust tjorourke/agentic-spend`
(the whole tap) or `brew trust --formula tjorourke/agentic-spend/agentic-spend` and install again (same as
[flightbay](https://github.com/tjorourke/flightbay)).

## Menu bar app (optional)

A small native app that sits in the macOS menu bar: the highest plan percentage in the bar
(for example `G 44%`), and a menu with each plan's weekly percentage, forecast and reset,
today's list-price spend, Open dashboard (a native window), Refresh, Start at login and Quit.
A gauge icon sits next to the percentage; "Menu bar shows" in the menu switches between icon and
percentage, icon only, or percentage only.
When it's running it shows the plan notifications natively instead of the server's
`osascript` ones. It talks only to the local server on `127.0.0.1:8787`, and starts it with
the Homebrew `agentic-spend` launcher if it isn't running.

```bash
brew install --cask tjorourke/agentic-spend/agentic-spend-app
```

This installs a prebuilt universal app (Apple silicon and Intel, macOS 13 or later) and the
`agentic-spend` formula it depends on. Nothing is compiled on your Mac.

**First launch needs one approval.** The app is signed ad hoc, not with an Apple Developer ID,
and it is not notarised, so macOS blocks it the first time:

1. Open **Agentic Spend** from Applications. macOS says it can't check it for malicious
   software. Click **Done** (not Move to Bin).
2. Open **System Settings > Privacy & Security**, scroll down to **Security**, and click
   **Open Anyway** next to "Agentic Spend was blocked".
3. Click **Open Anyway** again and enter your password or use Touch ID.

After that it opens normally. Allow notifications when it asks, if you want them. If Homebrew
says the tap is untrusted, run `brew trust --cask tjorourke/agentic-spend/agentic-spend-app`
first.

To build it yourself instead: `macos/build.sh 1.5.0` (needs the Xcode command line tools),
then open `macos/out/Agentic Spend.app`.

## Run it

```bash
agentic-spend          # starts the server (if not already running) and opens the browser
agentic-spend --no-open  # starts the server without opening a browser
agentic-spend stop     # stops it
agentic-spend login on # start at login (also: off, status)
```

Binds to `127.0.0.1:8787` only — nothing here leaves the machine except the optional OpenAI
Costs API call (see below), and the pricing refresh the bundled ccusage does itself (it fetches
model prices from LiteLLM and models.dev and falls back to its built-in prices if that fails). For local
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

## Forecasts, comparisons, notifications and the weekly report

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
- **Notifications.** Settings, "Background and notifications": thresholds per plan, whole
  percentages from 1 to 100 (default 80% and 95%), an alert when paid extra credit starts being used, and a "Send test
  notification" button. The server checks every 10 minutes and shows each threshold once per
  plan window (state in `~/.agentic-spend/notification-state.json`). Delivered with
  `osascript` on macOS (or by the menu bar app when it's running) and `notify-send` on Linux
  (`libnotify-bin` on Debian and Ubuntu, `libnotify` on Fedora and Arch).
- **Weekly report.** `/?tab=digest` (and `/api/digest?week=YYYY-MM-DD`): one complete
  Monday-to-Sunday week, usage value at API prices and real spend, tokens, each plan's highest
  reading, top projects and models, and the change on the week before. Previous and Next (or the
  week menu) step back through every week in your history; a week with no usage says so. A
  notification points to it on Mondays at 09:00 while the server is running.
- **Start at login.** Settings toggle, or `agentic-spend login on|off|status`. On macOS it writes
  `~/Library/LaunchAgents/com.agenticspend.server.plist`, which runs
  `agentic-spend serve` once at login (RunAtLoad, no KeepAlive), so readings and
  notifications carry on without the browser open. On Linux it writes a systemd user service,
  `~/.config/systemd/user/agentic-spend.service` (enabled, Restart=no). With no systemd user
  session (some containers, WSL without systemd) it writes a desktop autostart entry,
  `~/.config/autostart/agentic-spend.desktop`, instead.

## Team value, 5-hour pacing, model mix and sources (1.7.0)

- **Team value.** Each subscription row shows ccusage's API-equivalent cost for that account
  this month (US-dollar list price) next to the plan's price. See "Plans and list prices
  (1.7.1)" below for where the price comes from. Nothing is converted between currencies; the
  "×" multiple only appears when the price is in US dollars.
- **5-hour pacing.** For the current Claude 5-hour window the server works out a pace from
  the status line's readings (or, with one Claude account, ccusage's active block burn rate)
  and projects when it reaches 100%. If that is before the reset, the row and the menu bar
  turn amber and one notification is sent for that window (switch off in
  Settings, "Background and notifications"). Thin data (under 20 minutes or under 5%) and no
  Claude activity for 30 minutes do not warn.
- **Model mix** (Overview): spend by model for the selected range, and an estimated saving had
  the Opus usage been priced at Sonnet's rates. ccusage does the pricing: it is run again with
  a temporary config whose `pricingOverrides` give the Opus models the Sonnet model's LiteLLM
  rates (from ccusage's own price cache), both runs in `--mode calculate`. It is labelled as
  an estimate: another model would not produce the same tokens.
- **Sources.** ccusage 20.1.2 reads 20 tools (Claude Code, Codex, OpenCode, Amp, Droid,
  Codebuff, Hermes Agent, pi-agent, Goose, OpenClaw, Kilo, GitHub Copilot CLI, Gemini CLI,
  Antigravity, Kimi, Qwen, Grok Build CLI, ZCode, Cline, DeepSeek Harness). Each one with data
  on this computer gets a row and a small badge with ccusage's own tier, copied from the title of
  its page in ccusage's docs: no tag is shown as "first-class", "(Beta)" as beta and
  "(Experimental)" as experimental. ccusage marks a source experimental when the tool's local
  log format is undocumented or still changing. Tools that aren't on this computer aren't listed.
- **ccusage telemetry.** Official ccusage builds send one anonymous ping a day (version, OS,
  architecture and command name, never usage data; see https://ccusage.com/guide/telemetry).
  The bundled binary is the official build. To turn it off: `ccusage telemetry disable`, or
  set `CCUSAGE_TELEMETRY_DISABLED=1` or `DO_NOT_TRACK=1` for the server.

## Plans and list prices (1.7.1)

- **Plan detection, local files only.** No token is printed, stored or sent.
  - Claude: `~/.claude.json`'s `oauthAccount` (organisation type, seat tier and rate-limit
    tier) for the current login, plus Claude Code's own backups of that file for accounts
    you've switched away from. The non-secret fields are kept in `plan-profiles.json` in the
    data directory. Team seat tiers use Claude Code's own table (`team_tier_1` is a Premium
    seat). Max with the 20x rate tier is shown as Ultra, the name Anthropic now uses for it.
  - Codex / ChatGPT: the plan in Codex's own rate-limit reply, or the `chatgpt_plan_type`
    claim in `~/.codex/auth.json`. Only that claim is read.
  - Grok: the plan in the Grok Bot app's local usage cache.
  - OpenCode: the credential type for each provider in its `auth.json`.
  - Anything else ccusage reads shows "unknown plan". The app never guesses a plan.
- **List prices.** `plan_prices.json` ships list prices from each provider's official
  pricing or help page. Each row stores the source URL, currency, billing (monthly or annual)
  and the date it was fetched. Enterprise plans and prices that couldn't be read are left
  blank ("contact sales"); nothing is invented. US dollars throughout, plus pounds for xAI,
  which publishes them. Prices exclude VAT unless the row says otherwise.
- **Defaults.** When the plan is detected and you haven't set a price, the monthly-billing
  list price is used, in the account's billing currency where that is known, otherwise in
  the pricing-region currency (see 1.7.2). The app marks it "list price (default)". A price
  you enter in Settings is marked "your price" and always wins. Use it for annual billing or
  a discount, because the billing cycle can't be read locally.

## Pricing region and UK list prices (1.7.2)

- **Pricing region.** Settings has a UK (GBP) or US (USD) choice. It defaults from the
  system locale and can be changed: `AppleLocale` on macOS (for example `en_GB` → UK), and on
  Linux `LC_ALL`, then `LC_MONETARY`, then `LANG` (for example `en_GB.UTF-8` → UK). When choosing
  a default price, the account's own billing currency wins if it's known (Claude's
  usage-credit currency); otherwise the region setting is used. A published GBP row is
  preferred when the provider has one; otherwise the USD row is shown and labelled
  "USD list price, no UK price published". Nothing is converted between currencies.
- **UK prices shipped only when official.** xAI publishes pounds on [grok.com/plans](https://grok.com/plans)
  (excluding tax). On 26 Nov 2026, [claude.com/pricing](https://claude.com/pricing) still
  showed US dollars from a UK Mac, and neither ChatGPT's nor OpenCode's pricing pages could
  be read as a published UK pound list, so no GBP rows are shipped for those.

## Version 1.8.6

- **Linux.** Agentic Spend now runs on Linux as well as macOS, from the same Homebrew formula.
  It bundles ccusage's static Linux build (x86_64 or arm64), so it runs on any distribution.
  What's different on Linux:
  - The Grok Bot app's cache is read from `~/.config/Grok Bot/` and the Claude desktop app's
    plan file from `~/.config/Claude/` (both follow `XDG_CONFIG_HOME`). Both apps have official
    Linux builds. If one isn't installed, it's listed as not found, not as an error.
  - Apps are found from their `.desktop` entries (`~/.local/share/applications`,
    `/usr/share/applications`, Flatpak and Snap) instead of `/Applications`.
  - Start at login uses a systemd user service, or an autostart entry without systemd.
  - Notifications use `notify-send`. Without it the test button says so and nothing else breaks.
  - The pricing region comes from `LANG` and friends.
  - The launcher opens the browser with `xdg-open`. On a machine with no desktop it prints the URL.
  - The dashboard says "this computer" on Linux and "this Mac" on a Mac.
- **Accounts card after a reset.** Straight after a session or weekly reset the card said
  "reset Fri 15:00, no use since" with nothing after "since". It now says "reset Fri 15:00".
  A window with no use since its last reading says "no use since 17:23".

## Version 1.8.5

- **Allowance history.** Every time the server reads your plan limits (Claude session and weekly
  %, Codex, Grok Bot, reset times and usage credits), it adds a line to
  `~/.agentic-spend/allowance-history.jsonl`, one per account. A reading that repeats the last one
  is skipped, apart from one every three hours so a quiet week still draws a line. Each line holds
  the account key, a label, percentages, reset times and credit figures. No emails, tokens or
  transcript text. Readings are only taken while the server or the Claude status line is running,
  so a week with the computer asleep will have gaps.
- **Plan limit through the week.** The Weekly report now draws a line chart per account: the
  weekly limit (or the plan's own window, such as Codex's 30 days) and the 5-hour session, with
  the week's peaks and the usage credits used. Hover a dot for the time and figure. The table of
  peaks against the week before is still there. Weeks before 1.8.5 only have what the status line,
  Grok Bot and Codex recorded at the time.
- **Trend on the plan card.** Overview's Plans and real spend card shows a small line of the last
  seven days of weekly % next to each plan that has at least two readings.
- **Seat value.** A card on the Overview compares each subscription's usage value at API prices,
  month to date, with what the seat costs: the list price for the detected plan (labelled list
  price) or the price you enter in Settings, Plans and seat prices (labelled your price). It shows
  the ratio, for example about 15x, and the usage credits used. The Weekly report has the same card
  with the seat cost prorated to the week (7 / days in the month). Only accounts with a paid seat
  and some usage in the period appear. Usage value is in US dollars and nothing is converted, so a
  seat billed in pounds gets a ratio only against the provider's US list price, marked US list.
- **Usage archive.** Claude Code and the other tools delete old transcripts, and ccusage can only
  count what is still on disk. Once a day, and on the first start when there is no archive, the
  server writes each complete day's totals to `~/.agentic-spend/archive/YYYY-MM.json`: per day,
  account, agent, model and project, tokens by type and cost. It never copies transcript content.
  When a day is no longer in the transcripts, the dashboard uses the archived totals and says so on
  the Overview. A day that is still live always uses the live figures, so nothing is counted twice
  and live totals still match ccusage exactly. An archived day is only ever replaced by a fuller
  live reading, never a smaller one. The first run seeds the archive from everything ccusage can see
  today.

## Version 1.8.4

- **Token bars stacked by kind.** In the Tokens view, the Weekly report's Top projects, Top models,
  By agent and By day bars, and Overview's Model mix, are stacked and colour-coded by input, output,
  cache write and cache read, with a legend. Hover a bar for exact counts. These bars count every
  token, cache included, so they match ccusage's totals (the Tokens tile still shows input plus
  output). The Text view gains the four columns; the $ view is unchanged. The Daily chart's tooltips
  now give exact counts too, and it has the same legend.
- **Claude desktop card.** Reads the Claude desktop app's `plan-usage-history.json`: the 5-hour and
  weekly plan limits, per Claude organisation, as percentages only (no tokens or cost), with when
  each reading was taken. The app writes it now and then while it is open, so it can be days old.
- **Codex plan limit without the codex CLI.** When the `codex` binary or its app-server isn't
  available (Codex desktop or IDE users), the plan percentage comes from the newest rate-limit
  reading in `~/.codex/sessions`, labelled with when Codex wrote it, never "live". Codex is
  detected from `~/.codex` alone.
- **Cards only when there's something to show.** The Claude desktop and Codex plan cards appear
  only when that app has usage or readings in the period on screen (day, week or custom range);
  otherwise they are left out, with no empty cards.
- **What's covered** (bottom of Overview). Which apps had usage or readings in the period, which are
  on this computer with nothing in the period (collapsed), and the chat apps that keep no usage data on
  disk, with each vendor's own usage page. See Desktop and chat apps below.
- **Install:** the Quick start now includes `brew trust tjorourke/agentic-spend`.

## Desktop and chat apps (1.8.4)

Agentic Spend only reads files already on your computer. It never opens cookies, browser storage,
keychains or auth files, and it does not scrape websites.

| App | What Agentic Spend shows | Where it comes from |
| --- | --- | --- |
| Claude Code, Codex, OpenCode, Gemini CLI, Copilot CLI, Grok CLI and the rest of ccusage's list | Tokens and usage value | Local logs, read by the bundled ccusage |
| Claude desktop | 5-hour and weekly plan limits, percentages only | `~/Library/Application Support/Claude/plan-usage-history.json` on macOS, `~/.config/Claude/plan-usage-history.json` on Linux |
| Codex desktop or IDE | Tokens (via ccusage) and the plan limit percentage | `~/.codex/sessions` |
| Grok Bot app | Share of the weekly Grok limit | The app's local usage cache (`~/Library/Application Support/Grok Bot/` on macOS, `~/.config/Grok Bot/` on Linux) |
| ChatGPT, Gemini app and web, Perplexity, Grok app, Microsoft Copilot, Cursor (individual plan) | Nothing: no usable usage data on disk | Check the app's own usage page (linked from What's covered) |

Browser chat (claude.ai, chatgpt.com, gemini.google.com) is not covered. Chat in the browser and in
Claude Code draws on the same Claude plan limit, so the Claude limit bars already include it; there
is no supported way to split out how much came from chat.

### GitHub Copilot in VS Code (opt-in)

Copilot Chat in VS Code writes no token log unless you turn on its OpenTelemetry export. To have
ccusage (and so Agentic Spend) read it, export to a file under `~/.copilot/otel/`, which is where
ccusage looks for Copilot OpenTelemetry files. In VS Code's `settings.json`:

```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "file",
  "github.copilot.chat.otel.outfile": "/Users/YOU/.copilot/otel/vscode-copilot-chat.jsonl"
}
```

Use your own home folder in the path and create `~/.copilot/otel` first. Prompts and responses are
not written unless you also turn on `captureContent`. See VS Code's
[Monitor agent usage with OpenTelemetry](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)
and ccusage's [Copilot source](https://ccusage.com/guide/copilot/). Only usage from after you turn it
on is recorded. The Copilot CLI needs nothing extra: ccusage reads `~/.copilot/session-state`.

### Gemini CLI telemetry (opt-in)

ccusage reads Gemini CLI's own chat files in `~/.gemini/tmp`, and shows nothing for sessions whose
files have no token counts (some Gemini CLI versions write none). Gemini CLI can also record token
counts per request to a local telemetry file. In `~/.gemini/settings.json`:

```json
{
  "telemetry": {
    "enabled": true,
    "target": "local",
    "logPrompts": false,
    "outfile": ".gemini/telemetry.log"
  }
}
```

`logPrompts: false` keeps prompt text out of the file. See
[Gemini CLI telemetry](https://geminicli.com/docs/cli/telemetry). Agentic Spend does not read that
telemetry file yet; inside a session, `/stats` shows the same token figures.

## Version 1.8.3

- **Menu bar icon, root cause found:** macOS (Control Center) keeps an Allow in the Menu Bar entry per
  bundle id (`com.agenticspend.menubar`), not per signature: ad hoc rebuilds and upgrades keep it. If
  the app is switched off there, its entry can stay blocked even after switching it back on. A fresh
  install shows the icon after the usual Open Anyway approval. The help window and caveats now give
  the reset (uninstall, open Menu Bar settings once, reinstall).
- **Charts:** the Cache read chart (and every other drawn chart) now has a fixed pixel height, so its
  axis labels stay the same size as the chart above at any window width.
- **Dev builds:** `macos/build.sh` signs with a self-signed "Agentic Spend Local" certificate (or
  `AGENTIC_SPEND_SIGN_IDENTITY`) when you have one; otherwise ad hoc, as releases are.

## Version 1.8.2

- **Menu bar icon:** macOS (Control Center) can hide a menu bar app's icon: until the app is
  approved under Privacy & Security > Open Anyway, or when it is switched off under Menu Bar >
  Allow in the Menu Bar. The app used SwiftUI's `MenuBarExtra` without `isInserted`, which quits
  the whole app as soon as the icon is hidden, so it vanished without a word. It now stays open,
  shows where to switch it on, and the build fails if the label would render empty.
- **First run:** Formula and Cask caveats print the dashboard URL and the Open Anyway path; the
  server opens the dashboard once on its first run; the menu bar app opens it with a welcome on
  its first launch.
- **$ | Tokens:** one switch in the top bar, next to Graph | Text, for the Overview, Model mix,
  Projects and Weekly report. Remembered in the browser.
- **Tokens in the Model mix:** every cell shows usage value and tokens (input + output, as on the
  Tokens tile); hover for input, output, cache write and cache read.
- **Faster tabs:** after the Overview loads, the Weekly report and Settings data are fetched in
  the background, and the server answers the Weekly report from a recent cache instead of
  re-running ccusage (about 5 s).

### Notarisation (not done yet)

The Open Anyway step goes away only if the app is signed with an Apple **Developer ID
Application** certificate (Apple Developer Program, USD 99 a year) and notarised. That needs:
the certificate exported as a `.p12` and stored, with its password, as GitHub Actions secrets;
an App Store Connect API key (issuer ID, key ID, `.p8`) as secrets for `xcrun notarytool`; and a
release workflow on a macOS runner that imports the certificate into a temporary keychain,
signs with `codesign --options runtime --timestamp`, submits the zip with
`xcrun notarytool submit --wait`, staples the ticket with `xcrun stapler staple`, then uploads
the zip and updates the Cask sha256. Until then the app stays ad-hoc signed; nothing here
bypasses Gatekeeper.

## Version 1.8.1

- **Status line no longer wraps itself.** If `AGENTIC_SPEND_WRAP_STATUSLINE` points at this
  project's `statusline.py` (the repo, a Homebrew Cellar keg, or the `opt` prefix), or if a
  wrap is already in progress, the hook prints its own line and does not start another copy
  of itself. A leftover wrap setting in a long-running Claude Code session could previously
  spawn hundreds of processes.

## Version 1.8.0

- **One Save in Settings.** Every field on the Settings page (account names, pricing region,
  seat prices, OpenCode backend billing, the manual note, the OpenAI Admin key, notifications
  and start at login) is saved by one **Save settings** button, in one request to
  `POST /api/settings`. The server checks every section first and writes nothing if any part is
  wrong; each file is written to a temp file and renamed into place. The bar says when you have
  unsaved changes, and a background refresh no longer wipes what you typed. (In 1.7.2 each
  account name had its own Save, and saving one re-drew the page and cleared the other box.)
- **OpenCode backends.** OpenCode sends each prompt to a backend. The server reads the provider
  behind every model call from OpenCode's own history (`opencode.db`, read-only) and how you
  signed in from `auth.json` (credential type only, never the key), then splits OpenCode's
  ccusage cost by backend: for example "Google AI Studio (Gemini API key), pay as you go". API
  keys and cloud billing (Bedrock, Vertex) count as real spend at list price; subscription
  logins, company gateways and free tiers do not. A custom provider (a company gateway or a
  local proxy) can't be classified from local files, so Settings > OpenCode backends lets you
  choose who pays; a choice there always wins. Stored in `~/.agentic-spend/opencode-providers.json`
  (see `opencode-providers.json.example`).
- **Clearer names.** "Spend by account/model/day/project" are now "Usage value by ... (API
  prices)": what the usage would cost at published API prices, not what you were charged. The
  Overview cost card is "Plans and real spend", with a headline list-price figure over a bar split
  into covered by subscriptions (green) and real / unverified spend (red).
- **Overview.** The single-account "Plan limit" tile is gone (every plan's limits are already in
  "Plans and real spend"). The Models tab is folded into Overview: Model mix now splits each
  model by the account it ran under (your names from Settings; Codex, OpenCode and Grok under
  their own name; Claude sessions with no account record as Unassigned), and the full sortable
  model table sits under it. Old `?tab=models` links land there.
- **Projects** show the accounts and models behind each project, with tokens and usage value,
  when you click a row.
- **Table or Graph.** One switch in the top bar shows the Weekly report cards, Model mix and
  Projects as tables or as bars, with the same figures. The choice is remembered in the browser.
- **Look.** Light grey side navigation with icons, a white main area with a thin top bar (page
  title and Refresh), and a wider layout (up to 1600px, more on very large screens).

## Live Claude usage (on by default)

**It turns itself on.** The first time the server starts and Claude Code is installed, it wires
up the status line hook below and records that in `~/.agentic-spend/setup-state.json`. If you
turn it off in Settings → "Live Claude usage" → **Turn off**, it stays off: the server never
switches it back on. To opt out before the first start, set `AGENTIC_SPEND_NO_AUTO_ENABLE=1`.
You can also turn it on or off yourself from that Settings section. No file editing needed.

The hook points at a path that survives `brew upgrade` (`<brew prefix>/opt/agentic-spend/libexec/statusline.py`).
Every start also tidies older installs: entries from earlier versions (a versioned Cellar path,
a git checkout, or several of them stacked inside each other) are collapsed into one.
The live file is written atomically (a temp file renamed into place, mode 0600), so a Claude Code
refresh that cancels the status line mid-update can't leave it empty. The "limits may be out of
date" banner shows only when the hook isn't ours and the Claude numbers are cached ones. Under the hood it's Anthropic's own documented status line mechanism
(https://code.claude.com/docs/en/statusline): `statusline.py` (in this repo) gets wired into
`~/.claude/settings.json`'s `"statusLine"` — this is global, every Claude Code session on the
machine, not just this project — and Claude Code feeds it real, server-reported
`rate_limits.five_hour`/`seven_day` (% used + reset time) on every reply, for Pro/Max/Ultra
accounts. The script prints a useful status line (cwd/model/5h%/7d%) AND, as a side effect,
writes that data to `~/.claude/cc-usage-live/<accountUuid>.json` (mode 0600). `account_usage.py`'s
`get_max_usage_snapshots()` reads that directory and prefers it over the cached
`cachedUsageUtilization` snapshot whenever it's present — shown in the UI as a green "live 17:23" (or "live, 1 day old") pill. A window whose reset has passed, with no newer reading, shows 0% and "reset Fri 15:00" — never a stale percentage, and never the word "cached".

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
  missing into a collapsed "Not found on this Mac" (or "this computer" on Linux) section. `/api/usage` includes a `detection`
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
  local cache (`~/Library/Application Support/Grok Bot/sand-client-persistence/` on macOS,
  `~/.config/Grok Bot/sand-client-persistence/` on Linux).
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
