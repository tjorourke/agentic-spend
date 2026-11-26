#!/usr/bin/env python3
"""Every data source ccusage 20.1.2 reads, with ccusage's own support tier.

The list and order come from ccusage's source registry
(rust/crates/ccusage-adapter-all/src/sources.rs at tag v20.1.2). The tier is
the tag in the title of each source's guide page in ccusage's docs
(docs/guide/<source>/index.md): "(Beta)" -> beta, "(Experimental)" ->
experimental, no tag -> first-class. We copy those labels; we do not judge
the sources ourselves.

ccusage's unified report already reads every one of these sources when its
logs exist, so "turning a source on" here means recognising it: giving it a
name, a tier badge and a row. A source with no data in ccusage's output and
nothing detected locally is not shown at all.
"""

CCUSAGE_DOCS = "https://ccusage.com/guide/{id}/"

FIRST_CLASS = "first-class"
BETA = "beta"
EXPERIMENTAL = "experimental"

# (ccusage agent id, label from the guide title, tier from the guide title)
CATALOG = (
    ("claude", "Claude Code", FIRST_CLASS),
    ("codex", "Codex", BETA),
    ("opencode", "OpenCode", BETA),
    ("amp", "Amp", BETA),
    ("droid", "Droid", EXPERIMENTAL),
    ("codebuff", "Codebuff", EXPERIMENTAL),
    ("hermes", "Hermes Agent", EXPERIMENTAL),
    ("pi", "pi-agent", BETA),
    ("goose", "Goose", EXPERIMENTAL),
    ("openclaw", "OpenClaw", EXPERIMENTAL),
    ("kilo", "Kilo", BETA),
    ("copilot", "GitHub Copilot CLI", BETA),
    ("gemini", "Gemini CLI", BETA),
    ("antigravity", "Antigravity", FIRST_CLASS),
    ("kimi", "Kimi", EXPERIMENTAL),
    ("qwen", "Qwen", EXPERIMENTAL),
    ("grok", "Grok Build CLI", FIRST_CLASS),
    ("zcode", "ZCode", FIRST_CLASS),
    ("cline", "Cline", EXPERIMENTAL),
    ("dsh", "DeepSeek Harness", EXPERIMENTAL),
)

_BY_ID = {cid: {"id": cid, "label": label, "tier": tier, "docs": CCUSAGE_DOCS.format(id=cid)}
          for cid, label, tier in CATALOG}

TIER_NOTE = {
    FIRST_CLASS: "ccusage lists this source with no Beta or Experimental tag.",
    BETA: "ccusage tags this source Beta: supported, but the tool's log format may still change.",
    EXPERIMENTAL: "ccusage tags this source Experimental: its maintainers expect breaking changes "
                  "while the tool's local data format evolves.",
}


def info(agent):
    """Catalog entry for a ccusage agent id (also accepts 'grok-cli'), or None."""
    if agent == "grok-cli":
        agent = "grok"
    entry = _BY_ID.get(agent)
    return dict(entry) if entry else None


def tier(agent):
    entry = info(agent)
    return entry["tier"] if entry else None


def agents_with_data(daily_rows=None, session_rows=None):
    """ccusage agent ids that have any usage in ccusage's own output."""
    seen = set()
    for row in daily_rows or []:
        for sub in row.get("agents") or []:
            if isinstance(sub, dict) and sub.get("agent"):
                seen.add(sub["agent"])
            elif isinstance(sub, str):
                seen.add(sub)
    for row in session_rows or []:
        if row.get("agent"):
            seen.add(row["agent"])
    return seen


def present(daily_rows=None, session_rows=None, detection=None):
    """One entry per ccusage source that is actually here: it has data in
    ccusage's output, or Agentic Spend's own detection found it installed.
    Sources with neither are left out entirely (no 'not found' noise)."""
    with_data = agents_with_data(daily_rows, session_rows)
    detected = set()
    for d in detection or []:
        if d.get("installed") or d.get("hasData"):
            detected.add("grok" if d.get("id") == "grok-cli" else d.get("id"))
    out = []
    for cid, label, tier_name in CATALOG:
        has_data = cid in with_data
        if not has_data and cid not in detected:
            continue
        out.append({"id": cid, "label": label, "tier": tier_name, "hasData": has_data,
                    "docs": CCUSAGE_DOCS.format(id=cid), "tierNote": TIER_NOTE[tier_name]})
    return out
