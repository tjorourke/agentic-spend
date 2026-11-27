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


TOKEN_KINDS = ("input", "output", "cacheWrite", "cacheRead")


def _split(breakdowns):
    """The four token kinds ccusage reports per model, summed (1.8.4). They
    do not overlap, so they add up to ccusage's totalTokens."""
    out = dict.fromkeys(TOKEN_KINDS, 0)
    for mb in breakdowns or []:
        out["input"] += mb.get("inputTokens") or 0
        out["output"] += mb.get("outputTokens") or 0
        out["cacheWrite"] += mb.get("cacheCreationTokens") or 0
        out["cacheRead"] += mb.get("cacheReadTokens") or 0
    return out


def _add_split(into, more):
    for k in TOKEN_KINDS:
        into[k] += more.get(k) or 0
    return into


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


def week_spend(payload, start, end, peaks):
    """Real, covered and not-known spend for a finished week, from what was
    recorded during that week, never from a plan's status today.

    ``peaks`` is {plan key: highest weekly % read during the week} (from
    plan_peaks). A Claude account or Codex counts as covered when readings
    that week stayed under 100%; at 100% extra usage may have been billed, and
    with no readings we can't tell, so both are "not known" rather than a
    guess. Grok CLI with a known plan, OpenCode backends you've marked as
    included, and manual entries marked covered keep their own rules, because
    those are settings rather than readings. Anything else is real spend."""
    sources = payload.get("sources") or []
    has_grok_cli = any(s.get("kind") == "local-live" for s in sources)
    plan_known = _grok_plan_known(payload)
    real = covered = unknown = 0.0
    notes = []

    def note(label, status, cost, peak=None):
        if cost > 0.005:
            notes.append({"label": label, "status": status, "cost": round(cost, 2),
                          "peakPct": round(peak, 1) if peak is not None else None})

    for s in sources:
        if s.get("grokBot"):
            continue
        if has_grok_cli and s.get("tool") == "grok" and s.get("kind") in ("manual", "none", "unofficial-live"):
            continue
        if s.get("kind") == "backends":
            r, c, u = opencode_backends.real_and_covered(
                payload.get("daily"), s.get("opencodeBackends"), lambda p: _in(p, start, end))
            real, covered, unknown = real + r, covered + c, unknown + u
            note("OpenCode", "split", r + c + u)
            continue
        estimate = _source_estimate(s, payload, start, end)
        tool = s.get("tool")
        label = s.get("label") or tool
        on_plan = (tool == "claude" and s.get("maxUsage")) or (tool == "codex" and s.get("kind") == "official-live")
        if on_plan:
            key = "claude:" + str(s.get("id")) if tool == "claude" else "codex:primary"
            peak = peaks.get(key)
            if peak is None:
                unknown += estimate
                note(label, "no-readings", estimate)
            elif peak >= 100:
                unknown += estimate
                note(label, "hit-limit", estimate, peak)
            else:
                covered += estimate
                note(label, "covered", estimate, peak)
            continue
        if (s.get("kind") == "local-live" and plan_known and s.get("costKnown") is not False) \
                or (s.get("kind") == "manual" and s.get("coveredByPlan")):
            covered += estimate
            note(label, "covered", estimate)
        else:
            real += estimate
            note(label, "real", estimate)
    return {"real": real, "covered": covered, "unknown": unknown, "notes": notes}


def _week_totals(payload, start, end):
    list_total = 0.0
    tokens = 0
    days = []
    agents = {}
    agent_tokens = {}
    models = {}
    agent_split = {}
    week_split = dict.fromkeys(TOKEN_KINDS, 0)
    for d in payload.get("daily") or []:
        if not _in(d.get("period"), start, end):
            continue
        cost = d.get("totalCost") or 0.0
        list_total += cost
        day_tokens = _real_tokens(d.get("modelBreakdowns"))
        tokens += day_tokens
        day_split = _split(d.get("modelBreakdowns"))
        _add_split(week_split, day_split)
        days.append({"date": d["period"][:10], "cost": round(cost, 2), "tokens": day_tokens, "tok": day_split})
        for sub in d.get("agents") or []:
            name = "Grok CLI" if sub.get("agent") == "grok" else sub.get("agent")
            agents[name] = agents.get(name, 0.0) + (sub.get("totalCost") or 0.0)
            agent_tokens[name] = agent_tokens.get(name, 0) + _real_tokens(sub.get("modelBreakdowns"))
            _add_split(agent_split.setdefault(name, dict.fromkeys(TOKEN_KINDS, 0)), _split(sub.get("modelBreakdowns")))
        for mb in d.get("modelBreakdowns") or []:
            m = models.setdefault(mb.get("modelName") or "unknown", {"cost": 0.0, "tokens": 0, "tok": dict.fromkeys(TOKEN_KINDS, 0)})
            m["cost"] += mb.get("cost") or 0.0
            m["tokens"] += (mb.get("inputTokens") or 0) + (mb.get("outputTokens") or 0)
            _add_split(m["tok"], _split([mb]))
    have = {d["date"] for d in days}
    day = start
    while day <= end:
        if day.isoformat() not in have:
            days.append({"date": day.isoformat(), "cost": 0.0, "tokens": 0, "tok": dict.fromkeys(TOKEN_KINDS, 0)})
        day += datetime.timedelta(days=1)
    return list_total, tokens, sorted(days, key=lambda x: x["date"]), agents, models, agent_tokens, agent_split, week_split


