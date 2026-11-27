#!/usr/bin/env python3
"""Start Agentic Spend at login.

    python3 login_item.py on|off|status

macOS: a per-user LaunchAgent. Linux: a systemd user service, or an XDG
autostart entry when there is no systemd user session (some containers,
WSL without systemd, minimal window managers).

Each runs the Homebrew launcher (`agentic-spend serve`) once at login
(RunAtLoad true, KeepAlive false on macOS; Restart=no on Linux), so the
server keeps sampling plan usage without the browser being open. Status is
always read back from disk and from launchctl or systemctl itself, never
inferred from a command's exit code.
"""
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys

import platform_info

LABEL = "com.agenticspend.server"
UNIT = "agentic-spend.service"
AUTOSTART = "agentic-spend.desktop"
HERE = os.path.dirname(os.path.abspath(__file__))


def plist_path(home=None):
    home = home or os.path.expanduser("~")
    return os.path.join(home, "Library", "LaunchAgents", LABEL + ".plist")


def _brew_bin_from_libexec(here):
    """.../opt/agentic-spend/libexec or .../Cellar/agentic-spend/<v>/libexec
    -> <prefix>/bin/agentic-spend, the stable symlink that survives upgrades."""
    parts = here.rstrip("/").split("/")
    for marker in ("opt", "Cellar"):
        if marker in parts:
            i = len(parts) - 1 - parts[::-1].index(marker)
            if i + 1 < len(parts) and parts[i + 1] == "agentic-spend":
                prefix = "/".join(parts[:i]) or "/"
                return os.path.join(prefix, "bin", "agentic-spend")
    return None


def resolve_launcher(here=HERE, which=shutil.which, exists=os.path.exists, env=None):
    """Find the launcher to run at login. Prefers a stable path over a
    versioned Cellar path, which would break after `brew upgrade`.
    Returns (program_arguments, description)."""
    env = os.environ if env is None else env
    candidates = []
    explicit = env.get("AGENTIC_SPEND_LAUNCHER")
    if explicit:
        candidates.append(explicit)
    derived = _brew_bin_from_libexec(here)
    if derived:
        candidates.append(derived)
    found = which("agentic-spend")
    if found:
        candidates.append(found)
    candidates += ["/opt/homebrew/bin/agentic-spend", "/usr/local/bin/agentic-spend",
                   "/home/linuxbrew/.linuxbrew/bin/agentic-spend",
                   os.path.expanduser("~/.linuxbrew/bin/agentic-spend")]
    for c in candidates:
        if c and "/Cellar/" not in c and exists(c):
            return [c, "serve"], "brew launcher"
    for c in candidates:
        if c and exists(c):
            return [c, "serve"], "brew launcher"
    # Running from a git clone: start the server directly.
    python = which("python3") or sys.executable or "/usr/bin/python3"
    return [python, os.path.join(here, "server.py")], "server.py from this folder"


