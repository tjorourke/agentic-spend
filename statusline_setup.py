#!/usr/bin/env python3
"""Self-service setup/teardown for the live-usage status line (see
statusline.py). Lets the dashboard wire up ~/.claude/settings.json's
"statusLine" with one click, instead of the user hand-editing JSON.

Rules this module keeps:
- A status line the user already had is wrapped (its own output still prints,
  via AGENTIC_SPEND_WRAP_STATUSLINE) and saved to a local backup so "disable"
  can restore it exactly. It is only ever wrapped ONCE.
- Anything that points at one of our own statusline.py copies (a Homebrew
  Cellar path for any version, the stable opt path, or a git checkout) counts
  as ours, however deeply earlier versions nested it. Enabling unwraps all of
  those and rewrites a single entry at a path that survives `brew upgrade`.
- Never touches a status line that isn't ours, except to wrap it.
"""
import datetime
import json
import os
import re
import shlex
import tempfile

import data_dir

CLAUDE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline.py")
BACKUP_PATH = data_dir.path(".statusline-backup.json")
SETUP_STATE_PATH = data_dir.path("setup-state.json")
WRAP_VAR = "AGENTIC_SPEND_WRAP_STATUSLINE"

# <prefix>/Cellar/agentic-spend/<version>/libexec/statusline.py
_CELLAR_RE = re.compile(r"^(?P<prefix>.*)/Cellar/agentic-spend/[^/]+/libexec/statusline\.py$")
# <prefix>/opt/agentic-spend/libexec/statusline.py  (Homebrew's stable symlink)
_OPT_RE = re.compile(r"^(?P<prefix>.*)/opt/agentic-spend/libexec/statusline\.py$")
_BREW_PREFIXES = ("/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew")


def _is_our_script(path):
    """True for any statusline.py that belongs to this project: this copy, a
    Homebrew Cellar/opt copy of any version, or a git checkout (a directory
    that also holds server.py and statusline_setup.py)."""
    if not path or os.path.basename(path) != "statusline.py":
        return False
    path = os.path.expanduser(path)
    if path == SCRIPT_PATH or _CELLAR_RE.match(path) or _OPT_RE.match(path):
        return True
    folder = os.path.dirname(path)
    return os.path.isfile(os.path.join(folder, "server.py")) and \
        os.path.isfile(os.path.join(folder, "statusline_setup.py"))


def stable_script_path():
    """Where the hook should point so it keeps working after `brew upgrade`:
    Homebrew's opt symlink when this install is (or sits next to) a brew
    install, otherwise this copy's own path (a git checkout)."""
    m = _CELLAR_RE.match(SCRIPT_PATH) or _OPT_RE.match(SCRIPT_PATH)
    if m:
        return f"{m.group('prefix')}/opt/agentic-spend/libexec/statusline.py"
    for prefix in _BREW_PREFIXES:
        candidate = f"{prefix}/opt/agentic-spend/libexec/statusline.py"
        if os.path.isfile(candidate):
            return candidate
    return SCRIPT_PATH


def _hook_command(script, wrapped=None):
    plain = f"python3 -I {script}"
    if not wrapped:
        return plain
    escaped = wrapped.replace("'", "'\\''")
    return f"{WRAP_VAR}='{escaped}' {plain}"


def unwrap(command):
    """Peel every layer of OUR hook off a statusLine command.

    Returns (user_command, layers): user_command is the genuine, non-ours
    status line command underneath (None if there isn't one), layers is how
    many of our own entries were stacked on top of it."""
    layers = 0
    cmd = command or ""
    for _ in range(50):  # nesting depth guard
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return cmd or None, layers
        wrapped = None
        if tokens and tokens[0].startswith(WRAP_VAR + "="):
            wrapped = tokens[0][len(WRAP_VAR) + 1:]
            tokens = tokens[1:]
        if len(tokens) == 3 and tokens[0].startswith("python") and tokens[1] == "-I" \
                and _is_our_script(tokens[2]):
            layers += 1
            if not wrapped:
                return None, layers
            cmd = wrapped
            continue
        return (cmd or None), layers
    return None, layers


def _load_settings():
    if os.path.exists(CLAUDE_SETTINGS_PATH):
        with open(CLAUDE_SETTINGS_PATH) as f:
            return json.load(f)
    return {}


def _atomic_write_json(path, obj, mode=0o600):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777  # keep whatever the file already had
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_settings(settings):
    _atomic_write_json(CLAUDE_SETTINGS_PATH, settings)


def _analyse(current):
    """(kind, user_command, layers). kind: none | other | ours | wraps_other."""
    if not current:
        return "none", None, 0
    cmd = current.get("command", "") if isinstance(current, dict) else ""
    user_cmd, layers = unwrap(cmd)
    if layers == 0:
        return "other", cmd, 0
    return ("wraps_other" if user_cmd else "ours"), user_cmd, layers


