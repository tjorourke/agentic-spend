#!/usr/bin/env python3
"""Read Grok Bot weekly usage from the desktop app's local cache.

The macOS Grok Bot app stores a snapshot under
``~/Library/Application Support/Grok Bot/sand-client-persistence/``.
Filenames are RFC 4648 base32 encodings of slice keys. The file this
module reads is:

    sand.client.slice.account.<accountId>.weekly-usage.cache

It is a small JSON object the app writes after fetching usage. This
module only reads that cache and an optional history file in the
Agentic Spend data directory. It does not call any network API, and it
does not open cookies, tokens, or other secrets files.

Override the cache directory with GROK_BOT_PERSISTENCE_DIR (used by
tests, and by anyone whose app data is not in the default place).
"""
import datetime
import json
import os
import re
import sys
import threading
import urllib.parse

import data_dir

HISTORY_PATH = data_dir.path("grokbot-usage.json")
HISTORY_DAYS = 400
FULL_RES_DAYS = 14
DEDUPE_SECONDS = 30 * 60
DEFAULT_NOTE = (
    "Grok Bot reports usage as a share of a weekly limit shared with other apps, "
    "not tokens or dollars."
)

# Default macOS location. Tests and unusual installs set GROK_BOT_PERSISTENCE_DIR.
_DEFAULT_PERSISTENCE = os.path.join(
    os.path.expanduser("~/Library/Application Support"),
    "Grok Bot",
    "sand-client-persistence",
)
_store_lock = threading.Lock()

ACCOUNT_SLOT_KEY = "sand.client.slice.client-meta.account-slot"
_CACHE_KEY_RE = re.compile(r"^sand\.client\.slice\.account\.(.+)\.weekly-usage\.cache$")
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_.:|%+\-@]{1,200}$")


def persistence_dir():
    return os.environ.get("GROK_BOT_PERSISTENCE_DIR") or _DEFAULT_PERSISTENCE


def encode_slice_key(key):
    """RFC 4648 base32, lowercase, no padding — matches the app's filenames."""
    import base64
    return base64.b32encode(key.encode("utf-8")).decode("ascii").rstrip("=").lower()


def decode_slice_key(filename):
    import base64
    stem = filename[:-5] if filename.endswith(".blob") else filename
    pad = (-len(stem)) % 8
    try:
        raw = base64.b32decode(stem.upper() + ("=" * pad), casefold=True)
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _normalize_account_id(raw):
    if not raw or not isinstance(raw, str):
        return None
    return urllib.parse.unquote(raw.strip())


def _ms_to_iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return dt.isoformat(timespec="seconds")


def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def parse_cache_payload(obj):
    """Turn a weekly-usage.cache JSON object into a snapshot, or None."""
    if not isinstance(obj, dict):
        return None
    value = obj.get("value")
    if not isinstance(value, dict):
        return None
    reading = value.get("reading")
    if not isinstance(reading, dict):
        return None
    usage = reading.get("usage")
    if not isinstance(usage, dict):
        return None
    pct = usage.get("percentUsed")
    try:
        weekly_pct = float(pct)
    except (TypeError, ValueError):
        return None
    if weekly_pct < 0:
        return None
    credits = None
    cents = usage.get("prepaidBalanceCents")
    if isinstance(cents, (int, float)) and cents >= 0:
        credits = round(float(cents) / 100.0, 2)
    plan = usage.get("unifiedPlanName")
    if not isinstance(plan, str) or not plan.strip():
        plan = None
    else:
        plan = plan.strip()
    plan_label = usage.get("grokPlanLabel")
    if not isinstance(plan_label, str) or not plan_label.strip():
        plan_label = None
    else:
        plan_label = plan_label.strip()
    return {
        "kind": value.get("kind") if isinstance(value.get("kind"), str) else None,
        "plan": plan,
        "planLabel": plan_label,
        "weeklyPct": weekly_pct,
        "resetAt": _ms_to_iso(usage.get("nextResetMs")),
        "creditsUsd": credits,
        "updatedAt": _ms_to_iso(reading.get("readAtMs")) or _now_iso(),
        "expiresAt": _ms_to_iso(value.get("expiresAtMs")),
        "wouldRefuseSend": bool(usage.get("wouldRefuseSend")),
        "isSandTrial": bool(usage.get("isSandTrial")),
        "isTeamSeat": bool(usage.get("isTeamSeat")),
        "source": "local-cache",
        "note": DEFAULT_NOTE,
    }


