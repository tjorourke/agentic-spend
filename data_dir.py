#!/usr/bin/env python3
"""Where this app's mutable local data lives — deliberately NOT next to the
code. Homebrew (and any packaged install in general) puts the code in a
read-only, upgrade-replaced location (Cellar/libexec); writing personal data
files there would silently lose them on the next `brew upgrade`. Override
with AGENTIC_SPEND_DATA_DIR for a non-default location.
"""
import os

DATA_DIR = os.environ.get("AGENTIC_SPEND_DATA_DIR") or os.path.expanduser("~/.agentic-spend")
os.makedirs(DATA_DIR, exist_ok=True)


def path(filename):
    return os.path.join(DATA_DIR, filename)


def atomic_write_json(target, obj):
    """Write JSON to a temp file in the same folder, then rename over the
    target, so a reader never sees a half-written file and a crash mid-write
    leaves the old file intact."""
    import json
    import tempfile
    folder = os.path.dirname(os.path.abspath(target))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(target) + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