def _login_path(program_arguments, path_env=None):
    """launchd and systemd start jobs with a minimal PATH, so the folder
    holding the launcher is put first (the bundled ccusage is found by path,
    so no Node.js is needed)."""
    first = os.path.dirname(program_arguments[0])
    dirs = [first, "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin",
            "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen = []
    for d in (path_env.split(":") if path_env else []) + dirs:
        if d and d not in seen:
            seen.append(d)
    return ":".join(seen)


def _log_file(home):
    log_dir = os.environ.get("AGENTIC_SPEND_DATA_DIR") or os.path.join(home, ".agentic-spend")
    return os.path.join(log_dir, ".login.log")


def build_plist(program_arguments, home=None, path_env=None):
    """The LaunchAgent as a dict (plistlib-ready)."""
    home = home or os.path.expanduser("~")
    log_file = _log_file(home)
    seen = []
    return {
        "Label": LABEL,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": _login_path(program_arguments, path_env), "AGENTIC_SPEND_NO_OPEN": "1"},
        "StandardOutPath": log_file,
        "StandardErrorPath": log_file,
    }


def _uid_domain():
    return "gui/%d" % os.getuid()


def _launchctl(args, runner=subprocess.run):
    try:
        r = runner(["launchctl"] + args, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def _mac_status(home=None, runner=subprocess.run):
    path = plist_path(home)
    installed = os.path.exists(path)
    program = None
    if installed:
        try:
            with open(path, "rb") as f:
                program = plistlib.load(f).get("ProgramArguments")
        except Exception:
            program = None
    code, out = _launchctl(["print", "%s/%s" % (_uid_domain(), LABEL)], runner)
    loaded = code == 0 and LABEL in out
    pid = None
    if loaded:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("pid = "):
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return {
        "enabled": installed,
        "loaded": loaded,
        "pid": pid,
        "method": "launchd",
        "path": path,
        "plistPath": path,
        "programArguments": program,
    }


def _mac_turn_on(home=None, runner=subprocess.run):
    args, how = resolve_launcher()
    plist = build_plist(args, home=home)
    path = plist_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, path)
    # Reload so a changed launcher path takes effect. If the server is already
    # running, `agentic-spend serve` sees the pidfile and exits straight away.
    st = _mac_status(home, runner)
    if st["loaded"] and not st["pid"]:
        _launchctl(["bootout", "%s/%s" % (_uid_domain(), LABEL)], runner)
    if not _mac_status(home, runner)["loaded"]:
        _launchctl(["bootstrap", _uid_domain(), path], runner)
    result = _mac_status(home, runner)
    result["launcher"] = how
    return result


def _mac_turn_off(home=None, runner=subprocess.run):
    path = plist_path(home)
    st = _mac_status(home, runner)
    # If launchd is running the server right now, leave that process alone
    # (the toggle in Settings is served by it). Removing the file is enough
    # to stop it starting at the next login.
    if st["loaded"] and not st["pid"]:
        _launchctl(["bootout", "%s/%s" % (_uid_domain(), LABEL)], runner)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return _mac_status(home, runner)


# --- Linux: systemd user service, or XDG autostart -------------------------

def _config_home(home=None):
    """$XDG_CONFIG_HOME, or ~/.config. A home passed in (tests) ignores the
    host's XDG_CONFIG_HOME."""
    if home is None:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg and os.path.isabs(xdg):
            return xdg
    return os.path.join(home or os.path.expanduser("~"), ".config")


def unit_path(home=None):
    return os.path.join(_config_home(home), "systemd", "user", UNIT)


def autostart_path(home=None):
    return os.path.join(_config_home(home), "autostart", AUTOSTART)


def _systemctl(args, runner=subprocess.run):
    try:
        r = runner(["systemctl", "--user"] + args, capture_output=True, text=True, timeout=15)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 127, str(e)


def systemd_available(runner=subprocess.run):
    """True when there is a systemd user manager to talk to."""
    code, _ = _systemctl(["show-environment"], runner)
    return code == 0


def _systemd_quote(arg):
    """A word for ExecStart=: quoted when it has spaces or quotes; % and $
    doubled so systemd does not expand them."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if any(c in arg for c in " \t\"'\\"):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def build_unit(program_arguments, home=None, path_env=None):
    """The systemd user unit as text. Runs once at login (Restart=no), like
    the LaunchAgent's RunAtLoad without KeepAlive."""
    home = home or os.path.expanduser("~")
    log_file = _log_file(home)
    lines = [
        "[Unit]",
        "Description=Agentic Spend dashboard server",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_systemd_quote(a) for a in program_arguments),
        "Environment=" + _systemd_quote("PATH=" + _login_path(program_arguments, path_env)),
        "Environment=AGENTIC_SPEND_NO_OPEN=1",
        "StandardOutput=append:" + log_file,
        "StandardError=append:" + log_file,
        "Restart=no",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _desktop_quote(arg):
    """Quoting for a Desktop Entry Exec= key: double quotes, with ", `, $
    and backslash escaped, and % doubled (it starts a field code)."""
    arg = arg.replace("%", "%%")
    if any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        for c in ("\\", '"', "`", "$"):
            arg = arg.replace(c, "\\" + c)
        return '"' + arg + '"'
    return arg


def build_autostart(program_arguments):
    """An XDG autostart entry (freedesktop Desktop Entry spec)."""
    exec_line = " ".join(_desktop_quote(a) for a in program_arguments)
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Agentic Spend",
        "Comment=Local AI spend dashboard server",
        "Exec=env AGENTIC_SPEND_NO_OPEN=1 " + exec_line,
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
        "",
    ])


