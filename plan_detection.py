#!/usr/bin/env python3
"""Work out which subscription plan each account is on, from local data only.

Sources (all on this computer; nothing is fetched and no token is ever returned,
logged or stored):
  - Claude: ~/.claude.json's oauthAccount (organizationType, seatTier,
    rate-limit tiers) for the current login, plus Claude Code's own backups
    of that file for accounts you have switched away from. The non-secret
    plan fields are kept in plan-profiles.json in the data directory so a
    switched-away account stays known after the backups rotate.
  - Codex / ChatGPT: the plan Codex's own rate-limit call reports, or the
    chatgpt_plan_type claim inside ~/.codex/auth.json's ID token (only that
    claim is read).
  - Grok: the plan in the Grok Bot app's local usage cache.
  - OpenCode: which kind of credential each provider in its auth.json uses
    (the type field only).
Anything that can't be read plainly is reported as "unknown plan", never a
guess.
"""
import base64
import glob
import json
import os

import data_dir
import platform_info

CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
CLAUDE_BACKUPS = [os.path.expanduser("~/.claude.json.backup"),
                  os.path.expanduser("~/.claude/backups/.claude.json.backup.*")]
CODEX_AUTH = os.path.expanduser("~/.codex/auth.json")
OPENCODE_AUTH = os.path.expanduser("~/.local/share/opencode/auth.json")
PROFILES_PATH = data_dir.path("plan-profiles.json")

PROFILE_FIELDS = ("organizationType", "seatTier", "userRateLimitTier",
                  "organizationRateLimitTier", "billingType", "organizationName",
                  "planDisplayName", "emailAddress")

# Claude Code's own seat-tier table (from its bundled code, version 2.1.332).
PREMIUM_SEATS = {"team_tier_1", "team_bendep_nonprofit_premium", "team_labs_premium",
                 "enterprise_tier_1", "enterprise_bendep_premium"}
STANDARD_SEATS = {"team_standard", "team_bendep_nonprofit_standard", "team_labs_standard"}

UNKNOWN = "unknown plan"


def _entry(key, provider, label, plan_id, plan_label, evidence, billing_currency=None):
    return {"key": key, "provider": provider, "label": label, "planId": plan_id,
            "planLabel": plan_label if plan_id or plan_label else UNKNOWN,
            "detected": bool(plan_id), "evidence": evidence,
            "billingCurrency": billing_currency}


# --- Claude ------------------------------------------------------------------

def _profile_from(oauth, mtime):
    uuid = (oauth or {}).get("accountUuid")
    if not uuid:
        return None, None
    prof = {k: oauth.get(k) for k in PROFILE_FIELDS if oauth.get(k) is not None}
    prof["seenAt"] = mtime
    return uuid, prof


def scan_claude_profiles(config=CLAUDE_CONFIG, backups=None, profiles_path=None, save=True):
    """accountUuid -> newest non-secret plan profile seen in ~/.claude.json,
    its backups and our own saved copy."""
    profiles_path = profiles_path or PROFILES_PATH
    try:
        with open(profiles_path) as f:
            known = json.load(f)
        if not isinstance(known, dict):
            known = {}
    except (OSError, ValueError):
        known = {}
    files = []
    for pat in (backups if backups is not None else CLAUDE_BACKUPS):
        files.extend(glob.glob(pat))
    files.append(config)
    changed = False
    for path in files:
        try:
            mtime = os.path.getmtime(path)
            with open(path) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        uuid, prof = _profile_from(cfg.get("oauthAccount") if isinstance(cfg, dict) else None, mtime)
        if not uuid:
            continue
        old = known.get(uuid)
        if not old or (old.get("seenAt") or 0) < mtime:
            if not old or {k: v for k, v in old.items() if k != "seenAt"} != {k: v for k, v in prof.items() if k != "seenAt"}:
                changed = True
            known[uuid] = prof
    if save and changed:
        try:
            tmp = profiles_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(known, f, indent=2, sort_keys=True)
            os.replace(tmp, profiles_path)
        except OSError:
            pass
    return known


def claude_plan(profile):
    """(planId or None, label, evidence list) from one oauthAccount profile."""
    if not profile:
        return None, UNKNOWN, ["no oauthAccount record for this account on " + platform_info.here()]
    org = profile.get("organizationType")
    seat = profile.get("seatTier")
    rate = profile.get("userRateLimitTier") or profile.get("organizationRateLimitTier")
    ev = ["organizationType=%s" % org]
    if seat:
        ev.append("seatTier=%s" % seat)
    if rate:
        ev.append("rateLimitTier=%s" % rate)
    if org == "claude_pro":
        return "claude/pro", "Claude Pro", ev
    if org == "claude_max":
        tiers = {profile.get("userRateLimitTier"), profile.get("organizationRateLimitTier")}
        if "default_claude_max_20x" in tiers:
            return "claude/ultra", "Claude Ultra (was Max 20x)", ev
        if "default_claude_max_5x" in tiers:
            return "claude/max", "Claude Max (was Max 5x)", ev
        return None, "Claude Max (tier unknown)", ev
    if org == "claude_team":
        if seat in PREMIUM_SEATS or (seat or "").endswith("_premium"):
            return "claude/team-premium", "Claude Team, Premium seat", ev
        if seat in STANDARD_SEATS or (seat or "").endswith("_standard"):
            return "claude/team-standard", "Claude Team, Standard seat", ev
        return None, "Claude Team (seat type unknown)", ev
    if org == "claude_enterprise":
        return "claude/enterprise", "Claude Enterprise", ev
    return None, UNKNOWN, ev


