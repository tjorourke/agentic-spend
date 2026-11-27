#!/usr/bin/env python3
"""Start Agentic Spend at login on Linux, with a systemd user unit.

    python3 login_item_linux.py on|off|status

The Linux counterpart of login_item.py (a macOS LaunchAgent), with the same
functions and the same status keys, so server.py and the dashboard use either.
The unit runs the launcher (`agentic-spend serve`) once at login and does not
restart it, matching RunAtLoad without KeepAlive. Status is read back from
disk and from systemctl, never inferred from a command's exit code.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys

UNIT = "agentic-spend.service"
HERE = os.path.dirname(os.path.abspath(__file__))


def unit_path(home=None):
    home = home or os.path.expanduser("~")
    base = os.environ.get("XDG_CONFIG_HOME") if home == os.path.expanduser("~") else None
    return os.path.join(base or os.path.join(home, ".config"), "systemd", "user", UNIT)


def resolve_launcher(here=HERE, which=shutil.which, exists=os.path.exists, env=None):
    """(argv, description). The launcher on PATH, else the one in this
    checkout, else server.py directly."""
    env = os.environ if env is None else env
    candidates = [env.get("AGENTIC_SPEND_LAUNCHER"), which("agentic-spend"),
                  os.path.join(here, "linux", "agentic-spend")]
    for c in candidates:
        if c and exists(c):
            return [c, "serve"], "launcher"
    python = which("python3") or sys.executable or "/usr/bin/python3"
    return [python, os.path.join(here, "server.py")], "server.py from this folder"


def _sd_quote(arg, executable=False):
    """One ExecStart word in systemd's syntax (not the shell's): double
    quotes, with backslash, quote and the % specifier escaped, and $ too
    except in the executable, where systemd does not expand variables.
    (systemd itself refuses an executable path holding a quote or backslash.)"""
    arg = str(arg).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if not executable:
        arg = arg.replace("$", "$$")
    return '"%s"' % arg


def build_unit(program_arguments, path_env=None):
    """The unit file as text. systemd starts user services with a short PATH,
    so the folder holding the launcher goes first, followed by the PATH this
    was switched on from: that is how the server finds codex and the like."""
    dirs = [os.path.dirname(program_arguments[0])]
    dirs += (path_env if path_env is not None else os.environ.get("PATH", "")).split(":")
    dirs += ["/usr/local/bin", "/usr/bin", "/bin"]
    seen = []
    for d in dirs:
        if d and d not in seen and not any(c in d for c in '%"\\\n'):
            seen.append(d)
    return "\n".join([
        "[Unit]",
        "Description=Agentic Spend dashboard server (http://127.0.0.1:8787)",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=%s" % " ".join(_sd_quote(a, executable=(i == 0)) for i, a in enumerate(program_arguments)),
        'Environment="PATH=%s"' % ":".join(seen),
        "Environment=AGENTIC_SPEND_NO_OPEN=1",
        # A browser opened from here would belong to this service and be
        # closed with it; the launcher and the Settings page open it instead.
        "Environment=AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1",
        "Restart=no",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _systemctl(args, runner=subprocess.run):
    try:
        r = runner(["systemctl", "--user"] + args, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def _exec_start(path):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("ExecStart="):
                    words = shlex.split(line[len("ExecStart="):].strip())
                    return [w.replace("%%", "%") if i == 0 else w.replace("%%", "%").replace("$$", "$")
                            for i, w in enumerate(words)]
    except (OSError, ValueError):
        pass
    return None


def status(home=None, runner=subprocess.run):
    path = unit_path(home)
    installed = os.path.exists(path)
    _, out = _systemctl(["show", UNIT, "--property=ActiveState,MainPID,UnitFileState"], runner)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pid = None
    try:
        pid = int(props.get("MainPID") or 0) or None
    except ValueError:
        pass
    return {
        # On means it will start at the next login: the file is there and enabled.
        "enabled": installed and props.get("UnitFileState") == "enabled",
        "loaded": props.get("ActiveState") in ("active", "activating"),
        "pid": pid,
        "plistPath": path,  # the key the dashboard and login_item.py use
        "programArguments": _exec_start(path) if installed else None,
        "manager": "systemd",
    }


def turn_on(home=None, runner=subprocess.run):
    args, how = resolve_launcher()
    path = unit_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(build_unit(args))
    os.replace(tmp, path)
    _systemctl(["daemon-reload"], runner)
    code, _ = _systemctl(["enable", UNIT], runner)
    if code != 0:  # no systemd user session: leave nothing behind
        try:
            os.remove(path)
        except OSError:
            pass
        return dict(status(home, runner), launcher=how)
    # Start it now as well, as a LaunchAgent with RunAtLoad does. If a server
    # is already running, `agentic-spend serve` sees its pidfile and exits.
    if not status(home, runner)["loaded"]:
        _systemctl(["start", "--no-block", UNIT], runner)
    result = status(home, runner)
    result["launcher"] = how
    return result


def turn_off(home=None, runner=subprocess.run):
    """Stops it starting at the next login. A server that systemd is running
    right now is left alone: the Settings toggle is served by it."""
    path = unit_path(home)
    _systemctl(["disable", UNIT], runner)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    _systemctl(["daemon-reload"], runner)
    return status(home, runner)


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
        print("Start at login: %s" % ("on" if st["enabled"] else "off"))
        print("  systemd unit: %s (%s)" % (st["plistPath"], "present" if os.path.exists(st["plistPath"]) else "absent"))
        print("  systemd: %s" % ("running, pid %s" % st["pid"] if st["loaded"] else "not running"))
        if st.get("programArguments"):
            print("  runs: %s" % " ".join(st["programArguments"]))
    if cmd == "on" and not st["enabled"]:
        return 1
    if cmd == "off" and st["enabled"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
