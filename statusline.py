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
# Set on the wrapped command's environment. If an older, stacked install of
# this script sits underneath us, it sees the flag and skips its own write and
# history side effects, so one status line update is only recorded once.
ACTIVE_FLAG = "AGENTIC_SPEND_STATUSLINE_ACTIVE"
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



def _as_epoch(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            try:
                text = value[:-1] + "+00:00" if value.endswith("Z") else value
                return int(datetime.datetime.fromisoformat(text).timestamp())
            except ValueError:
                return None
    return None


def merge_live_record(previous, new, now=None):
    """Keep an in-window figure when this payload omitted that window.

    Claude Code may drop a window from rate_limits once its resets_at has
    passed, and it can also omit five_hour on a single event even mid-window
    (we have seen both in the same second). Overwriting a still-valid
    session figure with null is what left the live file looking empty for
    the 5-hour window. If the previous resets_at is already in the past we
    leave the field null — the dashboard then shows 0% with a reset note.
    """
    if not previous:
        return new
    now = now or datetime.datetime.now(tz=datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    now_ts = now.timestamp()
    out = dict(new)
    for prefix in ("fiveHour", "sevenDay"):
        if out.get(f"{prefix}Percent") is not None:
            # Same window, lower figure: an idle session re-sending the
            # reading from its own last API response. Within one window the
            # real figure never goes down, so keep the higher one.
            prev_pct = previous.get(f"{prefix}Percent")
            prev_reset = _as_epoch(previous.get(f"{prefix}ResetsAt"))
            new_reset = _as_epoch(out.get(f"{prefix}ResetsAt"))
            if (prev_pct is not None and prev_reset is not None and new_reset is not None
                    and abs(prev_reset - new_reset) <= SAME_WINDOW_SECONDS and prev_reset > now_ts
                    and out[f"{prefix}Percent"] < prev_pct):
                out[f"{prefix}Percent"] = prev_pct
            continue
        prev_pct = previous.get(f"{prefix}Percent")
        prev_reset = _as_epoch(previous.get(f"{prefix}ResetsAt"))
        if prev_pct is None and prev_reset is None:
            continue
        if prev_reset is not None and prev_reset > now_ts:
            out[f"{prefix}Percent"] = prev_pct
            out[f"{prefix}ResetsAt"] = previous.get(f"{prefix}ResetsAt")
    return out


SAME_WINDOW_SECONDS = 3600


def reading_belongs_to_other_account(acct_uuid, seven_day, live_dir=None, now=None):
    """True when this payload's weekly window is another account's, not the
    logged-in one's.

    Claude Code hands each session the rate limits from that session's own
    last API response, but we can only ask ~/.claude.json which account is
    logged in now. A session still running on the account you switched away
    from therefore sends that account's figures. The weekly reset time tells
    them apart: if it matches another account's current weekly window and not
    this account's, the reading is not ours and is not written."""
    live_dir = live_dir or LIVE_DIR
    new_reset = _as_epoch((seven_day or {}).get("resets_at"))
    if new_reset is None:
        return False
    now_ts = (now or datetime.datetime.now(tz=datetime.timezone.utc)).timestamp()
    own_reset = None
    others = []
    try:
        names = os.listdir(live_dir)
    except OSError:
        return False
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(live_dir, name)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        reset = _as_epoch(rec.get("sevenDayResetsAt"))
        if name == f"{acct_uuid}.json":
            own_reset = reset
        elif reset is not None and reset > now_ts:
            others.append(reset)
    if own_reset is not None and abs(own_reset - new_reset) <= SAME_WINDOW_SECONDS:
        return False
    if own_reset is not None and own_reset <= now_ts:
        own_reset = None  # our own window has rolled over; a new reset is expected
    matches_other = any(abs(r - new_reset) <= SAME_WINDOW_SECONDS for r in others)
    return matches_other and (own_reset is None or abs(own_reset - new_reset) > SAME_WINDOW_SECONDS)


def write_live_file(out_path, record):
    """Write the live usage file atomically: a temp file in the same folder
    (created 0600), then a rename over the real name. Readers therefore only
    ever see the old complete file or the new complete one, and a status line
    process that Claude Code cancels mid-write can't leave an empty or half
    written file behind."""
    folder = os.path.dirname(out_path)
    tmp_path = os.path.join(folder, f".{os.path.basename(out_path)}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    if (five_hour or seven_day) and not os.environ.get(ACTIVE_FLAG):
        acct_uuid, email = current_account()
        if acct_uuid and reading_belongs_to_other_account(acct_uuid, seven_day):
            acct_uuid = None  # another account's session; do not mislabel it
        if acct_uuid:
            try:
                os.makedirs(LIVE_DIR, exist_ok=True)
                out_path = os.path.join(LIVE_DIR, f"{acct_uuid}.json")
                record = {
                    "accountUuid": acct_uuid,
                    "email": email,
                    "fiveHourPercent": five_hour.get("used_percentage"),
                    "fiveHourResetsAt": five_hour.get("resets_at"),
                    "sevenDayPercent": seven_day.get("used_percentage"),
                    "sevenDayResetsAt": seven_day.get("resets_at"),
                    "writtenAt": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
                }
                previous = None
                try:
                    with open(out_path) as existing:
                        previous = json.load(existing)
                except (OSError, ValueError):
                    previous = None
                record = merge_live_record(previous, record)
                write_live_file(out_path, record)
                five_hour_for_hist = {"used_percentage": record.get("fiveHourPercent"), "resets_at": record.get("fiveHourResetsAt")}
                seven_day_for_hist = {"used_percentage": record.get("sevenDayPercent"), "resets_at": record.get("sevenDayResetsAt")}
                record_usage_history(acct_uuid, five_hour_for_hist, seven_day_for_hist)
            except Exception:
                pass  # never break the status line over a write failure

    # --- what actually shows in the terminal ---
    if WRAP_COMMAND:
        try:
            result = subprocess.run(WRAP_COMMAND, shell=True, input=raw_stdin,
                                     capture_output=True, text=True, timeout=5,
                                     env={**os.environ, ACTIVE_FLAG: "1"})
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
