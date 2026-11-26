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

import ccusage_bin
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
        # accept both the old {uuid: "label"} shape and {uuid: {label, note}}.
        # Keys starting with "_" are comments (see account-labels.json.example)
        # and are not accounts.
        return {
            k: (v if isinstance(v, dict) else {"label": v, "note": None})
            for k, v in raw.items()
            if not str(k).startswith("_")
        }
    return {}


def save_labels(labels):
    data_dir.atomic_write_json(LABELS_PATH, labels)


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
    data_dir.atomic_write_json(PLANS_PATH, raw)
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



# --- plan-window readings ---------------------------------------------------
#
# A "reading" is one observation of a single rate-limit window (the 5-hour
# session or the 7-day week): percent, when it resets, and when we saw it.
# Live status-line samples, older history samples, and Claude Code's own
# cachedUsageUtilization are all readings. resolve_window picks the one to
# show and applies two rules:
#   - if the window's resetsAt has passed and no later reading exists, the
#     figure is 0% — usage can only rise again through Claude Code activity,
#     which would have written a fresh live reading.
#   - if the reading is older but the window has not reset, show the value
#     with its age, and "no use since" when local session logs confirm there
#     has been no later activity for that account.

LIVE_FRESH_AFTER = datetime.timedelta(hours=24)
# A status-line write and the transcript update for the same reply can be a
# few seconds apart; treat activity inside this window as the same event.
ACTIVITY_GRACE = datetime.timedelta(minutes=5)


