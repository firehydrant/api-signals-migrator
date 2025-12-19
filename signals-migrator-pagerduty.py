#!/usr/bin/env python3
# Migrator: PagerDuty → FireHydrant
# Usage: python3 migrate-teams-pd.py [flags]
#
# Notes:
# - Reads FIREHYDRANT_API_KEY and PAGERDUTY_API_TOKEN from environment (.env via config.env supported)
# - Default output is minimal, with 5 stage lines per team; use --verbose for details

import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from dotenv import load_dotenv
    load_dotenv('config.env', override=True)
except Exception:
    pass

import requests



# -------- Config / Globals --------

FIREHYDRANT_API_KEY = os.getenv('FIREHYDRANT_API_KEY') or ''
PAGERDUTY_API_TOKEN = os.getenv('PAGERDUTY_API_TOKEN') or os.getenv('PAGERDUTY_TOKEN') or ''

_SETUP_FLAGS = any(f in sys.argv for f in ("--set-fh-token","--set-pd-token","--configure"))
if not FIREHYDRANT_API_KEY and not _SETUP_FLAGS:
    print("❌ FIREHYDRANT_API_KEY not set")
    sys.exit(1)
# Allow FH-only operations or setup without a PD token (revert/delete/configure)
_FH_ONLY = any(f in sys.argv for f in ("--revert-all","--delete-team","--delete","--delete-schedule"))
_JUST_HELP = any(f in sys.argv for f in ("--help","-h"))
if not PAGERDUTY_API_TOKEN and not (_FH_ONLY or _JUST_HELP or _SETUP_FLAGS):
    print("❌ PAGERDUTY_API_TOKEN not set")
    sys.exit(1)

FIREHYDRANT_BASE_URL = "https://api.firehydrant.io/v1"
PAGERDUTY_BASE_URL = os.getenv('PAGERDUTY_BASE_URL', 'https://api.pagerduty.com')

# default minimal; enable with flag or env
VERBOSE = (os.getenv('VERBOSE_LOGS', 'false').lower() in ('1','true','yes')) or ('--verbose' in sys.argv)
SUMMARY_ONLY = True
NO_OVERRIDES = ('--no-overrides' in sys.argv)
DRY_RUN = ('--dry-run' in sys.argv)
VERIFY_ONLY = ('--verify-only' in sys.argv)
ALIGN_NOW = (('--no-align' not in sys.argv) or ('--align' in sys.argv))
TIMEZONE_OVERRIDE = os.getenv('TIMEZONE_OVERRIDE')
RESTRICTION_STRATEGY = (os.getenv('RESTRICTION_STRATEGY') or 'weekly').lower()
OUTPUT_PATH = None
PD_NO_TEAMS = (os.getenv('PD_NO_TEAMS','').lower() in ('1','true','yes')) or ('--pd-no-teams' in sys.argv)
PD_GROUP_BY = (os.getenv('PD_GROUP_BY') or '').strip().lower()
if '--pd-group-by' in sys.argv:
    try:
        PD_GROUP_BY = sys.argv[sys.argv.index('--pd-group-by')+1].strip().lower()
    except Exception:
        PD_GROUP_BY = PD_GROUP_BY or ''
if '--pd-group' in sys.argv:
    try:
        PD_GROUP_BY = sys.argv[sys.argv.index('--pd-group')+1].strip().lower()
    except Exception:
        PD_GROUP_BY = PD_GROUP_BY or ''
if '--output' in sys.argv:
    try:
        OUTPUT_PATH = sys.argv[sys.argv.index('--output')+1]
    except Exception:
        pass

CONCURRENCY = int(os.getenv('MIGRATOR_CONCURRENCY', '16'))
PD_ENFORCE_BOUNDARIES = (os.getenv('PD_ENFORCE_BOUNDARIES', 'true').lower() in ('1','true','yes'))

# Pooled session
try:
    from requests.adapters import HTTPAdapter
    _session = requests.Session()
    _adapter = HTTPAdapter(pool_connections=CONCURRENCY, pool_maxsize=max(CONCURRENCY*2, 32))
    _session.mount('https://', _adapter)
    _session.mount('http://', _adapter)
    requests.get = _session.get
    requests.post = _session.post
    requests.put = _session.put
    requests.patch = _session.patch
    requests.delete = _session.delete
except Exception:
    pass

# --- Simple config helpers ---
def _config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")

