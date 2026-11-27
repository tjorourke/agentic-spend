"""Detect which supported agent tools are present on this machine.

Each tool is `installed` if its CLI is on PATH, a known app bundle exists,
or a data directory the app already reads is present. It `hasData` if local
usage artefacts exist (session logs, a usage database, a Grok Bot cache, or
history in the Agentic Spend data directory).

Paths are resolved at call time so tests can pass a fake HOME. This module
never reads credentials or calls a network API.
"""
import os
import stat
import sys

try:
    import data_dir as _data_dir
except Exception:  # pragma: no cover - used before data_dir in some tests
    _data_dir = None


# Stable ids used in the /api/usage `detection` list and by the dashboard.
CLAUDE = "claude"
CODEX = "codex"
OPENCODE = "opencode"
GROK_CLI = "grok-cli"
GROK_BOT = "grok-bot"

# ccusage / catalog `tool` values mapped onto the ids above.
TOOL_TO_ID = {
    "claude": CLAUDE,
    "codex": CODEX,
    "opencode": OPENCODE,
    "grok": GROK_CLI,
    "grok-cli": GROK_CLI,
    "grok-bot": GROK_BOT,
}

SUPPORTED = (
    {"id": CLAUDE, "label": "Claude Code", "binaries": ("claude",), "apps": ("Claude.app",)},
    {"id": CODEX, "label": "Codex", "binaries": ("codex",), "apps": ()},
    {"id": OPENCODE, "label": "OpenCode", "binaries": ("opencode",), "apps": ()},
    {"id": GROK_CLI, "label": "Grok CLI", "binaries": ("grok",), "apps": ()},
    {"id": GROK_BOT, "label": "Grok Bot", "binaries": (), "apps": ("Grok Bot.app",)},
)

# Walk a session tree only far enough to know that *something* is there.
_MAX_WALK_DEPTH = 6
_MAX_WALK_ENTRIES = 4000


def detection_id_for_tool(tool, source=None):
    """Map a catalog/ccusage tool name onto a detection id."""
    if source and (source.get("grokBot") or source.get("tool") == "grok-bot"):
        return GROK_BOT
    return TOOL_TO_ID.get(tool, tool)


def _split_path(value):
    if not value:
        return []
    return [p for p in value.split(os.pathsep) if p]


def app_roots(home, explicit=None):
    """Directories that may contain .app bundles.

    Honours AGENTIC_SPEND_APP_ROOTS when `explicit` is not passed (colon or
    semicolon separated, same as PATH). An empty value means "scan no app
    folders", which is how an isolated test home avoids /Applications.
    """
    if explicit is not None:
        if isinstance(explicit, str):
            return _split_path(explicit)
        return list(explicit)
    if "AGENTIC_SPEND_APP_ROOTS" in os.environ:
        return _split_path(os.environ.get("AGENTIC_SPEND_APP_ROOTS") or "")
    return ["/Applications", os.path.join(home, "Applications")]


def _exists(path):
    return bool(path) and os.path.exists(path)


def _is_dir(path):
    return bool(path) and os.path.isdir(path)


def _is_file(path):
    return bool(path) and os.path.isfile(path)


def _dir_nonempty(path):
    if not _is_dir(path):
        return False
    try:
        with os.scandir(path) as it:
            for _ in it:
                return True
    except OSError:
        return False
    return False


def _binary_on_path(name, path):
    if not name:
        return False
    # shutil.which also finds PATHEXT on Windows; a small scanner is enough
    # here and keeps the search confined to the supplied PATH.
    for folder in _split_path(path):
        candidate = os.path.join(folder, name)
        if _is_executable(candidate):
            return True
        if sys.platform == "win32":
            for ext in (".exe", ".cmd", ".bat"):
                if _is_executable(candidate + ext):
                    return True
    return False


