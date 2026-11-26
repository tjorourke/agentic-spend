#!/usr/bin/env python3
"""Which backend each OpenCode model call actually went to, and who bills it.

OpenCode is a router, not a provider: the same session can send one prompt to
Google's Gemini API, the next to Amazon Bedrock and the next to a company
gateway. ccusage reports OpenCode spend per model only, so on its own the
dashboard could not tell real pay-as-you-go spend from usage covered by a
subscription. OpenCode's own SQLite store records the provider id on every
assistant message, and auth.json records whether that provider was signed in
with an API key or a subscription login. This module joins the two:

* ``scan_db`` reads (local day, model, provider, tokens) from opencode.db.
* ``classify`` turns a provider id into a name and a billing kind, using the
  auth.json credential type and the provider block in opencode.json(c).
* ``build`` returns the payload the dashboard uses to split each ccusage
  OpenCode model row across its backends for any date range.

Billing kinds: ``payg`` (API key, metered: real spend at list price),
``cloud`` (cloud account billing such as Bedrock or Vertex: also real spend),
``subscription`` (flat-rate plan login: covered), ``company`` (someone else's
gateway or key: not your money), ``free`` and ``unknown``. Only ``unknown``
needs Tom to pick one in Settings; a saved choice always wins over detection.
"""
import datetime
import json
import os
import re
import sqlite3
import threading
from urllib.parse import urlparse

import data_dir

DB_CANDIDATES = (
    "~/.local/share/opencode/opencode.db",
    "~/.opencode/opencode.db",
)
AUTH_PATH = "~/.local/share/opencode/auth.json"
CONFIG_CANDIDATES = (
    "~/.config/opencode/opencode.jsonc",
    "~/.config/opencode/opencode.json",
)
OVERRIDES_FILE = "opencode-providers.json"

BILLING_KINDS = ("payg", "cloud", "subscription", "company", "free", "unknown")
REAL_SPEND = {"payg": True, "cloud": True, "subscription": False,
              "company": False, "free": False, "unknown": None}
BILLING_LABELS = {
    "payg": "pay as you go",
    "cloud": "cloud account billing",
    "subscription": "covered by a subscription",
    "company": "paid by someone else (company key or gateway)",
    "free": "free tier",
    "unknown": "billing not known",
}

# OpenCode's built-in provider ids (models.dev ids) -> (name, billing when
# signed in with an API key). Signed in with a subscription login (auth type
# "oauth") the billing becomes "subscription" instead, see classify().
BUILTIN = {
    "google": ("Google AI Studio (Gemini API key)", "payg"),
    "google-vertex": ("Google Vertex AI", "cloud"),
    "google-vertex-anthropic": ("Google Vertex AI (Claude)", "cloud"),
    "openai": ("OpenAI API", "payg"),
    "anthropic": ("Anthropic API", "payg"),
    "xai": ("xAI API", "payg"),
    "openrouter": ("OpenRouter", "payg"),
    "amazon-bedrock": ("Amazon Bedrock", "cloud"),
    "azure": ("Azure OpenAI", "cloud"),
    "deepseek": ("DeepSeek API", "payg"),
    "mistral": ("Mistral API", "payg"),
    "groq": ("Groq API", "payg"),
    "fireworks-ai": ("Fireworks AI", "payg"),
    "togetherai": ("Together AI", "payg"),
    "cerebras": ("Cerebras", "payg"),
    "opencode": ("OpenCode Zen", "payg"),
    "opencode-go": ("OpenCode Go", "subscription"),
    "github-copilot": ("GitHub Copilot", "subscription"),
}
OAUTH_LABELS = {
    "openai": "ChatGPT subscription",
    "anthropic": "Claude subscription",
    "github-copilot": "GitHub Copilot",
}

_lock = threading.Lock()


def _first_existing(paths):
    for p in paths:
        full = os.path.expanduser(p)
        if os.path.isfile(full):
            return full
    return None


