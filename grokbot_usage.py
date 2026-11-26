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
import urllib.parse

import data_dir

HISTORY_PATH = data_dir.path("grokbot-usage.json")
HISTORY_DAYS = 60
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


def append_history(history, weekly_pct, at, now=None):
    """Append a point, dropping same-value samples within 30 minutes and
    anything older than 60 days. ``history`` is a list of {at, weeklyPct}."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=HISTORY_DAYS)
    at_dt = _parse_iso(at) or now
    at_iso = at_dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
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
        cleaned.append({"at": when.astimezone(datetime.timezone.utc).isoformat(timespec="seconds"), "weeklyPct": pct})
    if cleaned:
        last = cleaned[-1]
        last_dt = _parse_iso(last["at"])
        if last_dt is not None and abs(float(last["weeklyPct"]) - float(weekly_pct)) < 1e-9:
            if abs((at_dt - last_dt).total_seconds()) <= DEDUPE_SECONDS:
                return cleaned
    cleaned.append({"at": at_iso, "weeklyPct": float(weekly_pct)})
    return cleaned


def merge_snapshot(store, snapshot, now=None):
    accounts = dict(store.get("accounts") or {})
    account_id = snapshot["accountId"]
    existing = accounts.get(account_id)
    if not isinstance(existing, dict):
        existing = {}
    history = append_history(
        existing.get("history") or [],
        snapshot["weeklyPct"],
        snapshot.get("updatedAt") or _now_iso(),
        now=now,
    )
    entry = dict(snapshot)
    entry["history"] = history
    accounts[account_id] = entry
    return {"accounts": accounts}


def collect(directory=None, store_path=None, now=None):
    """Discover the local cache, append history, and return the public report."""
    directory = directory or persistence_dir()
    store_path = store_path or HISTORY_PATH
    discovered = discover(directory)
    store = load_store(store_path)
    if discovered:
        for snap in discovered:
            store = merge_snapshot(store, snap, now=now)
        save_store(store, store_path)
        accounts = []
        for snap in discovered:
            entry = (store.get("accounts") or {}).get(snap["accountId"]) or snap
            accounts.append(_public_account(entry))
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
        stored.append(_public_account(row))
    stored.sort(key=lambda a: (not a.get("current"), a.get("accountId") or ""))
    return {"found": bool(stored), "accounts": stored, "source": "store" if stored else None}


def _public_account(entry):
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
        "history": list(entry.get("history") or []),
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


def apply_push(body, store_path=None, now=None):
    snap = validate_snapshot(body)
    store = load_store(store_path)
    store = merge_snapshot(store, snap, now=now)
    save_store(store, store_path)
    return _public_account((store.get("accounts") or {})[snap["accountId"]])


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
