#!/usr/bin/env python3
"""Allowance history: every plan-limit reading the server sees, kept on disk.

Each time the server reads plan limits (the Claude accounts in maxUsage, and
every plan in planInsights: Claude, Codex, Grok Bot), one line per account is
appended to allowance-history.jsonl in the data directory, unless it repeats
the last line kept for that account. A repeat is still kept once every few
hours, so a flat week still draws a line.

A line holds only the account key, a display label, percentages, reset times
and usage-credit figures. Never emails, tokens or anything from a transcript.
"""
import datetime
import json
import os
import threading

import data_dir

FILENAME = "allowance-history.jsonl"
HEARTBEAT_SECONDS = 3 * 3600
FIELDS = ("sessionPct", "sessionResetsAt", "weeklyPct", "weeklyResetsAt",
          "creditsUsed", "creditsLimit", "creditsCurrency")

_lock = threading.Lock()
_last = {}          # path -> {key: record}


def path():
    return data_dir.path(FILENAME)


def _utc(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(float(v), 2)


def _credits(mu, insight):
    """(used, limit, currency) in major units, from the account's usage-credit
    reading (Claude) or the plan insight's credit figure."""
    c = (mu or {}).get("usageCredits") or {}
    if c.get("usedMinor") is not None or c.get("limitMinor") is not None:
        scale = 10 ** int(c.get("exponent") or 2)
        used = _num(c.get("usedMinor"))
        limit = _num(c.get("limitMinor"))
        return (round(used / scale, 2) if used is not None else None,
                round(limit / scale, 2) if limit is not None else None,
                c.get("currency"))
    ic = (insight or {}).get("credits") or {}
    if ic.get("kind") == "used" and _num(ic.get("value")) is not None:
        scale = 10 ** int(ic.get("exponent") or 0) if ic.get("exponent") else 1
        return round(float(ic["value"]) / scale, 2), None, ic.get("currency")
    return None, None, None


def readings_from(plan_insights=None, max_usage=None, now=None):
    """Turn the server's current plan readings into history records.

    A window the server has worked out has reset (state "reset") is stamped
    with now, because its 0% is a fact about now, not about the old reading."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    mu_by_key = {"claude:" + m["accountUuid"]: m for m in (max_usage or []) if (m or {}).get("accountUuid")}
    out = []
    seen = set()
    for p in plan_insights or []:
        key = (p or {}).get("key")
        if not key or key in seen:
            continue
        seen.add(key)
        mu = mu_by_key.get(key)
        sess = p.get("session") or {}
        week = p.get("week") or {}
        weekly_pct = week.get("percent") if week else p.get("pct")
        at = None
        for w in (week, sess):
            if w and w.get("state") != "reset" and _parse(w.get("asOf")):
                cand = _parse(w.get("asOf"))
                at = cand if at is None or cand > at else at
        if at is None:
            at = _parse((p.get("forecast") or {}).get("asOf"))
        if at is None or week.get("state") == "reset":
            at = now
        used, limit, cur = _credits(mu, p)
        rec = {
            "at": _utc(at),
            "key": key,
            "tool": p.get("tool"),
            "label": p.get("label") or (mu or {}).get("label"),
            "sessionPct": _num(sess.get("percent")) if sess else None,
            "sessionResetsAt": (sess.get("resetsAt") if sess.get("state") != "reset" else None) if sess else None,
            "weeklyPct": _num(weekly_pct),
            # After a reset the old resetsAt is in the past; the insight
            # carries the next one.
            "weeklyResetsAt": (week.get("resetsAt") if week.get("state") != "reset" else p.get("resetAt")) if week else p.get("resetAt"),
            "windowSeconds": p.get("windowSeconds"),
            "creditsUsed": used,
            "creditsLimit": limit,
            "creditsCurrency": cur,
        }
        if rec["weeklyPct"] is None and rec["sessionPct"] is None:
            continue
        out.append(rec)
    # Claude accounts with a reading but no insight row (rare) still count.
    for key, mu in mu_by_key.items():
        if key in seen:
            continue
        sess, week = mu.get("session") or {}, mu.get("week") or {}
        if sess.get("percent") is None and week.get("percent") is None:
            continue
        at = _parse(week.get("asOf")) or _parse(sess.get("asOf")) or _parse(mu.get("fetchedAt")) or now
        used, limit, cur = _credits(mu, None)
        out.append({
            "at": _utc(at), "key": key, "tool": "claude", "label": mu.get("label"),
            "sessionPct": _num(sess.get("percent")), "sessionResetsAt": sess.get("resetsAt"),
            "weeklyPct": _num(week.get("percent")), "weeklyResetsAt": week.get("resetsAt"),
            "windowSeconds": 604800, "creditsUsed": used, "creditsLimit": limit, "creditsCurrency": cur,
        })
    return out


def _load_last(target):
    last = {}
    try:
        with open(target, "r", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("key"):
                    last[rec["key"]] = rec
    except OSError:
        pass
    return last


def _same(a, b):
    return all(a.get(k) == b.get(k) for k in FIELDS)


def record(plan_insights=None, max_usage=None, now=None, target=None):
    """Append the new readings. Returns the records written."""
    target = target or path()
    recs = readings_from(plan_insights, max_usage, now=now)
    if not recs:
        return []
    written = []
    with _lock:
        if target not in _last:
            _last[target] = _load_last(target)
        last = _last[target]
        lines = []
        for rec in recs:
            prev = last.get(rec["key"])
            if prev:
                pat, rat = _parse(prev.get("at")), _parse(rec["at"])
                if rat is None or (pat is not None and rat <= pat):
                    continue  # older or the same reading again
                if _same(prev, rec) and (rat - pat).total_seconds() < HEARTBEAT_SECONDS:
                    continue
            lines.append(json.dumps(rec, sort_keys=True))
            last[rec["key"]] = rec
            written.append(rec)
        if lines:
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            with open(target, "a") as f:
                f.write("\n".join(lines) + "\n")
    return written


def load(since=None, until=None, target=None):
    """Records, oldest first, optionally limited to [since, until)."""
    target = target or path()
    lo, hi = _parse(since), _parse(until)
    out = []
    try:
        with open(target, "r", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get("key"):
                    continue
                at = _parse(rec.get("at"))
                if at is None or (lo and at < lo) or (hi and at >= hi):
                    continue
                out.append(rec)
    except OSError:
        pass
    out.sort(key=lambda r: r.get("at") or "")
    return out


def reset_cache():
    with _lock:
        _last.clear()
