#!/usr/bin/env python3
"""Read Grok CLI session usage from ~/.grok/sessions.

Each session directory is ``<url-encoded-cwd>/<session-uuid>/`` and holds
``usage.json``. Grok's own user guide (``grok usage``) says ``costUsdTicks``
is 10^10 ticks per USD. Turns do not carry a cost, so a session total is
split across its turns by token share and marked apportioned.

Session files do not record which account was logged in. ``attribute_sessions``
keeps a small map in the data directory: a session whose ``updatedAt`` is
newer than the last watermark is tagged with the account that is signed in
now. Older sessions stay "Unattributed Grok account" until a later update.
"""
import datetime
import json
import os
import urllib.parse

import data_dir

GROK_HOME = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
ATTRIBUTION_PATH = data_dir.path("grok-account-map.json")
TICKS_PER_USD = 10 ** 10  # grok user guide: 1 USD = 10^10 costUsdTicks
UNATTRIBUTED_ID = "grok-unattributed"
UNATTRIBUTED_LABEL = "Unattributed Grok account"
COST_NOTE = (
    "USD from Grok CLI costUsdTicks (1 USD = 10^10 ticks, grok usage docs). "
    "Turns do not store a cost, so the session total is split across turns by token share."
)


def ticks_to_usd(ticks):
    if ticks is None:
        return None
    try:
        return float(ticks) / TICKS_PER_USD
    except (TypeError, ValueError):
        return None


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _local_day(iso):
    if not iso or not isinstance(iso, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone().date().isoformat()


def _parse_updated(iso):
    if not iso or not isinstance(iso, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _uncached_input(row):
    """Grok's inputTokens include cache read and cache write. ccusage splits
    those out, and the dashboard's input column is the uncached remainder."""
    inp = _num(row.get("inputTokens"))
    cached = _num(row.get("cachedReadTokens"))
    created = _num(row.get("cacheCreationTokens"))
    return max(0.0, inp - cached - created)


def _find_model_usage(obj, found):
    if isinstance(obj, dict):
        usage = obj.get("modelUsage")
        if isinstance(usage, dict):
            found.append(usage)
        for value in obj.values():
            _find_model_usage(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _find_model_usage(value, found)


def _model_weights(session_dir):
    """{model_name: token weight} from signals.json and updates.jsonl.

    Empty when neither file names a model. Callers then use a single
    "grok" bucket.
    """
    weights = {}
    signals = _load_json(os.path.join(session_dir, "signals.json")) or {}
    named = []
    models_used = signals.get("modelsUsed")
    if isinstance(models_used, list):
        named = [m for m in models_used if isinstance(m, str) and m]
    elif isinstance(models_used, str) and models_used:
        named = [models_used]

    updates_path = os.path.join(session_dir, "updates.jsonl")
    blobs = []
    try:
        with open(updates_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    blobs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    found = []
    for blob in blobs:
        _find_model_usage(blob, found)
        model_id = blob.get("modelId")
        if isinstance(model_id, str) and model_id:
            named.append(model_id)
    for usage in found:
        for name, row in usage.items():
            if not isinstance(name, str):
                continue
            if isinstance(row, dict):
                weight = _num(row.get("inputTokens")) + _num(row.get("outputTokens"))
                if weight <= 0:
                    weight = _num(row.get("totalTokens")) or 1.0
            else:
                weight = 1.0
            weights[name] = weights.get(name, 0.0) + weight
    if weights:
        return weights
    if len(named) == 1:
        return {named[0]: 1.0}
    return {}


def _split_models(model_name, weights, uncached, output, cache_read, cache_create, cost):
    if not weights:
        return [{
            "modelName": model_name,
            "cost": cost or 0.0,
            "inputTokens": uncached,
            "outputTokens": output,
            "cacheCreationTokens": cache_create,
            "cacheReadTokens": cache_read,
            "costKnown": cost is not None,
        }]
    total = sum(weights.values()) or 1.0
    rows = []
    for name, weight in weights.items():
        share = weight / total
        rows.append({
            "modelName": name,
            "cost": (cost or 0.0) * share,
            "inputTokens": uncached * share,
            "outputTokens": output * share,
            "cacheCreationTokens": cache_create * share,
            "cacheReadTokens": cache_read * share,
            "costKnown": cost is not None,
        })
    return rows


def iter_session_dirs(root):
    sessions = os.path.join(root, "sessions")
    if not os.path.isdir(sessions):
        return
    for cwd_name in os.listdir(sessions):
        cwd_dir = os.path.join(sessions, cwd_name)
        if not os.path.isdir(cwd_dir):
            continue
        project = urllib.parse.unquote(cwd_name)
        for session_id in os.listdir(cwd_dir):
            session_dir = os.path.join(cwd_dir, session_id)
            usage_path = os.path.join(session_dir, "usage.json")
            if os.path.isfile(usage_path):
                yield project, session_id, session_dir, usage_path


def load_attribution(path):
    raw = _load_json(path) or {}
    sessions = raw.get("sessions") if isinstance(raw.get("sessions"), dict) else {}
    return {"watermark": raw.get("watermark"), "sessions": sessions}


def save_attribution(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"watermark": state.get("watermark"), "sessions": state.get("sessions") or {}}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def attribute_sessions(records, state, current_email):
    """Tag sessions updated after the stored watermark with current_email.

    ``records`` is a list of {sessionId, updatedAt}. Already tagged sessions
    keep their email until a newer updatedAt crosses the watermark. The
    returned state is safe to persist. The first run has no watermark, so
    existing files stay unattributed.
    """
    sessions = dict(state.get("sessions") or {})
    watermark = _parse_updated(state.get("watermark"))
    newest = watermark
    tagged = {}
    for rec in records:
        session_id = rec.get("sessionId")
        updated = _parse_updated(rec.get("updatedAt"))
        if not session_id:
            continue
        prev = sessions.get(session_id) if isinstance(sessions.get(session_id), dict) else {}
        prev_email = prev.get("email")
        prev_updated = _parse_updated(prev.get("updatedAt"))
        email = prev_email
        if updated is not None and watermark is not None and updated > watermark and current_email:
            email = current_email
        elif not prev_email:
            email = None
        sessions[session_id] = {
            "email": email,
            "updatedAt": rec.get("updatedAt"),
        }
        tagged[session_id] = email
        if updated is not None and (newest is None or updated > newest):
            newest = updated
        # A repeat read of an unchanged file must not move the watermark
        # backwards, and must not retag it. prev_updated is kept for clarity.
        _ = prev_updated
    watermark_out = newest.isoformat() if newest is not None else state.get("watermark")
    return {"watermark": watermark_out, "sessions": sessions}, tagged


def turns_from_usage(usage, project, weights):
    """One dict per turn: tokens, local day, apportioned cost, model rows."""
    session = usage.get("session") if isinstance(usage.get("session"), dict) else {}
    turns = usage.get("turns") if isinstance(usage.get("turns"), list) else []
    session_tokens = _num(session.get("totalTokens")) or sum(_num(t.get("totalTokens")) for t in turns if isinstance(t, dict))
    session_cost = ticks_to_usd(session.get("costUsdTicks"))
    cost_known = session_cost is not None
    rows = []
    usable = [t for t in turns if isinstance(t, dict)]
    if not usable and session:
        usable = [session]
    for turn in usable:
        ended = turn.get("endedAt") or usage.get("updatedAt")
        day = _local_day(ended)
        if not day:
            continue
        total = _num(turn.get("totalTokens"))
        if session_cost is None:
            cost = None
        elif session_tokens > 0:
            cost = session_cost * (total / session_tokens)
        elif usable:
            cost = session_cost / len(usable)
        else:
            cost = session_cost
        uncached = _uncached_input(turn)
        output = _num(turn.get("outputTokens"))
        cache_read = _num(turn.get("cachedReadTokens"))
        cache_create = _num(turn.get("cacheCreationTokens"))
        rows.append({
            "period": day,
            "project": project,
            "sessionId": usage.get("sessionId"),
            "updatedAt": usage.get("updatedAt"),
            "inputTokens": uncached,
            "outputTokens": output,
            "cacheReadTokens": cache_read,
            "cacheCreationTokens": cache_create,
            "reasoningTokens": _num(turn.get("reasoningTokens")),
            "totalTokens": total,
            "costUsd": cost,
            "costKnown": cost_known,
            "models": _split_models(weights and next(iter(weights), "grok") or "grok", weights, uncached, output, cache_read, cache_create, cost),
        })
    return rows


def read_session_file(path, project, session_dir):
    usage = _load_json(path)
    if not isinstance(usage, dict):
        return []
    weights = _model_weights(session_dir)
    return turns_from_usage(usage, project, weights)


def collect(root=None, attribution_path=None, current_email=None):
    """Read every usage.json under root and apply account attribution.

    Returns days (for the daily chart), session rows (for Projects and
    account cards), and whether any session file was found. Does not raise
    if the Grok home directory is missing.
    """
    root = root or GROK_HOME
    attribution_path = attribution_path or ATTRIBUTION_PATH
    state = load_attribution(attribution_path)
    records = []
    parsed = []
    if os.path.isdir(root):
        for project, session_id, session_dir, usage_path in iter_session_dirs(root):
            usage = _load_json(usage_path) or {}
            records.append({"sessionId": usage.get("sessionId") or session_id, "updatedAt": usage.get("updatedAt")})
            parsed.append((usage.get("sessionId") or session_id, read_session_file(usage_path, project, session_dir)))
    new_state, tagged = attribute_sessions(records, state, current_email)
    if parsed:
        save_attribution(attribution_path, new_state)

    days = {}
    sessions = {}
    for session_id, turns in parsed:
        email = tagged.get(session_id)
        account_id = f"grok:{email}" if email else UNATTRIBUTED_ID
        label = email or UNATTRIBUTED_LABEL
        for turn in turns:
            bucket = days.setdefault(turn["period"], {
                "period": turn["period"], "totalCost": 0.0, "costKnown": True, "models": {},
            })
            if not turn["costKnown"]:
                bucket["costKnown"] = False
            if turn["costUsd"] is not None:
                bucket["totalCost"] += turn["costUsd"]
            for model in turn["models"]:
                slot = bucket["models"].setdefault(model["modelName"], {
                    "modelName": model["modelName"], "cost": 0.0,
                    "inputTokens": 0.0, "outputTokens": 0.0,
                    "cacheCreationTokens": 0.0, "cacheReadTokens": 0.0,
                })
                slot["cost"] += model["cost"]
                slot["inputTokens"] += model["inputTokens"]
                slot["outputTokens"] += model["outputTokens"]
                slot["cacheCreationTokens"] += model["cacheCreationTokens"]
                slot["cacheReadTokens"] += model["cacheReadTokens"]
            key = (turn["period"], turn["project"], account_id)
            row = sessions.setdefault(key, {
                "period": turn["period"],
                "project": turn["project"] or "Unattributed (no cwd recorded)",
                "accountUuid": account_id,
                "label": label,
                "agent": "grok",
                "totalCost": 0.0,
                "totalTokens": 0.0,
                "costKnown": True,
                "modelBreakdowns": {},
                "sessionIds": set(),
                "note": (
                    "Session files do not record the account. "
                    "This one was not updated while an account was signed in here."
                ) if not email else COST_NOTE,
            })
            if turn.get("sessionId"):
                row["sessionIds"].add(turn["sessionId"])
            if not turn["costKnown"]:
                row["costKnown"] = False
            if turn["costUsd"] is not None:
                row["totalCost"] += turn["costUsd"]
            row["totalTokens"] += turn["inputTokens"] + turn["outputTokens"]
            for model in turn["models"]:
                slot = row["modelBreakdowns"].setdefault(model["modelName"], {
                    "modelName": model["modelName"], "cost": 0.0,
                    "inputTokens": 0.0, "outputTokens": 0.0,
                    "cacheCreationTokens": 0.0, "cacheReadTokens": 0.0,
                })
                slot["cost"] += model["cost"]
                slot["inputTokens"] += model["inputTokens"]
                slot["outputTokens"] += model["outputTokens"]
                slot["cacheCreationTokens"] += model["cacheCreationTokens"]
                slot["cacheReadTokens"] += model["cacheReadTokens"]

    day_rows = []
    for period, bucket in sorted(days.items()):
        models = list(bucket["models"].values())
        for model in models:
            for key in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"):
                model[key] = int(round(model[key]))
        day_rows.append({
            "period": period,
            "totalCost": bucket["totalCost"],
            "costKnown": bucket["costKnown"],
            "modelBreakdowns": models,
        })
    session_rows = []
    for row in sessions.values():
        models = list(row.pop("modelBreakdowns").values())
        for model in models:
            for key in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"):
                model[key] = int(round(model[key]))
        row["modelBreakdowns"] = models
        row["totalTokens"] = int(round(row["totalTokens"]))
        row["sessions"] = len(row.pop("sessionIds", set())) or 1
        session_rows.append(row)
    return {
        "found": bool(parsed),
        "days": day_rows,
        "sessions": session_rows,
        "costNote": COST_NOTE,
    }


def _subtract_models(existing, removed):
    leftover = []
    for row in existing or []:
        copy = dict(row)
        for gone in removed:
            if copy.get("modelName") != gone.get("modelName"):
                continue
            for key in ("cost", "inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"):
                copy[key] = (copy.get(key) or 0) - (gone.get(key) or 0)
        if (copy.get("cost") or 0) > 1e-9 or (copy.get("inputTokens") or 0) > 0 or (copy.get("outputTokens") or 0) > 0:
            leftover.append(copy)
    return leftover


def merge_daily(daily_rows, grok_days):
    """Replace any ccusage grok slice with rows from usage.json.

    ccusage may already have priced the same sessions. Keeping both would
    count Grok twice. Days that only contained that ccusage slice are
    dropped when usage.json has nothing for them, then usage.json days are
    added back.
    """
    merged = []
    for day in daily_rows or []:
        agents = day.get("agents") or []
        grok = [a for a in agents if a.get("agent") == "grok"]
        rest = [a for a in agents if a.get("agent") != "grok"]
        if grok:
            removed_cost = sum(a.get("totalCost") or 0 for a in grok)
            removed_models = []
            for agent in grok:
                removed_models.extend(agent.get("modelBreakdowns") or [])
            day = dict(day)
            day["agents"] = rest
            day["totalCost"] = max(0.0, (day.get("totalCost") or 0) - removed_cost)
            if removed_models:
                day["modelBreakdowns"] = _subtract_models(day.get("modelBreakdowns") or [], removed_models)
            meta = dict(day.get("metadata") or {})
            meta["agents"] = [a for a in (meta.get("agents") or []) if a != "grok"]
            day["metadata"] = meta
        if rest or (day.get("totalCost") or 0) > 1e-9:
            merged.append(day)
    by_period = {d.get("period"): d for d in merged}
    for grok_day in grok_days or []:
        period = grok_day["period"]
        sub = {
            "agent": "grok",
            "totalCost": grok_day.get("totalCost") or 0.0,
            "modelBreakdowns": grok_day.get("modelBreakdowns") or [],
        }
        day = by_period.get(period)
        if day is None:
            day = {
                "period": period,
                "totalCost": 0.0,
                "modelBreakdowns": [],
                "metadata": {"agents": []},
                "agents": [],
            }
            by_period[period] = day
            merged.append(day)
        day["agents"] = list(day.get("agents") or []) + [sub]
        day["totalCost"] = (day.get("totalCost") or 0) + sub["totalCost"]
        day["modelBreakdowns"] = list(day.get("modelBreakdowns") or []) + list(sub["modelBreakdowns"])
        meta = dict(day.get("metadata") or {})
        agents = list(meta.get("agents") or [])
        if "grok" not in agents:
            agents.append("grok")
        meta["agents"] = agents
        day["metadata"] = meta
    merged.sort(key=lambda d: d.get("period") or "")
    return merged
