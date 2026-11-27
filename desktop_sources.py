#!/usr/bin/env python3
"""Usage that desktop and chat apps leave on this computer (1.8.4; Linux in 1.8.6).

Two things live here:

1. `claude_desktop_plan_usage()` reads the Claude desktop app's own
   `plan-usage-history.json`: occasional readings of the 5-hour and weekly
   plan limits, as percentages, per Claude organisation. No tokens, no cost.
   The app writes it now and then while it is open, so it can be days old.
2. `chat_app_coverage()` lists chat apps that keep no usable usage data on
   disk (ChatGPT, Gemini, Perplexity, Grok, Microsoft Copilot, Cursor on an
   individual plan), with whether each looks installed and the official page
   where the user can check usage themselves.

Both only look at file and folder names, plus that one JSON file. Nothing
here opens cookies, Local Storage, keychains or any auth file, and nothing
calls a network API.
"""
import datetime
import json
import os

import platform_info

# macOS path, relative to the home folder. On Linux the app (beta since June
# 2026) uses Electron's appData, ~/.config/Claude; see claude_desktop_path().
CLAUDE_DESKTOP_FILE = os.path.join("Library", "Application Support", "Claude", "plan-usage-history.json")
CLAUDE_DESKTOP_NAME = os.path.join("Claude", "plan-usage-history.json")
SUPPORTED_VERSIONS = (2,)


def _home(home=None):
    return home or os.path.expanduser("~")