def _strip_jsonc(text):
    # Drop // and /* */ comments outside strings, then trailing commas.
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_config(path=None):
    path = path or _first_existing(CONFIG_CANDIDATES)
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.loads(_strip_jsonc(f.read())) or {}
    except (OSError, ValueError):
        return {}


def load_auth_types(path=None):
    """provider id -> credential type ("api", "oauth", "wellknown"). Never
    returns or logs the key itself."""
    try:
        with open(os.path.expanduser(path or AUTH_PATH)) as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: (v or {}).get("type") for k, v in auth.items() if isinstance(v, dict)}


def load_overrides(path=None):
    try:
        with open(path or data_dir.path(OVERRIDES_FILE)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (raw or {}).items()
            if isinstance(k, str) and v in BILLING_KINDS and v != "unknown"}


def validate_overrides(body):
    """Accept {providerId: billing}. An empty value or "unknown" clears it."""
    if not isinstance(body, dict):
        raise ValueError("OpenCode backend billing must be an object")
    clean = {}
    for k, v in body.items():
        if not isinstance(k, str) or ".." in k or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@/-]{0,79}", k):
            raise ValueError("bad OpenCode provider id: %r" % (k,))
        if v in (None, "", "unknown"):
            continue
        if v not in BILLING_KINDS:
            raise ValueError("bad billing for %s: %r" % (k, v))
        clean[k] = v
    return clean


def save_overrides(overrides, path=None):
    with _lock:
        data_dir.atomic_write_json(path or data_dir.path(OVERRIDES_FILE), overrides)


def _host(url):
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def classify(provider_id, auth_types=None, config=None, overrides=None):
    auth_types = auth_types or {}
    custom = ((config or {}).get("provider") or {}).get(provider_id)
    auth = auth_types.get(provider_id)
    out = {"provider": provider_id, "label": None, "billing": "unknown",
           "detectedFrom": None, "setInSettings": False}

    if provider_id in BUILTIN and not (custom and (custom.get("options") or {}).get("baseURL")):
        name, billing = BUILTIN[provider_id]
        if auth == "oauth" and provider_id in OAUTH_LABELS:
            name, billing = OAUTH_LABELS[provider_id], "subscription"
            how = "subscription login in OpenCode's auth.json"
        elif auth == "api":
            how = "API key in OpenCode's auth.json"
        elif auth:
            how = "%s credential in OpenCode's auth.json" % auth
        else:
            how = "built-in provider, credentials from the environment"
        out.update(label=name, billing=billing, detectedFrom=how)
    elif custom is not None:
        base = (custom.get("options") or {}).get("baseURL") or ""
        host = _host(base)
        name = custom.get("name") or provider_id
        if host in ("127.0.0.1", "localhost", "::1"):
            out.update(label="%s (local proxy)" % name,
                       detectedFrom="custom provider pointing at a local proxy (%s); the proxy's own plan decides who pays" % host)
        else:
            out.update(label="%s (%s)" % (name, host) if host else name,
                       detectedFrom="custom provider in opencode.json%s" % (" at " + host if host else ""))
    else:
        out.update(label=provider_id,
                   detectedFrom="provider id seen in OpenCode history, no longer configured")

    ov = (overrides or {}).get(provider_id)
    if ov:
        out.update(billing=ov, setInSettings=True)
    out["billingLabel"] = BILLING_LABELS[out["billing"]]
    out["realSpend"] = REAL_SPEND[out["billing"]]
    return out