def read_current_account_id(directory=None):
    directory = directory or persistence_dir()
    path = os.path.join(directory, encode_slice_key(ACCOUNT_SLOT_KEY) + ".blob")
    obj = _load_json(path)
    if not isinstance(obj, dict):
        return None
    value = obj.get("value")
    if isinstance(value, str) and value.strip():
        return _normalize_account_id(value)
    return None


def discover(directory=None):
    """Read every weekly-usage.cache slice in the persistence directory.

    Returns a list of snapshots keyed by accountId. Missing or unreadable
    directories yield an empty list, not an error — the caller shows the
    empty state.
    """
    directory = directory or persistence_dir()
    accounts = []
    try:
        names = os.listdir(directory)
    except OSError:
        return accounts
    current = read_current_account_id(directory)
    for name in names:
        if not name.endswith(".blob"):
            continue
        key = decode_slice_key(name)
        if not key:
            continue
        match = _CACHE_KEY_RE.match(key)
        if not match:
            continue
        account_id = _normalize_account_id(match.group(1))
        if not account_id:
            continue
        obj = _load_json(os.path.join(directory, name))
        snap = parse_cache_payload(obj) if obj is not None else None
        if not snap:
            continue
        snap["accountId"] = account_id
        snap["current"] = bool(current and account_id == current)
        accounts.append(snap)
    accounts.sort(key=lambda a: (not a.get("current"), a.get("accountId") or ""))
    return accounts


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_store(path=None):
    path = path or HISTORY_PATH
    obj = _load_json(path)
    if not isinstance(obj, dict):
        return {"accounts": {}}
    accounts = obj.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    return {"accounts": accounts}


