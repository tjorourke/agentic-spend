#!/usr/bin/env python3
"""Local-only dashboard server for `ccusage` CLI-agent spend data.

Run:  python3 server.py
Open: http://127.0.0.1:8787/

Every request to /api/usage (cached briefly) runs ONE `ccusage --sections
daily,session --json --by-agent` call, then joins the session rows against
Claude Code's own session-owner records (see account_usage.py) to add a
per-account breakdown. Binds to 127.0.0.1 only — nothing here leaves this
machine.
"""
import datetime
import http.server
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

import account_usage
import data_dir
import digest
import grok_usage
import grokbot_usage
import insights
import login_item
import notify
import provider_usage
import source_detection
import statusline_setup
import ccusage_bin
import ccusage_sources
import model_mix
import seat_prices
import plan_detection
import plan_prices
import pricing_region
import opencode_backends
import token_sources

PORT = int(os.environ.get("AGENTIC_SPEND_PORT", "8787"))
_LABEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_LABEL_MAX = 80
_NOTE_MAX = 500
_LABEL_BODY_MAX = 8192
CACHE_TTL = 10  # seconds
GROKBOT_SAMPLE_INTERVAL = 10 * 60  # seconds, while the server is running
_sampler_stop = threading.Event()
_sampler_thread = None

CCUSAGE_VERSION = ccusage_bin.CCUSAGE_VERSION  # bundled build; see ccusage_bin.py
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
FAVICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.png")

_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()
_provider_cost_cache = {"ts": 0.0, "data": None}
PROVIDER_COST_TTL = 300  # seconds — real API calls to OpenAI, don't hammer it every 10s
_codex_live_cache = {"ts": 0.0, "data": None}
CODEX_LIVE_TTL = 60  # seconds — spawning `codex app-server` has real overhead, don't do it every request
_token_sources_cache = {}  # (since_iso, until_iso) -> {"ts", "data"}
TOKEN_SOURCES_TTL = 120  # seconds — parsing transcripts takes real time; kept off the 10s /api/usage path entirely
TOKEN_SOURCES_MAX_DAYS = 90
GROK_BILLING_CACHE_PATH = data_dir.path("grok-billing-cache.json")