def _is_executable(path):
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if sys.platform == "win32":
        return True
    return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _app_bundle_present(app_name, roots):
    if not app_name:
        return False
    for root in roots:
        if _is_dir(os.path.join(root, app_name)):
            return True
    return False


def _has_matching_file(root, names=None, suffixes=None, max_depth=_MAX_WALK_DEPTH):
    """True if a file under root has one of the given names or suffixes.

    Stops at the first match. Does not open the file. Depth and entry caps
    keep this cheap even when a tree is large (for example OpenCode's data
    directory).
    """
    if not _is_dir(root):
        return False
    names = tuple(names or ())
    suffixes = tuple(suffixes or ())
    root = os.path.abspath(root)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        for name in filenames:
            seen += 1
            if names and name in names:
                return True
            if suffixes and any(name.endswith(suf) for suf in suffixes):
                return True
            if seen >= _MAX_WALK_ENTRIES:
                return False
        if seen >= _MAX_WALK_ENTRIES:
            return False
    return False


def _xdg(home, kind, explicit, honor_env):
    if explicit is not None:
        return explicit
    env_key = "XDG_DATA_HOME" if kind == "data" else "XDG_CONFIG_HOME"
    fallback = os.path.join(home, ".local", "share") if kind == "data" else os.path.join(home, ".config")
    if honor_env:
        return os.environ.get(env_key) or fallback
    return fallback


def _grok_home(home, explicit=None):
    if explicit is not None:
        return explicit
    env = os.environ.get("GROK_HOME")
    if env:
        return env
    return os.path.join(home, ".grok")


def _grokbot_dir(home, explicit=None):
    if explicit is not None:
        return explicit
    env = os.environ.get("GROK_BOT_PERSISTENCE_DIR")
    if env:
        return env
    return os.path.join(home, "Library", "Application Support", "Grok Bot")


def _default_data_dir():
    env = os.environ.get("AGENTIC_SPEND_DATA_DIR")
    if env:
        return env
    if _data_dir is not None:
        return _data_dir.DATA_DIR
    return None


def _hints_flag(hints, source_id):
    if not hints:
        return False
    value = hints.get(source_id)
    if isinstance(value, dict):
        return bool(value)
    return bool(value)


def _extra_claude_dirs(home):
    """Claude config directories named by CLAUDE_CONFIG_DIR, for the real home
    only (an isolated test home must not see this machine's)."""
    if os.path.abspath(home) != os.path.expanduser("~"):
        return []
    import claude_dirs
    return claude_dirs.config_dirs()


def _claude_installed(home):
    if _is_dir(os.path.join(home, ".claude")) or _exists(os.path.join(home, ".claude.json")):
        return True
    return any(_is_dir(d) for d in _extra_claude_dirs(home))


def _claude_has_data(home):
    if _has_matching_file(os.path.join(home, ".claude", "projects"), suffixes=(".jsonl",)):
        return True
    for d in _extra_claude_dirs(home):
        if _has_matching_file(os.path.join(d, "projects"), suffixes=(".jsonl",)):
            return True
    live = os.path.join(home, ".claude", "cc-usage-live")
    if _dir_nonempty(live):
        return True
    return False


def _codex_installed(home):
    return _is_dir(os.path.join(home, ".codex"))


def _codex_has_data(home):
    return _has_matching_file(
        os.path.join(home, ".codex", "sessions"),
        suffixes=(".jsonl",),
        names=(),
    )


def _opencode_data_dirs(home, xdg_data, xdg_config):
    return [
        os.path.join(home, ".opencode"),
        os.path.join(home, ".local", "share", "opencode"),
        os.path.join(home, ".config", "opencode"),
        os.path.join(xdg_data, "opencode") if xdg_data else "",
        os.path.join(xdg_config, "opencode") if xdg_config else "",
    ]


def _opencode_installed(home, xdg_data, xdg_config):
    return any(_is_dir(p) for p in _opencode_data_dirs(home, xdg_data, xdg_config))