def save_store(store, path=None):
    path = path or HISTORY_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _iso(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        parsed = _parse_iso(dt)
        if parsed is None:
            return None
        dt = parsed
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def append_history(history, weekly_pct, at, now=None, reset_at=None):
    """Append a point, dropping same-value samples within 30 minutes and
    anything older than 400 days. ``history`` is a list of {at, weeklyPct}
    and an optional resetAt on each point. Callers downsample older points
    with downsample_history so the file stays modest."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=HISTORY_DAYS)
    at_dt = _parse_iso(at) or now
    at_iso = _iso(at_dt)
    reset_iso = _iso(reset_at) if reset_at else None
    cleaned = []
    for point in history or []:
        if not isinstance(point, dict):
            continue
        when = _parse_iso(point.get("at"))
        if when is None:
            continue
        if when < cutoff:
            continue
        try:
            pct = float(point.get("weeklyPct"))
        except (TypeError, ValueError):
            continue
        row = {
            "at": when.astimezone(datetime.timezone.utc).isoformat(timespec="seconds"),
            "weeklyPct": pct,
        }
        existing_reset = _iso(point.get("resetAt")) if point.get("resetAt") else None
        if existing_reset:
            row["resetAt"] = existing_reset
        cleaned.append(row)
    if cleaned:
        last = cleaned[-1]
        last_dt = _parse_iso(last["at"])
        if last_dt is not None and abs(float(last["weeklyPct"]) - float(weekly_pct)) < 1e-9:
            if abs((at_dt - last_dt).total_seconds()) <= DEDUPE_SECONDS:
                return cleaned
    row = {"at": at_iso, "weeklyPct": float(weekly_pct)}
    if reset_iso:
        row["resetAt"] = reset_iso
    cleaned.append(row)
    return cleaned


def merge_snapshot(store, snapshot, now=None):
    accounts = dict(store.get("accounts") or {})
    account_id = snapshot["accountId"]
    existing = accounts.get(account_id)
    if not isinstance(existing, dict):
        existing = {}
    # History is stamped with the time we sampled the cache, not the cache
    # file's own readAt, so repeated dashboard loads and the background
    # sampler accumulate coverage through the day (still deduped).
    sample_at = _iso(now) if now is not None else _now_iso()
    history = append_history(
        existing.get("history") or [],
        snapshot["weeklyPct"],
        sample_at,
        now=now,
        reset_at=snapshot.get("resetAt"),
    )
    windows = merge_window_summaries(
        existing.get("windows") or [],
        history,
        current_reset_at=snapshot.get("resetAt"),
        now=now,
    )
    history = downsample_history(history, now=now, current_reset_at=snapshot.get("resetAt"))
    entry = dict(snapshot)
    entry["history"] = history
    entry["windows"] = windows
    accounts[account_id] = entry
    return {"accounts": accounts}



def _history_points(history):
    points = []
    for point in history or []:
        if not isinstance(point, dict):
            continue
        when = _parse_iso(point.get("at"))
        if when is None:
            continue
        try:
            pct = float(point.get("weeklyPct"))
        except (TypeError, ValueError):
            continue
        points.append({
            "at": when,
            "weeklyPct": pct,
            "resetAt": _parse_iso(point.get("resetAt")) if point.get("resetAt") else None,
        })
    points.sort(key=lambda p: p["at"])
    return points


def _assign_windows(points, current_reset_at=None):
    """Split the series into weekly windows.

    A new window starts when the percentage drops, or when a reading is at
    or after the previous point's reset (nextResetMs / resetAt). The
    account's current resetAt is used when older points did not store one.
    """
    reset_now = _parse_iso(current_reset_at) if isinstance(current_reset_at, str) else current_reset_at
    window = 0
    for i, point in enumerate(points):
        if i == 0:
            point["window"] = 0
            continue
        prev = points[i - 1]
        new_window = False
        if point["weeklyPct"] < prev["weeklyPct"] - 1e-9:
            new_window = True
        deadline = prev["resetAt"]
        if deadline is None and reset_now is not None and prev["at"] < reset_now:
            deadline = reset_now
        if deadline is not None and point["at"] >= deadline:
            new_window = True
        if new_window:
            window += 1
        point["window"] = window
    return points


def daily_usage_from_history(history, tz=None, now=None, current_reset_at=None):
    """Per local day (and hour), sum positive increases in the same weekly window.

    The first reading in a new window counts from 0 if it is above 0 (usage
    already accumulated after a reset, before we sampled). The first reading
    in the whole history does not count from 0: that is the start of
    coverage, not a known increase. A delta that spans midnight is attributed
    to the local day and hour of the later reading.

    ``tz`` should be a DST-aware timezone (for example ZoneInfo("Europe/London")).
    When omitted, the machine's local timezone is used.
    """
    tz = tz or default_timezone()
    points = _assign_windows(_history_points(history), current_reset_at=current_reset_at)
    days = {}
    hours = {}

    def day_record(local):
        key = local.date().isoformat()
        rec = days.get(key)
        if rec is None:
            rec = {
                "date": key,
                "usedPct": 0.0,
                "sinceFirstPct": 0.0,
                "coverageStart": None,
                "coverageComplete": False,
                "firstPct": None,
                "lastPct": None,
                "_first_at": None,
                "_first_window": None,
            }
            days[key] = rec
        return rec

    def add_delta(later, delta):
        if delta <= 0:
            return
        local = later["at"].astimezone(tz)
        rec = day_record(local)
        rec["usedPct"] += delta
        hour_key = (rec["date"], local.hour)
        bucket = hours.get(hour_key)
        if bucket is None:
            bucket = {"date": rec["date"], "hour": local.hour, "usedPct": 0.0}
            hours[hour_key] = bucket
        bucket["usedPct"] += delta

    for i, point in enumerate(points):
        local = point["at"].astimezone(tz)
        rec = day_record(local)
        if rec["_first_at"] is None or point["at"] < rec["_first_at"]:
            rec["_first_at"] = point["at"]
            rec["firstPct"] = point["weeklyPct"]
            rec["_first_window"] = point["window"]
            rec["coverageStart"] = _iso(point["at"])
        rec["lastPct"] = point["weeklyPct"]
        if i == 0:
            # Start of coverage, not a known post-reset burst.
            continue
        prev = points[i - 1]
        if point["window"] != prev["window"]:
            add_delta(point, point["weeklyPct"] if point["weeklyPct"] > 0 else 0.0)
        else:
            add_delta(point, point["weeklyPct"] - prev["weeklyPct"])

    # coverageComplete: there was a same-window reading at or before local midnight.
    for rec in days.values():
        day_start = datetime.datetime.combine(
            datetime.date.fromisoformat(rec["date"]),
            datetime.time.min,
            tzinfo=tz,
        )
        earlier = [p for p in points if p["at"] < day_start]
        if earlier and rec["_first_window"] is not None and earlier[-1]["window"] == rec["_first_window"]:
            rec["coverageComplete"] = True
        first = rec["firstPct"]
        last = rec["lastPct"]
        if first is not None and last is not None:
            rec["sinceFirstPct"] = max(0.0, last - first)
        rec.pop("_first_at", None)
        rec.pop("_first_window", None)
        rec["usedPct"] = round(rec["usedPct"], 6)
        rec["sinceFirstPct"] = round(rec["sinceFirstPct"], 6)

    day_list = [days[k] for k in sorted(days)]
    hour_list = [hours[k] for k in sorted(hours)]
    for bucket in hour_list:
        bucket["usedPct"] = round(bucket["usedPct"], 6)
    return {"days": day_list, "hours": hour_list}



def _point_row(at, weekly_pct, reset_at=None):
    row = {"at": _iso(at), "weeklyPct": float(weekly_pct)}
    reset_iso = _iso(reset_at) if reset_at else None
    if reset_iso:
        row["resetAt"] = reset_iso
    return row



def _group_weekly_windows(points, current_reset_at=None):
    """Group readings that share a weekly limit window.

    Points with the same reset time stay together even if the percentage
    dips. A new group starts when a reading is at or after the previous
    point's reset, or when resetAt changes. If no reset is stored, a drop
    is treated as the start of a new window.
    """
    reset_now = _parse_iso(current_reset_at) if isinstance(current_reset_at, str) else current_reset_at
    groups = []
    current = []
    current_reset = None
    for point in points:
        if not current:
            current = [point]
            current_reset = point.get("resetAt")
            continue
        prev = current[-1]
        new_window = False
        deadline = prev.get("resetAt")
        if deadline is None and reset_now is not None and prev["at"] < reset_now:
            deadline = reset_now
        if deadline is not None and point["at"] >= deadline:
            new_window = True
        this_reset = point.get("resetAt")
        if this_reset is not None and current_reset is not None and this_reset != current_reset:
            new_window = True
        if (
            this_reset is None
            and prev.get("resetAt") is None
            and deadline is None
            and point["weeklyPct"] < prev["weeklyPct"] - 1e-9
        ):
            new_window = True
        if new_window:
            groups.append(current)
            current = [point]
            current_reset = this_reset
        else:
            current.append(point)
            if this_reset is not None:
                current_reset = this_reset
    if current:
        groups.append(current)
    return groups


def downsample_history(history, now=None, current_reset_at=None):
    """Keep full resolution for the last 14 days; older points are reduced
    to one reading per UTC hour, plus each weekly window's last reading
    before reset. Points older than HISTORY_DAYS are dropped.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=HISTORY_DAYS)
    full_after = now - datetime.timedelta(days=FULL_RES_DAYS)
    points = _history_points(history)
    kept = [p for p in points if p["at"] >= cutoff]
    if not kept:
        return []
    recent = [p for p in kept if p["at"] >= full_after]
    older = [p for p in kept if p["at"] < full_after]
    if not older:
        return [_point_row(p["at"], p["weeklyPct"], p["resetAt"]) for p in kept]

    window_last_isos = set()
    for group in _group_weekly_windows(list(kept), current_reset_at=current_reset_at):
        last = group[-1]
        if last["at"] < full_after:
            window_last_isos.add(_iso(last["at"]))

    by_hour = {}
    for point in older:
        key = point["at"].astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H")
        prev = by_hour.get(key)
        if prev is None or point["at"] >= prev["at"]:
            by_hour[key] = point
    chosen = {id(p): p for p in by_hour.values()}
    have = {_iso(p["at"]) for p in chosen.values()}
    for point in older:
        iso = _iso(point["at"])
        if iso in window_last_isos and iso not in have:
            chosen[id(point)] = point
            have.add(iso)
    older_rows = sorted(chosen.values(), key=lambda p: p["at"])
    rows = [_point_row(p["at"], p["weeklyPct"], p["resetAt"]) for p in older_rows + recent]
    return rows