def _write_config_pairs(pairs: dict):
    path = _config_path()
    existing = {}
    try:
        with open(path, "r") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    existing[k.strip()] = v
    except Exception:
        pass
    existing.update({k: str(v) for k, v in pairs.items() if v is not None})
    # Write back preserving simple KEY=VALUE lines
    with open(path, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")

def vprint(msg: str):
    if VERBOSE:
        print(msg)

def stage(msg: str):
    try:
        sys.__stdout__.write(msg + "\n")
        sys.__stdout__.flush()
    except Exception:
        print(msg)

# Interactive helper (optional)
try:
    from pick import pick
    PICK_AVAILABLE = True
except Exception:
    PICK_AVAILABLE = False

class SilentPrint:
    """Context manager to temporarily silence print() calls when SUMMARY_ONLY."""
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._orig = None
    def __enter__(self):
        if self.enabled:
            import builtins
            self._orig = builtins.print
            builtins.print = lambda *a, **k: None
    def __exit__(self, exc_type, exc, tb):
        if self.enabled and self._orig:
            import builtins
            builtins.print = self._orig
        return False

# Ledger for revert-all (PD-specific file to avoid collisions)
LEDGER_PATH = os.getenv('MIGRATOR_LEDGER_PATH', 'migrator_ledger_pd.json')

def _read_ledger() -> dict:
    try:
        if not os.path.exists(LEDGER_PATH):
            return {"teams": []}
        with open(LEDGER_PATH, 'r') as f:
            return json.load(f) or {"teams": []}
    except Exception:
        return {"teams": []}

def _write_ledger(data: dict) -> None:
    try:
        with open(LEDGER_PATH, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _record_created_team(team_id: str, name: str) -> None:
    if DRY_RUN or VERIFY_ONLY:
        return
    led = _read_ledger()
    teams = led.get('teams') or []
    if not any(t.get('id') == team_id for t in teams):
        teams.append({"id": team_id, "name": name})
        led['teams'] = teams
        _write_ledger(led)

def _remove_team_from_ledger(team_id: str) -> None:
    led = _read_ledger()
    teams = [t for t in (led.get('teams') or []) if t.get('id') != team_id]
    led['teams'] = teams
    _write_ledger(led)

# -------- PagerDuty API --------

PD_USER_EMAIL_BY_ID = {}

PD_HEADERS = {
    "Authorization": f"Token token={PAGERDUTY_API_TOKEN}",
    "Accept": "application/vnd.pagerduty+json;version=2",
    "Content-Type": "application/json",
    "User-Agent": "signals-migrator-pd/1.0"
}

def _pd_paginate(endpoint: str, params: dict = None, item_key: str = None):
    url = f"{PAGERDUTY_BASE_URL}{endpoint}"
    params = dict(params or {})
    # standard PD pagination
    limit = 100
    offset = 0
    more = True
    items = []
    while more:
        qp = params.copy()
        qp['limit'] = limit
        qp['offset'] = offset
        r = requests.get(url, headers=PD_HEADERS, params=qp)
        vprint(f"PD GET {endpoint} → {r.status_code}")
        if r.status_code == 402:
            # If the Teams endpoint is unavailable, enable no-teams mode automatically and return empty
            global PD_NO_TEAMS
            if endpoint == "/teams":
                PD_NO_TEAMS = True
                print("ℹ️  PagerDuty Teams API not available (402). Enabling no-teams mode.")
                return []
            # For other endpoints, surface the error
            print(f"❌ PagerDuty returned 402 Payment Required for {endpoint}.")
            print("   This likely indicates your PD plan lacks access to this API. Adjust plan or token.")
            sys.exit(2)
        try:
            r.raise_for_status()
        except Exception:
            raise
        data = r.json()
        payload_items = data.get(item_key) if item_key else None
        if payload_items is None:
            # best effort: grab first list in dict
            for k, v in data.items():
                if isinstance(v, list):
                    payload_items = v
                    break
        payload_items = payload_items or []
        items.extend(payload_items)
        more = bool(data.get('more'))
        offset += len(payload_items)
        if not payload_items:
            break
    return items

def pd_fetch_teams():
    # Decide grouping. When Teams API unavailable and no explicit group is set, default to services
    group_by = PD_GROUP_BY or ('services' if PD_NO_TEAMS else 'teams')
    if group_by == 'services':
        svcs = pd_fetch_services() or []
        return [{"id": f"service:{s.get('id')}", "name": f"{s.get('name') or 'Service'}", "_service": s} for s in svcs]
    if PD_NO_TEAMS:
        # Fallback single global bucket
        return [{"id": "global", "name": "PagerDuty Global"}]
    return _pd_paginate("/teams", {}, "teams")

def pd_fetch_users():
    return _pd_paginate("/users", {}, "users")

def pd_team_members(team_id: str):
    if PD_NO_TEAMS or team_id == "global":
        # No teams in PD; return empty. We'll derive members from schedules/participants when creating.
        return []
    return _pd_paginate(f"/teams/{team_id}/members", {}, "members")

def pd_team_schedules(team_id: str):
    # list schedules filtered by team
    if PD_NO_TEAMS or team_id == "global":
        return _pd_paginate("/schedules", {}, "schedules")
    return _pd_paginate("/schedules", {"team_ids[]": team_id}, "schedules")

def pd_schedule_details(schedule_id: str):
    r = requests.get(f"{PAGERDUTY_BASE_URL}/schedules/{schedule_id}", headers=PD_HEADERS, params={})
    vprint(f"PD GET /schedules/{schedule_id} → {r.status_code}")
    if r.status_code != 200:
        return None
    return r.json().get('schedule') or {}

def pd_list_overrides(schedule_id: str, since_iso: str, until_iso: str):
    params = {"since": since_iso, "until": until_iso}
    return _pd_paginate(f"/schedules/{schedule_id}/overrides", params, "overrides")

def pd_oncall_emails_for_schedule(schedule_id: str):
    r = requests.get(f"{PAGERDUTY_BASE_URL}/oncalls", headers=PD_HEADERS, params={"schedule_ids[]": schedule_id, "limit": 25})
    vprint(f"PD GET /oncalls schedule={schedule_id} → {r.status_code}")
    if r.status_code != 200:
        return []
    emails = []
    for oc in r.json().get('oncalls', []):
        user = oc.get('user') or {}
        # Prefer direct email if present
        if user.get('email'):
            emails.append(user['email'])
            continue
        # Fallback: resolve user id → email
        uid = user.get('id')
        if uid:
            # cached?
            if uid in PD_USER_EMAIL_BY_ID:
                emails.append(PD_USER_EMAIL_BY_ID[uid])
                continue
            try:
                ur = requests.get(f"{PAGERDUTY_BASE_URL}/users/{uid}", headers=PD_HEADERS)
                if ur.status_code == 200:
                    em = (ur.json().get('user') or {}).get('email')
                    if em:
                        PD_USER_EMAIL_BY_ID[uid] = em
                        emails.append(em)
            except Exception:
                pass
    return emails

def pd_oncall_segments_for_schedule(schedule_id: str, since_iso: str, until_iso: str):
    """Return on-call segments (start,end,email) for a PD schedule within a window."""
    params = {"schedule_ids[]": schedule_id, "since": since_iso, "until": until_iso, "limit": 100}
    r = requests.get(f"{PAGERDUTY_BASE_URL}/oncalls", headers=PD_HEADERS, params=params)
    vprint(f"PD GET /oncalls segments schedule={schedule_id} → {r.status_code}")
    segments = []
    if r.status_code != 200:
        return segments
    for oc in r.json().get('oncalls', []) or []:
        user = oc.get('user') or {}
        email = user.get('email')
        uid = user.get('id')
        # resolve email via cache or user lookup if missing
        if not email and uid:
            email = PD_USER_EMAIL_BY_ID.get(uid)
            if not email:
                try:
                    ur = requests.get(f"{PAGERDUTY_BASE_URL}/users/{uid}", headers=PD_HEADERS)
                    if ur.status_code == 200:
                        email = (ur.json().get('user') or {}).get('email')
                        if email:
                            PD_USER_EMAIL_BY_ID[uid] = email
                except Exception:
                    pass
        seg = {
            "start": oc.get('start'),
            "end": oc.get('end'),
            "email": email
        }
        if seg["start"] and seg["end"]:
            segments.append(seg)
    # Sort by start time just in case
    try:
        segments.sort(key=lambda s: s["start"])
    except Exception:
        pass
    return segments

def pd_fetch_escalation_policies(team_id: str):
    if PD_NO_TEAMS or team_id == "global":
        return _pd_paginate("/escalation_policies", {}, "escalation_policies")
    return _pd_paginate("/escalation_policies", {"team_ids[]": team_id}, "escalation_policies")

def pd_fetch_services():
    return _pd_paginate("/services", {}, "services")

def pd_find_service_by_name_or_id(ident: str):
    ident = (ident or "").strip()
    if not ident:
        return None
    # try direct id
    r = requests.get(f"{PAGERDUTY_BASE_URL}/services/{ident}", headers=PD_HEADERS)
    if r.status_code == 200:
        return (r.json().get('service') or {}).get('id')
    # fallback query by name
    q = {"query": ident, "limit": 25}
    r = requests.get(f"{PAGERDUTY_BASE_URL}/services", headers=PD_HEADERS, params=q)
    if r.status_code != 200:
        return None
    items = r.json().get('services', []) or []
    exact = next((s for s in items if (s.get('name') or '').strip().lower() == ident.lower()), None)
    if exact:
        return exact.get('id')
    return (items[0].get('id') if items else None)

def pd_find_schedule_by_name_or_id(ident: str):
    ident = (ident or "").strip()
    if not ident:
        return None
    # try direct id
    r = requests.get(f"{PAGERDUTY_BASE_URL}/schedules/{ident}", headers=PD_HEADERS)
    if r.status_code == 200:
        return (r.json().get('schedule') or {}).get('id')
    # fallback query by name
    q = {"query": ident, "limit": 25}
    r = requests.get(f"{PAGERDUTY_BASE_URL}/schedules", headers=PD_HEADERS, params=q)
    if r.status_code != 200:
        return None
    items = r.json().get('schedules', []) or []
    exact = next((s for s in items if (s.get('name') or '').strip().lower() == ident.lower()), None)
    if exact:
        return exact.get('id')
    return (items[0].get('id') if items else None)

def pd_escalation_policy_details(ep_id: str):
    if not ep_id:
        return None
    r = requests.get(f"{PAGERDUTY_BASE_URL}/escalation_policies/{ep_id}", headers=PD_HEADERS)
    vprint(f"PD GET /escalation_policies/{ep_id} → {r.status_code}")
    if r.status_code != 200:
        return None
    return r.json().get('escalation_policy') or {}

def pd_first_schedule_for_service(service_id: str):
    """Best-effort: from service -> EP -> first schedule id from rules."""
    if not service_id:
        return None
    rs = requests.get(f"{PAGERDUTY_BASE_URL}/services/{service_id}", headers=PD_HEADERS)
    vprint(f"PD GET /services/{service_id} → {rs.status_code}")
    if rs.status_code != 200:
        return None
    ep = (rs.json().get('service') or {}).get('escalation_policy') or {}
    ep_full = pd_escalation_policy_details(ep.get('id')) if ep.get('id') else None
    if not ep_full:
        return None
    for rule in ep_full.get('escalation_rules', []) or []:
        for t in rule.get('targets', []) or []:
            if (t.get('type') or '').lower() in ('schedule','schedule_reference'):
                sid = t.get('id')
                if sid:
                    return sid
    return None

# -------- FireHydrant Helpers --------

FH_HEADERS = {
    "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
    "Content-Type": "application/json"
}

def fh_fetch_teams():
    items = []
    page = 1
    while True:
        r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams?page={page}", headers=FH_HEADERS)
        if r.status_code != 200:
            break
        data = r.json()
        arr = data.get('data') if isinstance(data, dict) else data
        if not arr:
            break
        items.extend(arr)
        if len(arr) < 100:
            break
        page += 1
    return items

def fh_fetch_users():
    items = []
    page = 1
    while True:
        r = requests.get(f"{FIREHYDRANT_BASE_URL}/users?page={page}", headers=FH_HEADERS)
        if r.status_code != 200:
            break
        data = r.json()
        arr = data.get('data') if isinstance(data, dict) else data
        if not arr:
            break
        items.extend(arr)
        if len(arr) < 100:
            break
        page += 1
    return items

def fh_find_user_by_email(email: str, fh_users: list):
    em_l = (email or '').strip().lower()
    for u in fh_users or []:
        if (u.get('email') or '').lower() == em_l:
            return u
    return None

def fh_find_team_by_name_or_id(name_or_id: str):
    # list teams and try to match
    page = 1
    target = (name_or_id or '').strip().lower()
    best = None
    while True:
        r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams?page={page}", headers=FH_HEADERS)
        if r.status_code != 200:
            break
        data = r.json()
        arr = data.get('data') if isinstance(data, dict) else data
        if not arr:
            break
        for t in arr:
            if (t.get('id') or '').lower() == target:
                return t
            if (t.get('name') or '').strip().lower() == target:
                best = best or t
        if len(arr) < 100:
            break
        page += 1
    return best

def fh_create_team(pd_team: dict):
    if DRY_RUN or VERIFY_ONLY:
        return {"id": "DRY_RUN", "name": pd_team.get('name')}
    payload = {"name": pd_team.get('name'), "description": (pd_team.get('description') or '')}
    r = requests.post(f"{FIREHYDRANT_BASE_URL}/teams", headers=FH_HEADERS, json=payload)
    if r.status_code not in (200,201):
        print(f"❌ Error creating team: {r.status_code} {r.text}")
        return None
    team = r.json()
    try:
        _record_created_team(team.get('id'), team.get('name'))
    except Exception:
        pass
    return team

def fh_add_users_to_team(team_id: str, user_ids: list):
    if not user_ids:
        return 0
    if DRY_RUN or VERIFY_ONLY:
        vprint(f"🧪 Would add users: {user_ids}")
        return len(user_ids)
    # fetch current memberships
    g = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}", headers=FH_HEADERS)
    if g.status_code != 200:
        print(f"  ❌ Fetch team failed for memberships: {g.status_code}")
        return 0
    team = g.json()
    existing = team.get('memberships') or []
    existing_ids = {m.get('user',{}).get('id') for m in existing if m.get('user')}
    new_members = [{"user_id": uid} for uid in user_ids if uid not in existing_ids]
    if not new_members:
        return 0
    patch = requests.patch(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}", headers=FH_HEADERS,
                           json={"memberships": existing + new_members})
    if patch.status_code not in (200,201):
        print(f"  ❌ Add users failed: {patch.status_code} {patch.text}")
        return 0
    return len(new_members)

