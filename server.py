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
import grok_usage
import provider_usage
import statusline_setup
import token_sources

PORT = int(os.environ.get("AGENTIC_SPEND_PORT", "8787"))
_LABEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_LABEL_MAX = 80
_NOTE_MAX = 500
_LABEL_BODY_MAX = 8192
CACHE_TTL = 10  # seconds
CCUSAGE_VERSION = "20.1.1"  # pinned so refresh doesn't hit the npm registry every time
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
            ["npx", "--yes", f"ccusage@{CCUSAGE_VERSION}", "blocks", "--json",
             "-n", "1", "--since", since_date],
            capture_output=True, text=True, timeout=60, check=True,
        )
        return json.loads(result.stdout)
    except Exception:
        return None


def _build_provider_costs(force=False):
    openai_cost = get_openai_cost(force=force)
    return {"openai": openai_cost} if openai_cost is not None else {}


def build_sources(account_list, max_usage_list, codex_live, grok_on, grok_billing, plans, no_account_data, agent_provider):
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
        p = (plans or {}).get(agent)
        sources.append({
            "id": agent, "tool": agent, "provider": (agent_provider or {}).get(agent), "label": agent,
            "plan": p.get("plan") if p else None, "kind": "manual" if p else "none",
            "asOf": p.get("asOf") if p else None, "note": p.get("note") if p else None,
            "coveredByPlan": p.get("coveredByPlan") if p else None,
        })

    return sources


def _with_grok_sources(sources, grok_report, plans):
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
    bot = (plans or {}).get("grok-bot") or {}
    sources.append({
        "id": "grok-bot",
        "tool": "grok-bot",
        "provider": "xai",
        "label": "Grok Bot",
        "plan": bot.get("plan"),
        "kind": "manual" if bot else "none",
        "asOf": bot.get("asOf"),
        "note": bot.get("note") or "No token or cost feed. Usage stays in the Grok Bot app.",
        "coveredByPlan": False,
        "weeklyLimitPercent": bot.get("weeklyLimitPercent"),
        "resetsAt": bot.get("resetsAt"),
        "grokBot": True,
    })
    return sources


def refresh_usage(force=False):
    try:
        result = subprocess.run(
            ["npx", "--yes", f"ccusage@{CCUSAGE_VERSION}", "--sections", "daily,session",
             "--json", "--by-agent"],
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
        agent_provider = provider_usage.AGENT_PROVIDER
        codex_live = get_codex_live(force=force)
        unofficial_toggles = provider_usage.load_unofficial_toggles()
        grok_billing = _grok_billing_cache["data"]

        today_local = datetime.datetime.now().strftime("%Y%m%d")
        blocks_json = fetch_claude_hourly_blocks(today_local)
        claude_blocks = build_hourly_from_blocks(blocks_json) if blocks_json is not None else None

        return {
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
                "claude": account_usage.get_usage_history(),
                "codex": provider_usage.get_codex_rate_limit_history(),
            },
            "sources": _with_grok_sources(build_sources(
                account_list, max_usage, codex_live, unofficial_toggles.get("grok"),
                grok_billing, plans, no_account_data, agent_provider,
            ), grok_report, plans),
        }
    except FileNotFoundError as e:
        if getattr(e, "filename", None) == "npx":
            return {
                "error": "Node.js (npx) is required to run ccusage and wasn't found on PATH.",
                "errorType": "node_missing",
            }
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


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
            return cached
        return dict(_cache["data"], stale=False)


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
        else:
            self.send_response(404)
            self.end_headers()

    def _client_is_local(self):
        return request_is_local(self.headers.get("Host"), self.headers.get("Origin"))

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


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Serving dashboard at http://127.0.0.1:{PORT}/  (Ctrl-C to stop)")
        httpd.serve_forever()