def _partial_coverage(first_at, reset_at, previous_reset=None):
    if first_at is None or reset_at is None:
        return True
    start = previous_reset if previous_reset is not None else (reset_at - datetime.timedelta(days=7))
    return first_at > start + datetime.timedelta(hours=1)


def window_summaries_from_history(history, current_reset_at=None, now=None):
    """One compact summary per weekly window, keyed by that window's reset time."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    reset_now = _parse_iso(current_reset_at) if isinstance(current_reset_at, str) else current_reset_at
    groups = _group_weekly_windows(_history_points(history), current_reset_at=reset_now)
    summaries = []
    previous_reset = None
    for group in groups:
        reset = None
        for point in group:
            if point["resetAt"] is not None:
                reset = point["resetAt"]
                break
        if reset is None and reset_now is not None and group[0]["at"] < reset_now:
            reset = reset_now
        if reset is None:
            # No stored reset: still record a window keyed by the last reading
            # plus seven days so a summary can exist for trend charts.
            reset = group[-1]["at"] + datetime.timedelta(days=7)
        first = group[0]["at"]
        last = group[-1]["at"]
        peak = max(p["weeklyPct"] for p in group)
        current = reset > now
        summaries.append({
            "resetAt": _iso(reset),
            "peakPct": round(float(peak), 6),
            "firstReadingAt": _iso(first),
            "lastReadingAt": _iso(last),
            "partialCoverage": _partial_coverage(first, reset, previous_reset),
            "current": current,
        })
        previous_reset = reset
    return summaries


def merge_window_summaries(existing, history, current_reset_at=None, now=None):
    """Combine stored window summaries with a fresh pass over history.

    Peak, first reading and last reading take the wider of the two so a
    later downsample cannot erase a trend already recorded.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    by_reset = {}
    for row in existing or []:
        if not isinstance(row, dict):
            continue
        key = _iso(row.get("resetAt")) if row.get("resetAt") else None
        if not key:
            continue
        try:
            peak = float(row.get("peakPct"))
        except (TypeError, ValueError):
            continue
        by_reset[key] = {
            "resetAt": key,
            "peakPct": peak,
            "firstReadingAt": _iso(row.get("firstReadingAt")) or None,
            "lastReadingAt": _iso(row.get("lastReadingAt")) or None,
        }
    for row in window_summaries_from_history(history, current_reset_at=current_reset_at, now=now):
        key = row["resetAt"]
        if key in by_reset:
            old = by_reset[key]
            firsts = [t for t in (old.get("firstReadingAt"), row.get("firstReadingAt")) if t]
            lasts = [t for t in (old.get("lastReadingAt"), row.get("lastReadingAt")) if t]
            row = {
                "resetAt": key,
                "peakPct": max(old["peakPct"], row["peakPct"]),
                "firstReadingAt": min(firsts) if firsts else None,
                "lastReadingAt": max(lasts) if lasts else None,
            }
        by_reset[key] = {
            "resetAt": key,
            "peakPct": row["peakPct"],
            "firstReadingAt": row.get("firstReadingAt"),
            "lastReadingAt": row.get("lastReadingAt"),
        }

    cutoff = now - datetime.timedelta(days=HISTORY_DAYS)
    ordered = sorted(by_reset.values(), key=lambda w: w["resetAt"] or "")
    previous_reset = None
    out = []
    for row in ordered:
        reset = _parse_iso(row["resetAt"])
        last = _parse_iso(row.get("lastReadingAt"))
        first = _parse_iso(row.get("firstReadingAt"))
        if reset is None:
            previous_reset = reset
            continue
        if not (reset >= cutoff or reset > now or (last is not None and last >= cutoff)):
            previous_reset = reset
            continue
        out.append({
            "resetAt": row["resetAt"],
            "peakPct": round(float(row["peakPct"]), 6),
            "firstReadingAt": row.get("firstReadingAt"),
            "lastReadingAt": row.get("lastReadingAt"),
            "partialCoverage": _partial_coverage(first, reset, previous_reset),
            "current": reset > now,
        })
        previous_reset = reset
    return out


