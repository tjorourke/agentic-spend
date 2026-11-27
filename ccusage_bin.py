#!/usr/bin/env python3
"""Where the ccusage binary comes from, and how to run it.

ccusage 20 is a native (Rust) program. The Homebrew formula installs the right
build for this Mac or Linux machine into libexec/vendor/ccusage, so a brew install needs no
Node.js. Order of preference:

1. AGENTIC_SPEND_CCUSAGE: an explicit path (or command name) you chose.
2. The bundled binary next to this file (vendor/ccusage).
3. `ccusage` on PATH (for example one you installed yourself).
4. Last resort, only when running from source with none of the above:
   `npx --yes ccusage@<version>`. This needs Node.js and the network, and
   the message from describe() says so.

Pure and stdlib only, so it is easy to test.
"""
import os
import shutil

CCUSAGE_VERSION = "20.1.2"
ENV_VAR = "AGENTIC_SPEND_CCUSAGE"
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "ccusage")


def _runnable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def resolve(env=None, bundled=None, which=None):
    """The ccusage to run: {"argv": [...], "source": "env|bundled|path|npx|missing",
    "path": str|None, "error": str|None}. No process is started."""
    env = os.environ if env is None else env
    bundled = BUNDLED if bundled is None else bundled
    which = which or shutil.which

    explicit = (env.get(ENV_VAR) or "").strip()
    if explicit:
        path = explicit if os.sep in explicit else which(explicit)
        if _runnable(path):
            return {"argv": [path], "source": "env", "path": path, "error": None}
        return {"argv": None, "source": "missing", "path": explicit,
                "error": f"{ENV_VAR} is set to {explicit!r}, which is not an executable file."}

    if _runnable(bundled):
        return {"argv": [bundled], "source": "bundled", "path": bundled, "error": None}

    on_path = which("ccusage")
    if on_path:
        return {"argv": [on_path], "source": "path", "path": on_path, "error": None}

    npx = which("npx")
    if npx:
        return {"argv": [npx, "--yes", f"ccusage@{CCUSAGE_VERSION}"], "source": "npx", "path": npx,
                "error": None}

    return {"argv": None, "source": "missing", "path": None,
            "error": "ccusage was not found. Install Agentic Spend with Homebrew (it bundles ccusage), "
                     f"or set {ENV_VAR} to a ccusage binary, or put ccusage on your PATH."}


class CcusageNotFound(FileNotFoundError):
    """Raised when no ccusage can be run. str() is the plain message."""
    filename = "ccusage"

    def __str__(self):
        return self.args[0] if self.args else "ccusage not found"


def command(*args, **kwargs):
    """argv for a ccusage run, e.g. command("blocks", "--json"). Raises
    FileNotFoundError (errno-style, filename "ccusage") when nothing is available."""
    found = resolve(**kwargs)
    if not found["argv"]:
        raise CcusageNotFound(found["error"])
    return found["argv"] + list(args)


def describe(found=None):
    """One line for logs and the dashboard: which ccusage is in use and why."""
    found = found or resolve()
    if found["source"] == "bundled":
        return f"ccusage {CCUSAGE_VERSION} (bundled)"
    if found["source"] == "env":
        return f"ccusage from {ENV_VAR}: {found['path']}"
    if found["source"] == "path":
        return f"ccusage from PATH: {found['path']}"
    if found["source"] == "npx":
        return (f"no bundled ccusage; falling back to npx ccusage@{CCUSAGE_VERSION} "
                "(needs Node.js and the network; a Homebrew install bundles ccusage instead)")
    return found["error"] or "ccusage not found"
