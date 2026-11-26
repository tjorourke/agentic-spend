#!/usr/bin/env python3
"""Estimated saving if the Opus usage had run on Sonnet.

ccusage does the pricing, so the estimate uses exactly the same rules as
every other figure on the dashboard (cache writes, one-hour cache, long
context tiers): we run ccusage once more with a temporary config whose
pricingOverrides give each Opus model the Sonnet model's LiteLLM rates, and
compare its Opus costs with the normal run.

Both runs use `--mode calculate`, so ccusage prices every token from the
price list instead of using costs some tools record in their own logs
(OpenCode does); otherwise a recorded Opus cost would hide the override.
The saving is calculated-Opus minus calculated-Opus-at-Sonnet-rates.

The Sonnet rates come from the LiteLLM price list ccusage itself downloaded
(its HTTP cache in ~/.cache/ccusage/http-cache), so they match what ccusage
used. With no cached list there is no estimate, and the payload says why.

This is an estimate: the same tokens would not necessarily be produced by a
different model, and Sonnet may not have done the same work.
"""
import glob
import json
import os
import re
import subprocess
import tempfile

import ccusage_bin
import data_dir

LITELLM_GLOB = os.path.expanduser(
    "~/.cache/ccusage/http-cache/raw.githubusercontent.com-BerriAI-litellm-*.cache")
USER_CONFIGS = (
    os.path.expanduser("~/.config/ccusage/ccusage.json"),
    os.path.join(os.getcwd(), ".ccusage", "ccusage.json"),
)
# LiteLLM field -> ccusage pricingOverrides field
FIELD_MAP = {
    "input_cost_per_token": "inputCostPerToken",
    "output_cost_per_token": "outputCostPerToken",
    "cache_creation_input_token_cost": "cacheCreationInputTokenCost",
    "cache_read_input_token_cost": "cacheReadInputTokenCost",
    "cache_creation_input_token_cost_above_1hr": "cacheCreationInputTokenCostAbove1hr",
    "input_cost_per_token_above_200k_tokens": "inputCostPerTokenAbove200kTokens",
    "output_cost_per_token_above_200k_tokens": "outputCostPerTokenAbove200kTokens",
    "cache_creation_input_token_cost_above_200k_tokens": "cacheCreationInputTokenCostAbove200kTokens",
    "cache_read_input_token_cost_above_200k_tokens": "cacheReadInputTokenCostAbove200kTokens",
}
_SONNET_KEY = re.compile(r"^claude-sonnet-(\d+(?:-\d+)*)$")


def is_opus(model):
    return "claude-opus" in (model or "").lower() or "claude-3-opus" in (model or "").lower()


def is_sonnet(model):
    return "claude-sonnet" in (model or "").lower() or "claude-3-5-sonnet" in (model or "").lower()


def load_litellm(pattern=None):
    """The LiteLLM price list from ccusage's own HTTP cache, or None."""
    paths = sorted(glob.glob(pattern or LITELLM_GLOB), key=os.path.getmtime, reverse=True)
    for path in paths:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            start = raw.find(b"{")
            if start < 0:
                continue
            data = json.loads(raw[start:])
            if isinstance(data, dict) and data:
                return data
        except (OSError, ValueError):
            continue
    return None


def model_spend(daily_rows):
    totals = {}
    for row in daily_rows or []:
        for mb in row.get("modelBreakdowns") or []:
            name = mb.get("modelName")
            if name:
                totals[name] = totals.get(name, 0.0) + float(mb.get("cost") or 0.0)
    return totals


def pick_sonnet(daily_rows, litellm):
    """The Sonnet to compare with: the one you spent most on (it is the one
    you actually use), else the newest claude-sonnet-N in LiteLLM."""
    spend = {m: c for m, c in model_spend(daily_rows).items() if is_sonnet(m)}
    for model, _ in sorted(spend.items(), key=lambda kv: -kv[1]):
        bare = model.split("/")[-1]
        if litellm and (model in litellm or bare in litellm):
            return model if model in litellm else bare
    best = None
    for key in (litellm or {}):
        m = _SONNET_KEY.match(key)
        if not m:
            continue
        version = tuple(int(x) for x in m.group(1).split("-") if len(x) < 4)
        if best is None or version > best[0]:
            best = (version, key)
    return best[1] if best else None


def override_for(litellm, model):
    entry = (litellm or {}).get(model) or {}
    out = {}
    for src, dst in FIELD_MAP.items():
        value = entry.get(src)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[dst] = float(value)
    return out if "inputCostPerToken" in out and "outputCostPerToken" in out else None