def scan_db(db_path=None, since_ms=0):
    """[(local YYYY-MM-DD, model, provider, tokens, messages)] for assistant
    messages. Opened read-only so a running OpenCode is never disturbed."""
    db_path = db_path or _first_existing(DB_CANDIDATES)
    if not db_path:
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT time_created, json_extract(data,'$.modelID'), json_extract(data,'$.providerID'),"
            " coalesce(json_extract(data,'$.tokens.input'),0) + coalesce(json_extract(data,'$.tokens.output'),0)"
            " + coalesce(json_extract(data,'$.tokens.cache.read'),0) + coalesce(json_extract(data,'$.tokens.cache.write'),0)"
            " FROM message WHERE json_extract(data,'$.role')='assistant' AND time_created >= ?",
            (since_ms,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    agg = {}
    for ts, model, provider, tokens in rows:
        if not model or not provider:
            continue
        day = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        key = (day, model, provider)
        t, m = agg.get(key, (0, 0))
        agg[key] = (t + (tokens or 0), m + 1)
    return [(d, mo, p, t, m) for (d, mo, p), (t, m) in sorted(agg.items())]


def _has_price(model, litellm):
    if not litellm:
        return None
    bare = model.split("/")[-1]
    for prefix in ("", "gemini/", "vertex_ai/", "openai/", "anthropic/", "xai/", "bedrock/",
                   "openrouter/", "deepseek/", "mistral/", "groq/", "fireworks_ai/"):
        if prefix + model in litellm or prefix + bare in litellm:
            return True
    return False


def build(db_path=None, auth_path=None, config_path=None, overrides_path=None, litellm=None):
    """Payload for /api/usage. ``daily[day][model][provider]`` is that
    provider's share (0-1, by tokens) of the model's OpenCode use that day, so
    the dashboard can split ccusage's per-model cost for any range."""
    rows = scan_db(db_path)
    if not rows:
        return {"found": False, "providers": {}, "daily": {}, "models": {}}
    auth_types = load_auth_types(auth_path)
    config = load_config(config_path)
    overrides = load_overrides(overrides_path)
    per_day_model = {}
    for day, model, provider, tokens, msgs in rows:
        per_day_model.setdefault(day, {}).setdefault(model, {})[provider] = (tokens, msgs)
    daily = {}
    for day, models in per_day_model.items():
        for model, provs in models.items():
            total_tokens = sum(t for t, _ in provs.values())
            total_msgs = sum(m for _, m in provs.values())
            daily.setdefault(day, {})[model] = {
                p: round((t / total_tokens) if total_tokens else (m / total_msgs), 6)
                for p, (t, m) in provs.items()
            }
    provider_ids = sorted({p for _, _, p, _, _ in rows})
    providers = {p: classify(p, auth_types, config, overrides) for p in provider_ids}
    models = {}
    for _, model, provider, _, _ in rows:
        info = models.setdefault(model, {"providers": [], "listPrice": _has_price(model, litellm)})
        if provider not in info["providers"]:
            info["providers"].append(provider)
    return {"found": True, "providers": providers, "daily": daily, "models": models,
            "lastDay": rows[-1][0] if rows else None}


def split_cost(daily_rows, backends, in_range):
    """Split ccusage's OpenCode cost across backends for the days where
    ``in_range(period)`` is true. Returns {provider id: USD}; cost for a model
    with no matching OpenCode history row lands under ``None``."""
    shares = (backends or {}).get("daily") or {}
    out = {}
    for d in daily_rows or []:
        period = d.get("period")
        if not in_range(period):
            continue
        day = str(period)[:10]
        for sub in d.get("agents") or []:
            if sub.get("agent") != "opencode":
                continue
            for mb in sub.get("modelBreakdowns") or []:
                cost = mb.get("cost") or 0.0
                split = (shares.get(day) or {}).get(mb.get("modelName")) or {}
                if not split:
                    out[None] = out.get(None, 0.0) + cost
                    continue
                for provider, share in split.items():
                    out[provider] = out.get(provider, 0.0) + cost * share
    return out


def real_and_covered(daily_rows, backends, in_range):
    """(real USD, covered USD, unknown USD) for OpenCode in a range. Unknown
    is reported separately; callers count it on the unverified side."""
    providers = (backends or {}).get("providers") or {}
    real = covered = unknown = 0.0
    for provider, cost in split_cost(daily_rows, backends, in_range).items():
        flag = (providers.get(provider) or {}).get("realSpend") if provider else None
        if flag is True:
            real += cost
        elif flag is False:
            covered += cost
        else:
            unknown += cost
    return real, covered, unknown
