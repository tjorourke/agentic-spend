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
import subprocess
import threading
import time

import account_usage
import data_dir
import provider_usage
import statusline_setup

PORT = 8787
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


def build_hourly(session_rows):
    """Bucket each session's full cost into the local hour of its lastActivity
    timestamp — ccusage has no finer-grained (per-message) timestamp to split a
    session's cost across the hours it actually ran in, so like the existing
    daily-by-account view, a long session's cost lands in a single hour rather
    than being spread across the hours it was open."""
    buckets = {}
    for row in session_rows:
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


def _build_provider_costs(force=False):
    openai_cost = get_openai_cost(force=force)
    return {"openai": openai_cost} if openai_cost is not None else {}


def build_sources(account_list, max_usage_list, codex_live, grok_on, grok_billing, plans, no_account_data, agent_provider):
    """One catalog entry per (tool, account) actually discovered — all-time,
    not scoped to whatever date range happens to be selected in the UI (that
    was the earlier "only grok in the dropdown" bug: the old per-range
    `otherAgg` was used to build the tool list). Both the Overview card and
    the Manage Plans modal render from this single list instead of four
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


def refresh_usage(force=False):
    try:
        result = subprocess.run(
            ["npx", "--yes", f"ccusage@{CCUSAGE_VERSION}", "--sections", "daily,session",
             "--json", "--by-agent"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        raw = json.loads(result.stdout)

        labels = account_usage.seed_labels_from_snapshots(
            account_usage.seed_labels_from_current_account(account_usage.load_labels())
        )
        account_usage.save_labels(labels)
        session_account_map = account_usage.build_session_account_map()
        account_list, no_account_data, daily_by_bucket, account_sessions = account_usage.compute_accounts(
            raw.get("session", []), session_account_map, labels,
        )

        max_usage = account_usage.get_max_usage_snapshots()
        plans = account_usage.load_plans()
        agent_provider = provider_usage.AGENT_PROVIDER
        codex_live = get_codex_live(force=force)
        unofficial_toggles = provider_usage.load_unofficial_toggles()
        grok_billing = _grok_billing_cache["data"]

        return {
            "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "daily": raw.get("daily", []),
            "totals": raw.get("totals", {}),
            "accounts": account_list,
            "noAccountData": no_account_data,
            "dailyByBucket": daily_by_bucket,
            "accountSessions": account_sessions,
            "maxUsage": max_usage,
            "hourly": build_hourly(raw.get("session", [])),
            "plans": plans,
            "providerStatus": provider_usage.get_provider_statuses(),
            "agentProvider": agent_provider,
            "providerCosts": _build_provider_costs(force=force),
            "liveUsageSetup": statusline_setup.get_status(),
            "codexLive": codex_live,
            "unofficialToggles": unofficial_toggles,
            "grokBilling": grok_billing,
            "sources": build_sources(
                account_list, max_usage, codex_live, unofficial_toggles.get("grok"),
                grok_billing, plans, no_account_data, agent_provider,
            ),
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
    with _lock:
        now = time.time()
        stale = _cache["data"] is None or (now - _cache["ts"] > CACHE_TTL)
        if force or stale:
            data = refresh_usage(force=force)
            if "error" not in data:
                _cache["data"] = data
                _cache["ts"] = now
            elif _cache["data"] is None:
                return data
        return _cache["data"]


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
        else:
            self.send_response(404)
            self.end_headers()

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
            self._send_json({"ok": False, "error": "Grok live billing is not enabled — turn it on in Manage plans first."}, code=400)
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
