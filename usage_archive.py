#!/usr/bin/env python3
"""Usage archive: daily totals kept after the transcripts are gone.

Claude Code and the other tools delete old transcripts, and ccusage can only
count what is still on disk. Once a day (and on the first start if there is
no archive yet) the server writes each complete day's totals to
archive/YYYY-MM.json in the data directory: per day, the ccusage daily row
(tokens by type and cost per agent and model) and the per account, agent and
project rows. Numbers, model names and project paths only. Never transcript
content, prompts, emails or tokens.

Reading it back, live ccusage data always wins: an archived day is used only
when no live row exists for that day at all, so nothing is counted twice.
"""
import datetime
import json
import os

import data_dir

VERSION = 1
SESSION_KEYS = ("period", "project", "accountUuid", "agent", "totalCost", "totalTokens",
                "costKnown", "sessions", "modelBreakdowns")
DAILY_DROP = ("archived",)
MB_KEYS = ("modelName", "inputTokens", "outputTokens", "cacheCreationTokens",
           "cacheCreation1hTokens", "cacheReadTokens", "cost")


def root():
    return data_dir.path("archive")


def _day(row):
    return str((row or {}).get("period") or (row or {}).get("date") or "")[:10]


def _clean_mb(mbs):
    return [{k: m.get(k) for k in MB_KEYS if k in m} for m in (mbs or []) if isinstance(m, dict)]


def _clean_daily(row):
    out = {k: v for k, v in row.items() if k not in DAILY_DROP}
    out["modelBreakdowns"] = _clean_mb(row.get("modelBreakdowns"))
    if isinstance(row.get("agents"), list):
        out["agents"] = []
        for a in row["agents"]:
            if isinstance(a, dict):
                a2 = dict(a)
                a2["modelBreakdowns"] = _clean_mb(a.get("modelBreakdowns"))
                out["agents"].append(a2)
    return out


def _clean_session(row):
    out = {k: row.get(k) for k in SESSION_KEYS if k in row}
    out["modelBreakdowns"] = _clean_mb(row.get("modelBreakdowns"))
    return out


def _tokens(entry):
    d = (entry or {}).get("daily") or {}
    if d.get("totalTokens") is not None:
        return int(d.get("totalTokens") or 0)
    return sum(int(m.get(k) or 0) for m in d.get("modelBreakdowns") or []
               for k in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"))


def snapshot_days(daily, sessions, today=None):
    """{day: {"daily": row, "sessions": [rows]}} for complete days (before today)."""
    today_s = (today or datetime.date.today()).isoformat()
    days = {}
    for row in daily or []:
        d = _day(row)
        if not d or d >= today_s or row.get("archived"):
            continue
        days.setdefault(d, {"daily": None, "sessions": []})["daily"] = _clean_daily(row)
    for row in sessions or []:
        d = _day(row)
        if not d or d >= today_s or row.get("archived") or d not in days:
            continue
        days[d]["sessions"].append(_clean_session(row))
    return days


def month_path(month, base=None):
    return os.path.join(base or root(), month + ".json")


def _read_month(path):
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    days = raw.get("days") if isinstance(raw, dict) else None
    return days if isinstance(days, dict) else {}


def write(daily, sessions, today=None, base=None, now=None):
    """Merge today's view of every complete day into the month files.

    A day already archived is only replaced when the live day has at least as
    many tokens, so transcripts that have been partly cleaned up never shrink
    the archive. Returns {"written": n_days, "months": [...]}."""
    base = base or root()
    today = today or datetime.date.today()
    snap = snapshot_days(daily, sessions, today)
    by_month = {}
    for d, entry in snap.items():
        by_month.setdefault(d[:7], {})[d] = entry
    written = 0
    stamp = (now or datetime.datetime.now().astimezone()).isoformat(timespec="seconds")
    for month, entries in sorted(by_month.items()):
        path = month_path(month, base)
        days = _read_month(path)
        changed = False
        for d, entry in entries.items():
            old = days.get(d)
            if old is not None and _tokens(entry) < _tokens(old):
                continue
            core = {k: v for k, v in (old or {}).items() if k != "archivedAt"}
            if old is None or core != json.loads(json.dumps(entry)):
                days[d] = dict(entry, archivedAt=stamp)
                changed = True
                written += 1
        if changed or not os.path.exists(path):
            data_dir.atomic_write_json(path, {"version": VERSION, "month": month, "updatedAt": stamp,
                                              "days": dict(sorted(days.items()))})
    data_dir.atomic_write_json(os.path.join(base, "state.json"),
                               {"lastWrite": today.isoformat(), "updatedAt": stamp})
    return {"written": written, "months": sorted(by_month)}


def due(today=None, base=None):
    """True when the archive has not been written today, or does not exist."""
    base = base or root()
    today = today or datetime.date.today()
    try:
        with open(os.path.join(base, "state.json")) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return True
    if not any(n.endswith(".json") and n[:4].isdigit() for n in os.listdir(base)):
        return True
    return state.get("lastWrite") != today.isoformat()


def load(base=None):
    """{day: entry} from every month file."""
    base = base or root()
    out = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if not (name.endswith(".json") and len(name) == 12 and name[4] == "-"):
            continue
        out.update(_read_month(os.path.join(base, name)))
    return out


def merge(daily, sessions, archived, labels=None):
    """(daily, sessions, filled_days). Live rows are untouched; an archived day
    is added only when neither live list has any row for that day."""
    live_days = {_day(r) for r in daily or []} | {_day(r) for r in sessions or []}
    labels = labels or {}
    filled = sorted(d for d, e in (archived or {}).items() if d not in live_days and (e or {}).get("daily"))
    if not filled:
        return list(daily or []), list(sessions or []), []
    out_daily = list(daily or [])
    out_sessions = list(sessions or [])
    for d in filled:
        e = archived[d]
        out_daily.append(dict(e["daily"], archived=True))
        for s in e.get("sessions") or []:
            uuid = s.get("accountUuid")
            label = (labels.get(uuid) or {}).get("label") if isinstance(labels.get(uuid), dict) else labels.get(uuid)
            out_sessions.append(dict(s, archived=True,
                                     label=label or ("Archived account (%s\u2026)" % str(uuid)[:8] if uuid else "Unattributed")))
    out_daily.sort(key=_day)
    out_sessions.sort(key=_day)
    return out_daily, out_sessions, filled
