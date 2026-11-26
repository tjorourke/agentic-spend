#!/usr/bin/env python3
"""Shipped list prices for subscription plans (plan_prices.json).

Every row comes from the provider's own pricing or help page, with its source
URL, currency, billing (monthly or annual) and the date it was fetched. A row
with no price means the provider doesn't publish one (contact sales) or it
could not be read; nothing is filled in by guesswork.

The default for an account is the monthly-billing row for its detected plan.
Preferred currency is the account's own billing currency when known, otherwise
the pricing-region setting (UK/GBP or US/USD). A GBP row is used when the
provider publishes one; otherwise the USD row is returned and marked as a
fallback. Nothing is converted. Annual rows are shown alongside but never
chosen, because the billing cycle can't be read from local data.
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


def default_for(plan_id, preferred_currency=None, table=None):
    """Monthly-billing list price for a plan, or None when there isn't one
    (unknown plan, contact sales, unpublished).

    preferred_currency is the account's billing currency, or the pricing-region
    currency when that isn't known. The returned row includes preferredCurrency
    and usedFallback (True when we had to use a USD row because no price was
    published in the preferred currency).
    """
    if not plan_id:
        return None
    monthly = [r for r in rows_for(plan_id, table)
               if r.get("billing") == "monthly" and isinstance(r.get("perMonth"), (int, float))]
    if not monthly:
        return None
    want = (preferred_currency or "").upper() or None
    chosen = None
    if want:
        for r in monthly:
            if r.get("currency") == want:
                chosen = dict(r)
                break
    fallback = False
    if chosen is None:
        for r in monthly:
            if r.get("currency") == "USD":
                chosen = dict(r)
                fallback = bool(want and want != "USD")
                break
    if chosen is None:
        return None
    chosen["preferredCurrency"] = want
    chosen["usedFallback"] = fallback
    return chosen