def parse_when(value, tz=None):
    """ISO timestamp, Unix epoch seconds, or a datetime — as an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.datetime.fromtimestamp(float(text), tz=datetime.timezone.utc)
        except ValueError:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(text)
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if tz is not None:
        return dt.astimezone(tz)
    return dt


def _local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def fmt_reset_short(value, tz=None):
    """'Fri 15:00' in the given (or local) timezone."""
    dt = parse_when(value, tz or _local_tz())
    if not dt:
        return None
    return f"{dt.strftime('%a')} {dt.strftime('%H:%M')}"


def fmt_as_of(value, now=None, tz=None):
    """Clock time if the reading is today locally, otherwise '25 Nov, 09:36'."""
    tz = tz or _local_tz()
    dt = parse_when(value, tz)
    if not dt:
        return None
    now = parse_when(now, tz) or datetime.datetime.now(tz)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return f"{dt.day} {dt.strftime('%b')}, {dt.strftime('%H:%M')}"


def live_age_label(fetched_at, now=None, tz=None):
    """How the live badge should read.

    Within 24 hours the clock time is the most useful (a session window is
    only five hours). After that, the age in days is clearer than a clock
    time from yesterday or last week: 'live, 1 day old', 'live, 2 days old'.
    """
    tz = tz or _local_tz()
    dt = parse_when(fetched_at, tz)
    if not dt:
        return "live"
    now = parse_when(now, tz) or datetime.datetime.now(tz)
    age = now - dt
    if age < LIVE_FRESH_AFTER:
        when = fmt_as_of(dt, now, tz)
        return f"live {when}" if when else "live"
    days = int(age.total_seconds() // 86400)
    if days < 1:
        days = 1
    noun = "day" if days == 1 else "days"
    return f"live, {days} {noun} old"


def _newest_key(reading):
    at = parse_when(reading.get("at"))
    # missing timestamps sort last
    return at or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def resolve_window(readings, now=None, last_activity=None, tz=None):
    """Pick the figure to show for one rate-limit window.

    readings: iterable of dicts with percent, resetsAt, at, and optional
    source ('live', 'history', or 'cached'). Entries with no percent and no
    resetsAt are ignored. last_activity is the latest local transcript
    activity for this account, or None if we could not tell.
    """
    tz = tz or _local_tz()
    now = parse_when(now, tz) or datetime.datetime.now(tz)
    last_activity = parse_when(last_activity, tz)
    candidates = []
    for raw in readings or []:
        if not raw:
            continue
        percent = raw.get("percent")
        resets = parse_when(raw.get("resetsAt") or raw.get("resets_at"))
        if percent is None and resets is None:
            continue
        candidates.append({
            "percent": percent,
            "resetsAt": resets,
            "at": parse_when(raw.get("at")),
            "source": raw.get("source") or "cached",
        })

    def pack(percent, resets, as_of, source, state, note):
        live = source in ("live", "history")
        return {
            "percent": percent,
            "resetsAt": resets.isoformat() if isinstance(resets, datetime.datetime) else resets,
            "asOf": as_of.isoformat() if isinstance(as_of, datetime.datetime) else as_of,
            "source": source,
            "state": state,
            "note": note,
            "live": live,
        }

    with_pct = [c for c in candidates if c["percent"] is not None]
    if with_pct:
        source_rank = {"live": 2, "history": 1, "cached": 0}
        chosen = max(with_pct, key=lambda c: (_newest_key(c), source_rank.get(c["source"], 0)))
        resets, as_of, source = chosen["resetsAt"], chosen["at"], chosen["source"]
        if resets is not None and resets <= now and not (as_of and as_of >= resets):
            # Reading belongs to a window that has already reset, and nothing
            # newer exists. Usage is 0 until Claude Code next writes a reading.
            reset_label = fmt_reset_short(resets, tz) or "the window"
            return pack(0, resets, as_of, source, "reset",
                        f"reset {reset_label}, no use since")
        as_of_label = fmt_as_of(as_of, now, tz) or "unknown"
        quiet = False
        if last_activity is not None and as_of is not None:
            quiet = last_activity <= as_of + ACTIVITY_GRACE
        elif last_activity is not None and as_of is None:
            quiet = False
        if quiet:
            note = f"as of {as_of_label}, no use since"
        else:
            note = f"last reading {as_of_label}"
        return pack(chosen["percent"], resets, as_of, source, "current", note)

    with_reset = [c for c in candidates if c["resetsAt"] is not None]
    if with_reset:
        chosen = max(with_reset, key=_newest_key)
        resets = chosen["resetsAt"]
        if resets <= now:
            reset_label = fmt_reset_short(resets, tz) or "the window"
            return pack(0, resets, chosen["at"], chosen["source"], "reset",
                        f"reset {reset_label}, no use since")
    return pack(None, None, None, None, "unknown", None)


def last_history_reading(samples, percent_key, reset_key):
    """The most recent history sample that actually has a figure for this window."""
    for rec in reversed(list(samples or [])):
        if rec.get(percent_key) is None:
            continue
        return {
            "percent": rec.get(percent_key),
            "resetsAt": rec.get(reset_key),
            "at": rec.get("time"),
            "source": "history",
        }
    return None


def claude_last_activity(session_account_map=None):
    """Latest transcript mtime per accountUuid, from Claude Code session logs.

    A later mtime than the reading means we cannot claim there was no use;
    an earlier (or equal, within ACTIVITY_GRACE) mtime means the logs show
    no later activity. Accounts we cannot attribute are omitted, so the
    caller falls back to 'last reading' rather than a false 'no use since'.
    """
    if session_account_map is None:
        session_account_map, _ = build_session_account_map()
    latest = {}
    pattern = os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")
    for path in glob.glob(pattern):
        session_id = os.path.splitext(os.path.basename(path))[0]
        uuid = session_account_map.get(session_id)
        if not uuid or uuid == "unknown-session":
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
        if uuid not in latest or dt > latest[uuid]:
            latest[uuid] = dt
    return latest


def _epoch_to_iso(secs):
    dt = parse_when(secs)
    return dt.isoformat() if dt else None


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

        live[uuid] = {
            "accountUuid": uuid,
            "email": raw.get("email"),
            "fetchedAt": raw.get("writtenAt"),
            "live": True,
            "session": {"percent": raw.get("fiveHourPercent"), "resetsAt": _epoch_to_iso(raw.get("fiveHourResetsAt"))},
            "week": {"percent": raw.get("sevenDayPercent"), "resetsAt": _epoch_to_iso(raw.get("sevenDayResetsAt"))},
        }
    return live


def _as_reading(window, at, source):
    if not window:
        return None
    if window.get("percent") is None and not window.get("resetsAt"):
        return None
    return {
        "percent": window.get("percent"),
        "resetsAt": window.get("resetsAt"),
        "at": at,
        "source": source,
    }


def get_max_usage_snapshots(now=None, last_activity=None, history=None):
    """Last-known plan-limit usage for EVERY account Claude Code has ever been
    logged into on this machine, not just whichever one is active right now.

    Sources, merged per window (session and week independently):
    - Live: ~/.claude/cc-usage-live/<uuid>.json, written by the status line
      script on every reply — real server-reported rate_limits, fresh as of
      the user's last message in that account's session.
    - History: the most recent status-line sample that actually carried a
      figure for that window. Used when the live file omitted the window
      (Claude Code drops a window from the payload once it has reset, and
      can also omit five_hour on an individual event even mid-window).
    - Cached fallback: ~/.claude.json's cachedUsageUtilization, which only
      ever holds the CURRENTLY logged-in account's data and only refreshes on
      Claude Code's own schedule (can lag hours). Snapshotted into a local
      file keyed by accountUuid on every request so a switched-away account's
      last-known numbers don't disappear the moment you log into another one.

    A window whose resetsAt has passed, with no later reading, is shown as
    0% — not the stale percentage. Usage credits only come from the cached
    source (the status line doesn't carry them), so credits are always
    merged on top of, not instead of, the cached snapshot.
    """
    now = parse_when(now) or datetime.datetime.now(datetime.timezone.utc)
    current = _capture_current_max_usage()
    snapshots = _load_usage_snapshots()
    if current and current.get("accountUuid"):
        snapshots[current["accountUuid"]] = current
        _save_usage_snapshots(snapshots)

    live_by_uuid = _load_live_usage()
    if history is None:
        history = get_usage_history()
    hist_by = {}
    for rec in history or []:
        hist_by.setdefault(rec.get("accountUuid"), []).append(rec)
    if last_activity is None:
        last_activity = claude_last_activity()

    labels = seed_labels_from_current_account(load_labels())
    result = []

    def merge(uuid, snap, live):
        label = labels.get(uuid, {}).get("label") or (snap or live or {}).get("email") or f"Unlabeled ({uuid[:8]}…)"
        samples = hist_by.get(uuid) or []
        activity = (last_activity or {}).get(uuid)
        session = resolve_window([
            _as_reading((live or {}).get("session"), (live or {}).get("fetchedAt"), "live"),
            last_history_reading(samples, "fiveHourPercent", "fiveHourResetsAt"),
            _as_reading((snap or {}).get("session"), (snap or {}).get("fetchedAt"), "cached"),
        ], now=now, last_activity=activity)
        week = resolve_window([
            _as_reading((live or {}).get("week"), (live or {}).get("fetchedAt"), "live"),
            last_history_reading(samples, "sevenDayPercent", "sevenDayResetsAt"),
            _as_reading((snap or {}).get("week"), (snap or {}).get("fetchedAt"), "cached"),
        ], now=now, last_activity=activity)
        used_live = (session.get("source") in ("live", "history")) or (week.get("source") in ("live", "history")) or bool(live)
        live_at = (live or {}).get("fetchedAt") or session.get("asOf") or week.get("asOf")
        merged = dict(snap or {})
        merged.update({
            "accountUuid": uuid,
            "email": (snap or {}).get("email") or (live or {}).get("email"),
            "label": label,
            "session": session,
            "week": week,
            "live": used_live,
            "fetchedAt": live_at if used_live else (snap or {}).get("fetchedAt"),
            "liveLabel": live_age_label(live_at, now=now) if used_live else None,
            "usageCredits": (snap or {}).get("usageCredits") or (live or {}).get("usageCredits") or {},
        })
        if "weekBreakdown" not in merged:
            merged["weekBreakdown"] = (snap or {}).get("weekBreakdown") or []
        return merged

    seen = set()
    for uuid, snap in snapshots.items():
        seen.add(uuid)
        result.append(merge(uuid, snap, live_by_uuid.pop(uuid, None)))
    for uuid, live in live_by_uuid.items():
        if uuid in seen:
            continue
        result.append(merge(uuid, {}, live))

    result.sort(key=lambda s: s.get("fetchedAt") or "", reverse=True)
    return result


def get_usage_history():
    """Plan-usage samples statusline.py has been appending to
    usage-history.jsonl (data_dir.path) since this feature shipped — there's
    no retroactive data before that, only what's been recorded since. One
    flat, time-sorted list; the dashboard groups it by account itself, same
    pattern as account_sessions, rather than this function pre-grouping it."""
    path = data_dir.path("usage-history.jsonl")
    labels = seed_labels_from_current_account(load_labels())
    samples = []
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                uuid = rec.get("accountUuid")
                meta = labels.get(uuid) or {}
                rec["label"] = meta.get("label") or (f"Unlabeled ({uuid[:8]}…)" if uuid else "unknown")
                samples.append(rec)
    except OSError:
        pass
    samples.sort(key=lambda s: s.get("time") or "")
    return samples


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
    """Scan Claude Code transcripts for session-owner records and each
    session's working directory -> ({sessionId: accountUuid}, {sessionId: cwd}).
    Both show up in the same early lines of the same files, so one pass
    covers both instead of opening every transcript twice."""
    accounts = {}
    projects = {}
    pattern = os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")
    for path in glob.glob(pattern):
        session_id = os.path.splitext(os.path.basename(path))[0]
        have_account = session_id in accounts
        have_project = session_id in projects
        if have_account and have_project:
            continue
        try:
            with open(path, "r", errors="ignore") as f:
                for i, line in enumerate(f):
                    if i > 50 or (have_account and have_project):  # both show up near the top
                        break
                    line = line.strip()
                    if not line or ('"session-owner"' not in line and '"cwd"' not in line):
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not have_account and rec.get("type") == "session-owner":
                        accounts[session_id] = rec.get("accountUuid") or "unknown"
                        have_account = True
                    if not have_project and rec.get("cwd"):
                        projects[session_id] = rec["cwd"]
                        have_project = True
        except OSError:
            continue
    return accounts, projects


def run_ccusage_sessions(since=None):
    args = ["session", "--json", "--by-agent"]
    if since:
        args += ["--since", since]
    cmd = ccusage_bin.command(*args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)["session"]


def compute_accounts(session_rows, session_account_map=None, labels=None, session_project_map=None):
    """Pure aggregation: session rows (from `ccusage session --json`) -> per-account totals.

    Does not call ccusage or re-scan disk itself — pass in session_account_map
    and session_project_map (both from build_session_account_map()) and
    labels (from load_labels()) so callers that already have them (e.g. the
    dashboard server) don't repeat that work on every request.
    """
    if session_account_map is None:
        session_account_map, session_project_map = build_session_account_map()
    if session_project_map is None:
        session_project_map = {}
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
            "project": session_project_map.get(session_id) or "Unattributed (no cwd recorded)",
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

    session_account, _ = build_session_account_map()
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
    print(f"Labels file: {LABELS_PATH}. Edit it, or name an account in the dashboard. Key = accountUuid.")


if __name__ == "__main__":
    main()