def fh_list_schedules(team_id: str):
    r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=FH_HEADERS)
    if r.status_code != 200:
        return []
    data = r.json()
    return data.get('data') if isinstance(data, dict) else (data or [])

def fh_find_schedule_by_name_or_id(team_id: str, ident: str):
    ident_l = (ident or '').strip().lower()
    for s in fh_list_schedules(team_id):
        if (s.get('id') or '').lower() == ident_l:
            return s
    for s in fh_list_schedules(team_id):
        if (s.get('name') or '').strip().lower() == ident_l:
            return s
    return None

def fh_create_schedule_with_rotation(team_id: str, name: str, tz: str, member_ids: list, strategy: dict, restrictions: list = None):
    # If a schedule with the same name already exists, reuse it
    try:
        existing = fh_list_schedules(team_id) or []
        for s in existing:
            if (s.get('name') or '').strip().lower() == (name or '').strip().lower():
                return s
    except Exception:
        pass
    # Create schedule and attach rotation memberships inline so FH generates shifts with users
    payload = {
        "name": name,
        "team_id": team_id,
        "time_zone": TIMEZONE_OVERRIDE or tz or "UTC",
        "rotations": [
            {
                "name": "Rotation 1",
                "start_time": _now_iso(),
                "time_zone": TIMEZONE_OVERRIDE or tz or "UTC",
                "strategy": strategy or {"type": "weekly", "handoff_day": "monday", "handoff_time": "09:00:00"},
            }
        ]
    }
    # Prefer memberships; include compatible fields for older schemas
    try:
        if member_ids:
            rotation = payload["rotations"][0]
            rotation["memberships"] = [{"user_id": uid} for uid in member_ids]
            rotation["members"] = [{"user_id": uid} for uid in member_ids]
            rotation["member_ids"] = list(member_ids)
    except Exception:
        pass
    if restrictions:
        payload["restrictions"] = restrictions
        payload["rotations"][0]["restrictions"] = restrictions
    if DRY_RUN or VERIFY_ONLY:
        vprint(json.dumps(payload, indent=2))
        return {"id": "DRY_RUN", "name": name, "rotations": [{"id":"DRY_RUN"}]}
    r = requests.post(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=FH_HEADERS, json=payload)
    if r.status_code not in (200,201):
        print(f"  ❌ Create schedule failed: {r.status_code} {r.text}")
        return None
    return r.json()

def fh_add_members_to_rotation(team_id: str, schedule_id: str, rotation_id: str, member_ids: list) -> bool:
    if not member_ids:
        return True
    if DRY_RUN or VERIFY_ONLY:
        vprint(f"🧪 Would add {len(member_ids)} member(s) to rotation {rotation_id}")
        return True
    # Primary: schedule-level PATCH with rotations.memberships (supported)
    try:
        patch_payload = {
            "rotations": [
                {
                    "id": rotation_id,
                    "memberships": [{"user_id": uid} for uid in member_ids],
                    # backward-compatible shapes
                    "members": [{"user_id": uid} for uid in member_ids],
                    "member_ids": list(member_ids),
                }
            ]
        }
        pr = requests.patch(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}",
            headers=FH_HEADERS,
            json=patch_payload,
        )
        if pr.status_code in (200, 201, 204):
            return True
    except Exception:
        pass
    # Secondary: rotation PUT with full object including memberships
    rot_get = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}", headers=FH_HEADERS)
    if rot_get.status_code == 200:
        rot = rot_get.json() or {}
        rot['memberships'] = [{"user_id": uid} for uid in member_ids]
        rot['members'] = [{"user_id": uid} for uid in member_ids]
        rot['member_ids'] = list(member_ids)
        put = requests.put(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}", headers=FH_HEADERS, json=rot)
        if put.status_code in (200,204):
            return True
    # Final fallback: attempt legacy endpoints if available
    try:
        url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}/memberships"
        body = {"memberships": [{"user_id": uid} for uid in member_ids]}
        r = requests.post(url, headers=FH_HEADERS, json=body)
        if r.status_code in (200, 201):
            return True
    except Exception:
        pass
    return False

def fh_apply_override(team_id: str, schedule_id: str, rotation_id: str, start_iso: str, end_iso: str, user_id: str = None):
    if DRY_RUN or VERIFY_ONLY or NO_OVERRIDES:
        return True
    url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}/overrides"
    body = {"start_time": start_iso, "end_time": end_iso}
    if user_id:
        body["user_id"] = user_id
    r = requests.post(url, headers=FH_HEADERS, json=body)
    return r.status_code in (200,201)

def fh_apply_pd_boundary_overrides(team_id: str, schedule_id: str, rotation_id: str, pd_schedule_id: str, fh_users: list, weeks_ahead: int = 8) -> int:
    """Overlay PD on-call segments as FH overrides to ensure exact weekly boundaries."""
    try:
        since = (_now_dt() - timedelta(days=1)).isoformat().replace('+00:00','Z')
        until = (_now_dt() + timedelta(days=7*weeks_ahead)).isoformat().replace('+00:00','Z')
        segments = pd_oncall_segments_for_schedule(pd_schedule_id, since, until) or []
        if not segments:
            return 0
        applied = 0
        for seg in segments:
            email = (seg.get('email') or '').strip().lower()
            fh_user = fh_find_user_by_email(email, fh_users) if email else None
            uid = fh_user.get('id') if fh_user else None
            ok = fh_apply_override(team_id, schedule_id, rotation_id, seg.get('start'), seg.get('end'), uid)
            if ok:
                applied += 1
        return applied
    except Exception:
        return 0

def fh_align_active_shift_to_user(team_id: str, schedule_id: str, user_id: str):
    # fetch schedule and find active shift, then patch
    try:
        r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}", headers=FH_HEADERS)
        if r.status_code != 200:
            return False
        sj = r.json()
        now = _now_dt()
        for rot in sj.get('rotations', []) or []:
            for sh in rot.get('shifts', []) or []:
                st = _parse_iso(sh.get('start_time')); et = _parse_iso(sh.get('end_time'))
                if not st or not et:
                    continue
                if st <= now < et:
                    if (sh.get('user') or {}).get('id') == user_id:
                        return False
                    sid = sh.get('id')
                    pr = requests.patch(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/shifts/{sid}",
                                        headers=FH_HEADERS, json={"user_id": user_id})
                    vprint(f"Align active shift → {pr.status_code}")
                    return pr.status_code in (200,204)
        return False
    except Exception:
        return False

def fh_assign_unclaimed_shifts_round_robin(team_id: str, schedule_id: str, member_ids: list) -> int:
    """Claim any unassigned shifts by cycling through member_ids."""
    try:
        if not member_ids:
            return 0
        r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}", headers=FH_HEADERS)
        if r.status_code != 200:
            return 0
        sched = r.json() or {}
        # Collect shifts from rotations if present; fall back to top-level shifts
        shifts = []
        for rot in sched.get('rotations', []) or []:
            for sh in rot.get('shifts', []) or []:
                shifts.append(sh)
        if not shifts:
            shifts = sched.get('shifts', []) or []
        changed = 0
        for index, sh in enumerate(shifts):
            if (sh.get('user') or {}).get('id'):
                continue
            sid = sh.get('id')
            uid = member_ids[index % len(member_ids)]
            pr = requests.patch(
                f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/shifts/{sid}",
                headers=FH_HEADERS,
                json={"user_id": uid},
            )
            if pr.status_code in (200, 204):
                changed += 1
        return changed
    except Exception:
        return 0

def align_now_pd_to_fh(team_id: str, fh_schedule_id: str, pd_schedule_id: str = None, pd_service_id: str = None) -> bool:
    """Align FH active shift user to PD current on-call for given PD schedule or service."""
    # resolve PD schedule id if only service provided
    if not pd_schedule_id and pd_service_id:
        pd_schedule_id = pd_first_schedule_for_service(pd_service_id)
    if not pd_schedule_id:
        print("❌ Missing --pd-schedule-id or unable to resolve from --pd-service-id")
        return False
    # PD on-call email
    emails = pd_oncall_emails_for_schedule(pd_schedule_id)
    desired = emails[0] if emails else None
    if not desired:
        print("❌ PD returned no current on-call for schedule")
        return False
    # map to FH user id
    fh_users = fh_fetch_users()
    fu = fh_find_user_by_email(desired, fh_users)
    if not fu or not fu.get('id'):
        print(f"❌ Could not map PD on-call {desired} to a FireHydrant user")
        return False
    if DRY_RUN or VERIFY_ONLY:
        print(f"🧪 Would align FH schedule {fh_schedule_id} active shift to {desired} ({fu['id']})")
        return True
    ok = fh_align_active_shift_to_user(team_id, fh_schedule_id, fu['id'])
    print(f"{'✅' if ok else '⚠️'} Align-now {'succeeded' if ok else 'did not change'} for {desired}")
    return ok

