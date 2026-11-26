#!/usr/bin/env python3
"""Seat prices you enter, and the API-equivalent value of each subscription.

Claude does not expose what a seat costs, so there is no default: an account
with no saved price shows "set seat price". The price is stored per account
in seat-prices.json in the data directory (never in the repo), in the
account's billing currency.

The API-equivalent figure is ccusage's own cost for that account's sessions
(LiteLLM list prices, in US dollars), month to date, attributed by each
session's last-active day. It is not converted: when the seat is billed in
another currency we show both amounts side by side rather than inventing an
exchange rate.
"""
import datetime
import json
import os
import re

import data_dir

PATH = data_dir.path("seat-prices.json")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,120}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_AMOUNT = 100000


def load(path=None):
    path = path or PATH
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        try:
            out[key] = _clean_entry(value)
        except ValueError:
            continue
    return out


def save(prices, path=None):
    path = path or PATH
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prices, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _clean_entry(value):
    if not isinstance(value, dict):
        raise ValueError("seat price must be an object")
    amount = value.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("amount must be a number")
    amount = float(amount)
    if not (0 < amount <= MAX_AMOUNT):
        raise ValueError("amount must be more than 0 and at most %d" % MAX_AMOUNT)
    currency = value.get("currency")
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency.strip().upper()):
        raise ValueError("currency must be a three-letter code such as GBP or USD")
    return {"amount": round(amount, 2), "currency": currency.strip().upper(), "period": "month"}


def validate_body(body, billing_currency=None):
    """{accountUuid, amount|null, currency} -> (accountUuid, entry or None).

    amount null removes the price. When the account's billing currency is
    known, the price must be in it."""
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    account = body.get("accountUuid")
    if not isinstance(account, str) or not _ID_RE.match(account.strip()):
        raise ValueError("accountUuid is required and must be a short id")
    account = account.strip()
    if body.get("amount") is None:
        return account, None
    entry = _clean_entry({"amount": body.get("amount"),
                          "currency": body.get("currency") or billing_currency})
    if billing_currency and entry["currency"] != billing_currency.upper():
        raise ValueError("this account is billed in %s, so the seat price must be in %s"
                         % (billing_currency.upper(), billing_currency.upper()))
    return account, entry


def billing_currencies(max_usage):
    """accountUuid -> billing currency, from each account's own usage-credit
    reading (Claude reports the currency with its credit figures)."""
    out = {}
    for mu in max_usage or []:
        cur = ((mu or {}).get("usageCredits") or {}).get("currency")
        if mu.get("accountUuid") and isinstance(cur, str) and _CURRENCY_RE.match(cur.upper()):
            out[mu["accountUuid"]] = cur.upper()
    return out


def team_value(max_usage, account_sessions, prices, today=None):
    """One entry per subscription account (Claude accounts with a plan
    reading). Month to date, by session last-active day."""
    today = today or datetime.date.today()
    month_start = today.replace(day=1).isoformat()
    today_s = today.isoformat()
    currencies = billing_currencies(max_usage)
    out = []
    for mu in max_usage or []:
        uuid = (mu or {}).get("accountUuid")
        if not uuid:
            continue
        api = 0.0
        sessions = 0
        for s in account_sessions or []:
            if s.get("accountUuid") != uuid:
                continue
            period = (s.get("period") or "")[:10]
            if month_start <= period <= today_s:
                api += float(s.get("totalCost") or 0.0)
                sessions += 1
        price = (prices or {}).get(uuid)
        currency = currencies.get(uuid) or (price or {}).get("currency")
        entry = {
            "accountUuid": uuid,
            "label": mu.get("label"),
            "monthStart": month_start,
            "through": today_s,
            "apiEquivalentUsd": round(api, 2),
            "sessions": sessions,
            "billingCurrency": currencies.get(uuid),
            "seat": price,
            "multiple": None,
        }
        if price and price.get("currency") == "USD":
            entry["multiple"] = round(api / price["amount"], 1) if price["amount"] else None
        entry["currency"] = currency
        out.append(entry)
    return out
