#!/usr/bin/env python3
"""Pace in the current Claude 5-hour window, and whether it runs out early.

Two inputs, both local:
- percent readings for this account's current 5-hour window, from the status
  line (the live file plus usage-history.jsonl);
- ccusage's active 5-hour block (`ccusage blocks --active --json`): its cost
  so far, its burnRate.costPerHour and the time of its last entry.

Several Claude Code sessions write readings, and an idle session keeps
re-sending the figure from its own last API response, so a lower, older
percentage can arrive after a newer one. Within one window the real figure
never goes down, so readings are reduced to their running maximum first.

Pace, in order of preference:
- "readings": two or more readings at least 15 minutes apart in the last
  hour: the slope between them;
- "blocks": ccusage's burn rate scaled to percent (current % x cost per hour
  / block cost so far). Only used when this is the only Claude account with
  a reading in its window, because ccusage's blocks add up every account
  on this Mac, and only when the block lines up with the window;
- "window": the current % over the time since the window started (usage is
  0% at the start by definition).

No warning is raised on thin data (under 20 minutes into the window or
under 5%), or when ccusage shows no Claude activity for 30 minutes (you have
stopped; the window will not run out at the old pace).
"""
import datetime

WINDOW_SECONDS = 5 * 3600
READINGS_MIN_SPAN = 15 * 60
READINGS_LOOKBACK = 60 * 60
MIN_ELAPSED_FOR_WARNING = 20 * 60
MIN_PCT_FOR_WARNING = 5.0
IDLE_SECONDS = 30 * 60
RESET_MATCH_SECONDS = 15 * 60
_UTC = datetime.timezone.utc


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(float(value), tz=_UTC)
    text = str(value).strip()
    try:
        return datetime.datetime.fromtimestamp(float(text), tz=_UTC)
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_UTC)


def iso(dt):
    return dt.astimezone(_UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def envelope(points):
    """(time, pct) -> the running maximum, keeping the first time each new
    level was seen. Stale lower readings from idle sessions drop out."""
    out = []
    best = None
    for t, p in sorted(points, key=lambda x: x[0]):
        if best is None or p > best:
            best = p
            out.append((t, p))
    return out


def window_points(uuid, history, reset, now, current=None):
    start = reset - datetime.timedelta(seconds=WINDOW_SECONDS)
    pts = []
    for s in history or []:
        if s.get("accountUuid") != uuid or s.get("fiveHourPercent") is None:
            continue
        r = parse_time(s.get("fiveHourResetsAt"))
        if r is None or abs((r - reset).total_seconds()) > RESET_MATCH_SECONDS:
            continue
        t = parse_time(s.get("time"))
        if t is None or t < start or t > now + datetime.timedelta(minutes=5):
            continue
        pts.append((t, float(s["fiveHourPercent"])))
    if current is not None and current[0] is not None and current[1] is not None:
        if start <= current[0] <= now + datetime.timedelta(minutes=5):
            pts.append((current[0], float(current[1])))
    return envelope(pts)


def _block_fields(block):
    if not block:
        return None
    cost = block.get("costUSD")
    rate = (block.get("burnRate") or {}).get("costPerHour")
    start = parse_time(block.get("startTime"))
    last = parse_time(block.get("actualEndTime"))
    if cost is None or start is None:
        return None
    return {"cost": float(cost), "costPerHour": float(rate) if rate is not None else None,
            "start": start, "last": last, "active": bool(block.get("isActive")),
            "projectedCost": (block.get("projection") or {}).get("totalCost")}


def session_pace(uuid, session, history, now=None, active_block=None, only_claude_account=False):
    """Pace for one account's current 5-hour window, or None without one."""
    now = parse_time(now) or datetime.datetime.now(tz=_UTC)
    session = session or {}
    reset = parse_time(session.get("resetsAt"))
    if reset is None or now >= reset or session.get("state") == "reset":
        return None
    start = reset - datetime.timedelta(seconds=WINDOW_SECONDS)
    current = (parse_time(session.get("asOf")) or now, session.get("percent"))
    pts = window_points(uuid, history, reset, now, current if session.get("percent") is not None else None)
    if not pts:
        return None
    t_last, p_last = pts[-1]
    block = _block_fields(active_block)
    if block and abs((block["start"] - start).total_seconds()) > 3600:
        block = None

    basis, rate = None, None  # rate in percent per second
    recent = [(t, p) for t, p in pts if (t_last - t).total_seconds() <= READINGS_LOOKBACK]
    if len(recent) >= 2 and (recent[-1][0] - recent[0][0]).total_seconds() >= READINGS_MIN_SPAN:
        basis = "readings"
        rate = max(0.0, (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0]).total_seconds())
    elif block and only_claude_account and block["cost"] > 0 and block["costPerHour"] and p_last > 0:
        basis = "blocks"
        rate = p_last * (block["costPerHour"] / block["cost"]) / 3600.0
    else:
        elapsed = (t_last - start).total_seconds()
        if elapsed <= 0:
            return None
        basis = "window"
        rate = p_last / elapsed

    last_activity = block["last"] if block and block["last"] else None
    # active_block None: ccusage was not asked or failed (unknown).
    # active_block {}: ccusage says there is no active block (no Claude use
    # in the last 5 hours on this Mac).
    idle = bool(last_activity and (now - last_activity).total_seconds() > IDLE_SECONDS) or \
        bool(active_block == {} and (now - t_last).total_seconds() > IDLE_SECONDS)

    # Project from the newest of the last reading and now, at that pace.
    ref = t_last
    projected = p_last + rate * max(0.0, (reset - ref).total_seconds())
    hits_at = None
    if p_last >= 100:
        hits_at = t_last
    elif rate > 0 and projected >= 100:
        hits_at = ref + datetime.timedelta(seconds=(100.0 - p_last) / rate)

    elapsed_now = (now - start).total_seconds()
    thin = elapsed_now < MIN_ELAPSED_FOR_WARNING or p_last < MIN_PCT_FOR_WARNING
    warn = bool(hits_at is not None and hits_at < reset and not idle and not thin) or p_last >= 100
    return {
        "currentPct": round(p_last, 1),
        "asOf": iso(t_last),
        "ratePerHour": round(rate * 3600, 1),
        "projectedPct": round(min(projected, 999.0), 1),
        "hitsLimitAt": iso(hits_at),
        "resetAt": iso(reset),
        "windowStart": iso(start),
        "basis": basis,
        "readings": len(pts),
        "idle": idle,
        "thin": thin,
        "warn": warn,
        "block": {
            "costUsd": round(block["cost"], 4),
            "costPerHour": round(block["costPerHour"], 4) if block["costPerHour"] is not None else None,
            "projectedCostUsd": round(float(block["projectedCost"]), 4) if block["projectedCost"] is not None else None,
            "lastActivity": iso(block["last"]),
            "start": iso(block["start"]),
        } if block else None,
    }
