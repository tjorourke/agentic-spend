#!/usr/bin/env python3
"""Real (not list-price-estimated) spend pulled straight from a provider's own
billing API, for agents whose local log folder maps 1:1 to a single vendor.

ccusage's `agent` field already tells us which local log folder a session
came from; AGENT_PROVIDER maps the single-vendor ones to a provider id. Tools
that route to multiple backends (e.g. opencode) are deliberately left out —
we only claim a provider when the agent itself is that vendor's own CLI.

Keys live in provider-keys.json (gitignored, never committed) — nothing here
ships with a default/example key, and no key is read from this module's own
code.
"""
import datetime
import glob
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error

import data_dir

PROVIDER_KEYS_PATH = data_dir.path("provider-keys.json")
UNOFFICIAL_TOGGLES_PATH = data_dir.path("unofficial-toggles.json")
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"  # correctly weekly-scoped vs the bare endpoint
GROK_AUTH_PATH = os.path.expanduser("~/.grok/auth.json")

# agent (ccusage's local-folder identity) -> provider id, only where that
# agent is a single vendor's own CLI. Left out on purpose: opencode (routes to
# many backends), amp/droid/etc (backend unconfirmed).
AGENT_PROVIDER = {
    "codex": "openai",
    "gemini": "google",
}

SUPPORTED_PROVIDERS = {"openai"}  # the only one with a real API wired up so far


def load_provider_keys():
    if os.path.exists(PROVIDER_KEYS_PATH):
        with open(PROVIDER_KEYS_PATH) as f:
            return json.load(f)
    return {}


def save_provider_key(provider, api_key):
    keys = load_provider_keys()
    if api_key:
        keys[provider] = api_key
    else:
        keys.pop(provider, None)
    with open(PROVIDER_KEYS_PATH, "w") as f:
        json.dump(keys, f, indent=2, sort_keys=True)
    os.chmod(PROVIDER_KEYS_PATH, 0o600)  # holds an admin API key — never world-readable
    return {k: True for k in keys}  # never echo keys back, just which are set


def delete_provider_key(provider):
    keys = load_provider_keys()
    keys.pop(provider, None)
    with open(PROVIDER_KEYS_PATH, "w") as f:
        json.dump(keys, f, indent=2, sort_keys=True)
    return {k: True for k in keys}


