#!/usr/bin/env python3
"""Seat prices you enter, and the API-equivalent value of each subscription.

When the plan is detected from local data (plan_detection), the default is
the shipped list price for that plan (plan_prices.json, from the provider's
own pricing page), in the account's billing currency or, if that isn't known,
the pricing-region currency (UK/GBP or US/USD). A published GBP row is used
when there is one; otherwise the USD row is used and labelled as a fallback. A price you enter overrides it and is stored per account
in seat-prices.json in the data directory (never in the repo). An account
whose plan is unknown, or whose plan has no public price, shows "set seat
price".

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
import plan_prices

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


def _effective_seat(user_price, default_row):
    """Your price wins; otherwise the shipped list price for the detected plan."""
    if user_price:
        return dict(user_price, source="user")
    if default_row:
        return {"amount": float(default_row["perMonth"]), "currency": default_row["currency"],
                "period": "month", "source": "default"}
    return None


def _list_price(default_row, plan_id, table):
    rows = plan_prices.rows_for(plan_id, table) if plan_id else []
    return {
        "default": default_row,
        "rows": rows,
        "contactSales": bool(rows) and all(r.get("contactSales") or r.get("perMonth") is None for r in rows),
    }


def _value_entry(key, label, provider, api, sessions, month_start, today_s, billing_currency,
                 user_price, detection, table, region_currency=None):
    plan_id = (detection or {}).get("planId")
    preferred = billing_currency or region_currency
    default_row = plan_prices.default_for(plan_id, preferred, table)
    seat = _effective_seat(user_price, default_row)
    entry = {
        "accountUuid": key,
        "key": key,
        "provider": provider,
        "label": label,
        "monthStart": month_start,
        "through": today_s,
        "apiEquivalentUsd": round(api, 2),
        "sessions": sessions,
        "billingCurrency": billing_currency,
        "plan": detection,
        "listPrice": _list_price(default_row, plan_id, table),
        "userSeat": user_price,
        "seat": seat,
        "multiple": None,
    }
    if seat and seat.get("currency") == "USD" and seat.get("amount"):
        entry["multiple"] = round(api / seat["amount"], 1)
    entry["currency"] = billing_currency or (seat or {}).get("currency")
    return entry


def team_value(max_usage, account_sessions, prices, today=None, detections=None,
               daily=None, table=None, region_currency=None):
    """One entry per subscription account: each Claude account with a plan
    reading, plus every other tool plan detection knows about (Codex, Grok,
    OpenCode, other ccusage sources). Month to date. Claude accounts are
    attributed by session last-active day; other tools use ccusage's daily
    per-agent cost (one account each)."""
    today = today or datetime.date.today()
    month_start = today.replace(day=1).isoformat()
    today_s = today.isoformat()
    currencies = billing_currencies(max_usage)
    table = table if table is not None else plan_prices.load()
    det = {d.get("key"): d for d in (detections or [])}
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
        out.append(_value_entry(uuid, mu.get("label"), "claude", api, sessions, month_start, today_s,
                                currencies.get(uuid), (prices or {}).get(uuid), det.get(uuid), table,
                                region_currency=region_currency))
    for d in detections or []:
        if d.get("provider") == "claude":
            continue
        key = d.get("key")
        api = 0.0
        days = 0
        for row in daily or []:
            period = (row.get("period") or row.get("date") or "")[:10]
            if not (month_start <= period <= today_s):
                continue
            for a in row.get("agents") or []:
                if a.get("agent") == key and float(a.get("totalCost") or 0) > 0:
                    api += float(a.get("totalCost") or 0.0)
                    days += 1
        entry = _value_entry(key, d.get("label"), d.get("provider"), api, None, month_start, today_s,
                             d.get("billingCurrency"), (prices or {}).get(key), d, table,
                             region_currency=region_currency)
        entry["activeDays"] = days
        out.append(entry)
    return out