# --- Codex / ChatGPT ---------------------------------------------------------

CODEX_PLANS = {
    "free": ("chatgpt/free", "ChatGPT Free"),
    "go": ("chatgpt/go", "ChatGPT Go"),
    "plus": ("chatgpt/plus", "ChatGPT Plus"),
    "enterprise": ("chatgpt/enterprise", "ChatGPT Enterprise"),
    "edu": ("chatgpt/enterprise", "ChatGPT Edu"),
}


def codex_plan_claim(auth_path=CODEX_AUTH):
    """Only the chatgpt_plan_type claim from the ID token's payload."""
    try:
        with open(auth_path) as f:
            auth = json.load(f)
        token = ((auth.get("tokens") or {}).get("id_token")) or ""
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
        return plan if isinstance(plan, str) else None
    except Exception:
        return None


def codex_plan(live_plan, claim_plan):
    plan = (live_plan or claim_plan or "").strip().lower()
    src = "Codex rate-limit reply" if live_plan else "Codex auth file plan claim"
    if not plan:
        return None, UNKNOWN, ["no plan in Codex's reply or auth file"]
    ev = ["%s: %s" % (src, plan)]
    if plan in CODEX_PLANS:
        pid, label = CODEX_PLANS[plan]
        return pid, label, ev
    if plan.startswith("pro"):
        return None, "ChatGPT Pro (tier unknown: Pro 100, 200 or 500)", ev
    if plan in ("team", "business"):
        return None, "ChatGPT Business (seat type unknown)", ev
    return None, UNKNOWN, ev


# --- Grok --------------------------------------------------------------------

GROK_PLANS = {
    "free": ("xai/free", "X Subscription Free"),
    "plus": ("xai/plus", "X Subscription Plus"),
    "premium": ("xai/premium", "X Subscription Premium"),
    "super": ("xai/super", "X Subscription Super"),
    "ultra": ("xai/ultra", "X Subscription Ultra"),
    "supergrok": ("xai/legacy-supergrok", "SuperGrok (legacy)"),
    "supergrok heavy": ("xai/legacy-supergrok-heavy", "SuperGrok Heavy (legacy)"),
}


def grok_plan(grokbot):
    plans = {(a.get("plan") or "").strip() for a in ((grokbot or {}).get("accounts") or []) if a.get("plan")}
    if not plans:
        return None, UNKNOWN, ["no plan in the Grok Bot usage cache"]
    if len(plans) > 1:
        return None, UNKNOWN, ["Grok Bot cache lists several plans: %s" % ", ".join(sorted(plans))]
    raw = plans.pop()
    hit = GROK_PLANS.get(raw.lower())
    ev = ["Grok Bot usage cache: plan %s" % raw]
    if hit:
        return hit[0], hit[1], ev
    return None, UNKNOWN, ev


# --- OpenCode ----------------------------------------------------------------

def opencode_plan(auth_path=OPENCODE_AUTH):
    try:
        with open(auth_path) as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return None, UNKNOWN, ["no OpenCode auth file"]
    kinds = {k: (v or {}).get("type") for k, v in auth.items() if isinstance(v, dict)}
    ev = ["auth.json credential types: " + ", ".join("%s=%s" % kv for kv in sorted(kinds.items()))]
    if kinds and all(t == "api" for t in kinds.values()) and "opencode" not in kinds:
        return None, "No subscription: API keys, pay as you go", ev
    return None, UNKNOWN, ev


# --- everything --------------------------------------------------------------

def detect_all(accounts, codex_live=None, grokbot=None, ccusage_sources=None,
               billing_currencies=None, profiles=None, codex_claim=None, opencode=None):
    """accounts: [(accountUuid, label)] for Claude. Returns one entry per
    account or tool."""
    billing_currencies = billing_currencies or {}
    profiles = profiles if profiles is not None else scan_claude_profiles()
    out = []
    for uuid, label in accounts:
        pid, plabel, ev = claude_plan(profiles.get(uuid))
        out.append(_entry(uuid, "claude", label or (profiles.get(uuid) or {}).get("emailAddress") or uuid,
                          pid, plabel, ev, billing_currencies.get(uuid)))
    present = {s.get("id") for s in (ccusage_sources or [])}
    if codex_live or "codex" in present:
        claim = codex_claim if codex_claim is not None else codex_plan_claim()
        pid, plabel, ev = codex_plan((codex_live or {}).get("planType"), claim)
        out.append(_entry("codex", "openai", "Codex (ChatGPT)", pid, plabel, ev))
    if (grokbot or {}).get("found") or "grok" in present:
        pid, plabel, ev = grok_plan(grokbot)
        out.append(_entry("grok", "xai", "Grok (Grok Bot and Grok CLI)", pid, plabel, ev))
    if "opencode" in present:
        pid, plabel, ev = opencode or opencode_plan()
        out.append(_entry("opencode", "opencode", "OpenCode", pid, plabel, ev))
    for s in ccusage_sources or []:
        sid = s.get("id")
        if sid in ("claude", "codex", "grok", "opencode"):
            continue
        out.append(_entry(sid, sid, s.get("label") or sid, None, UNKNOWN,
                          ["no local plan record that agentic-spend can read for this tool"]))
    return out
