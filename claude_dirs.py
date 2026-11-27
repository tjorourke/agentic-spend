#!/usr/bin/env python3
"""Where Claude Code keeps its files, when there is more than one place.

Claude Code uses ~/.claude, with its config cache beside it in ~/.claude.json.
Setting CLAUDE_CONFIG_DIR moves both: everything, .claude.json included, then
lives inside that directory. ccusage reads CLAUDE_CONFIG_DIR as a
comma-separated list, so the same value drives this code and ccusage and the
two always look at the same transcripts.

Unset, this is exactly the single ~/.claude the rest of the code assumed.
Stdlib only.
"""
import os


def _default_dir():
    return os.path.expanduser("~/.claude")


def config_dirs(env=None):
    """Claude config directories, in the order given, without duplicates."""
    env = os.environ if env is None else env
    dirs = []
    for part in (env.get("CLAUDE_CONFIG_DIR") or "").split(","):
        part = part.strip()
        if not part:
            continue
        path = os.path.abspath(os.path.expanduser(part))
        if path not in dirs:
            dirs.append(path)
    return dirs or [_default_dir()]


def config_json(config_dir):
    """The .claude.json that belongs to a config directory: inside it, except
    for the default ~/.claude, whose file sits in the home folder."""
    inside = os.path.join(config_dir, ".claude.json")
    if os.path.abspath(config_dir) == _default_dir():
        home_file = os.path.expanduser("~/.claude.json")
        if os.path.isfile(home_file) or not os.path.isfile(inside):
            return home_file
    return inside


def config_jsons(env=None):
    return [config_json(d) for d in config_dirs(env)]


def projects_dirs(env=None):
    return [os.path.join(d, "projects") for d in config_dirs(env)]


def settings_paths(env=None):
    return [os.path.join(d, "settings.json") for d in config_dirs(env)]


def backup_globs(env=None):
    """Claude Code's own backups of .claude.json, for every config directory."""
    globs = []
    for d in config_dirs(env):
        globs.append(config_json(d) + ".backup")
        globs.append(os.path.join(d, "backups", ".claude.json.backup.*"))
    return globs