# -------- Utilities --------

from datetime import datetime, timedelta, timezone

def _now_dt():
    return datetime.now(timezone.utc)

def _now_iso():
    return _now_dt().isoformat().replace('+00:00', 'Z')

def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z','+00:00'))
    except Exception:
        return None

def _iso_duration_minutes(minutes: int):
    try:
        m = int(minutes or 0)
    except Exception:
        m = 0
    m = max(0, m)
    return f"PT{m}M"

def _pd_layer_strategy(layer: dict):
    # Map PD layer to FH strategy best-effort
    # PD: rotation_turn_length, rotation_virtual_start (ISO), users[]
    handoff_time = "09:00:00"
    dt = _parse_iso(layer.get('rotation_virtual_start'))
    if dt:
        handoff_time = dt.strftime("%H:%M:%S")
    length = int(layer.get('rotation_turn_length') or 7)
    unit = (layer.get('rotation_turn_unit') or 'days').lower()
    if unit.startswith('day') and length == 1:
        return {"type": "daily", "handoff_time": handoff_time}
    if unit.startswith('week') or (unit.startswith('day') and length % 7 == 0):
        # pick weekday from virtual start
        handoff_day = (dt.strftime("%A").lower() if dt else "monday")
        return {"type": "weekly", "handoff_day": handoff_day, "handoff_time": handoff_time}
    # Fallback: custom with hours
    hours = length
    if unit.startswith('day'):
        hours = length * 24
    elif unit.startswith('hour'):
        hours = length
    return {"type": "custom", "shift_duration": f"PT{max(1, int(hours))}H"}

def _layer_members_emails(layer: dict):
    emails = []
    for u in layer.get('users', []) or []:
        # Cases:
        # - u = {"email": "..."} (rare)
        # - u = {"user": {"id": "...", "email": "..."}} (common)
        # - u = {"id": "..."} (reference only)
        if isinstance(u, dict):
            if u.get('email'):
                emails.append(u['email'])
                continue
            inner = u.get('user') or {}
            if inner.get('email'):
                emails.append(inner['email'])
                continue
            uid = inner.get('id') or u.get('id')
            if uid and uid in PD_USER_EMAIL_BY_ID:
                emails.append(PD_USER_EMAIL_BY_ID[uid])
    # legacy: participants?
    for p in layer.get('participants', []) or []:
        if p.get('user', {}).get('email'):
            emails.append(p['user']['email'])
    # de-duplicate preserving order
    seen = set(); out = []
    for e in emails:
        if e not in seen:
            seen.add(e); out.append(e)
    return out

def _ep_schedule_ids(pd_ep: dict):
    ids = []
    if not pd_ep:
        return ids
    for rule in pd_ep.get('escalation_rules', []) or []:
        for t in rule.get('targets', []) or []:
            ttype = (t.get('type') or '').lower()
            if ttype in ('schedule_reference','schedule'):
                sid = t.get('id')
                if sid and sid not in ids:
                    ids.append(sid)
    return ids

# -------- EP mapping (PD → FH preview) --------

def map_pd_ep_to_fh_preview(pd_ep: dict, schedule_name_by_id: dict, ep_to_services: dict = None):
    """Best-effort preview mapping of PD EP to FH payload.
    Resolves schedule targets to names (ids resolved at create-time)."""
    name = pd_ep.get('name') or 'Escalation Policy'
    rules = pd_ep.get('escalation_rules', []) or []
    steps = []
    for index, rule in enumerate(rules):
        # timeout from PD rule
        timeout = _iso_duration_minutes(rule.get('escalation_delay_in_minutes') or 0)
        targets = []
        for t in rule.get('targets', []) or []:
            ttype = (t.get('type') or '').lower()
            if ttype == 'schedule_reference' or ttype == 'schedule':
                sched_id = t.get('id')
                sched_name = schedule_name_by_id.get(sched_id)
                if sched_name:
                    targets.append({"type": "OnCallSchedule", "name": sched_name})
            # We skip user/team targets in preview unless resolvable
        # Always create a step, even if there are no targets (delay-only rule)
        step = {
            "parent_position": index,
            "timeout": timeout,
            "distribution_type": "unspecified",
            "priorities": ["HIGH","MEDIUM","LOW"]
        }
        if targets:
            step["targets"] = targets
        steps.append(step)
    return {
        "name": name,
        "default": False,
        "step_strategy": "static",
        "steps": steps,
        "handoff_step": None,
        "repetitions": int(pd_ep.get('num_loops') or 0) if (pd_ep.get('repeat_enabled') in (True, 'true', 'True', 1)) else 0,
        "services": [
            {"id": s.get('id'), "name": s.get('name')}
            for s in (ep_to_services or {}).get(pd_ep.get('id'), [])
        ]
    }

# -------- Preview Builder (PD) --------

def build_team_preview_pd(pd_team: dict, members_map: dict):
    team_id = pd_team.get('id')
    # Services cache
    services = []
    try:
        services = pd_fetch_services() or []
    except Exception:
        services = []
    services_by_id = {s.get('id'): s for s in services}
    # Decide schedules for this bucket
    schedules = []
    if team_id and team_id.startswith("service:"):
        svc_id = team_id.split("service:",1)[1]
        svc = services_by_id.get(svc_id) or {}
        ep_ref = (svc.get('escalation_policy') or {}).get('id')
        # Prefer schedules referenced by the service's EP
        ep = pd_escalation_policy_details(ep_ref) if ep_ref else None
        sched_ids = _ep_schedule_ids(ep) if ep else []
        # Fetch each schedule detail
        for sid in sched_ids:
            det = pd_schedule_details(sid)
            if det:
                schedules.append({"id": sid, "name": det.get('name')})
    else:
        schedules = pd_team_schedules(team_id)
    # Build EP→services mapping (metadata)
    ep_to_services = {}
    for svc in services:
        ep = svc.get('escalation_policy') or {}
        if ep.get('id'):
            ep_to_services.setdefault(ep['id'], []).append({"id": svc.get('id'), "name": svc.get('name')})
    preview = {
        "team": pd_team.get('name'),
        "team_id": team_id,
        "members_to_add": [m.get('user',{}).get('email') for m in (members_map.get(team_id) or []) if (m.get('user',{}) or {}).get('email')],
        "schedules": [],
        "escalation_policies": [],
        "services": [{"id": s.get('id'), "name": s.get('name'), "escalation_policy_id": (s.get('escalation_policy') or {}).get('id')} for s in services]
    }
    schedule_name_by_id = {}
    for s in schedules or []:
        det = pd_schedule_details(s.get('id'))
        tz = det.get('time_zone') or det.get('time_zone_name') or 'UTC'
        layers = (det.get('schedule_layers') or det.get('layers') or [])
        rots = []
        for L in layers or []:  # include ALL layers so member preview is complete
            strat = _pd_layer_strategy(L)
            rots.append({
                "id": L.get('id'),
                "name": L.get('name') or 'Layer',
                "type": strat.get('type'),
                "length": L.get('rotation_turn_length'),
                "startDate": L.get('rotation_virtual_start'),
                "participants": _layer_members_emails(L),
                "time_restriction": None,
            })
        overrides = []
        try:
            since = (_now_dt() - timedelta(days=30)).isoformat().replace('+00:00','Z')
            until = (_now_dt() + timedelta(days=60)).isoformat().replace('+00:00','Z')
            overrides = pd_list_overrides(s.get('id'), since, until)
        except Exception:
            pass
        schedule_name_by_id[s.get('id')] = s.get('name')
        preview["schedules"].append({
            "name": s.get('name'),
            "id": s.get('id'),
            "timezone": tz,
            "rotations": rots,
            "schedule_override_count": len(overrides or [])
        })
    eps = pd_fetch_escalation_policies(team_id)
    # In service mode: only include its single EP
    if team_id and team_id.startswith("service:"):
        svc_id = team_id.split("service:",1)[1]
        svc = services_by_id.get(svc_id) or {}
        ep_ref = (svc.get('escalation_policy') or {}).get('id')
        eps = [pd_escalation_policy_details(ep_ref)] if ep_ref else []
    for ep in eps or []:
        preview["escalation_policies"].append({
            "name": ep.get('name'),
            "rules_count": len(ep.get('escalation_rules', [])),
            "mapped_firehydrant_preview": map_pd_ep_to_fh_preview(ep, schedule_name_by_id, ep_to_services)
        })
    return preview

# -------- Interactive UI (optional parity) --------

