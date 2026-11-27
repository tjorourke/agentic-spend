#!/usr/bin/env python3
"""How each Claude Code session's usage is spread over days.

ccusage prices a session as a whole and dates it by its last activity, while
`ccusage daily` dates each message by its own day. A session that runs over
several days therefore landed on its last day on the account pages and on
the right days on the Overview, so the two disagreed (for example a week's
account total could be a few dollars off the Overview's Claude total).

This module reads the transcripts once (then only what was appended) and
returns, per session and model, a weight per local day. account_usage uses
the weights to split each session's ccusage cost across the days it ran, so
every page adds up to the same figures. Weights only set proportions; the
dollar amounts always come from ccusage.

Weight per assistant message = input + 5 x output + 1.25 x cache write (5m)
+ 2 x cache write (1h) + 0.1 x cache read, which matches the ratios of every
current Claude model's published prices, so the split is close to exact.
"""
import collections
import datetime
import glob
import json
import os

import data_dir

CACHE_VERSION = 2
CACHE_PATH = data_dir.path("session-days-cache.json")


def message_weight(usage):
    usage = usage or {}
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    cc = usage.get("cache_creation") or {}
    e5 = cc.get("ephemeral_5m_input_tokens")
    e1 = cc.get("ephemeral_1h_input_tokens")
    if e5 is None and e1 is None:
        write = 1.25 * (usage.get("cache_creation_input_tokens") or 0)
    else:
        write = 1.25 * (e5 or 0) + 2.0 * (e1 or 0)
    return inp + 5.0 * out + write + 0.1 * read


def _local_day(ts, tz):
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(tz).date().isoformat()


def read_lines(lines, tz, entries=None):
    """Append [key, day, model, weight, first_ts] for each assistant message
    with usage to ``entries``. Claude Code writes one line per content block,
    each repeating the same usage, so the key (message id + request id) is
    how duplicates are dropped later."""
    entries = [] if entries is None else entries
    for line in lines:
        if '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        day = _local_day(rec.get("timestamp"), tz)
        if not day:
            continue
        w = message_weight(msg["usage"])
        if w <= 0:
            continue
        mid = msg.get("id")
        key = f"{mid}:{rec.get('requestId')}" if mid else None
        entries.append([key, day, msg.get("model") or "unknown", w, str(rec.get("timestamp"))])
    return entries


def _session_id(path, projects_dir):
    rel = os.path.relpath(path, projects_dir).split(os.sep)
    # <project>/<session>.jsonl, or <project>/<session>/subagents/<agent>.jsonl
    # (ccusage counts a session's subagents under the session itself).
    if len(rel) == 2:
        return os.path.splitext(rel[1])[0]
    if len(rel) >= 3:
        return rel[1]
    return None


def _load_cache(path):
    try:
        with open(path) as f:
            c = json.load(f)
        if c.get("version") == CACHE_VERSION and c.get("tz") == _tz_name():
            return c.get("files") or {}
    except (OSError, ValueError):
        pass
    return {}


def _tz_name():
    return str(datetime.datetime.now().astimezone().utcoffset())


def collect(projects_dir=None, cache_path=None, tz=None):
    """{sessionId: {day: {model: weight}}} for every transcript, reading only
    new bytes of files seen before.

    A resumed session's transcript repeats messages from the one it resumed,
    and ccusage counts each message once, for the file it meets first (files
    in order of their first message). Doing the same here keeps the old
    messages on their own session and days."""
    projects_dir = projects_dir or os.path.expanduser("~/.claude/projects")
    cache_path = cache_path or CACHE_PATH
    tz = tz or datetime.datetime.now().astimezone().tzinfo
    cache = _load_cache(cache_path)
    files = {}
    paths = glob.glob(os.path.join(projects_dir, "*", "*.jsonl")) + \
        glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"))
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        old = cache.get(path)
        if old and old.get("size") == st.st_size and old.get("mtime") == st.st_mtime:
            files[path] = old
            continue
        start, entries = 0, []
        if old and st.st_size > old.get("size", 0):
            start, entries = old.get("size", 0), old.get("entries") or []
        try:
            with open(path, "rb") as f:
                f.seek(start)
                raw = f.read()
        except OSError:
            continue
        # Only whole lines count; a half-written last line is read next time.
        cut = raw.rfind(b"\n") + 1
        entries = read_lines(raw[:cut].decode("utf-8", "ignore").splitlines(), tz, entries)
        files[path] = {"size": start + cut, "mtime": st.st_mtime if cut == len(raw) else 0,
                       "entries": entries}
    try:
        data_dir.atomic_write_json(cache_path, {"version": CACHE_VERSION, "tz": _tz_name(), "files": files})
    except OSError:
        pass
    order = sorted(files, key=lambda p: (min((e[4] for e in files[p]["entries"]), default="~"), p))
    seen = set()
    out = {}
    for path in order:
        sid = _session_id(path, projects_dir)
        for key, day, model, w, _ts in files[path]["entries"]:
            if key:
                if key in seen:
                    continue
                seen.add(key)
            if not sid:
                continue
            d = out.setdefault(sid, {}).setdefault(day, {})
            d[model] = d.get(model, 0.0) + w
    return out


TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens",
                "cacheCreation1hTokens", "totalTokens")


def split_session(row, last_day, day_weights):
    """One ccusage session row -> [(day, cost, totalTokens, modelBreakdowns)]
    spread over the days it ran. Without weights it stays on ``last_day``."""
    breakdowns = row.get("modelBreakdowns") or []
    if not day_weights or len(day_weights) < 2:
        only = next(iter(day_weights), None) if day_weights else None
        return [(only or last_day, row.get("totalCost", 0.0), row.get("totalTokens", 0), breakdowns)]
    all_models = {}
    for day, by_model in day_weights.items():
        all_models[day] = sum(by_model.values())
    days = {}
    for mb in breakdowns:
        name = mb.get("modelName")
        per_day = {d: by_model.get(name, 0.0) for d, by_model in day_weights.items()}
        if sum(per_day.values()) <= 0:
            per_day = dict(all_models)
        total = sum(per_day.values()) or 1.0
        for day, w in per_day.items():
            if w <= 0:
                continue
            share = w / total
            part = dict(mb)
            part["cost"] = (mb.get("cost") or 0.0) * share
            for k in TOKEN_FIELDS:
                if isinstance(mb.get(k), (int, float)):
                    part[k] = round(mb[k] * share)
            days.setdefault(day, []).append(part)
    if not days:
        return [(last_day, row.get("totalCost", 0.0), row.get("totalTokens", 0), breakdowns)]
    # Cost not in any model breakdown (should not happen) stays on the last day.
    rest = (row.get("totalCost") or 0.0) - sum(p["cost"] for parts in days.values() for p in parts)
    out = []
    for day in sorted(days):
        parts = days[day]
        cost = sum(p["cost"] for p in parts)
        if day == max(days) and abs(rest) > 1e-9:
            cost += rest
        tokens = sum((p.get("inputTokens") or 0) + (p.get("outputTokens") or 0) + (p.get("cacheCreationTokens") or 0)
                     + (p.get("cacheReadTokens") or 0) for p in parts)
        out.append((day, cost, round(tokens), parts))
    return out


def reconcile(sessions, day_totals, iterations=200, tolerance=0.005):
    """Nudge the split so each day adds up to ccusage's own daily figure.

    ``sessions`` is a list of piece lists from split_session, ``day_totals``
    {day: cost} from ccusage daily for the same agent. The weights get each
    day within a dollar or two (ccusage prices some tokens at tiered rates the
    weights can't see); scaling days and sessions in turn (iterative
    proportional fitting) closes that gap while every session keeps exactly
    its ccusage total. Sessions on a single day are never changed. Nothing is
    changed if the two sources don't describe the same total."""
    multi = [i for i, p in enumerate(sessions) if len(p) > 1]
    if not multi or not day_totals:
        return sessions
    fixed = collections.defaultdict(float)
    for p in sessions:
        if len(p) == 1:
            fixed[p[0][0]] += p[0][1]
    cells = {i: {d: c for d, c, _t, _b in sessions[i]} for i in multi}
    days = {d for i in multi for d in cells[i]}
    if any(d not in day_totals for d in days):
        return sessions
    target = {d: day_totals[d] - fixed.get(d, 0.0) for d in days}
    row_total = {i: sum(cells[i].values()) for i in multi}
    if any(v < -tolerance for v in target.values()):
        return sessions
    if abs(sum(target.values()) - sum(row_total.values())) > max(1.0, 0.01 * sum(row_total.values())):
        return sessions
    for _ in range(iterations):
        col = collections.defaultdict(float)
        for i in multi:
            for d, c in cells[i].items():
                col[d] += c
        for i in multi:
            for d in cells[i]:
                if col[d] > 0:
                    cells[i][d] *= max(target[d], 0.0) / col[d]
        for i in multi:
            s = sum(cells[i].values())
            if s > 0:
                for d in cells[i]:
                    cells[i][d] *= row_total[i] / s
        col = collections.defaultdict(float)
        for i in multi:
            for d, c in cells[i].items():
                col[d] += c
        if all(abs(col[d] - target[d]) < tolerance for d in days):
            break
    out = list(sessions)
    for i in multi:
        new = []
        for d, c, t, b in sessions[i]:
            k = cells[i][d] / c if c else 0.0
            new.append((d, cells[i][d], t, [dict(mb, cost=(mb.get("cost") or 0.0) * k) for mb in b]))
        out[i] = new
    return out