def default_timezone():
    """The machine's current local timezone, DST-aware where zoneinfo applies."""
    return datetime.datetime.now().astimezone().tzinfo


def collect(directory=None, store_path=None, now=None, tz=None):
    """Discover the local cache, append history, and return the public report."""
    directory = directory or persistence_dir()
    store_path = store_path or HISTORY_PATH
    tz = tz or default_timezone()
    with _store_lock:
        discovered = discover(directory)
        store = load_store(store_path)
        if discovered:
            for snap in discovered:
                store = merge_snapshot(store, snap, now=now)
            save_store(store, store_path)
            accounts = []
            for snap in discovered:
                entry = (store.get("accounts") or {}).get(snap["accountId"]) or snap
                accounts.append(_public_account(entry, tz=tz, now=now))
            return {"found": True, "accounts": accounts, "source": "local-cache"}
        # No cache on disk: still return any previously stored accounts so a
        # push/import is not wiped the next time the dashboard loads.
        stored = []
        for account_id, entry in (store.get("accounts") or {}).items():
            if not isinstance(entry, dict):
                continue
            row = dict(entry)
            row.setdefault("accountId", account_id)
            if row.get("weeklyPct") is None:
                continue
            stored.append(_public_account(row, tz=tz, now=now))
        stored.sort(key=lambda a: (not a.get("current"), a.get("accountId") or ""))
        return {"found": bool(stored), "accounts": stored, "source": "store" if stored else None}


