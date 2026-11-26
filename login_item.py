#!/usr/bin/env python3
"""Start Agentic Spend at login with a per-user LaunchAgent.

    python3 login_item.py on|off|status

The agent runs the Homebrew launcher (`agentic-spend serve`) once at login
(RunAtLoad true, KeepAlive false), so the server keeps sampling plan usage
without the browser being open. Status is always read back from disk and
from launchctl itself, never inferred from a command's exit code.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys

LABEL = "com.agenticspend.server"
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
    candidates += ["/opt/homebrew/bin/agentic-spend", "/usr/local/bin/agentic-spend"]
    for c in candidates:
        if c and "/Cellar/" not in c and exists(c):
            return [c, "serve"], "brew launcher"
    for c in candidates:
        if c and exists(c):
            return [c, "serve"], "brew launcher"
    # Running from a git clone: start the server directly.
    python = which("python3") or sys.executable or "/usr/bin/python3"
    return [python, os.path.join(here, "server.py")], "server.py from this folder"


def build_plist(program_arguments, home=None, path_env=None):
    """The LaunchAgent as a dict (plistlib-ready). launchd starts jobs with a
    minimal PATH, so the folder holding the launcher is put first (the bundled
    ccusage is found by path, so no Node.js is needed)."""
    home = home or os.path.expanduser("~")
    log_dir = os.path.join(os.environ.get("AGENTIC_SPEND_DATA_DIR") or os.path.join(home, ".agentic-spend"))
    first = os.path.dirname(program_arguments[0])
    dirs = [first, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen = []
    for d in (path_env.split(":") if path_env else []) + dirs:
        if d and d not in seen:
            seen.append(d)
    return {
        "Label": LABEL,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": ":".join(seen), "AGENTIC_SPEND_NO_OPEN": "1"},
        "StandardOutPath": os.path.join(log_dir, ".login.log"),
        "StandardErrorPath": os.path.join(log_dir, ".login.log"),
    }


def _uid_domain():
    return "gui/%d" % os.getuid()


def _launchctl(args, runner=subprocess.run):
    try:
        r = runner(["launchctl"] + args, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def status(home=None, runner=subprocess.run):
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
        "plistPath": path,
        "programArguments": program,
    }


def turn_on(home=None, runner=subprocess.run):
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
    st = status(home, runner)
    if st["loaded"] and not st["pid"]:
        _launchctl(["bootout", "%s/%s" % (_uid_domain(), LABEL)], runner)
    if not status(home, runner)["loaded"]:
        _launchctl(["bootstrap", _uid_domain(), path], runner)
    result = status(home, runner)
    result["launcher"] = how
    return result


def turn_off(home=None, runner=subprocess.run):
    path = plist_path(home)
    st = status(home, runner)
    # If launchd is running the server right now, leave that process alone
    # (the toggle in Settings is served by it). Removing the file is enough
    # to stop it starting at the next login.
    if st["loaded"] and not st["pid"]:
        _launchctl(["bootout", "%s/%s" % (_uid_domain(), LABEL)], runner)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
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
        state = "on" if st["enabled"] else "off"
        print("Start at login: %s" % state)
        print("  LaunchAgent: %s (%s)" % (st["plistPath"], "present" if st["enabled"] else "absent"))
        print("  launchd: %s" % ("loaded" + (", running pid %s" % st["pid"] if st["pid"] else ", not running") if st["loaded"] else "not loaded"))
        if st.get("programArguments"):
            print("  runs: %s" % " ".join(st["programArguments"]))
    if cmd == "on" and not st["enabled"]:
        return 1
    if cmd == "off" and st["enabled"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
