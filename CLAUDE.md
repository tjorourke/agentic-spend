# Agentic Spend — working conventions

This is a personal tool its author has floated maybe open-sourcing eventually, so:

- **Never hardcode personal data in source** (`.py`/`.html`/`.js`) — no email addresses,
  account UUIDs, org names, or bucketing assumptions like "everything not account X belongs to
  organization Y". Real identity (email, account) must always be *detected* at runtime from
  local auth/config files
  (`~/.claude.json`, `~/.grok/auth.json`, etc.), never typed into code. Personal data only
  belongs in the gitignored local data files (`account-labels.json`, `usage-snapshots.json`,
  `plans.json`, `provider-keys.json`) — see README.md for what each holds.
- When adding a new per-tool/per-provider concept (a plan note, a real-cost source, an
  account label), prefer a small gitignored JSON file over a code constant, with a
  `.example` version committed to show the shape to future (possibly public) readers.
- Every real-cost claim shown in the UI should make clear *where the number came from* —
  auto-detected (name the source file/field), or manually entered (say so, with a date it was
  checked). Never show a number that implies live/current when it's actually a cache or a
  guess — see the "cached \<time\>" staleness pattern in `dashboard.html` for the precedent.
- No build step. `dashboard.html` stays a single vanilla-JS file; `server.py` stays stdlib-only
  Python (no pip installs). Keep it that way — it's a big part of why this is trivial to run.