def fetch_openai_cost(api_key, since_date, days=30):
    """Real organization spend from OpenAI's own Costs API — GET
    /v1/organization/costs, daily buckets, needs an Admin API key (sk-admin-…,
    org-owner-only, NOT a regular project key).

    Note: OpenAI's own costs take ~2-3 days to settle, so the most recent
    day or two here can read low/zero even if usage already happened —
    that's a data-latency gap in their API, not a bug here.

    Returns {"totalUSD": float, "byDay": {"YYYY-MM-DD": float}, "fetchedAt": iso}.
    """
    start_dt = datetime.datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    start_time = int(start_dt.timestamp())
    limit = min(max(days, 1), 180)

    by_day = {}
    page = None
    for _ in range(10):  # hard cap on pagination loops
        params = {"start_time": start_time, "bucket_width": "1d", "limit": limit}
        if page:
            params["page"] = page
        url = f"{OPENAI_COSTS_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"OpenAI Costs API error {e.code}: {e.read().decode(errors='ignore')[:300]}")

        for bucket in payload.get("data", []):
            day = datetime.datetime.fromtimestamp(bucket["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
            total = sum((r.get("amount") or {}).get("value", 0.0) for r in bucket.get("results", []))
            by_day[day] = by_day.get(day, 0.0) + total

        if not payload.get("has_more"):
            break
        page = payload.get("next_page")
        if not page:
            break

    return {
        "totalUSD": sum(by_day.values()),
        "byDay": by_day,
        "fetchedAt": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def get_provider_statuses():
    """Which providers have a key configured, without ever exposing the key
    itself to the frontend."""
    keys = load_provider_keys()
    return {p: (p in keys and bool(keys[p])) for p in SUPPORTED_PROVIDERS}


# ---------------------------------------------------------------------------
# Codex: real, officially documented, automatic (same trust tier as Claude's
# status line — no opt-in needed). Confirmed against OpenAI's own
# `codex app-server generate-json-schema` output and a live test call:
# `account/rateLimits/read` is a real JSON-RPC method, camelCase fields
# (usedPercent/windowDurationMins/resetsAt), newline-delimited JSON over
# stdio, no Content-Length framing. One-shot: spawn, ask, kill — no
# persistent session, no credentials read by this code (codex handles its
# own auth).
# ---------------------------------------------------------------------------

def _codex_available():
    return shutil.which("codex") is not None


def fetch_codex_rate_limits(timeout=10):
    if not _codex_available():
        return None
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "agentic-spend", "version": "0.1"}},
        }) + "\n")
        proc.stdin.flush()
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}) + "\n")
        proc.stdin.flush()

        result = None
        start = time.time()
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                result = msg.get("result")
                break
        return result
    except Exception:
        return None
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def get_codex_live_snapshot():
    """{"label", "planType", "ordinaryUsageAllowed", "primary": {"percent",
    "windowMins", "resetsAt"}, "secondary": {...}|None, "creditsBalance",
    "hasCredits", "fetchedAt"} or None if codex isn't installed/reachable."""
    result = fetch_codex_rate_limits()
    if not result:
        # 1.8.4: no codex binary (Codex desktop or IDE users) or the
        # app-server didn't answer. Fall back to the newest rate_limits
        # sample Codex itself wrote into its session logs.
        return codex_snapshot_from_sessions()
    rl = result.get("rateLimits") or {}
    primary = rl.get("primary") or {}
    secondary = rl.get("secondary")
    credits = rl.get("credits") or {}

    def _iso(ts):
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat() if ts is not None else None

    return {
        "label": "Codex",
        "source": "app-server",
        "planType": rl.get("planType"),
        "ordinaryUsageAllowed": result.get("ordinaryUsageAllowed"),
        "primary": {
            "percent": primary.get("usedPercent"),
            "windowMins": primary.get("windowDurationMins"),
            "resetsAt": _iso(primary.get("resetsAt")),
        },
        "secondary": ({
            "percent": secondary.get("usedPercent"),
            "windowMins": secondary.get("windowDurationMins"),
            "resetsAt": _iso(secondary.get("resetsAt")),
        } if secondary else None),
        "creditsBalance": credits.get("balance"),
        "hasCredits": credits.get("hasCredits"),
        "fetchedAt": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
    }


CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")


def _codex_window(w):
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    return {
        "percent": w.get("used_percent"),
        "windowMins": w.get("window_minutes"),
        "resetsAt": _codex_epoch_to_iso(w.get("resets_at")),
    }


def _latest_rate_limits_in(path):
    """(timestamp, rate_limits) of the last token_count line in one rollout
    file that carries a rate-limit reading, or None. Only rate-limit fields
    are kept; message content is never looked at beyond the line filter."""
    found = None
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                if '"token_count"' not in line or '"rate_limits"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                rl = payload.get("rate_limits")
                if isinstance(rl, dict) and _codex_window(rl.get("primary")):
                    found = (rec.get("timestamp"), rl)
    except OSError:
        return None
    return found