def _agent_rows(payload, start, end, agents, agent_tokens, agent_split):
    """By-agent rows for the report, with Claude Code split by account (the
    same split the Projects and account tabs use). The account rows are scaled
    to the Claude total so the table still adds up to the week's figure."""
    rows = []
    for k, v in sorted(agents.items(), key=lambda kv: -kv[1]):
        if not (v > 0 or agent_tokens.get(k, 0) > 0):
            continue
        base = {"agent": k, "cost": round(v, 2), "tokens": agent_tokens.get(k, 0),
                "tok": agent_split.get(k, dict.fromkeys(TOKEN_KINDS, 0))}
        if k != "claude":
            rows.append(base)
            continue
        per = {}
        for s in payload.get("accountSessions") or []:
            if (s.get("agent") or "claude") != "claude" or not _in(s.get("period"), start, end):
                continue
            label = s.get("label") or "Unassigned"
            b = per.setdefault(label, {"cost": 0.0})
            b["cost"] += s.get("totalCost") or 0.0
        total = sum(b["cost"] for b in per.values())
        if len(per) < 2 or total <= 0:
            rows.append(base)
            continue
        scale = v / total
        for label, b in sorted(per.items(), key=lambda kv: -kv[1]["cost"]):
            share = b["cost"] / total
            tok = {t: round(base["tok"].get(t, 0) * share) for t in TOKEN_KINDS}
            rows.append({"agent": "claude", "account": label, "cost": round(b["cost"] * scale, 2),
                         "tokens": round(base["tokens"] * share), "tok": tok})
    return rows


def _top_projects(payload, start, end, limit=5):
    agg = {}
    for s in payload.get("accountSessions") or []:
        if not _in(s.get("period"), start, end):
            continue
        key = s.get("project") or "Unattributed"
        b = agg.setdefault(key, {"project": key, "cost": 0.0, "sessions": 0, "tokens": 0, "tok": dict.fromkeys(TOKEN_KINDS, 0)})
        b["cost"] += s.get("totalCost") or 0.0
        b["tokens"] += _real_tokens(s.get("modelBreakdowns"))
        _add_split(b["tok"], _split(s.get("modelBreakdowns")))
        b["sessions"] += 1 if s.get("sessions") is None else s["sessions"]
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


def _allowance_samples(allowance, field="weeklyPct"):
    """key -> [(at, pct)] from allowance-history records."""
    out = {}
    for r in allowance or []:
        if r.get("key") and r.get(field) is not None:
            out.setdefault(r["key"], []).append((r.get("at"), r.get(field)))
    return out


def plan_series(payload, start, end, tz, allowance=None):
    """Plan limit through the week, per account: every reading in the week
    from allowance-history.jsonl, plus the older per-tool histories (Claude's
    status line samples, Grok Bot and Codex), oldest first, with the peak."""
    lo, hi = _local_bounds(start, end, tz)
    series = {}

    def put(key, label, tool, at, weekly, session=None, window=None, credits=None):
        dt = insights.parse_time(at)
        if dt is None or not (lo <= dt < hi) or (weekly is None and session is None):
            return
        s = series.setdefault(key, {"key": key, "label": label, "tool": tool, "windowSeconds": window,
                                    "points": {}, "credits": None})
        if label and not s.get("label"):
            s["label"] = label
        if window and not s.get("windowSeconds"):
            s["windowSeconds"] = window
        iso = dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
        pt = s["points"].setdefault(iso, {"at": iso, "weekly": None, "session": None})
        if weekly is not None:
            pt["weekly"] = round(float(weekly), 1)
        if session is not None:
            pt["session"] = round(float(session), 1)
        if credits:
            s["credits"] = credits

    labels = {"claude:" + m["accountUuid"]: "Claude: " + (m.get("label") or m["accountUuid"][:8])
              for m in payload.get("maxUsage") or [] if m.get("accountUuid")}
    for r in allowance or []:
        key = r.get("key")
        if not key:
            continue
        credits = ({"used": r.get("creditsUsed"), "limit": r.get("creditsLimit"), "currency": r.get("creditsCurrency")}
                   if r.get("creditsUsed") is not None else None)
        label = labels.get(key) or ("Claude: " + r["label"] if r.get("tool") == "claude" and r.get("label") else r.get("label"))
        put(key, label, r.get("tool"), r.get("at"), r.get("weeklyPct"), r.get("sessionPct"),
            r.get("windowSeconds"), credits)
    for h in (payload.get("usageHistory") or {}).get("claude") or []:
        uuid = h.get("accountUuid")
        if uuid:
            put("claude:" + uuid, labels.get("claude:" + uuid) or "Claude: " + (h.get("label") or uuid[:8]), "claude",
                h.get("time"), h.get("sevenDayPercent"), h.get("fiveHourPercent"), 604800)
    for a in (payload.get("grokBot") or {}).get("accounts") or []:
        key = "grok-bot:" + str(a.get("accountId") or "default")
        label = "Grok Bot" + (" (%s)" % a["plan"] if a.get("plan") else "")
        for h in a.get("history") or []:
            put(key, label, "grok-bot", h.get("at"), h.get("weeklyPct"), None, 604800)
    for h in (payload.get("usageHistory") or {}).get("codex") or []:
        put("codex:primary", "Codex", "codex", h.get("time"), h.get("percent"), None, h.get("windowSeconds"))
    out = []
    for s in series.values():
        pts = sorted(s["points"].values(), key=lambda p: p["at"])
        weekly = [p["weekly"] for p in pts if p["weekly"] is not None]
        session = [p["session"] for p in pts if p["session"] is not None]
        out.append(dict(s, points=pts,
                        peakWeeklyPct=max(weekly) if weekly else None,
                        peakSessionPct=max(session) if session else None))
    out.sort(key=lambda s: (-(s.get("peakWeeklyPct") or 0), s.get("label") or ""))
    return out


