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