def codex_snapshot_from_sessions(sessions_dir=None, max_files=30):
    """Same shape as get_codex_live_snapshot(), built from the newest
    rate_limits sample in ~/.codex/sessions, with source "session-log" and
    fetchedAt set to when Codex wrote that reading (not now), so the UI never
    calls it live. None when there is no such sample."""
    root = sessions_dir or CODEX_SESSIONS_DIR
    try:
        paths = glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True)
    except OSError:
        return None
    def mtime(p):
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0
    best = None
    for path in sorted(paths, key=mtime, reverse=True)[:max_files]:
        hit = _latest_rate_limits_in(path)
        if hit and (best is None or (hit[0] or "") > (best[0] or "")):
            best = hit
    if not best:
        return None
    at, rl = best
    credits = rl.get("credits") or {}
    return {
        "label": "Codex",
        "source": "session-log",
        "planType": rl.get("plan_type"),
        "ordinaryUsageAllowed": None,
        "primary": _codex_window(rl.get("primary")),
        "secondary": _codex_window(rl.get("secondary")),
        "creditsBalance": credits.get("balance"),
        "hasCredits": credits.get("has_credits"),
        "fetchedAt": at,
    }
_codex_history_file_cache = {}  # path -> ((mtime, size), [samples from that file])


def get_codex_rate_limit_history():
    """Unlike the live app-server RPC above (current reading only), Codex's
    own rollout transcripts carry a rate_limits sample on nearly every reply
    (an event_msg/token_count line) — real history already sitting on disk,
    not something that has to be accumulated going forward. Cached per file
    by (mtime, size) so a refresh only re-parses rollouts that changed since
    the last call, not all of them."""
    samples = []
    try:
        paths = glob.glob(os.path.join(CODEX_SESSIONS_DIR, "**", "rollout-*.jsonl"), recursive=True)
    except OSError:
        return samples
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        cache_key = (st.st_mtime, st.st_size)
        cached = _codex_history_file_cache.get(path)
        if cached and cached[0] == cache_key:
            samples.extend(cached[1])
            continue
        file_samples = _parse_codex_rollout_rate_limits(path)
        _codex_history_file_cache[path] = (cache_key, file_samples)
        samples.extend(file_samples)
    samples.sort(key=lambda s: s["time"] or "")

    # Codex's own "primary" window definition isn't stable over time on this
    # machine (seen: 10080 min / 7 days, and 43200 min / 30 days) — charting
    # both as one line would zig-zag between two unrelated percentages, so
    # only the window type the most recent sample actually used is kept.
    if samples:
        current_window = samples[-1]["windowMins"]
        samples = [s for s in samples if s["windowMins"] == current_window]

    # Many identical consecutive readings (the percent hasn't moved between
    # replies) add points without adding information — collapse runs of them
    # to first-and-last so the chart stays a few points instead of thousands.
    deduped = []
    for s in samples:
        if deduped and deduped[-1]["percent"] == s["percent"] and len(deduped) >= 2 and deduped[-2]["percent"] == s["percent"]:
            deduped[-1] = s  # extend the run's end instead of growing it
        else:
            deduped.append(s)
    return deduped


def _parse_codex_rollout_rate_limits(path):
    out = []
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                if '"token_count"' not in line or '"used_percent"' not in line:
                    continue  # cheap substring check before the real parse
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                primary = (payload.get("rate_limits") or {}).get("primary")
                if not primary or primary.get("used_percent") is None:
                    continue
                out.append({
                    "time": rec.get("timestamp"),
                    "percent": primary.get("used_percent"),
                    "windowMins": primary.get("window_minutes"),
                    "resetsAt": _codex_epoch_to_iso(primary.get("resets_at")),
                })
    except OSError:
        pass
    return out


def _codex_epoch_to_iso(ts):
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Grok: UNOFFICIAL. cli-chat-proxy.grok.com/v1/billing is not documented
# anywhere — confirmed only by calling it directly and getting a real
# response back. Reuses the token already sitting in ~/.grok/auth.json
# (same file account_usage.py already reads for identity), rather than
# asking the user to paste a credential into this dashboard. Off by
# default — only called when explicitly enabled via the toggle below, and
# only on an explicit manual refresh, never polled automatically.
#
# What it actually returns is monthly on-demand/overage billing config
# (config.used / config.monthlyLimit / config.onDemandCap), NOT the weekly
# SuperGrok included-pool percentage — that's a different metric than
# originally hoped for, and the exact unit (dollars? cents?) is unconfirmed.
# Shown as raw numbers with that caveat, not asserted as a specific currency.
# ---------------------------------------------------------------------------

