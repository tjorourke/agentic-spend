#!/usr/bin/env python3
"""Plan-limit notifications, extra-credit alerts and the Monday digest nudge.

The server's background sampler calls run_checks() every few minutes. The
decision logic (evaluate) is pure so it can be tested; delivery goes through
osascript's "display notification", which needs nothing beyond macOS itself.

When the Agentic Spend menu bar app is running it sends a heartbeat; while
that heartbeat is fresh, notifications are queued for the app to show
natively instead of being shown through osascript, so nothing appears twice.

Files (both in the data directory, never in the repo):
- notifications.json: the user's settings.
- notification-state.json: what has already been sent, so each threshold
  fires once per plan window.
"""
import copy
import datetime
import json
import os
import re
import subprocess
import threading

import data_dir

SETTINGS_PATH = data_dir.path("notifications.json")
STATE_PATH = data_dir.path("notification-state.json")
DEFAULT_THRESHOLDS = [80, 95]
DEFAULTS = {
    "enabled": True,
    "thresholds": list(DEFAULT_THRESHOLDS),
    "perPlan": {},
    "credits": True,
    "digest": True,
    "pace": True,
}
APP_HEARTBEAT_SECONDS = 180
SENT_RETENTION_DAYS = 60
MAX_PENDING = 30
DIGEST_WEEKDAY = 0  # Monday
DIGEST_HOUR = 9
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:|@+\-]{0,200}$")
_lock = threading.RLock()


# ---- settings ---------------------------------------------------------------

def _clean_thresholds(value):
    if not isinstance(value, list):
        raise ValueError("thresholds must be a list of numbers")
    out = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("thresholds must be numbers")
        try:
            num = float(item)
        except (TypeError, ValueError):
            raise ValueError("thresholds must be numbers")
        if num <= 0 or num > 100:
            raise ValueError("each threshold must be between 1 and 100")
        num = int(num) if num == int(num) else round(num, 1)
        if num not in out:
            out.append(num)
    if len(out) > 5:
        raise ValueError("at most 5 thresholds")
    return sorted(out)


def validate_settings(body, current=None):
    """Merge a partial settings update into current settings, validating it."""
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    merged = copy.deepcopy(current if isinstance(current, dict) else DEFAULTS)
    for flag in ("enabled", "credits", "digest", "pace"):
        if flag in body:
            if not isinstance(body[flag], bool):
                raise ValueError(flag + " must be true or false")
            merged[flag] = body[flag]
    if "thresholds" in body:
        merged["thresholds"] = _clean_thresholds(body["thresholds"])
    if "perPlan" in body:
        per = body["perPlan"]
        if not isinstance(per, dict):
            raise ValueError("perPlan must be an object")
        cleaned = dict(merged.get("perPlan") or {})
        for key, value in per.items():
            if not isinstance(key, str) or not _KEY_RE.match(key):
                raise ValueError("invalid plan key")
            if value is None:
                cleaned.pop(key, None)
            else:
                cleaned[key] = _clean_thresholds(value)
        if len(cleaned) > 50:
            raise ValueError("too many per-plan entries")
        merged["perPlan"] = cleaned
    return merged


def load_settings(path=None):
    path = path or SETTINGS_PATH
    try:
        with open(path) as f:
            raw = json.load(f)
        return validate_settings(raw, DEFAULTS)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULTS)


def save_settings(settings, path=None):
    path = path or SETTINGS_PATH
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)


def thresholds_for(settings, key):
    per = (settings or {}).get("perPlan") or {}
    if key in per:
        return list(per[key])
    return list((settings or {}).get("thresholds") or [])


# ---- state ------------------------------------------------------------------

def load_state(path=None):
    path = path or STATE_PATH
    try:
        with open(path) as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {}


def save_state(state, path=None):
    path = path or STATE_PATH
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _window_id(reset_at):
    """Resets a few seconds apart are the same window, so key on the hour."""
    dt = _parse(reset_at)
    if dt is None:
        return "unknown"
    dt = dt.astimezone(datetime.timezone.utc)
    if dt.minute >= 30:
        dt = dt + datetime.timedelta(hours=1)
    return dt.strftime("%Y-%m-%dT%H")


def _fmt_pct(p):
    p = float(p)
    return str(int(round(p))) if abs(p - round(p)) < 0.05 else "%.1f" % p


def _fmt_reset(reset_at, tz=None):
    dt = _parse(reset_at)
    if dt is None:
        return "the reset"
    local = dt.astimezone(tz) if tz else dt.astimezone()
    return local.strftime("%a %H:%M")


