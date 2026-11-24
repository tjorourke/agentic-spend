#!/usr/bin/env python3
"""Attribute Claude Code spend to the Claude account that incurred it.

ccusage computes cost per session but has no concept of "account" — it only
reads token/cost fields. Claude Code's own transcripts do carry account
identity though: every session JSONL under ~/.claude/projects/**/*.jsonl
writes a `{"type": "session-owner", "accountUuid": ..., "organizationUuid": ...}`
record near the top. This module builds sessionId -> accountUuid from those
transcripts, then joins it against `ccusage session` rows (which already
compute cost correctly per session) to produce a per-account breakdown.

Only Claude Code sessions carry this; other agents (codex/grok/opencode)
have no equivalent field in their logs and are reported separately as
"no account data". Sessions older than Claude Code's session-owner rollout,
or whose transcript has been pruned, fall into "unknown-session".

Importable as a module (compute_accounts) by server.py, or run standalone:
    python3 account_usage.py                 # table, all time
    python3 account_usage.py --json           # machine-readable
    python3 account_usage.py --since 20261101
"""
import argparse
import datetime
import glob
import json
import os
import subprocess

import data_dir

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
LABELS_PATH = data_dir.path("account-labels.json")
USAGE_SNAPSHOTS_PATH = data_dir.path("usage-snapshots.json")
PLANS_PATH = data_dir.path("plans.json")
GROK_AUTH_PATH = os.path.expanduser("~/.grok/auth.json")

UNKNOWN_SESSION_LABEL = {
    "label": "Older sessions (no account record)",
    "note": "These session transcripts predate Claude Code's session-owner tracking, "
            "or have been pruned, so which account ran them can't be recovered.",
}


def detect_grok_email():
    """The locally logged-in grok CLI account's email, read straight out of
    ~/.grok/auth.json — same idea as Claude Code's own oauthAccount cache.
    No hardcoded "which email is personal" assumption: whatever account is
    logged in becomes its own bucket, named by its real email."""
    try:
        with open(GROK_AUTH_PATH) as f:
            entries = json.load(f)
        for entry in entries.values():
            email = entry.get("email")
            if email:
                return email
    except (OSError, ValueError):
        pass
    return None


def load_labels():
    if os.path.exists(LABELS_PATH):
        with open(LABELS_PATH) as f:
            raw = json.load(f)
        # accept both the old {uuid: "label"} shape and {uuid: {label, note}}
        return {k: (v if isinstance(v, dict) else {"label": v, "note": None}) for k, v in raw.items()}
    return {}


def save_labels(labels):
    with open(LABELS_PATH, "w") as f:
        json.dump(labels, f, indent=2, sort_keys=True)


def load_plans():
    """Manually-declared subscription-plan info for tools that have no local
    cache of real plan-limit usage the way Claude Code does (see
    get_max_usage_snapshots). Keyed by ccusage's `agent` name (e.g. "grok",
    "codex", "opencode") — one entry per tool, not per account, since these
    CLIs only ever have one locally logged-in account at a time anyway.

    Entirely optional: a tool with no entry here just shows up in the "real
    cost" view as "assumed real spend — verify this tool's own billing"
    instead of a plan-covered banner. Re-check each tool's own usage page
    occasionally and bump `asOf` — there's no API this can read automatically.

    Shape per entry: {"plan": str, "coveredByPlan": bool, "note": str, "asOf": "YYYY-MM-DD"}
    A top-level "_readme" key, if present, is informational only and ignored.
    """
    if os.path.exists(PLANS_PATH):
        with open(PLANS_PATH) as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    return {}


def save_plan(agent, entry):
    """Add or update one tool's plan entry (see load_plans), preserving every
    other entry already in the file, including any "_readme" key."""
    raw = {}
    if os.path.exists(PLANS_PATH):
        with open(PLANS_PATH) as f:
            raw = json.load(f)
    raw[agent] = entry
    with open(PLANS_PATH, "w") as f:
        json.dump(raw, f, indent=2, sort_keys=True)
    return raw


def delete_plan(agent):
    if not os.path.exists(PLANS_PATH):
        return {}
    with open(PLANS_PATH) as f:
        raw = json.load(f)
    raw.pop(agent, None)
    with open(PLANS_PATH, "w") as f:
        json.dump(raw, f, indent=2, sort_keys=True)
    return raw


def seed_labels_from_current_account(labels):
    """Auto-label whichever account is currently logged in, from ~/.claude.json."""
    try:
        with open(CLAUDE_CONFIG) as f:
            cfg = json.load(f)
        oauth = cfg.get("oauthAccount") or {}
        uuid = oauth.get("accountUuid")
        email = oauth.get("emailAddress")
        org_type = oauth.get("organizationType")
        if uuid and uuid not in labels and email:
            note = None
            if org_type and "max" in org_type.lower():
                note = (f"{org_type} flat-rate plan — this dollar figure is the API "
                         "list-price equivalent of tokens used, not money actually billed.")
            labels[uuid] = {"label": email, "note": note}
    except Exception:
        pass
    return labels


