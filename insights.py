#!/usr/bin/env python3
"""Forecasts and week-on-week comparisons for plan limits.

Every plan this app reads (Grok Bot, Claude's 7-day window, Codex's primary
window) reports a percentage of a limit that resets at a known time. This
module turns those readings into two things the dashboard shows next to each
plan row:

- a forecast: at the pace so far in this window, where will the percentage be
  at the reset, and would it reach 100% before then;
- a comparison: this window's peak against the previous window's peak, when
  the history actually covers the previous window.

Pure functions only, no file or network access, so they are easy to test.
Stdlib only, and Python 3.9 compatible (the macOS system python3).
"""
import datetime

import pacing

WEEK_SECONDS = 7 * 24 * 3600
LOW_CONFIDENCE_HOURS = 24  # readings spanning less than this mark a forecast as low confidence
RESET_MATCH_SECONDS = 3600  # resets reported a few seconds apart are the same window

_UTC = datetime.timezone.utc


def parse_time(value):
    """ISO string (with Z or offset), epoch seconds or epoch milliseconds, or
    a datetime. Returns an aware UTC datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.datetime.fromtimestamp(seconds, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            # Python 3.9 rejects fractional seconds that are not 3 or 6 digits.
            try:
                head, _, tail = text.partition(".")
                frac = ""
                rest = ""
                for i, ch in enumerate(tail):
                    if not ch.isdigit():
                        rest = tail[i:]
                        break
                    frac += ch
                frac = (frac + "000000")[:6]
                dt = datetime.datetime.fromisoformat(head + "." + frac + rest)
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def iso(dt):
    return dt.astimezone(_UTC).isoformat(timespec="seconds") if dt else None


def same_reset(a, b, tolerance=RESET_MATCH_SECONDS):
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= tolerance


def forecast(points, reset_at, window_seconds, now=None, low_confidence_hours=LOW_CONFIDENCE_HOURS):
    """Linear forecast for one plan window.

    points: iterable of (time, pct). Times may be anything parse_time accepts.
    Readings outside the current window (before reset - window, or after now)
    are ignored, so callers can pass a whole history.

    Pace:
    - "readings": when readings in this window span at least
      low_confidence_hours, the pace is the slope from the first reading in
      the window to the latest one (recent behaviour, not the whole window).
    - "window": otherwise, the pace is the latest reading divided by the time
      since the window started. Usage is 0% at a reset by definition, so this
      anchor is real even when this app was not running at the time.

    Returns None when there is no reset time, no reading in the window, or the
    window has already reset.
    """
    reset = parse_time(reset_at)
    if reset is None or not window_seconds or window_seconds <= 0:
        return None
    now = parse_time(now) or datetime.datetime.now(tz=_UTC)
    if now >= reset:
        return None
    start = reset - datetime.timedelta(seconds=window_seconds)
    cleaned = []
    for t, p in points or []:
        dt = parse_time(t)
        if dt is None or p is None:
            continue
        try:
            pct = float(p)
        except (TypeError, ValueError):
            continue
        if start <= dt <= now + datetime.timedelta(minutes=5):
            cleaned.append((dt, pct))
    if not cleaned:
        return None
    cleaned.sort(key=lambda x: x[0])
    t0, p0 = cleaned[0]
    t1, p1 = cleaned[-1]
    coverage_hours = (t1 - t0).total_seconds() / 3600.0
    elapsed = (t1 - start).total_seconds()
    if elapsed <= 0:
        return None

    basis = "window"
    rate = p1 / elapsed  # percent per second
    if coverage_hours >= low_confidence_hours and (t1 - t0).total_seconds() > 0:
        basis = "readings"
        rate = max(0.0, (p1 - p0) / (t1 - t0).total_seconds())

    remaining = (reset - t1).total_seconds()
    projected = p1 + rate * remaining
    hits_at = None
    if p1 >= 100:
        hits_at = t1
    elif projected >= 100 and rate > 0:
        hits_at = t1 + datetime.timedelta(seconds=(100.0 - p1) / rate)

    return {
        "currentPct": round(p1, 1),
        "projectedPct": round(min(projected, 999.0), 1),
        "resetAt": iso(reset),
        "windowStart": iso(start),
        "hitsLimitAt": iso(hits_at),
        "basis": basis,
        "confidence": "low" if coverage_hours < low_confidence_hours else "normal",
        "coverageHours": round(coverage_hours, 1),
        "coverageStart": iso(t0),
        "asOf": iso(t1),
        "ratePerDay": round(rate * 86400, 2),
    }


def window_peaks(samples):
    """samples: iterable of (time, pct, reset). Groups by reset (resets a few
    seconds apart count as one window) and returns a list of
    {"resetAt", "peakPct", "firstAt", "lastAt", "count"} sorted by reset."""
    groups = []
    for t, p, r in samples or []:
        reset = parse_time(r)
        dt = parse_time(t)
        if reset is None or p is None:
            continue
        try:
            pct = float(p)
        except (TypeError, ValueError):
            continue
        group = None
        for g in groups:
            if same_reset(g["_reset"], reset):
                group = g
                break
        if group is None:
            group = {"_reset": reset, "peakPct": pct, "firstAt": dt, "lastAt": dt, "count": 0}
            groups.append(group)
        group["peakPct"] = max(group["peakPct"], pct)
        if dt is not None:
            if group["firstAt"] is None or dt < group["firstAt"]:
                group["firstAt"] = dt
            if group["lastAt"] is None or dt > group["lastAt"]:
                group["lastAt"] = dt
        group["count"] += 1
    groups.sort(key=lambda g: g["_reset"])
    return [{
        "resetAt": iso(g["_reset"]),
        "peakPct": round(g["peakPct"], 1),
        "firstAt": iso(g["firstAt"]),
        "lastAt": iso(g["lastAt"]),
        "count": g["count"],
    } for g in groups]


def compare_windows(peaks, current_reset, window_seconds, current_pct=None):
    """This window's peak against the window immediately before it.

    peaks: output of window_peaks (or the same shape). The previous window
    must end at this window's start (within a day), otherwise the history has
    a gap and there is nothing honest to compare with, so None is returned.
    """
    reset = parse_time(current_reset)
    if reset is None or not window_seconds:
        return None
    start = reset - datetime.timedelta(seconds=window_seconds)
    current = None
    previous = None
    for p in peaks or []:
        r = parse_time(p.get("resetAt"))
        if r is None:
            continue
        if same_reset(r, reset):
            current = p
        elif abs((r - start).total_seconds()) <= 86400:
            if previous is None or r > parse_time(previous.get("resetAt")):
                previous = p
    if previous is None:
        return None
    cur_peak = current.get("peakPct") if current else None
    if current_pct is not None:
        cur_peak = max(float(current_pct), cur_peak or 0.0)
    if cur_peak is None:
        return None
    prev_peak = float(previous.get("peakPct") or 0.0)
    return {
        "currentPeakPct": round(cur_peak, 1),
        "previousPeakPct": round(prev_peak, 1),
        "deltaPct": round(cur_peak - prev_peak, 1),
        "previousResetAt": previous.get("resetAt"),
        "previousPartial": bool(previous.get("partialCoverage")),
    }


def pct_change(current, previous):
    """Percentage change from previous to current, or None when there is no
    meaningful previous value (missing or zero)."""
    if current is None or previous is None:
        return None
    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return None
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100.0, 1)


# ---- per-source builders ----------------------------------------------------

def _grokbot_insights(grokbot, now):
    out = []
    for acct in (grokbot or {}).get("accounts") or []:
        reset = parse_time(acct.get("resetAt"))
        pct = acct.get("weeklyPct")
        if reset is None or pct is None:
            continue
        start = reset - datetime.timedelta(seconds=WEEK_SECONDS)
        points = []
        for h in acct.get("history") or []:
            r = parse_time(h.get("resetAt"))
            if r is not None and not same_reset(r, reset):
                continue
            points.append((h.get("at"), h.get("weeklyPct")))
        points.append((acct.get("updatedAt") or now, pct))
        fc = forecast(points, reset, WEEK_SECONDS, now=now)
        peaks = []
        for w in acct.get("windows") or []:
            if w.get("resetAt") and w.get("peakPct") is not None:
                peaks.append({
                    "resetAt": w.get("resetAt"), "peakPct": w.get("peakPct"),
                    "partialCoverage": w.get("partialCoverage"),
                })
        cmp_ = compare_windows(peaks, reset, WEEK_SECONDS, current_pct=pct)
        out.append({
            "key": "grok-bot:" + str(acct.get("accountId") or "default"),
            "tool": "grok-bot",
            "label": "Grok Bot" + (" (" + acct["plan"] + ")" if acct.get("plan") else ""),
            "pct": round(float(pct), 1),
            "resetAt": iso(reset),
            "windowStart": iso(start),
            "windowSeconds": WEEK_SECONDS,
            "forecast": fc,
            "compare": cmp_,
            "credits": {"kind": "remaining", "value": acct.get("creditsUsd"), "currency": "USD"}
            if acct.get("creditsUsd") is not None else None,
        })
    return out


def _window_fields(window):
    window = window or {}
    return {
        "percent": window.get("percent"),
        "resetsAt": window.get("resetsAt"),
        "note": window.get("note"),
        "state": window.get("state"),
        "asOf": window.get("asOf"),
    }


def _claude_insights(max_usage, claude_history, now, active_block=None):
    out = []
    now_dt = parse_time(now)
    in_window = 0
    for mu in max_usage or []:
        sess = (mu or {}).get("session") or {}
        r = parse_time(sess.get("resetsAt"))
        if sess.get("percent") is not None and sess.get("state") != "reset" and r and now_dt and r > now_dt:
            in_window += 1
    for mu in max_usage or []:
        uuid = mu.get("accountUuid")
        week = mu.get("week") or {}
        pct = week.get("percent")
        reset = parse_time(week.get("resetsAt"))
        if not uuid or pct is None:
            continue
        points = []
        samples = []
        reset_passed = bool(week.get("state") == "reset" or (reset and now and reset <= parse_time(now)))
        for s in claude_history or []:
            if s.get("accountUuid") != uuid or s.get("sevenDayPercent") is None:
                continue
            r = parse_time(s.get("sevenDayResetsAt"))
            samples.append((s.get("time"), s.get("sevenDayPercent"), r))
            if not reset_passed and (r is None or (reset and same_reset(r, reset))):
                points.append((s.get("time"), s.get("sevenDayPercent")))
        if not reset_passed:
            points.append((mu.get("fetchedAt") or week.get("asOf") or now, pct))
            if reset is not None:
                samples.append((mu.get("fetchedAt") or week.get("asOf") or now, pct, reset))
        credits = mu.get("usageCredits") or {}
        # After a reset with no later reading the next window is the same
        # length after the reset we already have; forecast is skipped because
        # there are no points in the new window — the note tells the story.
        if reset_passed and reset is not None:
            while reset <= parse_time(now):
                reset = reset + datetime.timedelta(seconds=WEEK_SECONDS)
        item = {
            "key": "claude:" + uuid,
            "tool": "claude",
            "label": mu.get("label") or "Claude",
            "pct": round(float(pct), 1),
            "resetAt": iso(reset) if reset else None,
            "windowStart": iso(reset - datetime.timedelta(seconds=WEEK_SECONDS)) if reset else None,
            "windowSeconds": WEEK_SECONDS,
            "forecast": None if reset_passed or reset is None else forecast(points, reset, WEEK_SECONDS, now=now),
            "compare": None if reset_passed or reset is None else compare_windows(window_peaks(samples), reset, WEEK_SECONDS, current_pct=pct),
            "note": week.get("note"),
            "session": _window_fields(mu.get("session")),
            "week": _window_fields(week),
            "sessionPace": _safe_pace(uuid, mu, claude_history, now, active_block, in_window == 1),
            "credits": {
                "kind": "used", "value": credits.get("usedMinor"),
                "currency": credits.get("currency"), "exponent": credits.get("exponent"),
            } if credits.get("usedMinor") is not None else None,
        }
        out.append(item)
    return out


def _safe_pace(uuid, mu, claude_history, now, active_block, only_account):
    try:
        session = dict(mu.get("session") or {})
        if not session.get("asOf"):
            session["asOf"] = mu.get("fetchedAt")
        return pacing.session_pace(uuid, session, claude_history, now=now,
                                   active_block=active_block, only_claude_account=only_account)
    except Exception:
        return None


def _codex_insights(codex_live, codex_history, now):
    if not codex_live:
        return []
    primary = codex_live.get("primary") or {}
    pct = primary.get("percent")
    reset = parse_time(primary.get("resetsAt"))
    mins = primary.get("windowMins")
    if pct is None or reset is None or not mins:
        return []
    window = int(mins) * 60
    points = []
    samples = []
    for s in codex_history or []:
        if s.get("windowMins") != mins:
            continue
        r = parse_time(s.get("resetsAt"))
        samples.append((s.get("time"), s.get("percent"), r))
        if r is None or same_reset(r, reset):
            points.append((s.get("time"), s.get("percent")))
    points.append((codex_live.get("fetchedAt") or now, pct))
    samples.append((codex_live.get("fetchedAt") or now, pct, reset))
    plan = codex_live.get("planType")
    balance = codex_live.get("creditsBalance")
    return [{
        "key": "codex:primary",
        "tool": "codex",
        "label": "Codex" + (" (" + str(plan) + ")" if plan else ""),
        "pct": round(float(pct), 1),
        "resetAt": iso(reset),
        "windowStart": iso(reset - datetime.timedelta(seconds=window)),
        "windowSeconds": window,
        "forecast": forecast(points, reset, window, now=now),
        "compare": compare_windows(window_peaks(samples), reset, window, current_pct=pct),
        "credits": {"kind": "remaining", "value": balance, "currency": None}
        if isinstance(balance, (int, float)) and not isinstance(balance, bool) else None,
    }]


def build_plan_insights(grokbot=None, max_usage=None, claude_history=None, codex_live=None,
                        codex_history=None, now=None, claude_active_block=None):
    """One entry per plan window with a current reading. Keys:
    grok-bot:<accountId>, claude:<accountUuid>, codex:primary."""
    now = parse_time(now) or datetime.datetime.now(tz=_UTC)
    out = []
    for builder in (
        lambda: _grokbot_insights(grokbot, now),
        lambda: _claude_insights(max_usage, claude_history, now, claude_active_block),
        lambda: _codex_insights(codex_live, codex_history, now),
    ):
        try:
            out.extend(builder())
        except Exception:
            # One malformed source must not take the others down.
            continue
    return out