def load_unofficial_toggles():
    if os.path.exists(UNOFFICIAL_TOGGLES_PATH):
        with open(UNOFFICIAL_TOGGLES_PATH) as f:
            return json.load(f)
    return {}


def set_unofficial_toggle(name, enabled):
    toggles = load_unofficial_toggles()
    toggles[name] = bool(enabled)
    with open(UNOFFICIAL_TOGGLES_PATH, "w") as f:
        json.dump(toggles, f, indent=2, sort_keys=True)
    return toggles


def _grok_token():
    try:
        with open(GROK_AUTH_PATH) as f:
            entries = json.load(f)
        for entry in entries.values():
            key = entry.get("key")
            if key:
                return key
    except (OSError, ValueError):
        pass
    return None


def _cents(obj):
    try:
        return int((obj or {}).get("val")) / 100.0
    except (TypeError, ValueError):
        return None


def _parse_grok_billing(body):
    """Shape confirmed live, 2026-11-24, against both the ACP route and the
    HTTP fallback with ?format=credits — same payload either way. No
    `creditUsagePercent`/weekly-pool-% field exists in the real response
    despite earlier hopes; only on-demand/overage + period info does."""
    cfg = body.get("config") or {}
    period = cfg.get("currentPeriod") or {}
    return {
        "plan": body.get("subscription_tier"),
        "period": {"USAGE_PERIOD_TYPE_WEEKLY": "week", "USAGE_PERIOD_TYPE_MONTHLY": "month"}.get(period.get("type"), period.get("type")),
        "periodStart": period.get("start") or cfg.get("billingPeriodStart"),
        "resetsAt": period.get("end") or cfg.get("billingPeriodEnd"),
        "onDemandUsedUsd": _cents(cfg.get("onDemandUsed")),
        "onDemandCapUsd": _cents(cfg.get("onDemandCap")),
        "prepaidBalanceUsd": _cents(cfg.get("prepaidBalance")),
        "fetchedAt": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def _fetch_grok_billing_via_acp(timeout=8):
    """Preferred: let the `grok` CLI handle its own login over its ACP stdio
    protocol, rather than this code reading/replaying the token itself.
    Confirmed live, 2026-11-24. Returns None (not an error dict) on any
    failure, so the caller falls back to the direct HTTP route."""
    if not shutil.which("grok"):
        return None
    try:
        proc = subprocess.Popen(
            ["grok", "agent", "stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
    except OSError:
        return None
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "1", "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}},
        }) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}}) + "\n")
        proc.stdin.flush()
        start = time.time()
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                if "error" in msg:
                    return None  # e.g. method not found on this build — fall back
                return _parse_grok_billing(msg.get("result") or {})
        return None
    except Exception:
        return None
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def _fetch_grok_billing_via_proxy():
    """Fallback: the same HTTP call the Grok TUI's own /usage panel makes,
    using the token already in ~/.grok/auth.json. Never refreshes or writes
    back to that file."""
    token = _grok_token()
    if not token:
        return {"error": "No Grok auth token found at ~/.grok/auth.json — log into the grok CLI first."}
    req = urllib.request.Request(GROK_BILLING_URL, headers={
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _parse_grok_billing(json.loads(resp.read()))
    except urllib.error.HTTPError as e:
        return {"error": f"Grok billing endpoint error {e.code} — it may have changed or broken (unofficial, undocumented)."}
    except Exception as e:
        return {"error": f"Grok billing call failed: {e}"}


def fetch_grok_billing():
    """Only call this when load_unofficial_toggles().get("grok") is True,
    and only on an explicit user-triggered refresh — never on a timer."""
    result = _fetch_grok_billing_via_acp()
    if result is not None:
        result["source"] = "grok-acp"
        return result
    result = _fetch_grok_billing_via_proxy()
    result["source"] = "grok-proxy"
    return result