def _read_exec(path, key):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _linux_status(home=None, runner=subprocess.run):
    unit = unit_path(home)
    auto = autostart_path(home)
    if systemd_available(runner):
        installed = os.path.exists(unit)
        code, out = _systemctl(["is-enabled", UNIT], runner)
        enabled = installed and code == 0 and out.splitlines()[:1] == ["enabled"]
        code, out = _systemctl(["is-active", UNIT], runner)
        loaded = code == 0 and out.splitlines()[:1] == ["active"]
        pid = None
        if loaded:
            code, out = _systemctl(["show", "-p", "MainPID", "--value", UNIT], runner)
            try:
                pid = int(out.strip()) or None
            except ValueError:
                pid = None
        program = _read_exec(unit, "ExecStart") if installed else None
        return {"enabled": enabled, "loaded": loaded, "pid": pid, "method": "systemd",
                "path": unit, "plistPath": None,
                "programArguments": shlex.split(program) if program else None}
    installed = os.path.exists(auto)
    program = _read_exec(auto, "Exec") if installed else None
    return {"enabled": installed, "loaded": False, "pid": None, "method": "autostart",
            "path": auto, "plistPath": None,
            "programArguments": shlex.split(program) if program else None}


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _linux_turn_on(home=None, runner=subprocess.run):
    args, how = resolve_launcher()
    if systemd_available(runner):
        _write(unit_path(home), build_unit(args, home=home))
        _systemctl(["daemon-reload"], runner)
        # Like launchctl bootstrap with RunAtLoad: start it now as well. If a
        # server is already running, `agentic-spend serve` exits at once.
        _systemctl(["enable", "--now", UNIT], runner)
    else:
        _write(autostart_path(home), build_autostart(args))
    result = _linux_status(home, runner)
    result["launcher"] = how
    return result


def _linux_turn_off(home=None, runner=subprocess.run):
    if systemd_available(runner):
        # Leave a running server alone (it may be serving this request);
        # disabling is enough to stop it starting at the next login.
        _systemctl(["disable", UNIT], runner)
        try:
            os.remove(unit_path(home))
        except FileNotFoundError:
            pass
        _systemctl(["daemon-reload"], runner)
    try:
        os.remove(autostart_path(home))
    except FileNotFoundError:
        pass
    return _linux_status(home, runner)


# --- Per-OS entry points ----------------------------------------------------

def _unsupported(home=None, runner=None):
    return {"enabled": False, "loaded": False, "pid": None, "method": None, "path": None,
            "plistPath": None, "programArguments": None,
            "error": "Start at login is supported on macOS and Linux."}


def _impl(sys_name):
    s = sys_name or platform_info.system()
    if s == platform_info.MACOS:
        return _mac_status, _mac_turn_on, _mac_turn_off
    if s == platform_info.LINUX:
        return _linux_status, _linux_turn_on, _linux_turn_off
    return _unsupported, _unsupported, _unsupported


def status(home=None, runner=subprocess.run, sys_name=None):
    return _impl(sys_name)[0](home, runner)


def turn_on(home=None, runner=subprocess.run, sys_name=None):
    return _impl(sys_name)[1](home, runner)


def turn_off(home=None, runner=subprocess.run, sys_name=None):
    return _impl(sys_name)[2](home, runner)


def describe_location(st):
    """Where the setting lives, for messages: the file and what reads it."""
    method = (st or {}).get("method")
    path = platform_info.tilde((st or {}).get("path") or "")
    if method == "launchd":
        return "LaunchAgent %s" % path
    if method == "systemd":
        return "systemd user service %s" % path
    if method == "autostart":
        return "autostart entry %s" % path
    return "start-at-login setting"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "on":
        st = turn_on()
    elif cmd == "off":
        st = turn_off()
    elif cmd == "status":
        st = status()
    else:
        print("usage: agentic-spend login on|off|status", file=sys.stderr)
        return 2
    if "--json" in argv:
        print(json.dumps(st))
    else:
        state = "on" if st["enabled"] else "off"
        print("Start at login: %s" % state)
        if st.get("error"):
            print("  %s" % st["error"])
        elif st.get("method") == "autostart":
            print("  autostart entry: %s (%s)" % (st["path"], "present" if st["enabled"] else "absent"))
            print("  no systemd user session, so it starts with your desktop session")
        else:
            kind = "LaunchAgent" if st.get("method") == "launchd" else "systemd user service"
            print("  %s: %s (%s)" % (kind, st["path"], "present" if st["enabled"] else "absent"))
            print("  %s: %s" % (st.get("method"), "loaded" + (", running pid %s" % st["pid"] if st["pid"] else ", not running") if st["loaded"] else "not loaded"))
        if st.get("programArguments"):
            print("  runs: %s" % " ".join(st["programArguments"]))
    if cmd == "on" and not st["enabled"]:
        return 1
    if cmd == "off" and st["enabled"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
