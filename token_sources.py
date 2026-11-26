#!/usr/bin/env python3
"""Where tokens actually went: main thread vs subagent vs compaction vs
anything unrecognised, for Claude Code and Codex. ccusage has no concept of
this at all — it only ever reports a session as one total — so this reads
the same local transcripts directly, scoped to tokens, not dollars (there's
no per-bucket pricing to attach here).

Claude Code (`~/.claude/projects/**/*.jsonl` and each session's own
`subagents/agent-*.jsonl`): one `assistant` line's usage is repeated across
several lines sharing a `requestId`, so a single request is only counted
once, keeping the line with the largest output. `api-request` lines (2.1.29+)
carry a `querySource` for the main thread, joined through `requestRef`; a
file under `subagents/` is a subagent transcript regardless of its own
internal querySource.

Codex (`~/.codex/sessions/**/rollout-*.jsonl`): unlike Claude, a rollout has
no separate per-response usage line in the versions seen here — only a
running `event_msg`/`token_count` total, so a whole file is attributed as
one unit, classified by its `session_meta.payload.source`. Codex's own
`input_tokens` figure includes already-cached tokens (Anthropic's doesn't),
so the cached amount is subtracted to land on the same "uncached input"
meaning ccusage itself uses.
"""
import datetime
import glob
import json
import os

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")

SOURCE_MAIN = "main"
SOURCE_SUBAGENT = "subagent"
SOURCE_COMPACTION = "compaction"
SOURCE_REVIEW = "review"
SOURCE_OTHER = "other"


def _empty_totals():
    return {"uncached": 0, "cacheWrite": 0, "cacheRead": 0, "output": 0, "reasoning": 0, "requests": 0}


def classify_claude_query_source(query_source):
    """The real values seen on a lived-in machine are `repl_main_thread`,
    `sdk`, and a handful of `agent:builtin:*`/`agent:custom:*` names — there
    is no confirmed example of Claude Code ever emitting a "background" or
    "compact"-flavoured querySource, so anything outside those two buckets
    goes to "other" rather than a guessed label."""
    if not query_source:
        return None
    if query_source in ("repl_main_thread", "sdk") or query_source.startswith("repl_main_thread"):
        return SOURCE_MAIN
    if "agent" in query_source:
        return SOURCE_SUBAGENT
    if "compact" in query_source:
        return SOURCE_COMPACTION
    return SOURCE_OTHER


def parse_claude_transcript(lines, in_subagent_file, subagent_type=None):
    """lines: an iterable of raw JSON-line strings from one transcript file
    (the main session file, or one of its subagents/agent-*.jsonl files).
    Returns a list of per-request dicts. Pure — does no file I/O itself, so
    it's cheap to test against a handful of fixture lines."""
    api_request_sources = {}
    pending = {}  # dedupe key -> (line_dict, source_detail)
    order = []

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue

        if rec.get("type") == "api-request":
            if rec.get("id"):
                api_request_sources[rec["id"]] = rec.get("querySource")
            continue

        if rec.get("type") != "assistant":
            continue
        message = rec.get("message") or {}
        usage = message.get("usage")
        if not usage or message.get("model") == "<synthetic>":
            continue

        key = rec.get("requestId") or message.get("id") or rec.get("uuid")
        if key is None:
            continue
        existing = pending.get(key)
        if existing is None:
            pending[key] = rec
            order.append(key)
        elif (usage.get("output_tokens") or 0) > ((existing.get("message") or {}).get("usage") or {}).get("output_tokens", 0):
            pending[key] = rec

    results = []
    for key in order:
        rec = pending[key]
        message = rec.get("message") or {}
        usage = message.get("usage") or {}

        if in_subagent_file:
            source, detail = SOURCE_SUBAGENT, f"agent:{subagent_type}" if subagent_type else "agent"
        else:
            query_source = api_request_sources.get(rec.get("requestRef"))
            classified = classify_claude_query_source(query_source)
            if classified is not None:
                source, detail = classified, query_source
            elif rec.get("isSidechain"):
                source, detail = SOURCE_SUBAGENT, "sidechain"
            else:
                source, detail = SOURCE_MAIN, "main"

        cache_creation = usage.get("cache_creation") or {}
        cache_write = cache_creation.get("ephemeral_5m_input_tokens", 0) + cache_creation.get("ephemeral_1h_input_tokens", 0)
        if not cache_write:
            cache_write = usage.get("cache_creation_input_tokens", 0) or 0

        results.append({
            "time": rec.get("timestamp"),
            "model": message.get("model"),
            "source": source,
            "sourceDetail": detail,
            "uncached": usage.get("input_tokens", 0) or 0,
            "cacheWrite": cache_write,
            "cacheRead": usage.get("cache_read_input_tokens", 0) or 0,
            "output": usage.get("output_tokens", 0) or 0,
            "reasoning": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0,
        })
    return results


def iter_claude_session_files():
    """(path, in_subagent_file, subagent_type) for every Claude Code
    transcript on this machine — the main session file and, separately, each
    of its subagent transcripts, since both need their own dedupe pass."""
    for session_path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        yield session_path, False, None
        session_dir = session_path[:-len(".jsonl")]
        for agent_path in glob.glob(os.path.join(session_dir, "subagents", "agent-*.jsonl")):
            subagent_type = None
            meta_path = agent_path[:-len(".jsonl")] + ".meta.json"
            try:
                with open(meta_path) as f:
                    subagent_type = (json.load(f) or {}).get("agentType")
            except (OSError, ValueError):
                pass
            yield agent_path, True, (subagent_type or "agent")