def get_status():
    """{"state": "ours" | "wraps_other" | "other" | "none", "command": ...,
    "needsRepair": bool}. needsRepair: ours, but nested or not at the stable
    path, so enable() would tidy it."""
    current = _load_settings().get("statusLine")
    kind, user_cmd, layers = _analyse(current)
    if kind == "none":
        return {"state": "none", "needsRepair": False}
    cmd = current.get("command", "") if isinstance(current, dict) else ""
    out = {"state": kind, "command": cmd}
    if kind in ("ours", "wraps_other"):
        out["needsRepair"] = cmd != _hook_command(stable_script_path(), user_cmd)
    else:
        out["needsRepair"] = False
    return out


def enable():
    settings = _load_settings()
    current = settings.get("statusLine")
    kind, user_cmd, _layers = _analyse(current)
    other_cmd = current.get("command", "") if kind == "other" and isinstance(current, dict) else ""
    target = _hook_command(stable_script_path(), user_cmd if kind != "other" else other_cmd)

    if kind in ("ours", "wraps_other"):
        if current.get("command") == target:
            return {"ok": True, "status": "already_enabled"}
        # tidy our own stacked/old-path entries into one. The backup keeps only
        # the genuine user line (never one of ours), or goes away if there is none.
        _write_backup(user_cmd, current)
        entry = dict(current)
        entry["command"] = target
        settings["statusLine"] = entry
        _save_settings(settings)
        return {"ok": True, "status": "repaired"}

    wrapped = False
    entry = {"type": "command", "command": target}
    if current:
        _atomic_write_json(BACKUP_PATH, current)  # so "disable" can restore it exactly
        orig_cmd = current.get("command", "") if isinstance(current, dict) else ""
        wrapped = bool(orig_cmd)
        if not orig_cmd:
            target = _hook_command(stable_script_path())  # unrecognised shape; can't wrap it
            entry["command"] = target
    settings["statusLine"] = entry
    _save_settings(settings)
    return {"ok": True, "status": "wrapped_existing" if wrapped else "enabled"}


def _write_backup(user_cmd, current):
    if user_cmd:
        entry = {k: v for k, v in (current or {}).items() if k != "command"}
        entry["type"] = entry.get("type", "command")
        entry["command"] = user_cmd
        _atomic_write_json(BACKUP_PATH, entry)
    elif os.path.exists(BACKUP_PATH):
        # keep a backup only if it holds a genuine user line, else drop it
        try:
            with open(BACKUP_PATH) as f:
                saved = json.load(f)
            genuine, _ = unwrap(saved.get("command", "") if isinstance(saved, dict) else "")
        except (OSError, ValueError):
            genuine = None
        if not genuine:
            os.remove(BACKUP_PATH)


def disable():
    settings = _load_settings()
    current = settings.get("statusLine")
    kind, user_cmd, _layers = _analyse(current)
    if kind not in ("ours", "wraps_other"):
        return {"ok": True, "status": "not_ours"}  # never touch a statusLine we didn't set

    restored = None
    if os.path.exists(BACKUP_PATH):
        try:
            with open(BACKUP_PATH) as f:
                saved = json.load(f)
            genuine, _ = unwrap(saved.get("command", "") if isinstance(saved, dict) else "")
            if genuine:
                restored = dict(saved)
                restored["command"] = genuine
        except (OSError, ValueError):
            pass
        os.remove(BACKUP_PATH)
    if restored is None and user_cmd:
        restored = dict(current)
        restored["command"] = user_cmd
    if restored is not None:
        settings["statusLine"] = restored
        status = "restored_previous"
    else:
        settings.pop("statusLine", None)
        status = "removed"
    _save_settings(settings)
    _record_setup({"liveUsageDisabledAt": _now()})
    return {"ok": True, "status": status}


# --- on by default ----------------------------------------------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _load_setup_state():
    try:
        with open(SETUP_STATE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_setup(updates):
    state = _load_setup_state()
    state.update(updates)
    _atomic_write_json(SETUP_STATE_PATH, state)


def auto_enable_once():
    """Turn live updates on the first time the server starts (and only then).

    The attempt is recorded in setup-state.json, so a person who later turns
    live updates off in Settings stays off. If our hook is already there but
    nested or on an old path, it is tidied on every start. Does nothing when
    Claude Code isn't installed, or AGENTIC_SPEND_NO_AUTO_ENABLE is set."""
    if os.environ.get("AGENTIC_SPEND_NO_AUTO_ENABLE"):
        return {"ok": True, "status": "skipped_env"}
    try:
        status = get_status()
        state = _load_setup_state()
        if status["state"] in ("ours", "wraps_other"):
            result = enable() if status.get("needsRepair") else {"ok": True, "status": "already_enabled"}
            if not state.get("liveUsageAutoEnabledAt"):
                _record_setup({"liveUsageAutoEnabledAt": _now()})
            return result
        if state.get("liveUsageAutoEnabledAt") or state.get("liveUsageDisabledAt"):
            return {"ok": True, "status": "skipped_already_decided"}
        if not os.path.isdir(os.path.dirname(CLAUDE_SETTINGS_PATH)):
            return {"ok": True, "status": "skipped_no_claude"}
        result = enable()
        _record_setup({"liveUsageAutoEnabledAt": _now()})
        return result
    except Exception as e:  # never stop the server starting over this
        return {"ok": False, "status": "error", "error": str(e)}