def _public_account(entry, tz=None, now=None):
    history = list(entry.get("history") or [])
    usage = daily_usage_from_history(
        history,
        tz=tz or default_timezone(),
        now=now,
        current_reset_at=entry.get("resetAt"),
    )
    return {
        "accountId": entry.get("accountId"),
        "current": bool(entry.get("current")),
        "plan": entry.get("plan"),
        "planLabel": entry.get("planLabel"),
        "weeklyPct": entry.get("weeklyPct"),
        "resetAt": entry.get("resetAt"),
        "creditsUsd": entry.get("creditsUsd"),
        "updatedAt": entry.get("updatedAt"),
        "expiresAt": entry.get("expiresAt"),
        "wouldRefuseSend": bool(entry.get("wouldRefuseSend")),
        "isSandTrial": bool(entry.get("isSandTrial")),
        "isTeamSeat": bool(entry.get("isTeamSeat")),
        "source": entry.get("source") or "local-cache",
        "note": entry.get("note") or DEFAULT_NOTE,
        "history": history,
        "dailyUsage": usage["days"],
        "hourlyUsage": usage["hours"],
        "windows": merge_window_summaries(
            entry.get("windows") or [],
            history,
            current_reset_at=entry.get("resetAt"),
            now=now,
        ),
    }


def validate_snapshot(body):
    """Validate a POST /api/grokbot JSON object and return a snapshot.

    Used as a fallback when the local cache is not on this machine (for
    example an assistant pushing a reading). Not required for the usual
    Mac install, where the cache is read automatically.
    """
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    raw_text = body.get("rawText") or body.get("text")
    if isinstance(raw_text, str) and raw_text.strip() and body.get("weeklyPct") is None:
        parsed = parse_report_text(raw_text)
        merged = dict(parsed)
        for key, value in body.items():
            if key in ("rawText", "text"):
                continue
            if value is not None and value != "":
                merged[key] = value
        body = merged
    account = body.get("accountId") or body.get("account") or "default"
    if not isinstance(account, str) or not _ACCOUNT_ID_RE.match(account.strip()):
        raise ValueError("accountId must be a short label or id")
    account = account.strip()
    pct = body.get("weeklyPct")
    try:
        weekly_pct = float(pct)
    except (TypeError, ValueError):
        raise ValueError("weeklyPct must be a number")
    if weekly_pct < 0 or weekly_pct > 1000:
        raise ValueError("weeklyPct must be between 0 and 1000")
    reset_at = body.get("resetAt")
    if reset_at is not None and reset_at != "":
        if not isinstance(reset_at, str) or _parse_iso(reset_at) is None:
            raise ValueError("resetAt must be an ISO-8601 timestamp")
        reset_at = _parse_iso(reset_at).astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    else:
        reset_at = None
    credits = body.get("creditsUsd")
    if credits is None or credits == "":
        credits_usd = None
    else:
        try:
            credits_usd = float(credits)
        except (TypeError, ValueError):
            raise ValueError("creditsUsd must be a number")
        if credits_usd < 0:
            raise ValueError("creditsUsd must be zero or more")
        credits_usd = round(credits_usd, 2)
    plan = body.get("plan")
    if plan is not None:
        if not isinstance(plan, str):
            raise ValueError("plan must be a string")
        plan = plan.strip() or None
        if plan and len(plan) > 80:
            raise ValueError("plan must be 80 characters or fewer")
    plan_label = body.get("planLabel")
    if plan_label is not None:
        if not isinstance(plan_label, str):
            raise ValueError("planLabel must be a string")
        plan_label = plan_label.strip() or None
        if plan_label and len(plan_label) > 80:
            raise ValueError("planLabel must be 80 characters or fewer")
    updated = body.get("updatedAt")
    if updated:
        parsed_updated = _parse_iso(updated) if isinstance(updated, str) else None
        if parsed_updated is None:
            raise ValueError("updatedAt must be an ISO-8601 timestamp")
        updated = parsed_updated.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    else:
        updated = _now_iso()
    return {
        "accountId": account,
        "current": bool(body.get("current")),
        "plan": plan,
        "planLabel": plan_label,
        "weeklyPct": weekly_pct,
        "resetAt": reset_at,
        "creditsUsd": credits_usd,
        "updatedAt": updated,
        "expiresAt": None,
        "wouldRefuseSend": bool(body.get("wouldRefuseSend")),
        "isSandTrial": bool(body.get("isSandTrial")),
        "isTeamSeat": bool(body.get("isTeamSeat")),
        "source": "push",
        "note": DEFAULT_NOTE,
    }