def display_team_selector(pd_teams: list, members_map: dict, schedules_map: dict):
    if not PICK_AVAILABLE:
        # Fallback: select all
        return pd_teams
    options = []
    for t in pd_teams:
        mct = len(members_map.get(t['id'], []))
        sct = len(schedules_map.get(t['id'], []))
        label = f"{t.get('name')} ({mct} members, {sct} schedules)"
        options.append(label)
    title = "Select teams to migrate (SPACE to toggle, ENTER to confirm)"
    selected = pick(options, title, multiselect=True, min_selection_count=1)
    labels = [s[0] for s in selected]
    idx_map = {f"{t.get('name')} ({len(members_map.get(t['id'], []))} members, {len(schedules_map.get(t['id'], []))} schedules)": t for t in pd_teams}
    return [idx_map[l] for l in labels if l in idx_map]

def select_existing_team(fh_teams: list):
    if not PICK_AVAILABLE:
        return None
    labels = [f"{t.get('name')} ({t.get('id')})" for t in fh_teams]
    if not labels:
        return None
    label, _ = pick(labels, "Select an existing FireHydrant team to map to", multiselect=False)
    # find team by id embedded in label
    tid = label[label.rfind('(')+1: label.rfind(')')]
    for t in fh_teams:
        if t.get('id') == tid:
            return t
    return None

def display_team_mapping_options(pd_team: dict):
    if not PICK_AVAILABLE:
        return {"action": "create"}
    options = [
        ("Skip the team", "skip"),
        ("Create a new team", "create"),
        ("Match to an existing team", "map"),
        ("Preview what will migrate (read-only)", "preview"),
        ("Exit", "exit"),
    ]
    labels = [o[0] for o in options]
    label, index = pick(labels, f"{pd_team.get('name')}: choose action (↑/↓, ENTER)", multiselect=False)
    return {"action": options[index][1]}

# -------- Main Flow (PD → FH) --------