def seed_labels_from_snapshots(labels):
    """Auto-label every account whose plan-limit snapshot we've ever captured
    (see get_max_usage_snapshots), not just the one currently logged in — so a
    switched-away account still shows its real email instead of a bare UUID."""
    for uuid, snap in _load_usage_snapshots().items():
        email = snap.get("email")
        if uuid and email and uuid not in labels:
            labels[uuid] = {"label": email, "note": None}
    return labels


def _load_usage_snapshots():
    if os.path.exists(USAGE_SNAPSHOTS_PATH):
        with open(USAGE_SNAPSHOTS_PATH) as f:
            return json.load(f)
    return {}


def _save_usage_snapshots(snapshots):
    with open(USAGE_SNAPSHOTS_PATH, "w") as f:
        json.dump(snapshots, f, indent=2, sort_keys=True)


LIVE_USAGE_DIR = os.path.expanduser("~/.claude/cc-usage-live")


def _load_live_usage():
    """Per-account live plan usage, written by ~/.claude/cc_usage_statusline.py
    on every status line update (see that script + ~/.claude/settings.json's
    "statusLine"). Fresher than cachedUsageUtilization, which Claude Code only
    refreshes on its own schedule — this updates on every reply instead.
    Returns {accountUuid: {session:{...}, week:{...}, fetchedAt}}, same shape
    as a cachedUsageUtilization-derived snapshot, so callers don't care which
    source it came from."""
    live = {}
    if not os.path.isdir(LIVE_USAGE_DIR):
        return live
    for name in os.listdir(LIVE_USAGE_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(LIVE_USAGE_DIR, name)) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        uuid = raw.get("accountUuid")
        if not uuid:
            continue

        def _epoch_to_iso(secs):
            if secs is None:
                return None
            return datetime.datetime.fromtimestamp(secs, tz=datetime.timezone.utc).isoformat()

        live[uuid] = {
            "accountUuid": uuid,
            "email": raw.get("email"),
            "fetchedAt": raw.get("writtenAt"),
            "live": True,
            "session": {"percent": raw.get("fiveHourPercent"), "resetsAt": _epoch_to_iso(raw.get("fiveHourResetsAt"))},
            "week": {"percent": raw.get("sevenDayPercent"), "resetsAt": _epoch_to_iso(raw.get("sevenDayResetsAt"))},
        }
    return live


def get_max_usage_snapshots():
    """Last-known plan-limit usage for EVERY account Claude Code has ever been
    logged into on this machine, not just whichever one is active right now.

    Two sources, live preferred over cached per account:
    - Live: ~/.claude/cc-usage-live/<uuid>.json, written by the status line
      script on every reply — real server-reported rate_limits, fresh as of
      the user's last message in that account's session.
    - Cached fallback: ~/.claude.json's cachedUsageUtilization, which only
      ever holds the CURRENTLY logged-in account's data and only refreshes on
      Claude Code's own schedule (can lag hours). Snapshotted into a local
      file keyed by accountUuid on every request so a switched-away account's
      last-known numbers don't disappear the moment you log into another one.

    Usage credits (the $ figure, not session/week %) only come from the
    cached source — the status line doesn't carry that — so a live snapshot
    is merged on top of (not instead of) the cached one where both exist.
    """
    current = _capture_current_max_usage()
    snapshots = _load_usage_snapshots()
    if current and current.get("accountUuid"):
        snapshots[current["accountUuid"]] = current
        _save_usage_snapshots(snapshots)

    live_by_uuid = _load_live_usage()

    labels = seed_labels_from_current_account(load_labels())
    result = []
    for uuid, snap in snapshots.items():
        label = labels.get(uuid, {}).get("label") or snap.get("email") or f"Unlabeled ({uuid[:8]}…)"
        merged = {**snap, "label": label}
        live = live_by_uuid.pop(uuid, None)
        if live and live.get("session", {}).get("percent") is not None:
            merged["session"] = live["session"]
            merged["week"] = live["week"]
            merged["fetchedAt"] = live["fetchedAt"]
            merged["live"] = True
        result.append(merged)

    # any account with ONLY a live snapshot (no cachedUsageUtilization ever
    # captured for it — e.g. usage credits never checked) still gets a row
    for uuid, live in live_by_uuid.items():
        label = labels.get(uuid, {}).get("label") or live.get("email") or f"Unlabeled ({uuid[:8]}…)"
        result.append({**live, "label": label, "usageCredits": {}})

    result.sort(key=lambda s: s.get("fetchedAt") or "", reverse=True)
    return result