def get_claude_token_sources(since_dt=None):
    """since_dt: a datetime — only files modified on or after it are read,
    so a request for "today" doesn't walk months of transcripts. None means
    no filter."""
    requests = []
    for path, in_subagent_file, subagent_type in iter_claude_session_files():
        try:
            if since_dt is not None and os.path.getmtime(path) < since_dt.timestamp():
                continue
            with open(path, "r", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        requests.extend(parse_claude_transcript(lines, in_subagent_file, subagent_type))
    return requests


def classify_codex_source(source_field):
    """session_meta.payload.source is a plain string for the main thread, or
    an object with a "subagent" key for a spawned thread — "guardian" is
    Codex's own auto-review subagent, broken out as its own bucket since
    it's conceptually different from a thread the user actually asked for."""
    if isinstance(source_field, str) or source_field is None:
        return SOURCE_MAIN, (source_field or "main")
    if isinstance(source_field, dict) and "subagent" in source_field:
        subagent = source_field["subagent"]
        if isinstance(subagent, dict):
            for kind, value in subagent.items():
                if kind == "other" and value == "guardian":
                    return SOURCE_REVIEW, "guardian auto-review"
                return SOURCE_SUBAGENT, kind
        return SOURCE_SUBAGENT, "subagent"
    return SOURCE_OTHER, "unrecognised"


def parse_codex_rollout(lines):
    """One rollout file -> one totals dict, or None if it never recorded a
    real token_count. Codex has no per-response usage line to dedupe in the
    versions seen here, only a running cumulative total, so the file's last
    reading already is the right number — no summing-and-risking-double-
    counting across token_count events needed."""
    source, detail, last_usage, last_time = SOURCE_MAIN, "main", None, None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        payload = rec.get("payload") or {}
        if rec.get("type") == "session_meta" or payload.get("type") == "session_meta":
            src = payload.get("source", rec.get("source"))
            source, detail = classify_codex_source(src)
        elif payload.get("type") == "token_count":
            usage = (payload.get("info") or {}).get("total_token_usage")
            if usage:
                last_usage = usage
                last_time = rec.get("timestamp")

    if not last_usage:
        return None
    uncached = max(0, (last_usage.get("input_tokens", 0) or 0) - (last_usage.get("cached_input_tokens", 0) or 0))
    return {
        "time": last_time,
        "source": source,
        "sourceDetail": detail,
        "uncached": uncached,
        "cacheWrite": last_usage.get("cache_write_input_tokens", 0) or 0,
        "cacheRead": last_usage.get("cached_input_tokens", 0) or 0,
        "output": last_usage.get("output_tokens", 0) or 0,
        "reasoning": last_usage.get("reasoning_output_tokens", 0) or 0,
    }


def get_codex_token_sources(since_dt=None):
    threads = []
    for path in glob.glob(os.path.join(CODEX_SESSIONS, "**", "rollout-*.jsonl"), recursive=True):
        try:
            if since_dt is not None and os.path.getmtime(path) < since_dt.timestamp():
                continue
            with open(path, "r", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        parsed = parse_codex_rollout(lines)
        if parsed:
            threads.append(parsed)
    return threads


def summarize_by_source(claude_requests, codex_threads):
    """{"claude": {source: totals}, "codex": {source: totals}} — tokens
    only, deliberately: there's no per-bucket pricing to attach, and the
    point of this view is volume, not cost."""
    summary = {"claude": {}, "codex": {}}
    for tool, rows in (("claude", claude_requests), ("codex", codex_threads)):
        bucket = summary[tool]
        for row in rows:
            totals = bucket.setdefault(row["source"], _empty_totals())
            totals["uncached"] += row["uncached"]
            totals["cacheWrite"] += row["cacheWrite"]
            totals["cacheRead"] += row["cacheRead"]
            totals["output"] += row["output"]
            totals["reasoning"] += row["reasoning"]
            totals["requests"] += 1
    return summary


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def row_in_window(row, since_dt=None, until_dt=None):
    """Keep a parsed request when its timestamp sits in [since_dt, until_dt).

    until_dt is exclusive. A row with no readable time is kept only when
    there is no upper bound, so a later day cannot leak into a closed range.
    """
    raw = (row or {}).get("time")
    if not raw:
        return until_dt is None
    try:
        when = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return until_dt is None
    when = _as_utc(when)
    since = _as_utc(since_dt)
    until = _as_utc(until_dt)
    if since is not None and when < since:
        return False
    if until is not None and when >= until:
        return False
    return True


def get_token_sources(since_dt=None, until_dt=None):
    claude_requests = [r for r in get_claude_token_sources(since_dt) if row_in_window(r, since_dt, until_dt)]
    codex_threads = [r for r in get_codex_token_sources(since_dt) if row_in_window(r, since_dt, until_dt)]
    return {
        "summary": summarize_by_source(claude_requests, codex_threads),
        "generatedAt": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds"),
    }
