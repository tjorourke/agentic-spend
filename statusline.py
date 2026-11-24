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
