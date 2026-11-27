#!/usr/bin/env python3
"""The live-usage status line hook, for every Claude config directory.

statusline_setup.py wires one settings.json. With CLAUDE_CONFIG_DIR listing
several directories (see claude_dirs.py), each has its own settings.json and
its own status line, so the same action is applied to each in turn, with a
backup file per directory. One directory behaves exactly as statusline_setup
does on its own.

Same four entry points as statusline_setup: get_status, enable, disable,
auto_enable_once.
"""
import hashlib
import os
import threading

import claude_dirs
import data_dir
import statusline_setup as ss

_lock = threading.Lock()
_ON = ("ours", "wraps_other")


def targets(env=None):
    """[(settings.json, backup file)]. Each directory has its own backup file,
    named after the directory, so reordering or shortening the list never
    hands one directory another's saved status line. The default ~/.claude
    keeps the original name, so an install without CLAUDE_CONFIG_DIR is
    unchanged."""
    default = os.path.expanduser("~/.claude/settings.json")
    out = []
    for path in claude_dirs.settings_paths(env):
        if path == default:
            backup = data_dir.path(".statusline-backup.json")
        else:
            tag = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
            backup = data_dir.path(".statusline-backup-%s.json" % tag)
        out.append((path, backup))
    return out


def _each(fn, only_existing=False, env=None):
    """Run a statusline_setup function once per target. Returns
    [(settings_path, result)]."""
    results = []
    with _lock:
        saved = (ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH)
        try:
            for settings_path, backup in targets(env):
                if only_existing and not os.path.isdir(os.path.dirname(settings_path)):
                    continue
                ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH = settings_path, backup
                try:
                    results.append((settings_path, fn()))
                except Exception as e:  # e.g. a settings.json that is not valid JSON
                    results.append((settings_path, {"ok": False, "status": "error", "error": "%s: %s" % (settings_path, e)}))
        finally:
            ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH = saved
    return results


def _with_paths(result, per):
    if len(per) > 1:
        result["perConfigDir"] = [dict(r, settingsPath=p) for p, r in per]
    return result


def get_status(env=None):
    """One status for the dashboard. On only when every directory is on; a
    mix reads as the first one that is not, so Enable is offered and finishes
    the job (it leaves the ones already on alone)."""
    per = _each(ss.get_status, only_existing=True, env=env)
    readable = [r for _, r in per if "state" in r]
    if not readable:
        out = {"state": "none", "needsRepair": False}
    else:
        not_on = [r for r in readable if r["state"] not in _ON]
        out = dict(not_on[0] if not_on else readable[0])
        out["needsRepair"] = any(r.get("needsRepair") for r in readable)
    errors = [r["error"] for _, r in per if r.get("status") == "error"]
    if errors:
        out["error"] = "; ".join(errors)
    return _with_paths(out, per) if per else out


def _summarise(per, quiet):
    if not per:
        return {"ok": True, "status": quiet}
    failed = [r for _, r in per if not r.get("ok")]
    changed = [r for _, r in per if r.get("status") != quiet]
    out = dict((failed or changed or [per[0][1]])[0])
    out["ok"] = not failed
    return _with_paths(out, per)


def enable(env=None):
    per = _each(ss.enable, only_existing=True, env=env)
    if not per:  # no Claude directory yet: set up the first, as statusline_setup does
        with _lock:
            saved = (ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH)
            try:
                ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH = targets(env)[0]
                return ss.enable()
            finally:
                ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH = saved
    return _summarise(per, "already_enabled")


def disable(env=None):
    return _summarise(_each(ss.disable, only_existing=True, env=env), "not_ours")


def auto_enable_once(env=None):
    """statusline_setup.auto_enable_once, with the once-only decision made for
    all directories together rather than by whichever comes first."""
    if os.environ.get("AGENTIC_SPEND_NO_AUTO_ENABLE"):
        return {"ok": True, "status": "skipped_env"}
    try:
        state = ss._load_setup_state()
        decided = bool(state.get("liveUsageAutoEnabledAt") or state.get("liveUsageDisabledAt"))

        def step():
            status = ss.get_status()
            if status["state"] in _ON:
                return ss.enable() if status.get("needsRepair") else {"ok": True, "status": "already_enabled"}
            if decided:
                return {"ok": True, "status": "skipped_already_decided"}
            return ss.enable()

        per = _each(step, only_existing=True, env=env)
        if not per:
            return {"ok": True, "status": "skipped_no_claude"}
        acted = [r for _, r in per if r.get("status") in ("enabled", "wrapped_existing", "repaired")]
        on = [r for _, r in per if r.get("status") in ("enabled", "wrapped_existing", "repaired", "already_enabled")]
        if on and not state.get("liveUsageAutoEnabledAt") and not state.get("liveUsageDisabledAt"):
            ss._record_setup({"liveUsageAutoEnabledAt": ss._now()})
        out = dict(acted[0] if acted else per[0][1])
        out["ok"] = all(r.get("ok") for _, r in per)
        return _with_paths(out, per)
    except Exception as e:  # never stop the server starting over this
        return {"ok": False, "status": "error", "error": str(e)}
