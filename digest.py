#!/usr/bin/env python3
"""Weekly digest: a summary of the last complete Monday-to-Sunday week.

Built from the same /api/usage payload the dashboard uses, so every figure
here matches what the Overview shows for that date range. Pure functions,
stdlib only, Python 3.9 compatible.
"""
import opencode_backends
import datetime

import insights


def last_complete_week(today):
    """(monday, sunday) of the most recent week that has fully ended. On a
    Monday that is the week that ended yesterday."""
    this_monday = today - datetime.timedelta(days=today.weekday())
    start = this_monday - datetime.timedelta(days=7)
    return start, start + datetime.timedelta(days=6)


def _in(period, start, end):
    return bool(period) and start.isoformat() <= period[:10] <= end.isoformat()


def _real_tokens(breakdowns):
    return sum((mb.get("inputTokens") or 0) + (mb.get("outputTokens") or 0) for mb in breakdowns or [])


def _grok_plan_known(payload):
    plans = payload.get("plans") or {}
    grok = plans.get("grok") or {}
    if grok.get("plan") or grok.get("coveredByPlan"):
        return True
    for a in (payload.get("grokBot") or {}).get("accounts") or []:
        if a.get("plan") or a.get("planLabel"):
            return True
    for s in payload.get("sources") or []:
        if (s.get("tool") == "grok" or s.get("grokBot")) and s.get("plan"):
            return True
        if (s.get("grokBilling") or {}).get("plan"):
            return True
    return False


def _source_estimate(source, payload, start, end):
    if source.get("grokBot"):
        return 0.0
    if source.get("tool") == "claude":
        return sum(r.get("totalCost") or 0.0 for r in payload.get("accountSessions") or []
                   if r.get("accountUuid") == source.get("id") and _in(r.get("period"), start, end))
    agent = "grok" if source.get("tool") == "grok-cli" else source.get("tool")
    total = 0.0
    for d in payload.get("daily") or []:
        if not _in(d.get("period"), start, end):
            continue
        for sub in d.get("agents") or []:
            if sub.get("agent") == agent:
                total += sub.get("totalCost") or 0.0
    return total


def real_spend(payload, start, end):
    """Same covered-versus-real rules as the Overview's cost card, applied to
    one date range. Coverage reflects each plan's status now."""
    sources = payload.get("sources") or []
    has_grok_cli = any(s.get("kind") == "local-live" for s in sources)
    plan_known = _grok_plan_known(payload)
    real = 0.0
    for s in sources:
        if s.get("grokBot"):
            continue
        if has_grok_cli and s.get("tool") == "grok" and s.get("kind") in ("manual", "none"):
            continue
        if has_grok_cli and s.get("tool") == "grok" and s.get("kind") == "unofficial-live":
            continue
        if s.get("kind") == "backends":
            r, _covered, unknown = opencode_backends.real_and_covered(
                payload.get("daily"), s.get("opencodeBackends"), lambda p: _in(p, start, end))
            real += r + unknown
            continue
        estimate = _source_estimate(s, payload, start, end)
        mu = s.get("maxUsage") or {}
        covered = (
            (s.get("tool") == "claude" and (mu.get("usageCredits") or {}).get("usedMinor") == 0)
            or (s.get("tool") == "codex" and s.get("kind") == "official-live"
                and (s.get("codexLive") or {}).get("ordinaryUsageAllowed") is not False)
            or (s.get("tool") == "grok" and s.get("kind") == "unofficial-live"
                and ((s.get("grokBilling") or {}).get("onDemandUsedUsd") or 0) == 0)
            or (s.get("kind") == "local-live" and plan_known and s.get("costKnown") is not False)
            or (s.get("kind") == "manual" and s.get("coveredByPlan"))
        )
        if not covered:
            real += estimate
    return real


def _week_totals(payload, start, end):
    list_total = 0.0
    tokens = 0
    days = []
    agents = {}
    models = {}
    for d in payload.get("daily") or []:
        if not _in(d.get("period"), start, end):
            continue
        cost = d.get("totalCost") or 0.0
        list_total += cost
        tokens += _real_tokens(d.get("modelBreakdowns"))
        days.append({"date": d["period"][:10], "cost": round(cost, 2)})
        for sub in d.get("agents") or []:
            name = "Grok CLI" if sub.get("agent") == "grok" else sub.get("agent")
            agents[name] = agents.get(name, 0.0) + (sub.get("totalCost") or 0.0)
        for mb in d.get("modelBreakdowns") or []:
            m = models.setdefault(mb.get("modelName") or "unknown", {"cost": 0.0, "tokens": 0})
            m["cost"] += mb.get("cost") or 0.0
            m["tokens"] += (mb.get("inputTokens") or 0) + (mb.get("outputTokens") or 0)
    return list_total, tokens, sorted(days, key=lambda x: x["date"]), agents, models


def _top_projects(payload, start, end, limit=5):
    agg = {}
    for s in payload.get("accountSessions") or []:
        if not _in(s.get("period"), start, end):
            continue
        key = s.get("project") or "Unattributed"
        b = agg.setdefault(key, {"project": key, "cost": 0.0, "sessions": 0})
        b["cost"] += s.get("totalCost") or 0.0
        b["sessions"] += s.get("sessions") or 1
    rows = sorted(agg.values(), key=lambda r: -r["cost"])[:limit]
    for r in rows:
        r["cost"] = round(r["cost"], 2)
    return rows


def _local_bounds(start, end, tz):
    lo = datetime.datetime.combine(start, datetime.time.min).replace(tzinfo=tz)
    hi = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min).replace(tzinfo=tz)
    return lo, hi