_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:used|of(?:\s+the)?\s+weekly\s+limit)", re.I)
_PLAN_RE = re.compile(r"(?:plan\s*[:—-]\s*|(?:^|\b)plan:\s*)([A-Za-z0-9][A-Za-z0-9 .+-]{0,60})", re.I)
_PLAN_ALT_RE = re.compile(r"\b((?:Super|Pro|Plus|Free|Ultra|Team|Enterprise|Business)(?:\s+plan)?)\b", re.I)
_CREDITS_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*(?:of\s+)?(?:additional\s+)?credits", re.I)
_RESET_RE = re.compile(
    r"resets?\s+(?:on\s+)?(?:(?P<dow>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(?P<day>\d{1,2})(?:(?:st|nd|rd|th)?)?(?:,?\s*(?P<year>\d{4}))?\s+at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>am|pm)?",
    re.I,
)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_report_text(text, now=None):
    """Tolerant parse of a pasted Grok Bot usage report. Missing fields stay unset."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("report text is empty")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {}
    pct = _PCT_RE.search(text)
    if not pct:
        # also accept a lone "12.5% used" already covered; try a looser "12.5% of weekly"
        loose = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if loose:
            pct = loose
    if pct:
        out["weeklyPct"] = float(pct.group(1))
    plan = _PLAN_RE.search(text)
    if plan:
        out["plan"] = plan.group(1).strip().removesuffix(" plan").strip()
    else:
        alt = _PLAN_ALT_RE.search(text)
        if alt:
            out["plan"] = re.sub(r"\s+plan$", "", alt.group(1), flags=re.I).strip()
    credits = _CREDITS_RE.search(text)
    if credits:
        out["creditsUsd"] = float(credits.group(1))
    reset = _RESET_RE.search(text)
    if reset:
        mon_name = reset.group("mon").lower()
        mon = _MONTHS.get(mon_name) or _MONTHS.get(mon_name[:3])
        if mon is None:
            raise ValueError("could not parse the reset month")
        day = int(reset.group("day"))
        year = int(reset.group("year")) if reset.group("year") else now.year
        hour = int(reset.group("hour"))
        minute = int(reset.group("minute"))
        ampm = (reset.group("ampm") or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        try:
            dt = datetime.datetime(year, mon, day, hour, minute, tzinfo=datetime.timezone.utc)
        except ValueError:
            dt = None
        if dt is not None and not reset.group("year") and dt < now - datetime.timedelta(days=1):
            try:
                dt = dt.replace(year=year + 1)
            except ValueError:
                pass
        if dt is not None:
            out["resetAt"] = dt.isoformat(timespec="seconds")
    if "weeklyPct" not in out:
        raise ValueError("could not find a weekly usage percentage in the report text")
    return out


def apply_push(body, store_path=None, now=None, tz=None):
    snap = validate_snapshot(body)
    with _store_lock:
        store = load_store(store_path)
        store = merge_snapshot(store, snap, now=now)
        save_store(store, store_path)
        entry = (store.get("accounts") or {})[snap["accountId"]]
    return _public_account(entry, tz=tz, now=now)


def _import_stdin(raw, store_path=None):
    raw = raw.strip()
    if not raw:
        raise ValueError("stdin is empty")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
    except json.JSONDecodeError:
        body = {"rawText": raw}
    return apply_push(body, store_path=store_path)


if __name__ == "__main__":
    # Fallback used by scripts/grokbot-push.sh when the server is down.
    try:
        saved = _import_stdin(sys.stdin.read())
    except ValueError as e:
        print(f"grokbot-usage: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"ok": True, "account": saved}, indent=2))