def _opencode_has_data(home, xdg_data, xdg_config):
    # OpenCode's session database can be very large; only check it exists.
    seen = set()
    for folder in _opencode_data_dirs(home, xdg_data, xdg_config):
        if not folder or folder in seen or not _is_dir(folder):
            continue
        seen.add(folder)
        if _is_file(os.path.join(folder, "opencode.db")):
            return True
        if _dir_nonempty(os.path.join(folder, "storage")):
            return True
        if _has_matching_file(folder, suffixes=(".jsonl",), max_depth=3):
            return True
    return False


def _grok_cli_installed(home, grok_home):
    return _is_dir(grok_home)


def _grok_cli_has_data(grok_home):
    sessions = os.path.join(grok_home, "sessions")
    if _has_matching_file(sessions, names=("usage.json",), suffixes=(".json",)):
        return True
    return False


def _grokbot_installed(home, grokbot_dir, roots):
    if _is_dir(grokbot_dir):
        return True
    # The Application Support folder is enough even when the caller overrode
    # the persistence path and the default folder still exists.
    default_support = os.path.join(home, "Library", "Application Support", "Grok Bot")
    if grokbot_dir != default_support and _is_dir(default_support):
        return True
    return False


def _grokbot_has_data(grokbot_dir, data_dir_path):
    persistence = os.path.join(grokbot_dir, "sand-client-persistence")
    if _dir_nonempty(persistence):
        return True
    if _has_matching_file(grokbot_dir, suffixes=(".json",), max_depth=3):
        return True
    store = os.path.join(data_dir_path, "grokbot-usage.json") if data_dir_path else ""
    if store and _is_file(store) and os.path.getsize(store) > 2:
        return True
    return False


def detect(
    home=None,
    path=None,
    app_roots_list=None,
    grok_home=None,
    grokbot_dir=None,
    data_dir_path=None,
    hints=None,
    xdg_data_home=None,
    xdg_config_home=None,
):
    """Return one dict per supported tool: id, label, installed, hasData.

    `home` defaults to the current user home (respects $HOME). When the
    caller passes `home`, process-level XDG_* variables are ignored so a
    fake home cannot pick up the host's data directories.
    """
    explicit_home = home is not None
    home = home if home is not None else os.path.expanduser("~")
    path = path if path is not None else os.environ.get("PATH", "")
    roots = app_roots(home, explicit=app_roots_list)
    honor_xdg = (not explicit_home) and (xdg_data_home is None and xdg_config_home is None)
    xdg_data = _xdg(home, "data", xdg_data_home, honor_xdg)
    xdg_config = _xdg(home, "config", xdg_config_home, honor_xdg)
    grok_home = _grok_home(home, grok_home)
    grokbot_dir = _grokbot_dir(home, grokbot_dir)
    data_dir_path = data_dir_path if data_dir_path is not None else _default_data_dir()

    results = []
    for spec in SUPPORTED:
        source_id = spec["id"]
        installed = any(_binary_on_path(name, path) for name in spec["binaries"])
        installed = installed or any(_app_bundle_present(app, roots) for app in spec["apps"])
        has_data = _hints_flag(hints, source_id)

        if source_id == CLAUDE:
            installed = installed or _claude_installed(home)
            has_data = has_data or _claude_has_data(home)
        elif source_id == CODEX:
            installed = installed or _codex_installed(home)
            has_data = has_data or _codex_has_data(home)
        elif source_id == OPENCODE:
            installed = installed or _opencode_installed(home, xdg_data, xdg_config)
            has_data = has_data or _opencode_has_data(home, xdg_data, xdg_config)
        elif source_id == GROK_CLI:
            installed = installed or _grok_cli_installed(home, grok_home)
            has_data = has_data or _grok_cli_has_data(grok_home)
        elif source_id == GROK_BOT:
            installed = installed or _grokbot_installed(home, grokbot_dir, roots)
            has_data = has_data or _grokbot_has_data(grokbot_dir, data_dir_path)

        # Data implies the tool was present at some point; keep installed
        # as the stricter "is it here now" flag.
        results.append({
            "id": source_id,
            "label": spec["label"],
            "installed": bool(installed),
            "hasData": bool(has_data),
        })
    return results


