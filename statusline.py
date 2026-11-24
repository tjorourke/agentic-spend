#!/usr/bin/env python3
"""Claude Code status line script — lives in this project so it travels with
the repo (set up via the dashboard's own "Enable live usage" action instead
of being hand-copied into ~/.claude/).

Two jobs:
1. Print a useful one-line status (cwd, model, 5h/7d plan usage) — whatever
   this prints becomes the visible status line in the terminal. If
   AGENTIC_SPEND_WRAP_STATUSLINE is set (to another status line command this
   one replaced), that command's own output is printed instead, so nothing
   the user already had is lost — only the live-data side effect is added.
2. As a side effect, persist the rate_limits payload Claude Code feeds us
   (documented: https://code.claude.com/docs/en/statusline) to a small JSON
   file per account, so other tools (this dashboard) can read real, fresh
   plan-usage numbers instead of Claude Code's own internal cache, which
   only updates on its own schedule.

Only reads what Claude Code pipes to it on stdin — no network calls, no
credentials read. Safe to remove any time: run the dashboard's "Disable live
usage" action, or just delete the "statusLine" key from
~/.claude/settings.json (restoring AGENTIC_SPEND_WRAP_STATUSLINE's value if
one was wrapped).
"""
import json
import os
import subprocess
import sys
import datetime

LIVE_DIR = os.path.expanduser("~/.claude/cc-usage-live")
CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
WRAP_COMMAND = os.environ.get("AGENTIC_SPEND_WRAP_STATUSLINE")
# Inlined rather than `import data_dir`: Claude Code runs this script with
# `python3 -I` (isolated mode), which leaves the script's own directory off
# sys.path, so a sibling import fails here even though it works everywhere
# else in this codebase.
_DATA_DIR = os.environ.get("AGENTIC_SPEND_DATA_DIR") or os.path.expanduser("~/.agentic-spend")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    pass
HISTORY_PATH = os.path.join(_DATA_DIR, "usage-history.jsonl")
HISTORY_MAX_LINES = 20000  # ~a year of 5-per-hour reads for one account; cheap to keep


def current_account():
    """Best-effort: whichever account is logged in right now, per Claude
    Code's own config cache. Same source account_usage.py already uses."""
    try:
        with open(CLAUDE_CONFIG) as f:
            cfg = json.load(f)
        oauth = cfg.get("oauthAccount") or {}
        return oauth.get("accountUuid"), oauth.get("emailAddress")
    except Exception:
        return None, None


def _last_history_sample(acct_uuid):
    """The most recent recorded sample for this account, so record_usage_history
    can skip writing one more line when nothing has actually changed. Several
    accounts can be interleaved in the file (every Claude Code session on the
    machine writes here), so this has to scan backward for a matching one,
    not just check the last line."""
    try:
        with open(HISTORY_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("accountUuid") == acct_uuid:
            return rec
    return None


def record_usage_history(acct_uuid, five_hour, seven_day):
    """Append one plan-usage sample, only when the reading actually moved —
    this script runs on every Claude Code reply on the machine, so writing
    unconditionally would make the file grow far faster than the data is
    worth. Lets the dashboard chart plan usage over time instead of showing
    only the current percentage. Best-effort: never allowed to break the
    status line itself."""
    try:
        sample = {
            "time": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
            "accountUuid": acct_uuid,
            "fiveHourPercent": five_hour.get("used_percentage"),
            "fiveHourResetsAt": five_hour.get("resets_at"),
            "sevenDayPercent": seven_day.get("used_percentage"),
            "sevenDayResetsAt": seven_day.get("resets_at"),
        }
        last = _last_history_sample(acct_uuid)
        unchanged = last and all(last.get(k) == sample[k] for k in
                                  ("fiveHourPercent", "fiveHourResetsAt", "sevenDayPercent", "sevenDayResetsAt"))
        if unchanged:
            return
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(sample) + "\n")
        if os.path.getsize(HISTORY_PATH) > 4_000_000:  # ~ worst case for HISTORY_MAX_LINES
            with open(HISTORY_PATH) as f:
                lines = f.readlines()
            if len(lines) > HISTORY_MAX_LINES:
                with open(HISTORY_PATH, "w") as f:
                    f.writelines(lines[-HISTORY_MAX_LINES:])
    except Exception:
        pass


def main():
    raw_stdin = sys.stdin.read()
    try:
        payload = json.loads(raw_stdin)
    except Exception:
        payload = {}

    rate_limits = payload.get("rate_limits") or {}
    five_hour = rate_limits.get("five_hour") or {}
    seven_day = rate_limits.get("seven_day") or {}

    # --- side effect: persist for other tools to read ---
    if five_hour or seven_day:
        acct_uuid, email = current_account()
        if acct_uuid:
            try:
                os.makedirs(LIVE_DIR, exist_ok=True)
                out_path = os.path.join(LIVE_DIR, f"{acct_uuid}.json")
                with open(out_path, "w") as f:
                    json.dump({
                        "accountUuid": acct_uuid,
                        "email": email,
                        "fiveHourPercent": five_hour.get("used_percentage"),
                        "fiveHourResetsAt": five_hour.get("resets_at"),
                        "sevenDayPercent": seven_day.get("used_percentage"),
                        "sevenDayResetsAt": seven_day.get("resets_at"),
                        "writtenAt": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
                    }, f)
                os.chmod(out_path, 0o600)
            except Exception:
                pass  # never break the status line over a write failure
            record_usage_history(acct_uuid, five_hour, seven_day)

    # --- what actually shows in the terminal ---
    if WRAP_COMMAND:
        try:
            result = subprocess.run(WRAP_COMMAND, shell=True, input=raw_stdin,
                                     capture_output=True, text=True, timeout=5)
            print(result.stdout.rstrip("\n"))
            return
        except Exception:
            pass  # fall through to our own line if the wrapped command fails

    cwd = (payload.get("workspace") or {}).get("current_dir") or os.getcwd()
    home = os.path.expanduser("~")
    short_cwd = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    model = (payload.get("model") or {}).get("display_name") or ""

    parts = [p for p in [short_cwd, model] if p]
    if five_hour.get("used_percentage") is not None:
        parts.append(f"5h {round(five_hour['used_percentage'])}%")
    if seven_day.get("used_percentage") is not None:
        parts.append(f"7d {round(seven_day['used_percentage'])}%")

    print(" | ".join(parts))


if __name__ == "__main__":
    main()
