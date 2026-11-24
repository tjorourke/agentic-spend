# Next session: Agentic Spend is public, hardened, and verified

Continue work on the Agentic Spend dashboard at `~/code/agentic-spend` (`agentic-spend` to
launch, read `README.md` first). The repo went public this session (`github.com/tjorourke/
agentic-spend`, history squashed to one clean commit first — the old private history had a
stray personal email in it). This pass addressed a 10-item self-review of the public-facing
state: what's done, what was explicitly deferred, and why.

## What changed this session

- **Node.js dependency is now declared and handled.** `server.py` shells out to `npx ccusage`,
  which needs Node — previously undeclared anywhere. Formula now has `depends_on "node"`; README
  has a Prerequisites section; and if `npx` is missing anyway (e.g. running from a git clone),
  `/api/usage` returns `{"error": ..., "errorType": "node_missing"}` and the dashboard shows a
  specific install-Node message with a Retry button instead of a blank page or raw stack trace.
- **A real test suite**: `tests/`, stdlib `unittest` (no pip installs, matches the project's
  no-build-step convention), 15 tests covering the pure data-transformation functions —
  `account_usage.compute_accounts`, `server.build_hourly`, `server.build_sources`,
  `provider_usage._parse_grok_billing`/`_cents`. Run with `python3 -m unittest discover -s tests`.
- **CI**: `.github/workflows/ci.yml` — import-checks every module and runs the test suite on
  push/PR to main. Basic on purpose (no lint, no coverage gate); verify it's actually green on
  GitHub after the next push, not just passing locally.
- **LICENSE**: Apache 2.0, matching the author's other OSS tool (flightbay). Added to the repo,
  the Formula's `license` field, and a README section.
- **Empty-state UX**: a fresh install with zero ccusage data (nobody's used Claude Code/Codex/
  Grok/OpenCode on the machine yet) now shows a plain-language banner explaining that, instead
  of just rendering blank/zero everything with no explanation. Screenshot-verified on Overview
  and Days with a mocked empty dataset — no crashes elsewhere either.
- **Stale docs fixed**: README's Live Claude/Grok usage sections still said "Manage plans modal"
  — that UI was replaced by the Settings page back in the nav redesign and the docs never caught
  up. Fixed both references.
- **Desktop-only, documented as a deliberate choice**, not a gap: it binds to `127.0.0.1` and
  reads machine-local logs, so a mobile layout was explicitly scoped out (not "missing").

## Reviewed, left alone on purpose

- **Account-billing ambiguity** ("is this account's spend genuinely metered by its org") is not
  code-fixable — there is no local signal that answers it, by design of what's available. The UI
  already says this plainly rather than guessing either way.
- **A handover note's proposed generic cross-tool identity-attribution system**
  (`~/Downloads/cc-usage-live-handover/`) was re-checked this session: opencode's local `account`
  table is still 0 rows on this machine (`sqlite3 ~/.local/share/opencode/opencode.db "SELECT
  COUNT(*) FROM account"`). Building against a table with no real data to verify the schema
  against would be speculative, unverifiable code — not built, deliberately, not forgotten.
- **Mobile responsiveness and accessibility** (`aria-*` attributes) were flagged in the same
  review and explicitly deprioritized — this is a single-user, same-machine tool, not public-
  facing software with an accessibility audience, and mobile isn't the intended use (see above).
- **Data export (CSV/JSON) and budget-threshold alerts** were flagged and explicitly skipped —
  no current need, would be speculative scope addition.

## Still open from the advisor's original nav-redesign spec (unchanged, low priority)

Merging Models+Days into one "Breakdown" page, a fully unified header "Source" filter across
every page type (vs. the current tool-dropdown-plus-accounts-optgroup workaround), and
generalizing the hourly chart beyond the literal `1d` preset. None of these are regressions —
they didn't exist before the redesign either — just the advisor's fuller vision, scoped down
for risk at the time.

## Housekeeping

No known bugs. Formula is at v1.0.0 with the public HTTPS-tarball install method (no SSH/tap
trust needed). If starting fresh work, check `git log`, confirm CI is still green, and glance at
Overview/Settings in both themes before assuming state — screenshots age fast.
