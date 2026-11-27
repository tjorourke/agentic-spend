#!/usr/bin/env python3
"""Which operating system this is, and the per-OS paths and words that follow.

Agentic Spend runs on macOS and Linux. Everything that differs between them
asks this module rather than checking sys.platform itself: where desktop apps
keep their data, how to open a URL, what to call "this machine" in the UI.

Every function takes an optional `system` ("macos", "linux", "windows" or
"other") so tests can ask about another OS without patching sys.platform.
Stdlib only.
"""
import os
import shutil
import sys

MACOS = "macos"
LINUX = "linux"
WINDOWS = "windows"
OTHER = "other"

_LABELS = {MACOS: "macOS", LINUX: "Linux", WINDOWS: "Windows", OTHER: "this OS"}


def system(platform=None):
    """'macos', 'linux', 'windows' or 'other' for a sys.platform value."""
    p = sys.platform if platform is None else platform
    if p == "darwin":
        return MACOS
    if p.startswith("linux"):
        return LINUX
    if p in ("win32", "cygwin"):
        return WINDOWS
    return OTHER


def _sys(value):
    return value if value in _LABELS else system()


def label(sys_name=None):
    return _LABELS[_sys(sys_name)]


def here(sys_name=None):
    """How the UI refers to the machine it runs on: 'this Mac' or 'this computer'."""
    return "this Mac" if _sys(sys_name) == MACOS else "this computer"


def app_data_dir(home=None, sys_name=None, env=None):
    """The folder desktop (Electron) apps keep their user data under, the same
    one Electron's app.getPath("appData") returns: ~/Library/Application Support
    on macOS, $XDG_CONFIG_HOME or ~/.config on Linux, %APPDATA% on Windows."""
    s = _sys(sys_name)
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if s == MACOS:
        return os.path.join(home, "Library", "Application Support")
    if s == WINDOWS:
        return env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return xdg
    return os.path.join(home, ".config")


def desktop_entry_dirs(home=None, sys_name=None, env=None):
    """Where Linux desktop environments look for installed apps' .desktop
    files (XDG data dirs, plus Flatpak and Snap exports). Empty elsewhere."""
    if _sys(sys_name) != LINUX:
        return []
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    data_home = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    data_dirs = [d for d in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    roots = [data_home] + data_dirs + [
        os.path.join(home, ".local", "share", "flatpak", "exports", "share"),
        "/var/lib/flatpak/exports/share",
    ]
    out = []
    for r in roots:
        d = os.path.join(r, "applications")
        if d not in out:
            out.append(d)
    if "/var/lib/snapd/desktop/applications" not in out:
        out.append("/var/lib/snapd/desktop/applications")
    return out


def app_dirs(home=None, sys_name=None, env=None):
    """Folders that hold installed apps: .app bundles on macOS, .desktop
    entries on Linux."""
    s = _sys(sys_name)
    home = home or os.path.expanduser("~")
    if s == MACOS:
        return ["/Applications", os.path.join(home, "Applications")]
    if s == LINUX:
        return desktop_entry_dirs(home, s, env)
    return []


def tilde(path, home=None):
    """A path for display, with the home folder shown as ~."""
    if not path:
        return path
    home = home or os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


def open_url_command(url, sys_name=None, which=shutil.which):
    """argv that opens a URL in the default browser, or None when there is no
    opener (for example a Linux server with no desktop)."""
    s = _sys(sys_name)
    if s == MACOS:
        return ["open", url]
    if s == LINUX:
        for prog in ("xdg-open", "gio", "sensible-browser"):
            found = which(prog)
            if found:
                return [found, "open", url] if prog == "gio" else [found, url]
        return None
    if s == WINDOWS:
        return ["cmd", "/c", "start", "", url]
    return None


def info(home=None, sys_name=None):
    """What the dashboard needs to word things for this OS."""
    s = _sys(sys_name)
    return {
        "os": s,
        "osLabel": label(s),
        "here": here(s),
        "appDataDir": tilde(app_data_dir(home, s), home),
        "notifier": {MACOS: "osascript", LINUX: "notify-send"}.get(s),
        "menuBarApp": s == MACOS,
    }
