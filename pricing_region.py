#!/usr/bin/env python3
"""Pricing region (UK/GBP or US/USD) used when an account has no billing currency.

Detected from the system locale and stored, if you change it, in
pricing-region.json in the data directory. On macOS that is AppleLocale
(e.g. en_GB → UK); on Linux it is LC_ALL, then LC_MONETARY, then LANG
(e.g. en_GB.UTF-8 → UK). The setting is
only a preference for which published list-price row to use. Nothing is
converted between currencies.
"""
import json
import os
import re
import subprocess

import data_dir
import platform_info

PATH = data_dir.path("pricing-region.json")

REGIONS = {
    "UK": {"region": "UK", "currency": "GBP", "label": "United Kingdom (GBP)"},
    "US": {"region": "US", "currency": "USD", "label": "United States (USD)"},
}

# AppleLocale is typically "en_GB" or "en_US"; sometimes "en_GB@currency=GBP".
_LOCALE_RE = re.compile(r"^[A-Za-z]{2}[_-]([A-Za-z]{2})")
_GB_COUNTRIES = {"GB", "UK"}


def region_from_locale(locale):
    """'en_GB' / 'en-US' → 'UK' / 'US', or None if it isn't one of those."""
    if not locale or not isinstance(locale, str):
        return None
    m = _LOCALE_RE.match(locale.strip())
    if not m:
        return None
    country = m.group(1).upper()
    if country in _GB_COUNTRIES:
        return "UK"
    if country == "US":
        return "US"
    return None


def detect_posix_locale(env=None):
    """The locale that decides money formatting on Linux: LC_ALL wins, then
    LC_MONETARY, then LANG. 'C' and 'POSIX' say nothing about a country."""
    env = os.environ if env is None else env
    for key in ("LC_ALL", "LC_MONETARY", "LANG"):
        value = (env.get(key) or "").strip()
        if value and value.split(".")[0] not in ("C", "POSIX"):
            return value
    return None


def detect_locale(sys_name=None, env=None):
    """The system locale for this OS, or None when it can't be read."""
    if (sys_name or platform_info.system()) == platform_info.MACOS:
        return detect_mac_locale()
    return detect_posix_locale(env)


def detect_mac_locale():
    """AppleLocale from `defaults`, otherwise None (non-Mac or unread)."""
    try:
        out = subprocess.check_output(
            ["defaults", "read", "-g", "AppleLocale"],
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode("utf-8", "replace").strip() or None
    except Exception:
        return None


def load(path=None):
    """Saved region ('UK' or 'US'), or None if you haven't chosen one."""
    try:
        with open(path or PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and raw.get("region") in REGIONS:
        return raw["region"]
    return None


def save(region, path=None):
    path = path or PATH
    if region is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    if region not in REGIONS:
        raise ValueError("region must be UK or US")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"region": region}, f, indent=2)
    os.replace(tmp, path)


def validate_body(body):
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    region = body.get("region")
    if region is None:
        return None
    if region not in REGIONS:
        raise ValueError("region must be UK or US")
    return region


def current(saved=None, locale=None, locale_fn=detect_locale):
    """Resolved region. A saved choice wins; otherwise the system locale;
    otherwise US (don't guess the UK)."""
    if saved is None:
        saved = load()
    if saved in REGIONS:
        out = dict(REGIONS[saved])
        out["source"] = "user"
        out["detectedRegion"] = region_from_locale(locale if locale is not None else locale_fn())
        return out
    detected = region_from_locale(locale if locale is not None else locale_fn())
    key = detected or "US"
    out = dict(REGIONS[key])
    out["source"] = "auto"
    out["detectedRegion"] = detected
    return out