def seat_value_week(payload, start, end):
    """Seat value for the week: usage value at API prices per subscription
    account vs its seat price prorated to the week (days / days in month).
    Only accounts with a seat price and some usage in the week."""
    import seat_prices
    try:
        rows = seat_prices.team_value(
            payload.get("maxUsage"), payload.get("accountSessions"), payload.get("seatPrices") or {},
            detections=payload.get("planDetection"), daily=payload.get("daily"),
            table=payload.get("planPrices"), region_currency=(payload.get("pricingRegion") or {}).get("currency"),
            today=end, start=start, end=end)
    except Exception:
        return []
    return [r for r in rows if r.get("seat") and (r.get("apiEquivalentUsd") or 0) > 0]


def plan_peaks(payload, start, end, tz, allowance=None):
    """Highest reading recorded during the week for each plan, plus the week
    before, where history has readings for it."""
    extra = _allowance_samples(allowance)
    lo, hi = _local_bounds(start, end, tz)
    plo, phi = _local_bounds(start - datetime.timedelta(days=7), start - datetime.timedelta(days=1), tz)
    rows = []

    done = set()

    def add(key, label, samples, window_peaks=None):
        done.add(key)
        samples = list(samples) + extra.get(key, [])
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
    names = {r.get("key"): r.get("label") for r in allowance or [] if r.get("key")}
    for key in sorted(k for k in extra if k not in done):
        label = labels.get(key.split(":", 1)[1]) if key.startswith("claude:") else None
        add(key, ("Claude: " + label) if label else (names.get(key) or key), [])
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


def build_digest(payload, today=None, tz=None, now=None, week=None, allowance=None):
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

    list_total, tokens, days, agents, models, agent_tokens, agent_split, week_split = _week_totals(payload, start, end)
    prev_list, prev_tokens = _week_totals(payload, pstart, pend)[:2]
    earliest = min((d.get("period", "")[:10] for d in payload.get("daily") or [] if d.get("period")), default=None)
    has_prev = bool(earliest) and earliest <= pend.isoformat()
    plans = plan_peaks(payload, start, end, tz, allowance)
    split = week_spend(payload, start, end, {r["key"]: r["peakPct"] for r in plans})
    real, covered = split["real"], split["covered"]
    # Whatever no source accounts for (rounding, or usage no source claims)
    # is shown as not known rather than quietly counted as covered.
    unknown = max(0.0, list_total - real - covered)

    earliest_start = None
    if earliest:
        e = datetime.date.fromisoformat(earliest)
        earliest_start = e - datetime.timedelta(days=e.weekday())
    has_data = any(d["cost"] or d["tokens"] for d in days) or bool(list_total)
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
            "covered": round(covered, 2),
            "unknown": round(unknown, 2),
            "notes": split["notes"],
            "basis": "readings-that-week",
            "previousList": round(prev_list, 2) if has_prev else None,
            "changePct": insights.pct_change(list_total, prev_list) if has_prev else None,
        },
        "tokens": {
            "total": tokens,
            "split": week_split,
            "previous": prev_tokens if has_prev else None,
            "changePct": insights.pct_change(tokens, prev_tokens) if has_prev else None,
        },
        "days": days,
        "agents": _agent_rows(payload, start, end, agents, agent_tokens, agent_split),
        "topModels": [{"model": k, "cost": round(v["cost"], 2), "tokens": v["tokens"], "tok": v["tok"]}
                      for k, v in sorted(models.items(), key=lambda kv: -kv[1]["cost"])[:5]],
        "topProjects": _top_projects(payload, start, end),
        "plans": plans,
        "allowance": plan_series(payload, start, end, tz, allowance),
        "seatValue": seat_value_week(payload, start, end),
    }