def _load_grok_billing_cache():
    if os.path.exists(GROK_BILLING_CACHE_PATH):
        try:
            with open(GROK_BILLING_CACHE_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return None


def _save_grok_billing_cache(data):
    with open(GROK_BILLING_CACHE_PATH, "w") as f:
        json.dump(data, f)


_grok_billing_cache = {"ts": 0.0, "data": _load_grok_billing_cache()}  # persisted — manual refresh only, but survives restarts


def get_openai_cost(force=False):
    keys = provider_usage.load_provider_keys()
    api_key = keys.get("openai")
    if not api_key:
        return None
    now = time.time()
    if not force and _provider_cost_cache["data"] is not None and (now - _provider_cost_cache["ts"]) < PROVIDER_COST_TTL:
        return _provider_cost_cache["data"]
    since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    try:
        result = provider_usage.fetch_openai_cost(api_key, since, days=31)
        _provider_cost_cache["data"] = result
        _provider_cost_cache["ts"] = now
        return result
    except Exception as e:
        return {"error": str(e)}


def get_codex_live(force=False):
    now = time.time()
    if not force and _codex_live_cache["data"] is not None and (now - _codex_live_cache["ts"]) < CODEX_LIVE_TTL:
        return _codex_live_cache["data"]
    try:
        snap = provider_usage.get_codex_live_snapshot()
    except Exception:
        snap = None
    _codex_live_cache["data"] = snap
    _codex_live_cache["ts"] = now
    return snap


def parse_token_sources_range(query, today=None):
    """Query string without the leading '?'.

    Returns (since_date, until_date, since_dt, until_dt, capped). until_dt is
    exclusive: the start of the day after until_date, in local time. A span
    longer than TOKEN_SOURCES_MAX_DAYS is clipped to that many days ending
    on until_date, and capped is True.
    """
    today = today or datetime.date.today()
    params = urllib.parse.parse_qs(query or "", keep_blank_values=False)

    def one(name):
        vals = params.get(name) or []
        text = vals[0].strip() if vals and vals[0] else ""
        return text or None

    def parse_day(text, label):
        if text is None:
            return None
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{label} must be YYYY-MM-DD")

    until = parse_day(one("until"), "until") or today
    since = parse_day(one("since"), "since") or until
    if since > until:
        since, until = until, since
    capped = False
    if (until - since).days + 1 > TOKEN_SOURCES_MAX_DAYS:
        since = until - datetime.timedelta(days=TOKEN_SOURCES_MAX_DAYS - 1)
        capped = True
    since_dt = datetime.datetime.combine(since, datetime.time.min).astimezone()
    until_dt = datetime.datetime.combine(until + datetime.timedelta(days=1), datetime.time.min).astimezone()
    return since, until, since_dt, until_dt, capped


def get_token_sources_cached(query=""):
    """Token destinations for one inclusive date range, cached per range.

    Parsing every transcript ever written takes several seconds, so All is
    capped at TOKEN_SOURCES_MAX_DAYS and each range has its own TTL, away
    from /api/usage.
    """
    since, until, since_dt, until_dt, capped = parse_token_sources_range(query)
    key = (since.isoformat(), until.isoformat())
    now = time.time()
    expired = [k for k, entry in _token_sources_cache.items() if now - entry["ts"] >= TOKEN_SOURCES_TTL]
    for old in expired:
        _token_sources_cache.pop(old, None)
    hit = _token_sources_cache.get(key)
    if hit is not None and now - hit["ts"] < TOKEN_SOURCES_TTL:
        return hit["data"]
    try:
        result = token_sources.get_token_sources(since_dt=since_dt, until_dt=until_dt)
    except Exception as e:
        return {"error": str(e)}
    result = dict(result)
    result["since"] = since.isoformat()
    result["until"] = until.isoformat()
    result["capped"] = capped
    if capped:
        result["capNote"] = f"All is capped at the last {TOKEN_SOURCES_MAX_DAYS} days so this stays fast."
    _token_sources_cache[key] = {"ts": now, "data": result}
    return result


def build_hourly(session_rows, claude_blocks):
    """Hour-by-hour spend for today. Claude's hours come from
    build_hourly_from_blocks() (below) — real per-request timing, so a past
    hour is final once it's past. Every other agent has no finer timestamp
    than its session's lastActivity, so its whole session still lands in a
    single hour bucket; those bars can keep changing until the session ends,
    same as before. Fine print belongs in the UI, not silently hidden here.

    claude_blocks is None when the `ccusage blocks` call itself failed (as
    opposed to an empty list, which genuinely means no Claude activity
    today) — in that case Claude falls back to the same lastActivity
    bucketing as every other agent, rather than silently vanishing from the
    chart because its own data source came back empty."""
    blocks_available = claude_blocks is not None
    buckets = {b["period"]: dict(b) for b in (claude_blocks or [])}
    for row in session_rows:
        if blocks_available and row.get("agent") == "claude":
            continue  # claude_blocks already covers this agent, with real per-hour timing
        iso = (row.get("metadata") or {}).get("lastActivity")
        if not iso:
            continue
        try:
            dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        hour_key = dt.strftime("%Y-%m-%dT%H:00")
        bucket = buckets.setdefault(hour_key, {"period": hour_key, "cost": 0.0, "agents": {}, "inputTokens": 0, "outputTokens": 0})
        cost = row.get("totalCost", 0.0)
        bucket["cost"] += cost
        agent = row.get("agent", "unknown")
        bucket["agents"][agent] = bucket["agents"].get(agent, 0.0) + cost
        for mb in row.get("modelBreakdowns", []):
            bucket["inputTokens"] += mb.get("inputTokens", 0)
            bucket["outputTokens"] += mb.get("outputTokens", 0)
    return [buckets[k] for k in sorted(buckets)]


def build_hourly_from_blocks(blocks_json):
    """Claude's own 5-hour billing blocks, asked for at 1-hour granularity —
    `ccusage blocks` groups by each request's own timestamp, not a session's
    lastActivity, so a heavy session's cost lands in every hour it actually
    ran in rather than all piling into whichever hour it last touched. A past
    hour's bar is therefore final; only the current hour still grows."""
    buckets = []
    for block in blocks_json.get("blocks", []):
        if block.get("isGap"):
            continue
        try:
            dt = datetime.datetime.fromisoformat(block["startTime"].replace("Z", "+00:00")).astimezone()
        except (ValueError, KeyError):
            continue
        tokens = block.get("tokenCounts", {})
        buckets.append({
            "period": dt.strftime("%Y-%m-%dT%H:00"),
            "cost": block.get("costUSD", 0.0),
            "agents": {"claude": block.get("costUSD", 0.0)},
            "inputTokens": tokens.get("inputTokens", 0),
            "outputTokens": tokens.get("outputTokens", 0),
        })
    return buckets


def fetch_claude_hourly_blocks(since_date):
    """since_date: a date.isoformat()-style 'YYYYMMDD' string, local time.
    Returns None on any failure — distinct from a real empty result — so
    build_hourly() can tell "ccusage found nothing" apart from "this call
    didn't work" and fall back instead of just dropping Claude."""
    try:
        result = subprocess.run(
            ccusage_bin.command("blocks", "--json", "-n", "1", "--since", since_date),
            capture_output=True, text=True, timeout=60, check=True,
        )
        return json.loads(result.stdout)
    except Exception:
        return None


def fetch_claude_active_block():
    """ccusage's active 5-hour block, {} when there is none, None on failure."""
    try:
        result = subprocess.run(
            ccusage_bin.command("blocks", "--active", "--json"),
            capture_output=True, text=True, timeout=60, check=True,
        )
        blocks = (json.loads(result.stdout) or {}).get("blocks") or []
        active = next((b for b in blocks if b.get("isActive") and not b.get("isGap")), None)
        if not active:
            return {}
        active = {k: v for k, v in active.items() if k != "entries" or isinstance(v, int)}
        active["fetchedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        return active
    except Exception:
        return None


MODEL_MIX_TTL = 15 * 60
_model_mix_cache = {"ts": 0.0, "data": None}


def get_model_mix(daily_rows, force=False):
    """Repriced ccusage runs are a few seconds, so keep the result 15 minutes."""
    now = time.time()
    if not force and _model_mix_cache["data"] is not None and now - _model_mix_cache["ts"] < MODEL_MIX_TTL:
        return _model_mix_cache["data"]
    data = model_mix.compute(daily_rows)
    _model_mix_cache.update(ts=now, data=data)
    return data


def _plan_detection(payload):
    """Plan per account/tool from local files only (see plan_detection)."""
    try:
        accounts = [(mu.get("accountUuid"), mu.get("label")) for mu in payload.get("maxUsage") or []
                    if mu.get("accountUuid")]
        return plan_detection.detect_all(
            accounts,
            codex_live=payload.get("codexLive"),
            grokbot=payload.get("grokBot"),
            ccusage_sources=payload.get("ccusageSources"),
            billing_currencies=seat_prices.billing_currencies(payload.get("maxUsage")),
        )
    except Exception:
        return []


def _team_value(payload):
    try:
        region = ((payload.get("pricingRegion") or {}).get("currency"))
        return seat_prices.team_value(payload.get("maxUsage"), payload.get("accountSessions"),
                                      seat_prices.load(), detections=payload.get("planDetection"),
                                      daily=payload.get("daily"), region_currency=region)
    except Exception:
        return []


def _build_provider_costs(force=False):
    openai_cost = get_openai_cost(force=force)
    return {"openai": openai_cost} if openai_cost is not None else {}


_opencode_cache = {"key": None, "at": 0.0, "data": None}


def get_opencode_backends(force=False):
    """OpenCode backend split, cached on the files' mtimes (the db scan takes
    about a second on a long history) with a 60 s floor while OpenCode writes."""
    paths = [os.path.expanduser(p) for p in opencode_backends.DB_CANDIDATES + opencode_backends.CONFIG_CANDIDATES
             + (opencode_backends.AUTH_PATH,)] + [data_dir.path(opencode_backends.OVERRIDES_FILE)]
    key = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)
    n_db = len(opencode_backends.DB_CANDIDATES)
    now = time.time()
    c = _opencode_cache
    if not force and c["data"] is not None and c["key"] is not None and c["key"][n_db:] == key[n_db:] \
            and (c["key"][:n_db] == key[:n_db] or now - c["at"] < 60):
        return c["data"]
    try:
        data = opencode_backends.build(litellm=model_mix.load_litellm())
    except Exception as e:  # never break the whole dashboard over this
        data = {"found": False, "error": str(e), "providers": {}, "daily": {}, "models": {}}
    c.update(key=key, at=now, data=data)
    return data


def build_sources(account_list, max_usage_list, codex_live, grok_on, grok_billing, plans, no_account_data, agent_provider,
                  opencode=None):
    """One catalog entry per (tool, account) actually discovered — all-time,
    not scoped to whatever date range happens to be selected in the UI (that
    was the earlier "only grok in the dropdown" bug: the old per-range
    `otherAgg` was used to build the tool list). Both the Overview card and
    the Settings page render from this single list instead of four
    separately hand-coded banner functions, so a source only needs fixing in
    one place when its status changes (e.g. a tool going from manual to
    automatic). `kind` is one of: official-live, official-cached,
    unofficial-live, manual, none."""
    sources = []
    max_usage_by_uuid = {m["accountUuid"]: m for m in (max_usage_list or [])}

    for acct in account_list:
        uuid = acct["accountUuid"]
        mu = max_usage_by_uuid.get(uuid)
        if mu:
            kind = "official-live" if mu.get("live") else "official-cached"
            note = acct.get("note")
        else:
            kind = "none"
            note = acct.get("note") or "No plan-limit data ever captured for this account — it's never been the one logged into Claude Code locally on this machine."
        sources.append({
            "id": uuid, "tool": "claude", "provider": "anthropic", "label": acct["label"],
            "plan": None, "kind": kind, "asOf": mu.get("fetchedAt") if mu else None,
            "note": note, "maxUsage": mu,
        })

    no_account_agents = (no_account_data or {}).get("agents") or {}

    if codex_live:
        sources.append({
            "id": "codex", "tool": "codex", "provider": "openai", "label": "Codex",
            "plan": codex_live.get("planType"), "kind": "official-live",
            "asOf": codex_live.get("fetchedAt"), "note": None, "codexLive": codex_live,
        })
    elif "codex" in no_account_agents:
        p = (plans or {}).get("codex")
        sources.append({
            "id": "codex", "tool": "codex", "provider": "openai", "label": "codex",
            "plan": p.get("plan") if p else None, "kind": "manual" if p else "none",
            "asOf": p.get("asOf") if p else None, "note": p.get("note") if p else None,
            "coveredByPlan": p.get("coveredByPlan") if p else None,
        })

    if grok_on and grok_billing and not grok_billing.get("error"):
        manual_grok = (plans or {}).get("grok")
        sources.append({
            "id": "grok", "tool": "grok", "provider": "xai", "label": "Grok",
            "plan": grok_billing.get("plan"), "kind": "unofficial-live",
            "asOf": grok_billing.get("fetchedAt"), "note": None, "grokBilling": grok_billing,
            "manualPlan": manual_grok.get("plan") if manual_grok else None,
        })
    elif "grok" in no_account_agents or grok_on:
        p = (plans or {}).get("grok")
        sources.append({
            "id": "grok", "tool": "grok", "provider": "xai", "label": "grok",
            "plan": p.get("plan") if p else None, "kind": "manual" if p else "none",
            "asOf": p.get("asOf") if p else None, "note": p.get("note") if p else None,
            "coveredByPlan": p.get("coveredByPlan") if p else None,
            "grokBillingError": grok_billing.get("error") if (grok_on and grok_billing) else None,
        })

    for agent in no_account_agents:
        if agent in ("codex", "grok"):
            continue
        if agent == "opencode" and (opencode or {}).get("found"):
            # Detected per backend from OpenCode's own history, so the old
            # free-text plan note no longer decides the colour of this row.
            sources.append({
                "id": agent, "tool": agent, "provider": None, "label": "OpenCode",
                "plan": None, "kind": "backends", "asOf": (opencode or {}).get("lastDay"),
                "note": None, "opencodeBackends": opencode,
            })
            continue
        p = (plans or {}).get(agent)
        sources.append({
            "id": agent, "tool": agent, "provider": (agent_provider or {}).get(agent), "label": agent,
            "plan": p.get("plan") if p else None, "kind": "manual" if p else "none",
            "asOf": p.get("asOf") if p else None, "note": p.get("note") if p else None,
            "coveredByPlan": p.get("coveredByPlan") if p else None,
        })

    return sources


def _with_grok_sources(sources, grok_report, plans, grokbot=None):
    sources = list(sources)
    if grok_report.get("found"):
        known = all(day.get("costKnown") for day in grok_report.get("days") or [])
        sources.append({
            "id": "grok-cli",
            "tool": "grok-cli",
            "provider": "xai",
            "label": "Grok CLI",
            "plan": None,
            "kind": "local-live",
            "asOf": datetime.datetime.now().isoformat(timespec="seconds"),
            "note": grok_usage.COST_NOTE,
            "costKnown": known,
        })
    accounts = (grokbot or {}).get("accounts") or []
    if accounts:
        multi = len(accounts) > 1
        for acct in accounts:
            if multi:
                suffix = "current" if acct.get("current") else (acct.get("plan") or "account")
                label = f"Grok Bot ({suffix})"
                source_id = "grok-bot:" + (acct.get("accountId") or suffix)
            else:
                label = "Grok Bot"
                source_id = "grok-bot"
            sources.append({
                "id": source_id,
                "tool": "grok-bot",
                "provider": "xai",
                "label": label,
                "plan": acct.get("plan"),
                "kind": "official-cached",
                "asOf": acct.get("updatedAt"),
                "note": acct.get("note") or grokbot_usage.DEFAULT_NOTE,
                "coveredByPlan": True,
                "weeklyLimitPercent": acct.get("weeklyPct"),
                "resetsAt": acct.get("resetAt"),
                "creditsUsd": acct.get("creditsUsd"),
                "grokBot": True,
                "grokBotAccount": acct,
            })
    else:
        sources.append({
            "id": "grok-bot",
            "tool": "grok-bot",
            "provider": "xai",
            "label": "Grok Bot",
            "plan": None,
            "kind": "none",
            "asOf": None,
            "note": "No local Grok Bot usage cache yet. The desktop app writes one when it has fetched usage; this dashboard reads that file and does not call Grok's servers.",
            "coveredByPlan": False,
            "weeklyLimitPercent": None,
            "resetsAt": None,
            "grokBot": True,
        })
    return sources


def refresh_usage(force=False):
    try:
        result = subprocess.run(
            ccusage_bin.command("--sections", "daily,session", "--json", "--by-agent"),
            capture_output=True, text=True, timeout=60, check=True,
        )
        raw = json.loads(result.stdout)

        grok_report = grok_usage.collect(current_email=account_usage.detect_grok_email())
        daily_rows = raw.get("daily") or []
        session_rows = raw.get("session") or []
        if grok_report["found"]:
            daily_rows = grok_usage.merge_daily(daily_rows, grok_report["days"])
            session_rows = [row for row in session_rows if row.get("agent") != "grok"]

        labels = account_usage.seed_labels_from_snapshots(
            account_usage.seed_labels_from_current_account(account_usage.load_labels())
        )
        account_usage.save_labels(labels)
        session_account_map, session_project_map = account_usage.build_session_account_map()
        account_list, no_account_data, daily_by_bucket, account_sessions = account_usage.compute_accounts(
            session_rows, session_account_map, labels, session_project_map,
        )
        if grok_report["found"]:
            for row in grok_report["sessions"]:
                custom = labels.get(row["accountUuid"]) or {}
                if custom.get("label"):
                    row["label"] = custom["label"]
            account_sessions = list(account_sessions) + grok_report["sessions"]

        max_usage = account_usage.get_max_usage_snapshots()
        plans = account_usage.load_plans()
        try:
            grokbot = grokbot_usage.collect()
        except Exception:
            grokbot = {"found": False, "accounts": [], "source": None}

        agent_provider = provider_usage.AGENT_PROVIDER
        codex_live = get_codex_live(force=force)
        unofficial_toggles = provider_usage.load_unofficial_toggles()
        grok_billing = _grok_billing_cache["data"]

        today_local = datetime.datetime.now().strftime("%Y%m%d")
        blocks_json = fetch_claude_hourly_blocks(today_local)
        claude_blocks = build_hourly_from_blocks(blocks_json) if blocks_json is not None else None

        claude_history = account_usage.get_usage_history()
        codex_history = provider_usage.get_codex_rate_limit_history()
        claude_active_block = fetch_claude_active_block() if max_usage else None
        opencode_info = get_opencode_backends(force=force) if "opencode" in (no_account_data or {}).get("agents", {}) else None
        payload = {
            "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "daily": daily_rows,
            "totals": raw.get("totals", {}),
            "accounts": account_list,
            "noAccountData": no_account_data,
            "dailyByBucket": daily_by_bucket,
            "accountSessions": account_sessions,
            "maxUsage": max_usage,
            "hourly": build_hourly(session_rows, claude_blocks),
            "plans": plans,
            "providerStatus": provider_usage.get_provider_statuses(),
            "agentProvider": agent_provider,
            "providerCosts": _build_provider_costs(force=force),
            "liveUsageSetup": statusline_setup.get_status(),
            "codexLive": codex_live,
            "unofficialToggles": unofficial_toggles,
            "grokBilling": grok_billing,
            "usageHistory": {
                "claude": claude_history,
                "codex": codex_history,
            },
            "grokBot": grokbot,
            "claudeActiveBlock": claude_active_block,
            "modelMix": get_model_mix(raw.get("daily") or [], force=force),
            "seatPrices": seat_prices.load(),
            "sources": _with_grok_sources(build_sources(
                account_list, max_usage, codex_live, unofficial_toggles.get("grok"),
                grok_billing, plans, no_account_data, agent_provider,
                opencode=opencode_info,
            ), grok_report, plans, grokbot),
            "opencodeBackends": opencode_info,
        }
        # `detection` is the per-tool installed/hasData catalog. The existing
        # `sources` key stays the cost/plan rows, with absent tools removed.
        hints = source_detection.hints_from_usage(
            account_list=account_list,
            no_account_data=no_account_data,
            grok_report=grok_report,
            grokbot=grokbot,
            codex_live=codex_live,
        )
        detection = source_detection.detect(hints=hints)
        payload["detection"] = detection
        payload["ccusageSources"] = ccusage_sources.present(raw.get("daily") or [], raw.get("session") or [], detection)
        payload["planDetection"] = _plan_detection(payload)
        payload["planPrices"] = plan_prices.load()
        payload["pricingRegion"] = pricing_region.current()
        payload["teamValue"] = _team_value(payload)
        payload["planInsights"] = _plan_insights_from(payload)
        payload["sources"] = source_detection.annotate_sources(
            source_detection.filter_absent_sources(payload["sources"], detection),
            detection,
        )
        return payload
    except FileNotFoundError as e:
        if getattr(e, "filename", None) in ("ccusage", "npx"):
            return {
                "error": str(e) if getattr(e, "filename", None) == "ccusage" else
                         "ccusage was not found. " + ccusage_bin.describe(),
                "errorType": "ccusage_missing",
            }
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def sample_grokbot_usage():
    """Read the Grok Bot cache and append history. Cheap, no network."""
    try:
        return grokbot_usage.collect()
    except Exception:
        return None


def _plan_insights_from(payload):
    """Forecast and previous-window comparison for every plan with a reading,
    built from data already in the usage payload (no extra reads)."""
    history = payload.get("usageHistory") or {}
    try:
        return insights.build_plan_insights(
            grokbot=payload.get("grokBot"),
            max_usage=payload.get("maxUsage"),
            claude_history=history.get("claude"),
            codex_live=payload.get("codexLive"),
            codex_history=history.get("codex"),
            claude_active_block=payload.get("claudeActiveBlock"),
        )
    except Exception:
        return []


def _attach_fresh_grokbot(payload):
    """Take a Grok Bot reading on every /api/usage, including cache hits.

    The ccusage payload can stay cached for a few seconds; the cache file
    still needs sampling so daily history fills in while the dashboard is open.
    """
    report = sample_grokbot_usage()
    if report is not None:
        payload["grokBot"] = report
        payload["planInsights"] = _plan_insights_from(payload)


def digest_url():
    return f"http://127.0.0.1:{PORT}/?tab=digest"


def current_plan_insights(grokbot=None):
    """Plan readings for the notification check. Uses only cheap local reads:
    the Grok Bot cache, Claude's snapshots and history, and the last Codex
    reading from the usage cache (never spawns Codex from the sampler)."""
    cached = _cache.get("data") or {}
    try:
        max_usage = account_usage.get_max_usage_snapshots()
    except Exception:
        max_usage = cached.get("maxUsage")
    try:
        claude_history = account_usage.get_usage_history()
    except Exception:
        claude_history = []
    codex_live = _codex_live_cache.get("data") or cached.get("codexLive")
    codex_history = (cached.get("usageHistory") or {}).get("codex") or []
    active_block = fetch_claude_active_block() if max_usage else None
    return insights.build_plan_insights(
        grokbot=grokbot if grokbot is not None else cached.get("grokBot"),
        max_usage=max_usage,
        claude_history=claude_history,
        codex_live=codex_live,
        codex_history=codex_history,
        claude_active_block=active_block,
    )


def run_notification_checks(grokbot=None):
    try:
        plans = current_plan_insights(grokbot)
        return notify.run_checks(plans, digest_url=digest_url())
    except Exception:
        return []


def _sampler_pass():
    report = sample_grokbot_usage()
    run_notification_checks(report)


def _grokbot_sampler_loop():
    _sampler_pass()
    while not _sampler_stop.wait(GROKBOT_SAMPLE_INTERVAL):
        _sampler_pass()


def start_grokbot_sampler():
    """Sample the Grok Bot cache every 10 minutes while the server runs."""
    global _sampler_thread
    if _sampler_thread is not None and _sampler_thread.is_alive():
        return _sampler_thread
    _sampler_stop.clear()
    _sampler_thread = threading.Thread(
        target=_grokbot_sampler_loop,
        name="grokbot-sampler",
        daemon=True,
    )
    _sampler_thread.start()
    return _sampler_thread


DIGEST_MAX_AGE = 300  # seconds: the weekly report covers complete weeks, so a recent payload is fine


def get_usage_recent(max_age=DIGEST_MAX_AGE):
    """The cached payload if it is at most max_age old, without waiting for a
    ccusage refresh (about 5 s). Falls back to get_usage() when there is none."""
    with _lock:
        cached = _cache["data"]
        if cached is not None and time.time() - _cache["ts"] <= max_age:
            return dict(cached, stale=False)
    return get_usage()


_digest_cache = {}  # (week, usage cache ts) -> digest; cleared when the payload changes


def get_digest(week):
    usage = get_usage_recent()
    if usage.get("error"):
        return usage
    key = (week, _cache["ts"])
    hit = _digest_cache.get(key)
    if hit is not None:
        return hit
    result = digest.build_digest(usage, week=week)
    _digest_cache.clear()
    _digest_cache[key] = result
    return result


def get_usage(force=False):

    """Return the usage payload.

    A forced refresh that fails after a previous success does not drop the
    last good payload. The copy handed back is marked stale and carries
    refreshError; the cache itself stays clean so a later success is not
    stuck looking failed.
    """
    with _lock:
        now = time.time()
        stale = _cache["data"] is None or (now - _cache["ts"] > CACHE_TTL)
        if force or stale:
            data = refresh_usage(force=force)
            if "error" not in data:
                clean = {k: v for k, v in data.items() if k not in ("stale", "refreshError")}
                _cache["data"] = clean
                _cache["ts"] = now
                return dict(clean, stale=False)
            if _cache["data"] is None:
                return data
            cached = dict(_cache["data"])
            cached["stale"] = True
            cached["refreshError"] = data.get("error") or "refresh failed"
            _attach_fresh_grokbot(cached)
            return cached
        payload = dict(_cache["data"], stale=False)
        _attach_fresh_grokbot(payload)
        return payload


def build_summary(usage, today=None):
    today = (today or datetime.date.today()).isoformat()
    row = next((d for d in usage.get("daily") or [] if (d.get("period") or "")[:10] == today), None)
    tokens = 0
    if row:
        for mb in row.get("modelBreakdowns") or []:
            tokens += (mb.get("inputTokens") or 0) + (mb.get("outputTokens") or 0)
    plans = []
    for p in usage.get("planInsights") or []:
        plans.append({k: p.get(k) for k in ("key", "tool", "label", "pct", "resetAt", "windowSeconds", "forecast", "compare", "note", "session", "week", "sessionPace")})
    plans.sort(key=lambda p: -(p.get("pct") or 0))
    pace_warnings = [p for p in plans if (p.get("sessionPace") or {}).get("warn")]
    return {
        "generatedAt": usage.get("generatedAt"),
        "stale": bool(usage.get("stale")),
        "today": {"date": today, "listUsd": round((row or {}).get("totalCost") or 0.0, 2), "tokens": tokens},
        "plans": plans,
        "highest": plans[0] if plans else None,
        "paceWarning": {"label": pace_warnings[0].get("label"), **pace_warnings[0]["sessionPace"]} if pace_warnings else None,
        "dashboardUrl": f"http://127.0.0.1:{PORT}/",
        "notifications": notify.public_status(),
    }


def _hostname(host_header):
    host = (host_header or "").strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end != -1 else ""
    return host.split(":")[0].strip().lower()


def request_is_local(host_header, origin_header, port=None):
    """Same-machine check for the labels API. The server already binds to
    127.0.0.1; this rejects a DNS-rebind Host and a cross-origin browser POST.
    A missing Origin is allowed (curl, and same-origin GET, omit it)."""
    if port is None:
        port = PORT
    host = _hostname(host_header)
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    if not origin_header:
        return True
    origin = origin_header.strip().rstrip("/")
    allowed = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    return origin in allowed


def validate_label_body(body):
    """accountUuid + label (+ optional note) for account-labels.json.

    accountUuid is an id we already discovered at runtime (or the synthetic
    unknown-session bucket). It is checked as a short token so a request
    cannot smuggle a path or a blob. label is the display name the user typed.
    """
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    account = body.get("accountUuid")
    if not isinstance(account, str) or not _LABEL_ID_RE.match(account.strip()):
        raise ValueError("accountUuid is required and must be a short id")
    raw_label = body.get("label")
    if not isinstance(raw_label, str):
        raise ValueError("label is required")
    if any(ord(ch) < 32 for ch in raw_label):
        raise ValueError("label contains invalid characters")
    label = " ".join(raw_label.split())
    if not label or len(label) > _LABEL_MAX:
        raise ValueError("label must be between 1 and 80 characters")
    note_provided = "note" in body
    note = None
    if note_provided:
        note = body.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("note must be a string")
            note = note.strip()
            if any(ord(ch) < 32 and ch not in "\n\t" for ch in note):
                raise ValueError("note contains invalid characters")
            if len(note) > _NOTE_MAX:
                raise ValueError("note must be 500 characters or fewer")
            if not note:
                note = None
    return account.strip(), label, note, note_provided


def _apply_label_to_cached_usage(cached, account_uuid, entry):
    """Patch a cached /api/usage payload so a rename shows up even when the
    next ccusage refresh fails and the previous payload is served again.
    dailyByBucket keys by the old label; the next successful refresh rebuilds it."""
    for key in ("accounts", "accountSessions", "maxUsage"):
        for row in cached.get(key) or []:
            if isinstance(row, dict) and row.get("accountUuid") == account_uuid:
                row["label"] = entry["label"]
                row["note"] = entry.get("note")
    for src in cached.get("sources") or []:
        if not isinstance(src, dict):
            continue
        if src.get("tool") == "claude" and src.get("id") == account_uuid:
            src["label"] = entry["label"]
            mu = src.get("maxUsage")
            if isinstance(mu, dict):
                mu["label"] = entry["label"]


def save_account_label(account_uuid, label, note, note_provided):
    """Merge one account name into account-labels.json, keeping an existing
    note when the request did not send one."""
    with _lock:
        labels = account_usage.load_labels()
        existing = labels.get(account_uuid)
        if not isinstance(existing, dict):
            existing = {}
        entry = {"label": label, "note": existing.get("note")}
        if note_provided:
            entry["note"] = note
        labels[account_uuid] = entry
        account_usage.save_labels(labels)
        _cache["ts"] = 0.0
        cached = _cache.get("data")
        if isinstance(cached, dict):
            _apply_label_to_cached_usage(cached, account_uuid, entry)
        return labels


def apply_settings(body, cached=None):
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    known = {"labels", "pricingRegion", "seatPrices", "plan", "opencodeProviders",
             "notifications", "login", "openaiKey"}
    extra = set(body) - known
    if extra:
        raise ValueError("unknown settings: " + ", ".join(sorted(extra)))
    cached = cached if cached is not None else (_cache.get("data") or {})
    errors = []

    def check(section, fn):
        try:
            return fn()
        except ValueError as e:
            errors.append("%s: %s" % (section, e))
            return None

    # ---- validate everything, write nothing yet ----
    new_labels = None
    if "labels" in body:
        def _labels():
            raw = body["labels"]
            if not isinstance(raw, dict):
                raise ValueError("must be an object")
            out = {}
            for uuid, name in raw.items():
                if name in (None, ""):
                    continue  # an empty box leaves the saved name as it is
                account, label, _n, _p = validate_label_body({"accountUuid": uuid, "label": name})
                out[account] = label
            return out
        new_labels = check("Account names", _labels)

    region_set, region = False, None
    if "pricingRegion" in body:
        v = body["pricingRegion"]
        region = check("Pricing region", lambda: pricing_region.validate_body({"region": None if v in (None, "auto") else v}))
        region_set = True

    seat_updates = None
    if "seatPrices" in body:
        def _seats():
            raw = body["seatPrices"]
            if not isinstance(raw, dict):
                raise ValueError("must be an object")
            currencies = seat_prices.billing_currencies(cached.get("maxUsage"))
            out = {}
            for key, entry in raw.items():
                entry = entry or {}
                amount = entry.get("amount")
                if amount in ("", None):
                    amount = None
                account, clean = seat_prices.validate_body(
                    {"accountUuid": key, "amount": amount, "currency": entry.get("currency")},
                    currencies.get(key))
                out[account] = clean
            return out
        seat_updates = check("Seat prices", _seats)

    plan_entry = None
    if body.get("plan"):
        def _plan():
            p = body["plan"]
            if not isinstance(p, dict):
                raise ValueError("must be an object")
            agent = (p.get("agent") or "").strip()
            if not agent or not _LABEL_ID_RE.match(agent):
                raise ValueError("tool is required")
            return agent, {
                "plan": p.get("plan") or None,
                "coveredByPlan": bool(p.get("coveredByPlan")),
                "note": p.get("note") or None,
                "asOf": p.get("asOf") or datetime.date.today().isoformat(),
            }
        plan_entry = check("Manual note", _plan)

    oc = None
    if "opencodeProviders" in body:
        oc = check("OpenCode backends", lambda: opencode_backends.validate_overrides(body["opencodeProviders"]))

    notif = None
    if "notifications" in body:
        notif = check("Notifications", lambda: notify.validate_settings(body["notifications"], notify.load_settings()))

    login = None
    if "login" in body:
        if not isinstance(body["login"], bool):
            errors.append("Start at login: must be true or false")
        else:
            login = body["login"]

    openai_key = None
    if body.get("openaiKey"):
        k = body["openaiKey"]
        if not isinstance(k, str) or not k.strip() or len(k) > 400 or any(ord(c) < 33 for c in k.strip()):
            errors.append("OpenAI Admin key: does not look like a key")
        else:
            openai_key = k.strip()

    if errors:
        return {"ok": False, "error": "Nothing was saved. " + " ".join(errors), "errors": errors}

    # ---- write ----
    saved = []
    with _lock:
        if new_labels:
            labels = account_usage.load_labels()
            for account, label in new_labels.items():
                existing = labels.get(account) if isinstance(labels.get(account), dict) else {}
                labels[account] = {"label": label, "note": existing.get("note")}
            account_usage.save_labels(labels)
            for account in new_labels:
                if isinstance(cached, dict) and cached:
                    _apply_label_to_cached_usage(cached, account, labels[account])
            saved.append("account names")
        if region_set:
            pricing_region.save(region)
            saved.append("pricing region")
        if seat_updates is not None:
            prices = seat_prices.load()
            for account, entry in seat_updates.items():
                if entry is None:
                    prices.pop(account, None)
                else:
                    prices[account] = entry
            seat_prices.save(prices)
            saved.append("seat prices")
        if plan_entry:
            account_usage.save_plan(*plan_entry)
            saved.append("manual note")
        if oc is not None:
            opencode_backends.save_overrides(oc)
            saved.append("OpenCode backends")
        if notif is not None:
            notify.save_settings(notif)
            saved.append("notifications")
        if openai_key:
            provider_usage.save_provider_key("openai", openai_key)
            saved.append("OpenAI key")
        _cache["ts"] = 0.0  # next /api/usage rebuilds from what was just written
        if isinstance(cached, dict) and cached:
            cached["pricingRegion"] = pricing_region.current()
            cached["seatPrices"] = seat_prices.load()
            cached["teamValue"] = _team_value(cached)

    warnings = []
    login_status = None
    if login is not None:
        try:
            current = login_item.status()
            if current.get("enabled") != login:
                login_item.turn_on() if login else login_item.turn_off()
            login_status = login_item.status()
            if login_status.get("enabled") != login:
                warnings.append("Start at login did not stick. Check ~/Library/LaunchAgents.")
            elif current.get("enabled") != login:
                saved.append("start at login")
        except Exception as e:
            warnings.append("Start at login: %s" % e)
    if openai_key:
        test = get_openai_cost(force=True)
        if test and "error" in test:
            warnings.append("OpenAI key saved, but the test call failed: %s" % test["error"])

    return {"ok": True, "saved": saved, "warnings": warnings,
            "notifications": notify.load_settings(), "login": login_status,
            "opencodeProviders": opencode_backends.load_overrides()}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/usage":
            force = "force=1" in self.path
            self._send_json(get_usage(force=force))
        elif path == "/api/token-sources":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            try:
                self._send_json(get_token_sources_cached(query))
            except ValueError as e:
                self._send_json({"error": str(e)}, code=400)
        elif path == "/api/grokbot":
            self._send_json(grokbot_usage.collect())
        elif path == "/api/digest":
            self._handle_digest_get()
        elif path == "/api/summary":
            self._handle_summary_get()
        elif path == "/api/notifications":
            self._send_json(notify.public_status())
        elif path == "/api/login-item":
            self._send_json(login_item.status())
        elif path == "/api/labels":
            self._handle_labels_get()
        elif path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/favicon.png":
            with open(FAVICON_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/plans":
            self._handle_plan_post()
        elif path == "/api/provider-keys":
            self._handle_provider_key_post()
        elif path == "/api/live-usage/enable":
            self._send_json(statusline_setup.enable())
        elif path == "/api/live-usage/disable":
            self._send_json(statusline_setup.disable())
        elif path == "/api/unofficial-toggles":
            self._handle_unofficial_toggle_post()
        elif path == "/api/grok-billing/refresh":
            self._handle_grok_billing_refresh()
        elif path == "/api/labels":
            self._handle_labels_post()
        elif path == "/api/settings":
            self._handle_local_post(self._settings_post)
        elif path == "/api/seat-prices":
            self._handle_local_post(self._seat_price_post)
        elif path == "/api/pricing-region":
            self._handle_local_post(self._pricing_region_post)
        elif path == "/api/grokbot":
            self._handle_grokbot_post()
        elif path == "/api/notifications":
            self._handle_local_post(self._notifications_post)
        elif path == "/api/notifications/test":
            self._handle_local_post(lambda body: {"ok": True, "result": notify.send_test()})
        elif path == "/api/app/heartbeat":
            self._handle_local_post(lambda body: {"ok": True, "pending": notify.record_heartbeat()})
        elif path == "/api/login-item":
            self._handle_local_post(self._login_item_post)
        else:
            self.send_response(404)
            self.end_headers()

    def _client_is_local(self):
        return request_is_local(self.headers.get("Host"), self.headers.get("Origin"))

    def _handle_local_post(self, fn):
        """Settings that change this machine (notifications, LaunchAgent) only
        accept same-machine, same-origin requests."""
        if not self._client_is_local():
            self._send_json({"ok": False, "error": "This endpoint only accepts requests from this machine."}, code=403)
            return
        try:
            body = self._read_json_object(_LABEL_BODY_MAX)
            self._send_json(fn(body))
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=500)

    def _seat_price_post(self, body):
        cached = _cache.get("data") or {}
        currencies = seat_prices.billing_currencies(cached.get("maxUsage"))
        account = body.get("accountUuid") if isinstance(body, dict) else None
        account, entry = seat_prices.validate_body(body, currencies.get(account))
        prices = seat_prices.load()
        if entry is None:
            prices.pop(account, None)
        else:
            prices[account] = entry
        seat_prices.save(prices)
        if cached:
            cached["seatPrices"] = prices
            cached["teamValue"] = _team_value(cached)
        return {"ok": True, "seatPrices": prices, "teamValue": cached.get("teamValue")}

    def _pricing_region_post(self, body):
        region = pricing_region.validate_body(body)
        pricing_region.save(region)
        cached = _cache.get("data") or {}
        cached["pricingRegion"] = pricing_region.current()
        cached["teamValue"] = _team_value(cached)
        _cache["data"] = cached
        return {"ok": True, "pricingRegion": cached["pricingRegion"], "teamValue": cached["teamValue"]}


    def _settings_post(self, body):
        """The Settings page's single Save. Every section is validated first;
        if anything is wrong nothing is written. Then each file is written
        with a temp-file-and-rename, under the same lock the usage refresh
        uses for account-labels.json, so a refresh cannot interleave with it."""
        result = apply_settings(body)
        if not result.get("ok"):
            raise ValueError(result["error"])
        return result

    def _notifications_post(self, body):
        settings = notify.validate_settings(body, notify.load_settings())
        notify.save_settings(settings)
        return {"ok": True, "settings": notify.load_settings()}

    def _login_item_post(self, body):
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        st = login_item.turn_on() if enabled else login_item.turn_off()
        # Read back rather than trusting the launchctl exit code.
        st = login_item.status()
        ok = st["enabled"] == enabled
        return {"ok": ok, "status": st, "error": None if ok else "The setting did not stick. Check ~/Library/LaunchAgents."}

    def _handle_digest_get(self):
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw_week = (query.get("week") or [None])[0]
            today = datetime.date.today()
            week = digest.parse_week(raw_week, digest.last_complete_week(today)[0])
            result = get_digest(week)
            if result.get("error") and "weekStart" not in result:
                self._send_json({"error": result["error"]}, code=503)
                return
            self._send_json(result)
        except ValueError as e:
            self._send_json({"error": str(e)}, code=400)
        except Exception as e:
            self._send_json({"error": str(e)}, code=500)

    def _handle_summary_get(self):
        """Compact view for the menu bar app: plan readings with forecasts,
        and today's list-price spend and tokens."""
        usage = get_usage()
        if usage.get("error"):
            self._send_json({"error": usage["error"]}, code=503)
            return
        self._send_json(build_summary(usage))

    def _read_json_object(self, max_bytes):
        raw_len = self.headers.get("Content-Length") or "0"
        if not str(raw_len).isdigit():
            raise ValueError("Content-Length is invalid")
        length = int(raw_len)
        if length > max_bytes:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid JSON")
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body

    def _handle_labels_get(self):
        if not self._client_is_local():
            self._send_json({"ok": False, "error": "This endpoint only accepts requests from this machine."}, code=403)
            return
        try:
            self._send_json({"ok": True, "labels": account_usage.load_labels()})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def _handle_labels_post(self):
        if not self._client_is_local():
            self._send_json({"ok": False, "error": "This endpoint only accepts requests from this machine."}, code=403)
            return
        try:
            body = self._read_json_object(_LABEL_BODY_MAX)
            account, label, note, note_provided = validate_label_body(body)
            labels = save_account_label(account, label, note, note_provided)
            saved = labels.get(account) or {}
            self._send_json({
                "ok": True,
                "accountUuid": account,
                "label": saved.get("label"),
                "note": saved.get("note"),
                "labels": labels,
            })
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def _handle_unofficial_toggle_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            name = (body.get("name") or "").strip()
            if not name:
                raise ValueError("name is required")
            toggles = provider_usage.set_unofficial_toggle(name, bool(body.get("enabled")))
            if name == "grok" and not toggles.get("grok"):
                _grok_billing_cache["data"] = None  # clear any cached reading once disabled
                _save_grok_billing_cache(None)
            self._send_json({"ok": True, "toggles": toggles})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def _handle_grok_billing_refresh(self):
        # Explicit, user-triggered only — never called from a timer. Refuses
        # unless the toggle is on, so a stray request can't silently call an
        # unofficial endpoint the user hasn't opted into.
        if not provider_usage.load_unofficial_toggles().get("grok"):
            self._send_json({"ok": False, "error": "Grok live billing is not enabled — turn it on in Settings first."}, code=400)
            return
        result = provider_usage.fetch_grok_billing()
        _grok_billing_cache["data"] = result
        _grok_billing_cache["ts"] = time.time()
        _save_grok_billing_cache(result)  # persisted — a server restart shouldn't lose the last manual reading
        self._send_json({"ok": "error" not in result, **result})

    def _handle_plan_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            agent = (body.get("agent") or "").strip()
            if not agent:
                raise ValueError("agent is required")
            entry = {
                "plan": body.get("plan") or None,
                "coveredByPlan": bool(body.get("coveredByPlan")),
                "note": body.get("note") or None,
                "asOf": body.get("asOf") or datetime.date.today().isoformat(),
            }
            if "weeklyLimitPercent" in body and body.get("weeklyLimitPercent") not in (None, ""):
                try:
                    pct = float(body.get("weeklyLimitPercent"))
                except (TypeError, ValueError):
                    raise ValueError("weeklyLimitPercent must be a number")
                if pct < 0 or pct > 100:
                    raise ValueError("weeklyLimitPercent must be between 0 and 100")
                entry["weeklyLimitPercent"] = pct
            if "resetsAt" in body:
                reset = body.get("resetsAt")
                entry["resetsAt"] = reset.strip() if isinstance(reset, str) and reset.strip() else None
            plans = account_usage.save_plan(agent, entry)
            with _lock:
                _cache["ts"] = 0.0  # force next GET /api/usage to pick up the new plan
            self._send_json({"ok": True, "plans": plans})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def _handle_provider_key_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            provider = (body.get("provider") or "").strip()
            api_key = (body.get("apiKey") or "").strip()
            if provider not in provider_usage.SUPPORTED_PROVIDERS:
                raise ValueError(f"unsupported provider: {provider}")
            if not api_key:
                raise ValueError("apiKey is required")
            statuses = provider_usage.save_provider_key(provider, api_key)
            with _lock:
                _cache["ts"] = 0.0
            test_result = get_openai_cost(force=True) if provider == "openai" else None
            resp = {"ok": True, "providerStatus": statuses}
            if test_result and "error" in test_result:
                resp["warning"] = f"Key saved, but the test call failed: {test_result['error']}"
            self._send_json(resp)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def _handle_grokbot_post(self):
        """Optional automation path. The dashboard usually reads the Grok Bot
        app's local cache on its own; this endpoint lets a local assistant
        push a snapshot when that cache is not available."""
        if not self._client_is_local():
            self._send_json({"ok": False, "error": "This endpoint only accepts requests from this machine."}, code=403)
            return
        try:
            raw_len = self.headers.get("Content-Length") or "0"
            if not str(raw_len).isdigit():
                raise ValueError("Content-Length is invalid")
            length = int(raw_len)
            if length > 100_000:
                raise ValueError("request body is too large")
            raw = self.rfile.read(length) if length else b""
            try:
                text_body = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("body must be UTF-8")
            stripped = text_body.strip()
            if not stripped:
                raise ValueError("body is empty")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = None
            if ctype in ("application/json", "text/json") or stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    body = parsed
                elif parsed is not None:
                    raise ValueError("JSON object required")
            if body is None:
                body = {"rawText": text_body}
            saved = grokbot_usage.apply_push(body)
            with _lock:
                _cache["ts"] = 0.0
            self._send_json({"ok": True, "account": saved, "grokBot": grokbot_usage.collect()})
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/plans":
            self._handle_plan_delete()
        elif path == "/api/provider-keys":
            self._handle_provider_key_delete()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_plan_delete(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            agent = (body.get("agent") or "").strip()
            if not agent:
                raise ValueError("agent is required")
            plans = account_usage.delete_plan(agent)
            with _lock:
                _cache["ts"] = 0.0
            self._send_json({"ok": True, "plans": plans})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)

    def _handle_provider_key_delete(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            provider = (body.get("provider") or "").strip()
            statuses = provider_usage.delete_provider_key(provider)
            with _lock:
                _cache["ts"] = 0.0
            self._send_json({"ok": True, "providerStatus": statuses})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=400)


FIRST_RUN_FILE = "first-run.json"
FIRST_RUN_OPT_OUT = "AGENTIC_SPEND_NO_FIRST_RUN_OPEN"


def first_run_open(url, opener=None, env=None):
    """Open the dashboard in the default browser once, the first time the
    server runs after install. Recorded in ~/.agentic-spend/first-run.json so
    it never happens again; set AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1 to skip it.
    Returns True when it opened (or asked the opener to)."""
    env = os.environ if env is None else env
    marker = data_dir.path(FIRST_RUN_FILE)
    if os.path.exists(marker):
        return False
    try:
        data_dir.atomic_write_json(marker, {"openedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                                            "skipped": bool(env.get(FIRST_RUN_OPT_OUT))})
    except OSError:
        return False  # can't record it, so don't risk opening on every start
    if env.get(FIRST_RUN_OPT_OUT):
        return False
    if opener is None:
        def opener(u):
            subprocess.Popen(["open", u], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        opener(url)
    except Exception:
        return False
    return True


if __name__ == "__main__":
    # Live Claude usage is on by default: the first start wires the status line
    # hook (recorded, so turning it off in Settings sticks) and tidies any
    # stacked or old-path entries from earlier versions.
    print(ccusage_bin.describe())
    _auto = statusline_setup.auto_enable_once()
    if _auto.get("status") in ("enabled", "wrapped_existing", "repaired"):
        print(f"Live Claude usage: {_auto['status'].replace('_', ' ')}")
    start_grokbot_sampler()
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Serving dashboard at http://127.0.0.1:{PORT}/  (Ctrl-C to stop)")
        if first_run_open(f"http://127.0.0.1:{PORT}/"):
            print("First run: opened the dashboard in your browser (once only).")
        httpd.serve_forever()