def _peak_in(samples, lo, hi):
    best = None
    for t, p in samples:
        dt = insights.parse_time(t)
        if dt is None or p is None or not (lo <= dt < hi):
            continue
        best = float(p) if best is None else max(best, float(p))
    return best


def plan_peaks(payload, start, end, tz):
    """Highest reading recorded during the week for each plan, plus the week
    before, where history has readings for it."""
    lo, hi = _local_bounds(start, end, tz)
    plo, phi = _local_bounds(start - datetime.timedelta(days=7), start - datetime.timedelta(days=1), tz)
    rows = []

    def add(key, label, samples, window_peaks=None):
        peak = _peak_in(samples, lo, hi)
        prev = _peak_in(samples, plo, phi)
        for w in window_peaks or []:
            last = insights.parse_time(w.get("lastReadingAt") or w.get("lastAt"))
            if last is None or w.get("peakPct") is None:
                continue
            if lo <= last < hi:
                peak = max(peak or 0.0, float(w["peakPct"]))
            elif plo <= last < phi:
                prev = max(prev or 0.0, float(w["peakPct"]))
        if peak is None:
            return
        rows.append({
            "key": key, "label": label,
            "peakPct": round(peak, 1),
            "previousPeakPct": round(prev, 1) if prev is not None else None,
            "deltaPct": round(peak - prev, 1) if prev is not None else None,
        })

    for a in (payload.get("grokBot") or {}).get("accounts") or []:
        samples = [(h.get("at"), h.get("weeklyPct")) for h in a.get("history") or []]
        label = "Grok Bot" + (" (%s)" % a["plan"] if a.get("plan") else "")
        add("grok-bot:" + str(a.get("accountId") or "default"), label, samples, a.get("windows"))

    claude_hist = (payload.get("usageHistory") or {}).get("claude") or []
    labels = {m.get("accountUuid"): m.get("label") for m in payload.get("maxUsage") or []}
    for uuid in sorted({s.get("accountUuid") for s in claude_hist if s.get("accountUuid")}):
        samples = [(s.get("time"), s.get("sevenDayPercent")) for s in claude_hist if s.get("accountUuid") == uuid]
        hist_label = next((s.get("label") for s in claude_hist if s.get("accountUuid") == uuid and s.get("label")), None)
        add("claude:" + uuid, "Claude: " + (labels.get(uuid) or hist_label or uuid[:8]), samples)

    codex_hist = (payload.get("usageHistory") or {}).get("codex") or []
    if codex_hist:
        add("codex:primary", "Codex", [(s.get("time"), s.get("percent")) for s in codex_hist])
    return rows


def parse_week(value, latest_start):
    """A ?week=YYYY-MM-DD value -> the Monday that starts that week. Any day
    of the week is accepted. Weeks after the last complete one are refused."""
    if value in (None, ""):
        return None
    try:
        day = datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("week must be a date like 2026-11-16")
    monday = day - datetime.timedelta(days=day.weekday())
    if monday > latest_start:
        raise ValueError("that week has not finished yet")
    return monday


def build_digest(payload, today=None, tz=None, now=None, week=None):
    """``week`` (a Monday) picks an earlier complete week; default is the
    last complete one."""
    tz = tz or datetime.datetime.now().astimezone().tzinfo
    today = today or datetime.datetime.now(tz=tz).date()
    latest_start, _latest_end = last_complete_week(today)
    if week is not None:
        start, end = week, week + datetime.timedelta(days=6)
    else:
        start, end = latest_start, _latest_end
    pstart, pend = start - datetime.timedelta(days=7), end - datetime.timedelta(days=7)

    list_total, tokens, days, agents, models = _week_totals(payload, start, end)
    prev_list, prev_tokens, _, _, _ = _week_totals(payload, pstart, pend)
    earliest = min((d.get("period", "")[:10] for d in payload.get("daily") or [] if d.get("period")), default=None)
    has_prev = bool(earliest) and earliest <= pend.isoformat()
    real = real_spend(payload, start, end)

    earliest_start = None
    if earliest:
        e = datetime.date.fromisoformat(earliest)
        earliest_start = e - datetime.timedelta(days=e.weekday())
    has_data = bool(days) or bool(list_total)
    return {
        "hasData": has_data,
        "latestWeekStart": latest_start.isoformat(),
        "earliestWeekStart": earliest_start.isoformat() if earliest_start else None,
        "weekStart": start.isoformat(),
        "weekEnd": end.isoformat(),
        "previousWeekStart": pstart.isoformat(),
        "previousWeekEnd": pend.isoformat(),
        "generatedAt": (now or datetime.datetime.now(tz=tz)).isoformat(timespec="seconds"),
        "spend": {
            "list": round(list_total, 2),
            "real": round(real, 2),
            "covered": round(max(0.0, list_total - real), 2),
            "previousList": round(prev_list, 2) if has_prev else None,
            "changePct": insights.pct_change(list_total, prev_list) if has_prev else None,
        },
        "tokens": {
            "total": tokens,
            "previous": prev_tokens if has_prev else None,
            "changePct": insights.pct_change(tokens, prev_tokens) if has_prev else None,
        },
        "days": days,
        "agents": [{"agent": k, "cost": round(v, 2)} for k, v in sorted(agents.items(), key=lambda kv: -kv[1]) if v > 0],
        "topModels": [{"model": k, "cost": round(v["cost"], 2), "tokens": v["tokens"]}
                      for k, v in sorted(models.items(), key=lambda kv: -kv[1]["cost"])[:5]],
        "topProjects": _top_projects(payload, start, end),
        "plans": plan_peaks(payload, start, end, tz),
    }