def _capture_current_max_usage():
    """Read the real plan-limit usage Claude Code already caches locally, for
    whichever account is currently logged in.

    ccusage's dollar figures are a locally-computed estimate (tokens x list
    price) — there's no such thing as a billed dollar amount for a flat-rate
    Claude Pro/Max or Teams subscription, and Anthropic's Console usage
    dashboard only covers API-key/org billing, not subscription usage. The
    actual plan-limit consumption (current session %, weekly %, purchased
    usage credits) is what claude.ai's own Settings -> Usage page shows, and
    Claude Code already caches that exact payload in ~/.claude.json under
    cachedUsageUtilization — written whenever the client last checked in.
    """
    try:
        with open(CLAUDE_CONFIG) as f:
            cfg = json.load(f)
    except Exception:
        return None

    cached = cfg.get("cachedUsageUtilization")
    oauth = cfg.get("oauthAccount") or {}
    if not cached:
        return None

    util = cached.get("utilization", {})
    limits = {l.get("kind"): l for l in util.get("limits", [])}
    session = limits.get("session") or {}
    weekly = limits.get("weekly_all") or {}
    extra = util.get("extra_usage") or {}
    spend = util.get("spend") or {}
    breakdown = ((util.get("seven_day_breakdown") or {}).get("rows")) or []

    fetched_at_ms = cached.get("fetchedAtMs")
    fetched_at = None
    if fetched_at_ms:
        fetched_at = datetime.datetime.fromtimestamp(fetched_at_ms / 1000, tz=datetime.timezone.utc).isoformat()

    spend_limit = (spend.get("limit") or {}).get("amount_minor")
    spend_used = (spend.get("used") or {}).get("amount_minor")
    currency = (spend.get("limit") or {}).get("currency") or extra.get("currency")
    exponent = (spend.get("limit") or {}).get("exponent", 2)

    return {
        "accountUuid": cached.get("accountUuid") or oauth.get("accountUuid"),
        "email": oauth.get("emailAddress"),
        "fetchedAt": fetched_at,
        "session": {"percent": session.get("percent"), "resetsAt": session.get("resets_at")},
        "week": {"percent": weekly.get("percent"), "resetsAt": weekly.get("resets_at")},
        "usageCredits": {
            "currency": currency,
            "exponent": exponent,
            "limitMinor": spend_limit,
            "usedMinor": spend_used,
            "remainingMinor": (spend_limit - spend_used) if spend_limit is not None and spend_used is not None else None,
            "enabled": spend.get("enabled"),
        },
        "weekBreakdown": [
            {"key": r.get("key"), "label": r.get("display_name"), "percentOfLimit": r.get("percent_of_limit")}
            for r in breakdown
        ],
    }


def build_session_account_map():
    """Scan Claude Code transcripts for session-owner records -> {sessionId: accountUuid}."""
    mapping = {}
    pattern = os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")
    for path in glob.glob(pattern):
        session_id = os.path.splitext(os.path.basename(path))[0]
        if session_id in mapping:
            continue
        try:
            with open(path, "r", errors="ignore") as f:
                for i, line in enumerate(f):
                    if i > 50:  # session-owner is always near the top
                        break
                    line = line.strip()
                    if not line or '"session-owner"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") == "session-owner":
                        mapping[session_id] = rec.get("accountUuid") or "unknown"
                        break
        except OSError:
            continue
    return mapping


def run_ccusage_sessions(since=None, version="20.1.1"):
    cmd = ["npx", "--yes", f"ccusage@{version}", "session", "--json", "--by-agent"]
    if since:
        cmd += ["--since", since]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)["session"]


