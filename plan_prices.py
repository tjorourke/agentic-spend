#!/usr/bin/env python3
"""Shipped list prices for subscription plans (plan_prices.json).

Every row comes from the provider's own pricing or help page, with its source
URL, currency, billing (monthly or annual) and the date it was fetched. A row
with no price means the provider doesn't publish one (contact sales) or it
could not be read; nothing is filled in by guesswork.

The default for an account is the monthly-billing row for its detected plan,
in the account's billing currency when the provider publishes that currency,
otherwise in US dollars. Annual rows are shown alongside but never chosen,
because the billing cycle can't be read from local data.
"""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_prices.json")


def load(path=None):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"rows": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        return {"rows": []}
    return raw


def rows_for(plan_id, table=None):
    table = table if table is not None else load()
    return [r for r in table.get("rows", []) if r.get("planId") == plan_id]


def default_for(plan_id, billing_currency=None, table=None):
    """Monthly-billing list price for a plan, or None when there isn't one
    (unknown plan, contact sales, unpublished)."""
    if not plan_id:
        return None
    monthly = [r for r in rows_for(plan_id, table)
               if r.get("billing") == "monthly" and isinstance(r.get("perMonth"), (int, float))]
    if not monthly:
        return None
    want = (billing_currency or "").upper()
    for r in monthly:
        if want and r.get("currency") == want:
            return dict(r)
    for r in monthly:
        if r.get("currency") == "USD":
            return dict(r)
    return None