def _iso_ms(ms):
    try:
        return datetime.datetime.fromtimestamp(float(ms) / 1000.0, tz=datetime.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _pct(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def org_label(org, labels=None, current_org=None, current_account=None):
    """A display name for a Claude organisation id. Uses a label saved for
    that org id, else the label of the signed-in account when the org is the
    signed-in one, else a short id. Never typed into code."""
    labels = labels or {}
    meta = labels.get(org)
    if isinstance(meta, dict) and meta.get("label"):
        return meta["label"]
    if current_org and org == current_org and current_account:
        acct = labels.get(current_account)
        if isinstance(acct, dict) and acct.get("label"):
            return acct["label"]
    return f"Claude organisation ({str(org)[:8]}…)" if org else "Claude organisation"


def parse_plan_usage_history(raw, labels=None, current_org=None, current_account=None):
    """Turn the file's JSON into {"orgs": [...], "lastUpdated", "version",
    "error"}. Bad samples are skipped, never guessed at."""
    out = {"orgs": [], "lastUpdated": None, "version": None, "error": None}
    if not isinstance(raw, dict):
        out["error"] = "unexpected file shape"
        return out
    version = raw.get("version")
    out["version"] = version
    if version not in SUPPORTED_VERSIONS:
        out["error"] = f"unsupported file version {version!r}"
        return out
    by_org = {}
    for s in raw.get("samples") or []:
        if not isinstance(s, dict):
            continue
        at = _iso_ms(s.get("t"))
        u = s.get("u") if isinstance(s.get("u"), dict) else {}
        five, week = _pct(u.get("fh")), _pct(u.get("sd"))
        if at is None or (five is None and week is None):
            continue
        org = s.get("org") if isinstance(s.get("org"), str) else ""
        by_org.setdefault(org, []).append({"at": at, "fiveHourPct": five, "sevenDayPct": week})
    orgs = []
    for org, samples in by_org.items():
        samples.sort(key=lambda x: x["at"])
        orgs.append({
            "org": org,
            "label": org_label(org, labels, current_org, current_account),
            "samples": samples,
            "latest": samples[-1],
        })
    orgs.sort(key=lambda o: o["latest"]["at"], reverse=True)
    out["orgs"] = orgs
    out["lastUpdated"] = orgs[0]["latest"]["at"] if orgs else None
    return out


def _current_claude_identity(home):
    """Org and account ids of the signed-in Claude Code account, from the
    oauthAccount block of ~/.claude.json (ids only; no tokens live there)."""
    try:
        with open(os.path.join(home, ".claude.json")) as f:
            oauth = (json.load(f) or {}).get("oauthAccount") or {}
        return oauth.get("organizationUuid"), oauth.get("accountUuid")
    except (OSError, ValueError, AttributeError):
        return None, None


def claude_desktop_path(home=None, sys_name=None):
    """Where the Claude desktop app keeps plan-usage-history.json on this OS.
    A home passed in (tests) ignores the host's XDG_CONFIG_HOME."""
    explicit = home is not None
    home = _home(home)
    base = platform_info.app_data_dir(home, sys_name, env={} if explicit else None)
    return os.path.join(base, CLAUDE_DESKTOP_NAME)


def claude_desktop_plan_usage(home=None, labels=None, sys_name=None):
    """{"found", "path", "orgs", "lastUpdated", "version", "error"}.
    found is False when the file is missing; an empty or unreadable file is
    found with no orgs and an error, so the dashboard can hide the card."""
    path = claude_desktop_path(home, sys_name)
    home = _home(home)
    base = {"found": False, "path": platform_info.tilde(path, home), "orgs": [], "lastUpdated": None,
            "version": None, "error": None,
            "note": "Percentages only. The Claude desktop app writes these readings now and then while it is open; there are no token or cost figures in this file."}
    if not os.path.isfile(path):
        return base
    base["found"] = True
    try:
        with open(path) as f:
            text = f.read()
        if not text.strip():
            base["error"] = "file is empty"
            return base
        raw = json.loads(text)
    except (OSError, ValueError) as e:
        base["error"] = f"could not read file: {e.__class__.__name__}"
        return base
    cur_org, cur_acct = _current_claude_identity(home)
    parsed = parse_plan_usage_history(raw, labels, cur_org, cur_acct)
    base.update(parsed)
    return base


# Chat apps with no local usage data we can read honestly. Links are each
# vendor's own help or settings page for checking usage. `apps` holds macOS
# .app bundle names and, where the vendor ships a Linux app, its .desktop
# entry name.
CHAT_APPS = (
    {"id": "chatgpt", "label": "ChatGPT", "apps": ("ChatGPT.app",),
     "link": "https://help.openai.com/en/articles/20001625-understanding-chatgpt-and-codex-usage-limits",
     "where": "ChatGPT → Settings → Usage"},
    {"id": "gemini-app", "label": "Gemini (app and web)", "apps": ("Gemini.app",),
     "link": "https://support.google.com/gemini/answer/16275805",
     "where": "gemini.google.com → Settings → Usage limits"},
    {"id": "perplexity", "label": "Perplexity", "apps": ("Perplexity.app",),
     "link": "https://www.perplexity.ai/help-center",
     "where": "Perplexity account settings"},
    {"id": "grok-app", "label": "Grok app", "apps": ("Grok.app",),
     "link": "https://docs.x.ai/grok/faq",
     "where": "Grok → Settings → Usage"},
    {"id": "ms-copilot", "label": "Microsoft Copilot", "apps": ("Copilot.app", "Microsoft Copilot.app"),
     "link": "https://support.microsoft.com/en-us/microsoft-365-copilot/ai-credits-and-limits-for-microsoft-365-subscriptions",
     "where": "Microsoft account → Services & subscriptions"},
    {"id": "cursor", "label": "Cursor (individual plan)", "apps": ("Cursor.app", "cursor.desktop"),
     "link": "https://cursor.com/dashboard",
     "where": "Cursor → Settings → Usage, or cursor.com/dashboard"},
)


def _app_roots(home, roots=None):
    if roots is not None:
        return list(roots)
    if "AGENTIC_SPEND_APP_ROOTS" in os.environ:
        return [p for p in (os.environ.get("AGENTIC_SPEND_APP_ROOTS") or "").split(os.pathsep) if p]
    return platform_info.app_dirs(home)


def _app_present(root, name):
    path = os.path.join(root, name)
    return os.path.isfile(path) if name.endswith(".desktop") else os.path.isdir(path)


def chat_app_coverage(home=None, roots=None):
    """One row per CHAT_APPS entry: installed is True when its .app bundle
    (macOS) or .desktop entry (Linux) exists in an app folder. Only the
    name is checked; nothing is opened."""
    home = _home(home)
    folders = _app_roots(home, roots)
    rows = []
    for app in CHAT_APPS:
        installed = any(_app_present(r, name) for r in folders for name in app["apps"])
        rows.append({"id": app["id"], "label": app["label"], "installed": installed,
                     "link": app["link"], "where": app["where"], "class": "D"})
    return rows