def _user_config():
    for path in USER_CONFIGS:
        try:
            with open(path) as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
        except (OSError, ValueError):
            continue
    return {}


def build_config(opus_models, sonnet_rates, user_config=None):
    """ccusage reads one config file, so start from the user's own config
    (if any) and add the Opus overrides to its defaults.pricingOverrides."""
    cfg = json.loads(json.dumps(user_config or {}))
    defaults = cfg.setdefault("defaults", {})
    overrides = defaults.setdefault("pricingOverrides", {})
    for model in opus_models:
        overrides[model] = dict(sonnet_rates)
    return cfg


def repriced_by_day(raw, opus_models):
    """{date: {model: cost}} for the Opus models in a repriced ccusage run."""
    out = {}
    wanted = set(opus_models)
    for row in (raw or {}).get("daily") or []:
        day = (row.get("period") or "")[:10]
        for mb in row.get("modelBreakdowns") or []:
            if mb.get("modelName") in wanted:
                out.setdefault(day, {})
                out[day][mb["modelName"]] = out[day].get(mb["modelName"], 0.0) + float(mb.get("cost") or 0.0)
    return out


def compute(daily_rows, runner=None, litellm=None, user_config=None):
    """The model-mix payload: which Sonnet, the Opus models, and ccusage's
    repriced Opus cost per day. Never raises; failures come back as `error`."""
    litellm = litellm if litellm is not None else load_litellm()
    opus = sorted(m for m in model_spend(daily_rows) if is_opus(m))
    base = {"estimate": True, "opusModels": opus, "sonnetModel": None,
            "repricedByDay": {}, "error": None, "priceSource": "LiteLLM via ccusage"}
    if not opus:
        return base
    if not litellm:
        base["error"] = "ccusage has not downloaded the LiteLLM price list yet, so there is no estimate."
        return base
    sonnet = pick_sonnet(daily_rows, litellm)
    rates = override_for(litellm, sonnet) if sonnet else None
    if not rates:
        base["error"] = "No Sonnet price found in the LiteLLM list."
        return base
    base["sonnetModel"] = sonnet
    base["sonnetRates"] = rates
    cfg = build_config(opus, rates, user_config if user_config is not None else _user_config())
    plain = build_config([], rates, user_config if user_config is not None else _user_config())
    run = runner or subprocess.run
    try:
        base_raw = _run_with_config(plain, run)
        sonnet_raw = _run_with_config(cfg, run)
        base_days = repriced_by_day(base_raw, opus)
        sonnet_days = repriced_by_day(sonnet_raw, opus)
        out = {}
        for day in set(base_days) | set(sonnet_days):
            for model in set(base_days.get(day, {})) | set(sonnet_days.get(day, {})):
                out.setdefault(day, {})[model] = {
                    "calculated": round(base_days.get(day, {}).get(model, 0.0), 6),
                    "atSonnet": round(sonnet_days.get(day, {}).get(model, 0.0), 6),
                }
        base["repricedByDay"] = out
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        base["error"] = "Repriced ccusage run failed: %s" % (str(e)[:200])
    return base


def _run_with_config(cfg, run):
    fd, path = tempfile.mkstemp(prefix=".model-mix-", suffix=".json", dir=data_dir.path(""))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        result = run(ccusage_bin.command("--sections", "daily", "--json", "--by-agent",
                                         "--mode", "calculate", "--config", path),
                     capture_output=True, text=True, timeout=90, check=True)
        return json.loads(result.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def summarise(daily_rows, mix, since=None, until=None):
    """Range totals (used by tests and the verification script; the
    dashboard does the same sums client-side for whatever range is picked)."""
    spend = {}
    opus_actual = 0.0
    calculated = 0.0
    at_sonnet = 0.0
    for row in daily_rows or []:
        day = (row.get("period") or "")[:10]
        if (since and day < since) or (until and day > until):
            continue
        for mb in row.get("modelBreakdowns") or []:
            name = mb.get("modelName")
            cost = float(mb.get("cost") or 0.0)
            spend[name] = spend.get(name, 0.0) + cost
            if name in (mix or {}).get("opusModels", []):
                opus_actual += cost
        for model, v in ((mix or {}).get("repricedByDay") or {}).get(day, {}).items():
            calculated += v.get("calculated", 0.0)
            at_sonnet += v.get("atSonnet", 0.0)
    return {"spend": spend, "opusActual": opus_actual, "opusCalculated": calculated,
            "opusAtSonnet": at_sonnet, "saving": calculated - at_sonnet}