def compute_accounts(session_rows, session_account_map=None, labels=None):
    """Pure aggregation: session rows (from `ccusage session --json`) -> per-account totals.

    Does not call ccusage or re-scan disk itself — pass in session_account_map
    (from build_session_account_map()) and labels (from load_labels()) so
    callers that already have them (e.g. the dashboard server) don't repeat
    that work on every request.
    """
    if session_account_map is None:
        session_account_map = build_session_account_map()
    if labels is None:
        labels = seed_labels_from_current_account(load_labels())

    accounts = {}
    no_account_data = {"cost": 0.0, "sessions": 0, "agents": {}}
    daily_by_bucket = {}  # date -> {bucket_label: cost}, bucket_label = real email or "<agent> (no account data)"
    account_sessions = []  # flat per-session rows, so the UI can re-aggregate per-account over any date range

    grok_email = detect_grok_email()

    def add_daily(date, bucket_label, cost):
        if not date:
            return
        daily_by_bucket.setdefault(date, {})
        daily_by_bucket[date][bucket_label] = daily_by_bucket[date].get(bucket_label, 0.0) + cost

    for row in session_rows:
        last_activity = (row.get("metadata") or {}).get("lastActivity", "")[:10]
        agent = row.get("agent", "unknown")

        if agent != "claude":
            cost = row.get("totalCost", 0.0)
            no_account_data["cost"] += cost
            no_account_data["sessions"] += 1
            no_account_data["agents"][agent] = no_account_data["agents"].get(agent, 0.0) + cost
            if agent == "grok" and grok_email:
                bucket_label = grok_email
            else:
                bucket_label = f"{agent} (no account data)"
            add_daily(last_activity, bucket_label, cost)
            continue

        session_id = row.get("period")
        acct = session_account_map.get(session_id, "unknown-session")
        if acct == "unknown-session":
            meta = UNKNOWN_SESSION_LABEL
        else:
            meta = labels.get(acct, {"label": f"Unlabeled account ({acct[:8]}…)", "note": None})
        bucket_label = meta["label"]  # real email whenever we've ever seen this account log in locally
        add_daily(last_activity, bucket_label, row.get("totalCost", 0.0))

        account_sessions.append({
            "period": last_activity,
            "accountUuid": acct,
            "label": bucket_label,
            "note": meta.get("note"),
            "totalCost": row.get("totalCost", 0.0),
            "totalTokens": row.get("totalTokens", 0),
            "modelBreakdowns": row.get("modelBreakdowns", []),
        })

        bucket = accounts.setdefault(acct, {
            "cost": 0.0, "sessions": 0, "tokens": 0,
            "first": None, "last": None, "models": {},
        })
        bucket["cost"] += row.get("totalCost", 0.0)
        bucket["sessions"] += 1
        bucket["tokens"] += row.get("totalTokens", 0)
        if last_activity:
            if bucket["first"] is None or last_activity < bucket["first"]:
                bucket["first"] = last_activity
            if bucket["last"] is None or last_activity > bucket["last"]:
                bucket["last"] = last_activity
        for mb in row.get("modelBreakdowns", []):
            bucket["models"][mb["modelName"]] = bucket["models"].get(mb["modelName"], 0.0) + mb["cost"]

    account_list = []
    for acct, bucket in accounts.items():
        if acct == "unknown-session":
            meta = UNKNOWN_SESSION_LABEL
        else:
            meta = labels.get(acct, {"label": f"Unlabeled account ({acct[:8]}…)", "note": None})
        account_list.append({"accountUuid": acct, "label": meta["label"], "note": meta.get("note"), **bucket})
    account_list.sort(key=lambda a: -a["cost"])

    daily_by_bucket_list = [{"period": d, **v} for d, v in sorted(daily_by_bucket.items())]

    return account_list, no_account_data, daily_by_bucket_list, account_sessions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", help="YYYYMMDD or YYYY-MM-DD")
    args = ap.parse_args()

    labels = seed_labels_from_snapshots(seed_labels_from_current_account(load_labels()))
    save_labels(labels)

    session_account = build_session_account_map()
    session_rows = run_ccusage_sessions(since=args.since)
    account_list, no_account_data, daily_by_bucket, _account_sessions = compute_accounts(session_rows, session_account, labels)

    if args.json:
        print(json.dumps({
            "accounts": account_list, "noAccountData": no_account_data, "dailyByBucket": daily_by_bucket,
        }, indent=2))
        return

    bucket_totals = {}
    for d in daily_by_bucket:
        for k, v in d.items():
            if k == "period":
                continue
            bucket_totals[k] = bucket_totals.get(k, 0.0) + v
    print("  ".join(f"{k}: ${v:,.2f}" for k, v in sorted(bucket_totals.items(), key=lambda kv: -kv[1])))
    print()

    print(f"{'Account':38} {'Label':45} {'Sessions':>8} {'Cost':>10}  Date range")
    for a in account_list:
        date_range = f"{a['first']} → {a['last']}" if a["first"] else "-"
        print(f"{a['accountUuid']:38} {a['label']:45} {a['sessions']:>8} {a['cost']:>10.2f}  {date_range}")
        if a.get("note"):
            print(f"    note: {a['note']}")
        for m, c in sorted(a["models"].items(), key=lambda kv: -kv[1])[:4]:
            print(f"    - {m}: ${c:,.2f}")

    if no_account_data["sessions"]:
        print()
        print(f"Non-Claude agents (no account data available): ${no_account_data['cost']:.2f} "
              f"across {no_account_data['sessions']} sessions")
        for a, c in sorted(no_account_data["agents"].items(), key=lambda kv: -kv[1]):
            print(f"    - {a}: ${c:,.2f}")

    print()
    print(f"Labels file: {LABELS_PATH} — edit it to rename accounts (key = accountUuid).")


if __name__ == "__main__":
    main()
