#!/usr/bin/env python3
"""Self-service setup/teardown for the live-usage status line (see
statusline.py). Lets the dashboard UI wire up ~/.claude/settings.json's
"statusLine" with one click, instead of the user hand-editing JSON.

Never clobbers a status line the user already had: if one is configured and
it isn't ours, we wrap it (its own output still prints, via
AGENTIC_SPEND_WRAP_STATUSLINE) and save the original config to a local
backup file so "disable" can restore it exactly.
"""
import json
import os

import data_dir

CLAUDE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline.py")
BACKUP_PATH = data_dir.path(".statusline-backup.json")


def _load_settings():
    if os.path.exists(CLAUDE_SETTINGS_PATH):
        with open(CLAUDE_SETTINGS_PATH) as f:
            return json.load(f)
    return {}


def _save_settings(settings):
    os.makedirs(os.path.dirname(CLAUDE_SETTINGS_PATH), exist_ok=True)
    with open(CLAUDE_SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def get_status():
    """{"state": "ours" | "wraps_other" | "other" | "none"}"""
    settings = _load_settings()
    current = settings.get("statusLine")
    if not current:
        return {"state": "none"}
    cmd = current.get("command", "") if isinstance(current, dict) else ""
    if SCRIPT_PATH in cmd:
        wraps = "AGENTIC_SPEND_WRAP_STATUSLINE=" in cmd
        return {"state": "wraps_other" if wraps else "ours", "command": cmd}
    return {"state": "other", "command": cmd}


def enable():
    settings = _load_settings()
    current = settings.get("statusLine")
    our_plain = f"python3 -I {SCRIPT_PATH}"

    if isinstance(current, dict) and SCRIPT_PATH in current.get("command", ""):
        return {"ok": True, "status": "already_enabled"}

    wrapped = False
    if current:
        # back up whatever was there so "disable" can restore it exactly
        with open(BACKUP_PATH, "w") as f:
            json.dump(current, f)
        orig_cmd = current.get("command", "") if isinstance(current, dict) else ""
        if orig_cmd:
            escaped = orig_cmd.replace("'", "'\\''")
            command = f"AGENTIC_SPEND_WRAP_STATUSLINE='{escaped}' {our_plain}"
            wrapped = True
        else:
            command = our_plain  # unrecognized shape; can't safely wrap it, just note we overwrote it
    else:
        command = our_plain

    settings["statusLine"] = {"type": "command", "command": command}
    _save_settings(settings)
    return {"ok": True, "status": "wrapped_existing" if wrapped else "enabled"}


def disable():
    settings = _load_settings()
    current = settings.get("statusLine")
    if not current or SCRIPT_PATH not in current.get("command", ""):
        return {"ok": True, "status": "not_ours"}  # never touch a statusLine we didn't set

    if os.path.exists(BACKUP_PATH):
        with open(BACKUP_PATH) as f:
            settings["statusLine"] = json.load(f)
        os.remove(BACKUP_PATH)
        status = "restored_previous"
    else:
        settings.pop("statusLine", None)
        status = "removed"

    _save_settings(settings)
    return {"ok": True, "status": status}