def main():
    argv = sys.argv[1:]
    # deletes / revert-all passthroughs (same semantics as OG file)
    if '--help' in argv or '-h' in argv:
        print("Usage: python3 migrate-teams-pd.py [options]\n\n"
              "Options:\n"
              "  --set-fh-token VALUE          Save FIREHYDRANT_API_KEY into config.env\n"
              "  --set-pd-token VALUE          Save PAGERDUTY_API_TOKEN into config.env\n"
              "  --configure                   Interactive prompt to set FH and PD tokens (and optional PD base URL)\n"
              "  --delete-team VALUE[,VALUE…]  Delete FH team(s) by exact name or id (CSV supported)\n"
              "  --delete VALUE[,VALUE…]       Alias for --delete-team\n"
              "  --revert-all                  Delete all teams created by this migrator (destructive)\n"
              "  --preview-team VALUE[,VALUE…] Preview what will migrate for PD team(s) by name or id (CSV)\n"
              "  --delete-schedule ID          Delete FH schedule (requires --team-id)\n"
              "  --team-id ID                  Team id for schedule deletes\n"
              "  --restriction-strategy STR    weekly | per-day (default: weekly)\n"
              "  --timezone-override Tz        Force schedule timezone\n"
              "  --no-overrides                Skip migrating overrides\n"
              "  --align / --no-align          Toggle PD→FH on-call alignment (default on)\n"
              "  --dry-run                     Print actions; no writes\n"
              "  --verify-only                 Fetch and compare; no writes\n"
              "  --verbose                     Show debug payloads and full API responses\n"
              "  --output PATH                 Write preview JSON for --dry-run/--preview-team\n"
              "  --pd-no-teams                 Fallback mode when PD Teams API is unavailable\n"
              "  --pd-group-by MODE            teams | services (default: teams; services if no-teams)\n"
              "  --pd-group MODE               Alias for --pd-group-by\n"
              "  --align-now [NAME]            Align FH schedule to PD current on-call\n"
              "     --team-id ID | --team NAME     FH team id or exact name (or use NAME)\n"
              "     --fh-schedule-id ID | --schedule NAME  FH schedule id or name (defaults from team/NAME)\n"
              "     --pd-schedule-id ID | --pd-schedule NAME  PD schedule id or name\n"
              "     --pd-service-id ID | --pd-service NAME  PD service id or name (resolves first sched via EP)\n"
              "  --force                       Skip confirmations for destructive actions\n")
        return
    # Config setters (run-and-exit)
    if '--set-fh-token' in argv:
        try:
            val = argv[argv.index('--set-fh-token') + 1]
        except Exception:
            print("⚠️  Missing value for --set-fh-token"); return
        _write_config_pairs({"FIREHYDRANT_API_KEY": val})
        print("✅ Saved FIREHYDRANT_API_KEY to config.env")
        return
    if '--set-pd-token' in argv:
        try:
            val = argv[argv.index('--set-pd-token') + 1]
        except Exception:
            print("⚠️  Missing value for --set-pd-token"); return
        _write_config_pairs({"PAGERDUTY_API_TOKEN": val})
        print("✅ Saved PAGERDUTY_API_TOKEN to config.env")
        return
    if '--configure' in argv:
        try:
            fh = input("FireHydrant API key: ").strip()
            pd = input("PagerDuty API token: ").strip()
            base = input("PagerDuty base URL (ENTER for default https://api.pagerduty.com): ").strip()
            pairs = {}
            if fh: pairs["FIREHYDRANT_API_KEY"] = fh
            if pd: pairs["PAGERDUTY_API_TOKEN"] = pd
            if base: pairs["PAGERDUTY_BASE_URL"] = base
            if pairs:
                _write_config_pairs(pairs)
                print("✅ Saved to config.env")
            else:
                print("ℹ️  No changes written")
        except KeyboardInterrupt:
            print("\n✋ Aborted")
        return
    # align-now mode (runs independently; no migration)
    if '--align-now' in argv:
        idx = argv.index('--align-now')
        align_hint = None
        if idx + 1 < len(argv) and not argv[idx+1].startswith('--'):
            align_hint = argv[idx+1]
        # Resolve FH team
        team_id_cli = argv[argv.index('--team-id') + 1] if '--team-id' in argv else None
        team_name = argv[argv.index('--team') + 1] if '--team' in argv else None
        if not team_id_cli:
            hint = team_name or align_hint
            if hint:
                t = fh_find_team_by_name_or_id(hint)
                if t and t.get('id'):
                    team_id_cli = t['id']
        if not team_id_cli:
            print("❌ --align-now could not resolve team. Provide --team-id or --team NAME (or NAME after --align-now).")
            return
        # Resolve FH schedule
        fh_sched_id = argv[argv.index('--fh-schedule-id') + 1] if '--fh-schedule-id' in argv else None
        sched_name = argv[argv.index('--schedule') + 1] if '--schedule' in argv else None
        if not fh_sched_id:
            hint = sched_name or align_hint
            if hint:
                s = fh_find_schedule_by_name_or_id(team_id_cli, hint)
                if s and s.get('id'):
                    fh_sched_id = s['id']
        if not fh_sched_id:
            # fallback to first schedule
            lst = fh_list_schedules(team_id_cli) or []
            if lst:
                fh_sched_id = lst[0].get('id')
        if not fh_sched_id:
            print("❌ --align-now could not resolve FH schedule. Provide --fh-schedule-id or --schedule NAME.")
            return
        # Resolve PD schedule/service
        pd_sched_id = argv[argv.index('--pd-schedule-id') + 1] if '--pd-schedule-id' in argv else None
        if not pd_sched_id and '--pd-schedule' in argv:
            pd_sched_id = pd_find_schedule_by_name_or_id(argv[argv.index('--pd-schedule') + 1])
        pd_service_id = argv[argv.index('--pd-service-id') + 1] if '--pd-service-id' in argv else None
        if not pd_service_id and '--pd-service' in argv:
            pd_service_id = pd_find_service_by_name_or_id(argv[argv.index('--pd-service') + 1])
        if not pd_sched_id and not pd_service_id:
            # use align hint for PD service/schedule
            if align_hint:
                pd_sched_id = pd_find_schedule_by_name_or_id(align_hint)
                if not pd_sched_id:
                    pd_service_id = pd_find_service_by_name_or_id(align_hint)
        align_now_pd_to_fh(team_id_cli, fh_sched_id, pd_sched_id, pd_service_id)
        return

    # destructive helpers
    if '--revert-all' in argv:
        led = _read_ledger()
        targets = led.get('teams') or []
        if not targets:
            print("No ledger teams to delete.")
            return
        force = ('--force' in argv)
        print("⚠️  You are about to DELETE the following migrator-created teams:")
        for t in targets:
            print(f"  - {t.get('name')} ({t.get('id')})")
        if not force:
            confirm = input("Type DELETE or DELETE ALL to confirm: ").strip()
            if confirm not in ("DELETE","DELETE ALL"):
                print("✋ Aborted"); return
        for t in targets:
            r = requests.delete(f"{FIREHYDRANT_BASE_URL}/teams/{t['id']}", headers=FH_HEADERS)
            ok = r.status_code in (200,204)
            print(f"{'✅' if ok else '❌'} Deleted team {t.get('name')} ({t.get('id')}) → {r.status_code}")
            if ok:
                _remove_team_from_ledger(t['id'])
        return

    if '--delete-team' in argv or '--delete' in argv:
        idx = next(i for i,f in enumerate(argv) if f in ('--delete-team','--delete'))+1
        values = []
        # collect CSV
        if idx < len(argv):
            raw = argv[idx]
            if raw.startswith('--'):
                print("⚠️  Missing values for --delete-team"); return
            values = [v.strip() for v in raw.split(',') if v.strip()]
        if not values:
            print("⚠️  Provide names or ids"); return
        fh_teams = []
        for v in values:
            t = fh_find_team_by_name_or_id(v)
            if t: fh_teams.append(t)
        if not fh_teams:
            print("No matching teams."); return
        force = ('--force' in argv)
        print("⚠️  About to delete team(s):")
        for t in fh_teams:
            print(f"  - {t.get('name')} ({t.get('id')})")
        if not force:
            if input("Type DELETE to confirm: ").strip() != "DELETE":
                print("✋ Aborted"); return
        for t in fh_teams:
            r = requests.delete(f"{FIREHYDRANT_BASE_URL}/teams/{t['id']}", headers=FH_HEADERS)
            ok = r.status_code in (200,204)
            print(f"{'✅' if ok else '❌'} Deleted team {t.get('name')} → {r.status_code}")
            if ok:
                _remove_team_from_ledger(t.get('id'))
        return

    # delete-schedule
    if '--delete-schedule' in argv:
        try:
            sched_ident = argv[argv.index('--delete-schedule') + 1]
        except Exception:
            print("⚠️  Missing value for --delete-schedule"); return
        if '--team-id' not in argv:
            print("⚠️  --team-id is required with --delete-schedule"); return
        try:
            team_id_cli = argv[argv.index('--team-id') + 1]
        except Exception:
            print("⚠️  Missing value for --team-id"); return
        s = fh_find_schedule_by_name_or_id(team_id_cli, sched_ident)
        if not s:
            print("❌ Schedule not found"); return
        force = ('--force' in argv)
        print(f"⚠️  About to delete schedule: {s.get('name')} ({s.get('id')})")
        if not force:
            if input("Type DELETE to confirm: ").strip() != "DELETE":
                print("✋ Aborted"); return
        r = requests.delete(f"{FIREHYDRANT_BASE_URL}/teams/{team_id_cli}/on_call_schedules/{s.get('id')}", headers=FH_HEADERS)
        ok = r.status_code in (200,204)
        print(f"{'✅' if ok else '❌'} Deleted schedule {s.get('name')} → {r.status_code}")
        return

    # ---------- Non-interactive utilities ----------
    # Human-readable rotation preview (PD and/or FH)
    if '--show-rotation' in argv:
        def _fh_list_schedules(team_id: str):
            rh = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
            r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=rh)
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get('data') if isinstance(data, dict) else (data or [])
        def _find_fh_schedule_by_name_or_id(team_id: str, ident: str):
            items = _fh_list_schedules(team_id)
            ident_l = (ident or '').strip().lower()
            for it in items:
                if (it.get('id') or '').lower() == ident_l:
                    return it
            for it in items:
                if (it.get('name') or '').strip().lower() == ident_l:
                    return it
            for it in items:
                if ident_l and ident_l in (it.get('name') or '').strip().lower():
                    return it
            return None
        def _fh_human_lines(team_id: str, schedule_id: str, days: int = 7):
            sj = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}", headers=FH_HEADERS)
            if sj.status_code != 200:
                return [f"⚠️  Failed to fetch FH schedule {schedule_id} ({sj.status_code})"]
            sj = sj.json()
            tz = sj.get('time_zone') or 'UTC'
            now = _now_dt(); end = now + timedelta(days=days)
            lines = []
            for rot in sj.get('rotations', []) or []:
                for sh in rot.get('shifts', []) or []:
                    st = _parse_iso(sh.get('start_time')); et = _parse_iso(sh.get('end_time'))
                    if not st or not et: continue
                    if st < end and et > now:
                        uname = (sh.get('user') or {}).get('name') or 'UNASSIGNED'
                        lines.append(f"{uname} {st.isoformat()} → {et.isoformat()} ({tz})")
            return sorted(lines)
        def _pd_human_lines(schedule_id: str, days: int = 7):
            now = _now_dt()
            since = now.isoformat().replace('+00:00','Z')
            until = (now + timedelta(days=days)).isoformat().replace('+00:00','Z')
            segs = pd_oncall_segments_for_schedule(schedule_id, since, until) or []
            lines = []
            for s in segs:
                email = s.get('email') or 'unknown'
                lines.append(f"{email} {s.get('start')} → {s.get('end')} (PD)")
            return lines
        print("\n🔎 Rotation preview (next 7 days):")
        # Optional name + provider flags
        name_filter = None
        try:
            if '--name' in argv:
                name_filter = argv[argv.index('--name') + 1].strip()
        except Exception:
            name_filter = None
        want_pd = ('--pagerduty' in argv)
        want_og = ('--opsgenie' in argv)
        want_fh = ('--firehydrant' in argv)

        # Opsgenie side by name
        if want_og and name_filter:
            try:
                OG_KEY = os.getenv('OPSGENIE_API_KEY') or ''
                OG_BASE = (os.getenv('OPSGENIE_BASE_URL') or 'https://api.opsgenie.com') + '/v2'
                H_OG = {"Authorization": f"GenieKey {OG_KEY}"}
                def _og_all_schedules():
                    r = requests.get(f"{OG_BASE}/schedules", headers=H_OG)
                    return (r.json().get('data') if r.status_code == 200 else []) or []
                def _norm(x: str) -> str:
                    try:
                        import re
                        v = (x or '').strip().lower()
                        v = re.sub(r"[^a-z0-9]+", "-", v)
                        v = re.sub(r"-+", "-", v)
                        return v.strip("-")
                    except Exception:
                        return (x or '').strip().lower()
                def _og_find_schedule_by_name_or_id(ident: str):
                    ident_l = (ident or '').strip().lower()
                    # try direct id
                    r = requests.get(f"{OG_BASE}/schedules/{ident_l}", headers=H_OG)
                    if r.status_code == 200 and (r.json().get('data') or {}).get('id'):
                        return r.json().get('data')
                    # fallback list with normalized contains match (handles spaces/_/- differences)
                    ident_n = _norm(ident_l)
                    best = None
                    for s in _og_all_schedules():
                        nm = (s.get('name') or '').strip()
                        if not nm:
                            continue
                        if ident_l in nm.lower():
                            return s
                        if ident_n and ident_n in _norm(nm):
                            best = best or s
                    return best
                def _og_timeline_segments(schedule_id: str, days: int = 7, tz: str = 'UTC'):
                    # Use timeline with integer interval (days) starting today
                    start = _now_dt()
                    date_str = start.strftime('%Y-%m-%dT00:00:00Z')
                    tl = requests.get(f"{OG_BASE}/schedules/{schedule_id}/timeline",
                                      headers=H_OG,
                                      params={"interval": days, "date": date_str, "timezone": tz})
                    segments = []
                    if tl.status_code != 200:
                        return segments
                    data = tl.json().get('data') or tl.json()
                    final_tl = (data.get('finalTimeline') or {})
                    # Parse rotations[].periods[]
                    for rot in final_tl.get('rotations', []) or []:
                        for it in rot.get('periods', []) or []:
                            rec = (it.get('recipient') or {})
                            email = rec.get('username') or rec.get('name')
                            segments.append({"email": email, "start": it.get('startDate'), "end": it.get('endDate')})
                    return segments
                og_sched = _og_find_schedule_by_name_or_id(name_filter)
                if og_sched:
                    print("\nOpsgenie:")
                    segs = _og_timeline_segments(og_sched.get('id'), days=7, tz=(og_sched.get('timezone') or 'UTC'))
                    if not segs:
                        print(f"  No timeline segments available for '{og_sched.get('name')}'.")
                    else:
                        for s in segs:
                            print(f"  - {s.get('email') or 'unknown'} {s.get('start')} → {s.get('end')} (OG)")
                else:
                    print(f"\nOpsgenie: no schedule matched name '{name_filter}'")
            except Exception as _og_e:
                print(f"\nOpsgenie: lookup failed ({_og_e})")

        # PD side by name
        if want_pd and name_filter:
            pd_sid_named = pd_find_schedule_by_name_or_id(name_filter)
            if pd_sid_named:
                print("\nPagerDuty:")
                for line in _pd_human_lines(pd_sid_named, days=7):
                    print(f"  - {line}")
            else:
                print(f"\nPagerDuty: no schedule matched name '{name_filter}'")

        # FH side by name (search all teams)
        if want_fh and name_filter:
            print("\nFireHydrant:")
            fh_teams_all = fh_fetch_teams()
            any_found = False
            for _t in fh_teams_all or []:
                rh = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
                rs = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{_t.get('id')}/on_call_schedules", headers=rh)
                items = (rs.json().get('data') if rs.status_code == 200 and isinstance(rs.json(), dict) else (rs.json() if rs.status_code == 200 else [])) or []
                for sc in items:
                    nm = (sc.get('name') or '')
                    if name_filter.lower() in nm.strip().lower():
                        any_found = True
                        for line in _fh_human_lines(_t.get('id'), sc.get('id'), days=7):
                            print(f"  - [{_t.get('name')}] {nm}: {line}")
            if not any_found:
                print(f"  No FH schedules matched name '{name_filter}'")

        # PD side by explicit id/name flags
        pd_sid = argv[argv.index('--pd-schedule-id') + 1] if '--pd-schedule-id' in argv else None
        if not pd_sid and '--pd-schedule' in argv:
            pd_sid = pd_find_schedule_by_name_or_id(argv[argv.index('--pd-schedule') + 1])
        if pd_sid:
            print("\nPagerDuty:")
            for line in _pd_human_lines(pd_sid, days=7):
                print(f"  - {line}")
        # FH side by explicit team/schedule flags
        team_id_cli = argv[argv.index('--team-id') + 1] if '--team-id' in argv else None
        fh_sid = argv[argv.index('--fh-schedule-id') + 1] if '--fh-schedule-id' in argv else None
        if not fh_sid and team_id_cli and '--schedule' in argv:
            s = _find_fh_schedule_by_name_or_id(team_id_cli, argv[argv.index('--schedule') + 1])
            fh_sid = s.get('id') if s else None
        if team_id_cli and fh_sid:
            print("\nFireHydrant:")
            for line in _fh_human_lines(team_id_cli, fh_sid, days=7):
                print(f"  - {line}")
        return

    # Backfill overrides/boundary overlays based on a filter (no interactive pick)
    if '--overrides' in argv:
        assume_yes = ('--yes' in argv)
        idx = argv.index('--overrides') + 1
        flt = None
        if idx < len(argv) and not argv[idx].startswith('-'):
            flt = argv[idx].strip()
        fh_users = fh_fetch_users()
        fh_teams = fh_fetch_teams()
        def _fh_list_schedules(team_id: str):
            r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=FH_HEADERS)
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get('data') if isinstance(data, dict) else (data or [])
        targets = []
        for t in fh_teams or []:
            tname = (t.get('name') or '').strip().lower()
            for s in _fh_list_schedules(t.get('id')) or []:
                sname = (s.get('name') or '').strip().lower()
                if not flt or (flt.lower() in tname) or (flt.lower() in sname):
                    targets.append((t, s))
        if not targets:
            print("ℹ️  No matching FH schedules found for the provided filter.")
            return
        total = 0
        for t, s in targets:
            pd_sid = pd_find_schedule_by_name_or_id(s.get('name')) or (pd_find_schedule_by_name_or_id(flt) if flt else None)
            print(f"\n▶ {t.get('name')} · {s.get('name')}  |  PD match: {pd_sid or 'not found'}")
            if not pd_sid:
                continue
            if not assume_yes:
                print("  🔎 PD segments (next 8 weeks):")
                since = _now_dt().isoformat().replace('+00:00','Z')
                until = (_now_dt() + timedelta(days=56)).isoformat().replace('+00:00','Z')
                segs = pd_oncall_segments_for_schedule(pd_sid, since, until) or []
                for seg in segs[:20]:
                    print(f"    - {seg.get('email') or 'unknown'} {seg.get('start')} → {seg.get('end')}")
                if len(segs) > 20:
                    print(f"    … and {len(segs)-20} more")
                yn = input("  Apply these as FH overrides? (y/N): ").strip().lower()
                if yn not in ('y','yes'):
                    continue
            rot_id = (s.get('rotations') or [{}])[0].get('id')
            applied = fh_apply_pd_boundary_overrides(t.get('id'), s.get('id'), rot_id, pd_sid, fh_users, weeks_ahead=12)
            print(f"  ✅ Applied {applied} override(s)")
            total += applied
        print(f"\n🎉 Done. Total overrides applied: {total}")
        return

    # Fetch PD teams/users
    pd_teams = pd_fetch_teams()
    pd_users = pd_fetch_users()
    # Build global PD user id → email map for schedule member resolution
    try:
        PD_USER_EMAIL_BY_ID.clear()
        for u in pd_users or []:
            if u.get('id') and u.get('email'):
                PD_USER_EMAIL_BY_ID[u['id']] = u['email']
    except Exception:
        pass
    fh_users = fh_fetch_users()
    fh_teams = fh_fetch_teams()
    if (PD_NO_TEAMS or (pd_teams is None) or (len(pd_teams) == 0)) and not VERIFY_ONLY and not DRY_RUN:
        print("ℹ️  Running in no-teams mode. Schedules and escalation policies will be fetched globally.")

    # members by team (parallel)
    team_members_map = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(pd_team_members, t['id']): t for t in pd_teams}
        for f in as_completed(futures):
            t = futures[f]
            try:
                team_members_map[t['id']] = f.result() or []
            except Exception:
                team_members_map[t['id']] = []

    # schedules by team (parallel)
    schedules_map = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(pd_team_schedules, t['id']): t for t in pd_teams}
        for f in as_completed(futures):
            t = futures[f]
            try:
                schedules_map[t['id']] = f.result() or []
            except Exception:
                schedules_map[t['id']] = []

    # verify-only summary
    if VERIFY_ONLY:
        print("\n🔎 Verify-only mode: No changes will be made.")
        print(f"  PD teams: {len(pd_teams)} | PD users: {len(pd_users)} | FH users: {len(fh_users)}")
        return

    # --preview-team flow (PD ids or names)
    if '--preview-team' in argv:
        idx = argv.index('--preview-team') + 1
        if idx >= len(argv):
            print("⚠️  Missing value for --preview-team"); return
        raw = argv[idx]
        idents = [v.strip() for v in raw.split(',') if v.strip()]
        def find_pd_team(ident: str):
            ident_l = ident.strip().lower()
            for t in pd_teams:
                if (t.get('id') or '').lower() == ident_l: return t
            for t in pd_teams:
                if (t.get('name') or '').strip().lower() == ident_l: return t
            return None
        previews = []
        for ident in idents:
            t = find_pd_team(ident)
            if not t:
                print(f"❌ PD team not found: {ident}")
                continue
            previews.append(build_team_preview_pd(t, team_members_map))
        # Choose output path: flag wins; otherwise prompt with sensible default
        default_name = f"migration_preview_pd_{_now_dt().strftime('%Y%m%dT%H%M%SZ')}.json"
        out_path = OUTPUT_PATH
        if not out_path:
            try:
                inp = input(f"Filename for PD preview (ENTER for {default_name}): ").strip()
                out_path = inp or default_name
            except Exception:
                out_path = default_name
        with open(out_path, 'w') as f:
            json.dump({"generated_at": _now_dt().isoformat(), "teams": previews}, f, indent=2)
        print(f"🧪 Preview written: {out_path}")
        return

    if DRY_RUN:
        previews = []
        for t in pd_teams:
            previews.append(build_team_preview_pd(t, team_members_map))
        # Choose output path: flag wins; otherwise prompt with sensible default
        default_name = f"migration_preview_pd_{_now_dt().strftime('%Y%m%dT%H%M%SZ')}.json"
        out_path = OUTPUT_PATH
        if not out_path:
            try:
                inp = input(f"Filename for PD DRY-RUN preview (ENTER for {default_name}): ").strip()
                out_path = inp or default_name
            except Exception:
                out_path = default_name
        with open(out_path, 'w') as f:
            json.dump({"generated_at": _now_dt().isoformat(), "teams": previews}, f, indent=2)
        print(f"🧪 DRY-RUN preview written: {out_path}")
        return

    # Interactive team selector (if available), else all PD teams
    selected_teams = display_team_selector(pd_teams, team_members_map, schedules_map)
    if not selected_teams:
        print("❌ No teams selected. Exiting.")
        return

    for t in selected_teams:
        # Team action
        decision = display_team_mapping_options(t)
        while decision.get('action') == 'preview':
            try:
                prev = build_team_preview_pd(t, team_members_map)
                print("\n🧪 Preview (no changes):\n" + json.dumps(prev, indent=2))
                path = OUTPUT_PATH or f"preview-pd-{(t.get('name') or 'team').lower().replace(' ','-')}-{_now_dt().strftime('%Y%m%dT%H%M%SZ')}.json"
                try:
                    with open(path, 'w') as f:
                        json.dump(prev, f, indent=2)
                    print(f"📄 Wrote preview to: {path}")
                except Exception as _we:
                    vprint(f"  ⚠️ Could not write file: {_we}")
                input("\nPress ENTER to return to action menu for this team…")
            except Exception as _pe:
                print(f"  ⚠️ Preview failed: {_pe}")
            decision = display_team_mapping_options(t)
            if decision.get('action') == 'exit':
                print("\n👋 Exit selected. Stopping migration.")
                return
        if decision.get('action') == 'exit':
            print("\n👋 Exit selected. Stopping migration.")
            return
        # Determine FH team target
        if decision.get('action') == 'map':
            mapped = select_existing_team(fh_teams)
            if not mapped:
                print("  ⚠️ No existing team selected; skipping")
                continue
            fh_team = mapped
        elif decision.get('action') == 'skip':
            continue
        else:
            stage(f"\n🔨 Creating team '{t.get('name')}' in FireHydrant...")
            fh_team = fh_create_team(t)
            if not fh_team:
                continue
            fh_teams.append(fh_team)
        # 1) Team
        # 2) Members
        stage("👥 Creating team members…")
        member_emails = [ (m.get('user') or {}).get('email') for m in (team_members_map.get(t['id']) or []) ]
        member_emails = [e for e in (member_emails or []) if e]
        fh_ids = []
        for em in member_emails:
            u = fh_find_user_by_email(em, fh_users)
            if u and u.get('id'):
                fh_ids.append(u['id'])
        added = fh_add_users_to_team(fh_team['id'], fh_ids)
        # 3) Schedules
        stage("📅 Creating schedules and rotations…")
        pd_scheds = pd_team_schedules(t['id']) or []
        created_schedules = []
        # Map PD schedule id -> FH schedule id for EP target resolution
        pd_to_fh_sched = {}
        for s in pd_scheds:
            det = pd_schedule_details(s['id']) or {}
            tz = det.get('time_zone') or det.get('time_zone_name') or 'UTC'
            layers = det.get('schedule_layers') or det.get('layers') or []
            if not layers:
                continue
            # collect members across ALL layers (fix)
            participant_emails = []
            seen_em = set()
            for L in layers or []:
                for em in _layer_members_emails(L):
                    if em and em not in seen_em:
                        seen_em.add(em); participant_emails.append(em)
            vprint(f"PD schedule '{s.get('name')}' participants (emails): {participant_emails}")
            member_ids = []
            for em in participant_emails:
                u = fh_find_user_by_email(em, fh_users)
                if u and u.get('id'):
                    member_ids.append(u['id'])
            vprint(f"FH member_ids resolved for '{s.get('name')}': {member_ids}")
            # Ensure team membership includes these users
            if member_ids:
                fh_add_users_to_team(fh_team['id'], member_ids)
            # anchor strategy from first layer
            strategy = _pd_layer_strategy(layers[0])
            restrictions = None  # PD has no direct weekly restriction shape; skip by default
            sched = fh_create_schedule_with_rotation(fh_team['id'], s.get('name'), tz, member_ids, strategy, restrictions)
            if not sched:
                continue
            created_schedules.append(sched)
            try:
                pd_to_fh_sched[s.get('id')] = sched.get('id')
            except Exception:
                pass
            # Assign rotation members explicitly (API sometimes ignores at create-time)
            try:
                rot_id = (sched.get('rotations') or [{}])[0].get('id')
                if rot_id and member_ids:
                    ok = fh_add_members_to_rotation(fh_team['id'], sched.get('id'), rot_id, member_ids)
                    vprint(f"Add rotation members → {ok}")
            except Exception as _me:
                vprint(f"  ⚠️ Could not add rotation members: {_me}")
            # overrides (window: now-30d .. now+60d)
            if not NO_OVERRIDES:
                try:
                    since = (_now_dt() - timedelta(days=30)).isoformat().replace('+00:00','Z')
                    until = (_now_dt() + timedelta(days=60)).isoformat().replace('+00:00','Z')
                    ovs = pd_list_overrides(s['id'], since, until) or []
                    rot_id = (sched.get('rotations') or [{}])[0].get('id')
                    for ov in ovs:
                        start = ov.get('start'); end = ov.get('end')
                        user_email = (ov.get('user') or {}).get('email')
                        fh_uid = None
                        if user_email:
                            fu = fh_find_user_by_email(user_email, fh_users)
                            fh_uid = fu.get('id') if fu else None
                        fh_apply_override(fh_team['id'], sched.get('id'), rot_id, start, end, fh_uid)
                except Exception:
                    pass
            # Overlay PD boundary segments as overrides to ensure exact parity
            try:
                if PD_ENFORCE_BOUNDARIES:
                    rot_id = (sched.get('rotations') or [{}])[0].get('id')
                    applied = fh_apply_pd_boundary_overrides(fh_team['id'], sched.get('id'), rot_id, s['id'], fh_users, weeks_ahead=12)
                    vprint(f"Applied PD boundary overrides: {applied}")
            except Exception:
                pass
            # Align "who's on-call now"
            if ALIGN_NOW:
                try:
                    emails = pd_oncall_emails_for_schedule(s['id'])
                    desired = emails[0] if emails else None
                    if desired:
                        fu = fh_find_user_by_email(desired, fh_users)
                        if fu and fu.get('id'):
                            fh_align_active_shift_to_user(fh_team['id'], sched.get('id'), fu['id'])
                except Exception:
                    pass
            # As a final safety, claim any unassigned future shifts in round-robin order
            try:
                if member_ids:
                    claimed = fh_assign_unclaimed_shifts_round_robin(fh_team['id'], sched.get('id'), member_ids)
                    vprint(f"Claimed {claimed} unassigned shifts via round-robin")
            except Exception:
                pass
        # 4) Escalation policies (best-effort)
        stage("📈 Creating escalation policies…")
        # Fetch PD EPs and create minimal FH EP per policy targeting team's first schedule if present
        try:
            eps = pd_fetch_escalation_policies(t['id']) or []
            fh_scheds = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/on_call_schedules", headers=FH_HEADERS).json()
            fh_sched_items = fh_scheds.get('data') if isinstance(fh_scheds, dict) else fh_scheds
            first_sched_id = (fh_sched_items or [{}])[0].get('id')
            # PD user id -> email
            pd_user_email_by_id = {}
            try:
                for u in pd_users or []:
                    if u.get('id') and u.get('email'):
                        pd_user_email_by_id[u['id']] = u['email']
            except Exception:
                pass
            # FH user lookup cache by email
            fh_user_id_by_email = {}
            try:
                for u in fh_users or []:
                    if u.get('email') and u.get('id'):
                        fh_user_id_by_email[u['email'].lower()] = u['id']
            except Exception:
                pass
            created = 0
            for ep in eps:
                name = ep.get('name') or 'Escalation Policy'
                # Avoid duplicates
                exist = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/escalation_policies", headers=FH_HEADERS)
                exists = { (e.get('name') or '').lower() for e in ((exist.json().get('data') if isinstance(exist.json(), dict) else exist.json()) or []) } if exist.status_code==200 else set()
                if name.strip().lower() in exists:
                    continue
                steps = []
                rules = ep.get('escalation_rules', []) or []
                # Build steps from PD rules, preserving "delay-only" rules
                for idx, rule in enumerate(rules):
                    timeout = _iso_duration_minutes(rule.get('escalation_delay_in_minutes') or 0)
                    mapped_targets = []
                    for trg in rule.get('targets', []) or []:
                        ttype = (trg.get('type') or '').lower()
                        # Schedules
                        if ttype in ('schedule_reference','schedule'):
                            pd_sid = trg.get('id')
                            fh_sid = pd_to_fh_sched.get(pd_sid) or first_sched_id
                            if fh_sid:
                                mapped_targets.append({"type": "OnCallSchedule", "id": fh_sid})
                        # Users
                        elif ttype in ('user_reference','user'):
                            pd_uid = trg.get('id')
                            email = pd_user_email_by_id.get(pd_uid)
                            if email:
                                fh_uid = fh_user_id_by_email.get(email.lower())
                                if fh_uid:
                                    mapped_targets.append({"type": "User", "id": fh_uid})
                    step = {
                        "parent_position": idx,
                        "timeout": timeout,
                        "distribution_type": "unspecified",
                        "priorities": ["HIGH","MEDIUM","LOW"]
                    }
                    if mapped_targets:
                        step["targets"] = mapped_targets
                    steps.append(step)
                # If PD EP has no rules, create a single step to the first schedule if it exists
                if not steps and first_sched_id:
                    steps.append({"parent_position": 0, "targets": [{"type": "OnCallSchedule","id": first_sched_id}], "timeout": "PT5M", "distribution_type": "unspecified", "priorities": ["HIGH","MEDIUM","LOW"]})
                if not steps:
                    continue
                repetitions = int(ep.get('num_loops') or 0) if (ep.get('repeat_enabled') in (True, 'true', 'True', 1)) else 0
                body = {
                    "name": name,
                    "default": bool(created == 0),
                    "step_strategy": "static",
                    "steps": steps,
                    "handoff_step": None,
                    "repetitions": repetitions,
                    "prioritized_settings": {"high":{"repetitions":None,"handoff_step":None},"medium":{"repetitions":None,"handoff_step":None},"low":{"repetitions":None,"handoff_step":None}},
                    "teamId": fh_team['id']
                }
                if not (DRY_RUN or VERIFY_ONLY):
                    r = requests.post(f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/escalation_policies", headers=FH_HEADERS, json=body)
                    if r.status_code in (200,201):
                        created += 1
            # 5) Summary
        except Exception:
            pass
        stage(f"✅ Finishing up… Team: {fh_team.get('name')} | Users: +{added} | Schedules: {len(created_schedules)} (rots 1) | Overrides: {'skipped' if NO_OVERRIDES else 'applied'} | EPs: best-effort")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