# ---- evaluation (pure) ------------------------------------------------------

def evaluate(plans, settings, state, now=None, tz=None):
    """Decide which notifications to send.

    plans: output of insights.build_plan_insights (key, label, pct, resetAt,
    credits). Returns (events, new_state); the input state is not modified.
    Each threshold fires once per plan window. When several thresholds are
    crossed at once only the highest is announced, and all of them are marked.
    """
    now = now or datetime.datetime.now(tz=datetime.timezone.utc)
    state = copy.deepcopy(state or {})
    sent = state.setdefault("sent", {})
    credits_seen = state.setdefault("credits", {})
    credits_notified = state.setdefault("creditsNotified", {})
    events = []
    if not (settings or {}).get("enabled", True):
        return events, state

    for plan in plans or []:
        key = plan.get("key")
        pct = plan.get("pct")
        if not key or pct is None:
            continue
        window = _window_id(plan.get("resetAt"))
        crossed = [t for t in thresholds_for(settings, key) if float(pct) >= float(t)]
        fresh = [t for t in crossed if "%s|%s|%s" % (key, window, t) not in sent]
        if fresh:
            top = max(fresh)
            for t in fresh:
                sent["%s|%s|%s" % (key, window, t)] = now.isoformat(timespec="seconds")
            label = plan.get("label") or key
            fc = plan.get("forecast") or {}
            extra = ""
            if fc.get("hitsLimitAt") and float(pct) < 100:
                extra = " At this pace it would hit 100%% around %s." % _fmt_reset(fc["hitsLimitAt"], tz)
            events.append({
                "kind": "threshold",
                "key": key,
                "threshold": top,
                "title": "%s at %s%% of its limit" % (label, _fmt_pct(pct)),
                "message": "Passed %s%%. Resets %s.%s" % (_fmt_pct(top), _fmt_reset(plan.get("resetAt"), tz), extra),
            })

        pace = plan.get("sessionPace")
        if pace and pace.get("warn") and (settings or {}).get("pace", True):
            pace_key = "pace|%s|%s" % (key, _window_id(pace.get("resetAt")))
            if pace_key not in sent:
                sent[pace_key] = now.isoformat(timespec="seconds")
                events.append(pace_event(plan, pace, tz))

        credits = plan.get("credits") if (settings or {}).get("credits", True) else None
        if credits and credits.get("value") is not None:
            try:
                value = float(credits["value"])
            except (TypeError, ValueError):
                value = None
            if value is not None:
                last = credits_seen.get(key)
                credits_seen[key] = value
                if last is not None:
                    used_more = (credits.get("kind") == "used" and value > float(last) + 1e-9) or \
                                (credits.get("kind") == "remaining" and value < float(last) - 0.005)
                    today = now.astimezone(tz).date().isoformat() if tz else now.astimezone().date().isoformat()
                    if used_more and credits_notified.get(key) != today:
                        credits_notified[key] = today
                        label = plan.get("label") or key
                        if credits.get("kind") == "remaining":
                            detail = "%s left" % _fmt_money(value, credits.get("currency"))
                        else:
                            detail = "%s used so far" % _fmt_money(value / (10 ** int(credits.get("exponent") or 2)), credits.get("currency"))
                        events.append({
                            "kind": "credits",
                            "key": key,
                            "title": "%s is using extra credit" % label,
                            "message": "Paid extra credit is being used: %s." % detail,
                        })

    cutoff = now - datetime.timedelta(days=SENT_RETENTION_DAYS)
    for k in list(sent):
        ts = _parse(sent[k])
        if ts is None or ts < cutoff:
            sent.pop(k, None)
    return events, state


def pace_event(plan, pace, tz=None):
    """The once-per-window 5-hour pacing notification."""
    label = plan.get("label") or plan.get("key")
    reset = _fmt_reset(pace.get("resetAt"), tz)
    if float(pace.get("currentPct") or 0) >= 100:
        message = "Session (5-hour) limit reached. It resets %s." % reset
    else:
        message = "At %s%% now. At this pace it hits 100%% around %s, before the %s reset." % (
            _fmt_pct(pace.get("currentPct") or 0), _fmt_reset(pace.get("hitsLimitAt"), tz), reset)
    return {
        "kind": "pace",
        "key": plan.get("key"),
        "title": "%s: Session (5-hour) on pace to run out" % label,
        "message": message,
    }


