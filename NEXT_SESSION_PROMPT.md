# Next session: five features shipped, all verified against real data

Continue work on the Agentic Spend dashboard at `~/code/agentic-spend` (`agentic-spend` to
launch, read `README.md` first). This pass was triggered by skimming two similar OSS tools
(boristopalov/tokenspend, taranek/tokenspend) for ideas, then implementing the ones worth having
— see the README's "Other things on Overview" section for the user-facing description of each.

## What changed this session

- **Hourly chart no longer shifts.** It used to attribute a whole session's cost to whichever
  hour it was last active in, so a long-running session visibly "moved" from one hour's bar to
  the next on every refresh — a user complaint, not a cosmetic nit. Now Claude's bars come from
  `ccusage blocks -n 1` (real per-request timing); other tools still only have a session's
  lastActivity, documented honestly in the chart's own caption. Falls back to the old behavior if
  the `blocks` call itself fails, rather than silently dropping Claude from the chart.
- **Cache efficiency card** (Overview) — uncached input / cache write / cache read / output, from
  fields ccusage's `modelBreakdowns` already provides. No new data source.
- **Projects tab** (next to Accounts) — spend per repo, from each session's real `cwd` (captured
  in the same transcript scan `build_session_account_map()` already did for the account map, not
  a second pass, and not decoded from the lossy dash-encoded directory name).
- **Plan usage over time** (Accounts page, under each Claude account and once for Codex) — a
  chart of the 5h/weekly/30-day window percentage over time, not just the current reading.
  Claude's history accumulates going forward via `statusline.py`; Codex's is backfilled for real
  from its own rollout files (`event_msg`/`token_count` lines already carry a rate-limit sample
  on nearly every reply — no forward-only wait needed for that one).
- **"Where tokens went today"** (Overview, lazy-loaded) — main thread vs. subagents vs.
  compaction vs. auto-review, for Claude Code and Codex only (the two tools with enough local
  transcript detail). New module `token_sources.py`, a separate `/api/token-sources` endpoint
  (never on the 10s `/api/usage` path — parsing a day's transcripts takes real time). Reconciled
  against ccusage's own numbers before shipping: Codex matched exactly across 96 files; Claude's
  per-day totals were within 0-1.5% (ccusage fills small transcript gaps from its own cost-state
  tracking; this parser only reads what's actually logged).

## A live bug, caused and fixed in the same session

Adding `import data_dir` to `statusline.py` broke the status line for every Claude Code session
on this machine for about 10 minutes — Claude Code runs it with `python3 -I`, which drops the
script's own directory from `sys.path`, so the sibling import raised `ModuleNotFoundError` before
anything could print. Fixed by inlining the data-dir path instead of importing it. Added a CI step
that runs `statusline.py` under `-I` with both an empty and a real payload, since this is exactly
the kind of failure a normal "does it import" check can't catch (it imports fine without `-I`).
If touching `statusline.py` again: it has to work standalone, under `-I`, with no sibling imports.

## Housekeeping

- Fixed three more stale "Manage plans" references left over from the earlier modal-to-Settings
  migration (two in `server.py` error/doc strings, one UI link removed from Overview).
- `server.py`'s `PORT` now reads `AGENTIC_SPEND_PORT` (default 8787) instead of needing a file
  edit to run a second instance for testing.
- 51 tests now (`python3 -m unittest discover -s tests`), all passing.

## Not done, and why

- The advisor-specced original build order (cache split → Projects → plan history → token
  destinations) got reordered once `ccusage blocks -n 1` turned out to solve the hourly-chart
  complaint almost for free — that became priority one instead.
- Token-source classification only recognises the querySource values actually observed on this
  machine (`repl_main_thread`, `sdk`, various `agent:*`); anything else lands in "other" rather
  than a guessed bucket. If a future Claude Code version adds a new querySource value, it'll show
  up there rather than being silently miscategorized — check `classify_claude_query_source()` in
  `token_sources.py` if "other" ever shows a meaningful total.
- No screenshots of the new Projects tab or Where-tokens-went card were added to the README —
  the existing four still roughly represent the app; add new ones (through the scrub-proxy
  technique used for the original four, since Projects shows real local paths) if that gap starts
  to matter.