def lookup(detection, source_id):
    for entry in detection or []:
        if entry.get("id") == source_id:
            return entry
    return None


def is_present(detection, source_id, unknown=True):
    """True if the tool is installed or has data. Unknown ids follow `unknown`."""
    entry = lookup(detection, source_id)
    if entry is None:
        return bool(unknown)
    return bool(entry.get("installed") or entry.get("hasData"))


def any_present(detection):
    return any(is_present(detection, e["id"], unknown=False) for e in (detection or []))


def is_plan_limit_row(source):
    """A reading of a plan or weekly limit, even when spend in range is $0."""
    if not source:
        return False
    if source.get("grokBot") and source.get("weeklyLimitPercent") is not None:
        return True
    if source.get("tool") == "claude" and source.get("maxUsage"):
        return True
    if source.get("tool") == "codex" and source.get("kind") == "official-live" and source.get("codexLive"):
        return True
    if source.get("tool") == "grok" and source.get("kind") == "unofficial-live" and source.get("grokBilling"):
        return True
    return False


def source_is_present(source, detection):
    source_id = detection_id_for_tool((source or {}).get("tool"), source)
    return is_present(detection, source_id, unknown=True)


def visible_in_range(source, estimate, detection):
    """Range-only lists: activity in range, or a plan-limit reading."""
    if not source_is_present(source, detection):
        return False
    if is_plan_limit_row(source):
        return True
    return (estimate or 0) > 0


def filter_absent_sources(sources, detection):
    """Drop catalog rows for tools that are not installed and have no data."""
    kept = []
    for source in sources or []:
        if source_is_present(source, detection):
            kept.append(source)
    return kept


def annotate_sources(sources, detection):
    """Copy installed/hasData onto each catalog row. Mutates and returns it."""
    for source in sources or []:
        entry = lookup(detection, detection_id_for_tool(source.get("tool"), source))
        if entry:
            source["installed"] = entry["installed"]
            source["hasData"] = entry["hasData"]
    return sources


def hints_from_usage(
    account_list=None,
    no_account_data=None,
    grok_report=None,
    grokbot=None,
    codex_live=None,
):
    """Coarse hasData hints from a usage payload already sitting in memory."""
    agents = ((no_account_data or {}).get("agents")) or {}
    return {
        CLAUDE: bool(account_list),
        CODEX: bool(codex_live) or "codex" in agents,
        OPENCODE: "opencode" in agents,
        GROK_CLI: bool((grok_report or {}).get("found")) or "grok" in agents,
        GROK_BOT: bool((grokbot or {}).get("accounts")),
    }


def attach(payload, detect_fn=None, **detect_kwargs):
    """Add `detection` to a usage payload and drop absent catalog rows."""
    payload = dict(payload or {})
    hints = hints_from_usage(
        account_list=payload.get("accounts"),
        no_account_data=payload.get("noAccountData"),
        grok_report={"found": any(
            (s or {}).get("tool") == "grok-cli" and (s or {}).get("kind") == "local-live"
            for s in (payload.get("sources") or [])
        )},
        grokbot=payload.get("grokBot"),
        codex_live=payload.get("codexLive"),
    )
    # Prefer an explicit grok report if the caller already has one.
    if "grok_report" in detect_kwargs:
        hints[GROK_CLI] = bool((detect_kwargs.pop("grok_report") or {}).get("found")) or hints[GROK_CLI]
    fn = detect_fn or detect
    detection = fn(hints=hints, **detect_kwargs)
    sources = annotate_sources(
        filter_absent_sources(payload.get("sources") or [], detection),
        detection,
    )
    payload["detection"] = detection
    payload["sources"] = sources
    return payload