def _fmt_money(value, currency):
    symbol = {"USD": "$", "GBP": "\u00a3", "EUR": "\u20ac"}.get((currency or "").upper(), "")
    text = "%.2f" % float(value)
    if symbol:
        return symbol + text
    return text + (" " + currency if currency else " credits")


def iso_week_id(local_dt):
    year, week, _ = local_dt.isocalendar()
    return "%d-W%02d" % (year, week)


def digest_due(settings, state, now_local):
    """True on Monday from 09:00 local until the digest for this ISO week has
    been announced once."""
    if not (settings or {}).get("digest", True) or not (settings or {}).get("enabled", True):
        return False
    if now_local.weekday() != DIGEST_WEEKDAY or now_local.hour < DIGEST_HOUR:
        return False
    return (state or {}).get("digestWeek") != iso_week_id(now_local)


# ---- delivery ---------------------------------------------------------------

_OSASCRIPT = (
    "on run argv\n"
    "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)\n"
    "end run"
)


def deliver_osascript(title, message, subtitle="Agentic Spend", runner=None):
    """Show a macOS notification. Text is passed as arguments, never spliced
    into the script, so quotes in a plan name cannot break out of it."""
    runner = runner or subprocess.run
    try:
        result = runner(
            ["osascript", "-e", _OSASCRIPT, str(title)[:200], str(message)[:400], str(subtitle)[:100]],
            capture_output=True, text=True, timeout=10,
        )
        return getattr(result, "returncode", 1) == 0
    except Exception:
        return False


def app_is_active(state, now=None):
    now = now or datetime.datetime.now(tz=datetime.timezone.utc)
    beat = _parse((state or {}).get("appHeartbeat"))
    return beat is not None and (now - beat).total_seconds() <= APP_HEARTBEAT_SECONDS


def dispatch(events, state, now=None, runner=None):
    """Send events through the menu bar app (queued) or osascript. Returns
    (state, delivered list of {"title", "via"})."""
    delivered = []
    state = state if state is not None else {}
    use_app = app_is_active(state, now)
    for ev in events:
        if use_app:
            pending = state.setdefault("pending", [])
            pending.append({"title": ev["title"], "message": ev["message"], "url": ev.get("url"),
                            "at": (now or datetime.datetime.now(tz=datetime.timezone.utc)).isoformat(timespec="seconds")})
            del pending[:-MAX_PENDING]
            delivered.append({"title": ev["title"], "via": "app"})
        else:
            ok = deliver_osascript(ev["title"], ev["message"], runner=runner)
            delivered.append({"title": ev["title"], "via": "osascript" if ok else "failed"})
    return state, delivered


def record_heartbeat(now=None, path=None):
    """Called by the menu bar app. Returns the queued notifications and
    clears the queue."""
    now = now or datetime.datetime.now(tz=datetime.timezone.utc)
    with _lock:
        state = load_state(path)
        state["appHeartbeat"] = now.isoformat(timespec="seconds")
        pending = state.pop("pending", [])
        save_state(state, path)
    return pending


def run_checks(plans, digest_url=None, now=None, settings_path=None, state_path=None, runner=None):
    """One sampler pass: thresholds, credits, and the Monday digest nudge."""
    now = now or datetime.datetime.now(tz=datetime.timezone.utc)
    with _lock:
        settings = load_settings(settings_path)
        state = load_state(state_path)
        events, state = evaluate(plans, settings, state, now=now)
        local = now.astimezone()
        if digest_due(settings, state, local):
            state["digestWeek"] = iso_week_id(local)
            events.append({
                "kind": "digest",
                "key": "digest",
                "title": "Your weekly AI spend digest is ready",
                "message": "Last week's spend, plan peaks and top projects. Open %s" % (digest_url or "the dashboard"),
                "url": digest_url,
            })
        state, delivered = dispatch(events, state, now=now, runner=runner)
        state["lastCheckAt"] = now.isoformat(timespec="seconds")
        save_state(state, state_path)
    return delivered


def send_test(now=None, state_path=None, runner=None):
    with _lock:
        state = load_state(state_path)
        ev = {"title": "Agentic Spend test notification",
              "message": "Notifications are working. You'll get one when a plan passes a threshold you set."}
        state, delivered = dispatch([ev], state, now=now, runner=runner)
        save_state(state, state_path)
    return delivered[0] if delivered else {"via": "failed"}


def public_status(now=None, state_path=None, settings_path=None):
    state = load_state(state_path)
    return {
        "settings": load_settings(settings_path),
        "appActive": app_is_active(state, now),
        "lastCheckAt": state.get("lastCheckAt"),
        "digestWeek": state.get("digestWeek"),
    }
