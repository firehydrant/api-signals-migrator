#!/usr/bin/env python3
"""
Simple Opsgenie to FireHydrant Team Migration Tool
"""

import requests
import json
import os
import sys
from pick import pick
from dotenv import load_dotenv
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import builtins
import sys

# Load environment variables
load_dotenv('config.env', override=True)

# API Configuration
OPSGENIE_API_KEY = os.getenv('OPSGENIE_API_KEY')
FIREHYDRANT_API_KEY = os.getenv('FIREHYDRANT_API_KEY')

VERBOSE = (os.getenv('VERBOSE_LOGS', 'false').lower() in ('1','true','yes')) or ('--verbose' in sys.argv)
# Minimal is now the default; enable verbose to show debug payloads
MINIMAL = True
def vprint(msg: str):
    if VERBOSE:
        print(msg)

def iprint(msg: str):
    if not MINIMAL:
        print(msg)

# Debug: Show loaded API keys (only when VERBOSE)
vprint(f"🔑 Loaded FireHydrant API Key: {FIREHYDRANT_API_KEY}")
vprint(f"🔑 Loaded Opsgenie API Key: {OPSGENIE_API_KEY[:8] if OPSGENIE_API_KEY else 'None'}...")

OPSGENIE_BASE_URL = "https://api.opsgenie.com/v2"
FIREHYDRANT_BASE_URL = "https://api.firehydrant.io/v1"

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
    with open(path, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")

# Runtime flags (set via CLI in main())
DRY_RUN = False
VERIFY_ONLY = False
NO_OVERRIDES = False
RESTRICTION_STRATEGY = os.getenv('RESTRICTION_STRATEGY', 'weekly')  # 'weekly' | 'per-day'
TIMEZONE_OVERRIDE = os.getenv('TIMEZONE_OVERRIDE')
ALIGN_NOW = True
OUTPUT_PATH = None
SUMMARY_ONLY = True
LEDGER_PATH = os.getenv('MIGRATOR_LEDGER_PATH', 'migrator_ledger.json')
CONCURRENCY = int(os.getenv('MIGRATOR_CONCURRENCY', '16'))

# High-performance HTTP session with connection pooling
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _session = requests.Session()
    _adapter = HTTPAdapter(pool_connections=CONCURRENCY, pool_maxsize=max(CONCURRENCY * 2, 32))
    _session.mount('https://', _adapter)
    _session.mount('http://', _adapter)
    # Monkeypatch requests.* to use the shared session by default
    requests.get = _session.get
    requests.post = _session.post
    requests.put = _session.put
    requests.patch = _session.patch
    requests.delete = _session.delete
except Exception:
    pass

def _slugify(value: str) -> str:
    """Create a filesystem-friendly slug from a name."""
    try:
        import re
        v = (value or '').strip().lower()
        v = re.sub(r"[^a-z0-9\-\_\s]", "", v)
        v = re.sub(r"[\s_]+", "-", v)
        v = re.sub(r"-+", "-", v)
        return v[:60] or "team"
    except Exception:
        return "team"


# ---------------------------- Run Ledger ---------------------------- #
def _load_ledger() -> dict:
    try:
        if os.path.exists(LEDGER_PATH):
            with open(LEDGER_PATH, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {"teams": []}


def _save_ledger(ledger: dict) -> None:
    try:
        with open(LEDGER_PATH, 'w') as f:
            json.dump(ledger, f, indent=2)
    except Exception as _e:
        vprint(f"⚠️  Failed to write ledger: {_e}")


def _record_created_team(team_id: str, name: str) -> None:
    try:
        ledger = _load_ledger()
        if not any(t.get('id') == team_id for t in ledger.get('teams', [])):
            ledger.setdefault('teams', []).append({"id": team_id, "name": name})
            _save_ledger(ledger)
    except Exception as _e:
        vprint(f"⚠️  Failed to record team in ledger: {_e}")


class SilentPrint:
    """Context manager to temporarily silence print() calls."""
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._orig = None
    def __enter__(self):
        if self.enabled:
            self._orig = builtins.print
            builtins.print = lambda *a, **k: None
    def __exit__(self, exc_type, exc, tb):
        if self.enabled and self._orig:
            builtins.print = self._orig
        return False


def stage_print(message: str) -> None:
    try:
        sys.__stdout__.write(message + "\n")
        sys.__stdout__.flush()
    except Exception:
        print(message)


def build_team_preview(og_team: dict, team_members_map: dict) -> dict:
    """Assemble a detailed, read-only preview for a single Opsgenie team.
    Includes members to add, schedules with rotation details, time restrictions,
    override counts, and a preview of mapped escalation policies (no writes).
    """
    team_id = og_team.get('id')
    team_name = og_team.get('name')
    members_to_add = [
        (m.get('user') or {}).get('username')
        for m in (team_members_map.get(team_id) or [])
        if (m.get('user') or {}).get('username')
    ]

    schedules_preview = []
    try:
        scheds = get_team_schedules(team_id)
    except Exception:
        scheds = []

    for s in scheds:
        sched_id = s.get('id')
        details = get_schedule_details(sched_id) or {}
        tz = details.get('timezone')
        rotations = details.get('rotations', []) or []
        rots_preview = []
        for rot in rotations:
            participants = []
            for p in rot.get('participants', []) or []:
                if p.get('type') == 'user' and p.get('username'):
                    participants.append(p.get('username'))
                elif p.get('type') == 'team' and (p.get('team') or {}).get('name'):
                    participants.append(f"team:{(p.get('team') or {}).get('name')}")
            tr = rot.get('timeRestriction') or {}
            tr_type = tr.get('type')
            single = tr.get('restriction') or {}
            arr = tr.get('restrictions') or []
            restrictions_preview = None
            if tr_type:
                restrictions_preview = {
                    'type': tr_type,
                    'restriction': {k: single.get(k) for k in ['startDay','endDay','startHour','endHour','startMin','endMin'] if k in single},
                    'restrictions_count': len(arr)
                }
            rots_preview.append({
                'id': rot.get('id'),
                'name': rot.get('name') or 'Rotation',
                'type': rot.get('type'),
                'length': rot.get('length'),
                'startDate': rot.get('startDate'),
                'participants': participants,
                'time_restriction': restrictions_preview,
                'override_count': len(rot.get('overrides') or [])
            })

        # Schedule-level overrides
        try:
            schedule_overrides = list_opsgenie_overrides(sched_id) or []
            schedule_override_count = len(schedule_overrides)
        except Exception:
            schedule_override_count = 0

        schedules_preview.append({
            'name': s.get('name'),
            'id': sched_id,
            'timezone': tz,
            'rotations': rots_preview,
            'schedule_override_count': schedule_override_count
        })

    # Escalation policies preview (mapped to FH payloads without posting)
    eps_preview = []
    try:
        og_eps = fetch_opsgenie_escalation_policies(team_id) or []
        og_eps = filter_escalation_policies_for_team(og_team, og_eps)
        for ep in og_eps:
            mapped = map_opsgenie_ep_to_fh(ep, [])  # pass empty users; payload preview only
            eps_preview.append({
                'name': ep.get('name'),
                'rules_count': len(ep.get('rules') or []),
                'mapped_firehydrant_preview': mapped
            })
    except Exception:
        pass

    return {
        'team': team_name,
        'team_id': team_id,
        'members_to_add': members_to_add,
        'schedules': schedules_preview,
        'escalation_policies': eps_preview
    }


# Global accumulator for override debug payloads (printed in MIGRATION SUMMARY)
OVERRIDE_PUT_DETAILS = []
OVERRIDE_GET_PAYLOADS = []
EP_MAPPING_SUMMARY = []


def fetch_opsgenie_teams():
    """Fetch all teams from Opsgenie"""
    iprint("\n🔍 Fetching teams from Opsgenie...")
    
    headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    
    try:
        response = requests.get(
            f"{OPSGENIE_BASE_URL}/teams",
            headers=headers
        )
        response.raise_for_status()
        
        teams = response.json().get('data', [])
        iprint(f"✅ Found {len(teams)} teams in Opsgenie")
        return teams
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching Opsgenie teams: {e}")
        return []


def fetch_firehydrant_teams():
    """Fetch all teams from FireHydrant"""
    iprint("\n🔍 Fetching teams from FireHydrant...")
    
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}"
    }
    
    try:
        response = requests.get(
            f"{FIREHYDRANT_BASE_URL}/teams",
            headers=headers
        )
        response.raise_for_status()
        
        teams = response.json().get('data', [])
        iprint(f"✅ Found {len(teams)} teams in FireHydrant")
        return teams
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching FireHydrant teams: {e}")
        return []


def find_firehydrant_team_by_name_or_id(identifier: str, teams: list):
    """Return a FH team dict by id or exact (case-insensitive) name match."""
    if not identifier:
        return None
    ident_lower = identifier.strip().lower()
    # Prefer id match
    for t in teams or []:
        if (t.get('id') or '').lower() == ident_lower:
            return t
    # Fallback to exact name (case-insensitive)
    for t in teams or []:
        nm = (t.get('name') or '').strip().lower()
        if nm == ident_lower:
            return t
    return None


def delete_firehydrant_team(team_id: str):
    """Delete a FireHydrant team by id. Returns (ok: bool, status: int, body: str)."""
    headers = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
    try:
        url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}"
        resp = requests.delete(url, headers=headers)
        return (resp.status_code in (200, 202, 204), resp.status_code, getattr(resp, 'text', ''))
    except Exception as e:
        return (False, -1, str(e))


def delete_firehydrant_schedule(team_id: str, schedule_id: str):
    """Delete a FireHydrant schedule by id. Returns (ok: bool, status: int, body: str)."""
    headers = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
    try:
        url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}"
        resp = requests.delete(url, headers=headers)
        return (resp.status_code in (200, 202, 204), resp.status_code, getattr(resp, 'text', ''))
    except Exception as e:
        return (False, -1, str(e))


def fetch_opsgenie_users():
    """Fetch all users from Opsgenie"""
    iprint("\n🔍 Fetching users from Opsgenie...")
    
    headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    
    try:
        response = requests.get(
            f"{OPSGENIE_BASE_URL}/users",
            headers=headers
        )
        response.raise_for_status()
        
        users = response.json().get('data', [])
        iprint(f"✅ Found {len(users)} users in Opsgenie")
        return users
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching Opsgenie users: {e}")
        return []


def fetch_firehydrant_users():
    """Fetch all users from FireHydrant"""
    iprint("\n🔍 Fetching users from FireHydrant...")
    
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}"
    }
    
    try:
        response = requests.get(
            f"{FIREHYDRANT_BASE_URL}/users",
            headers=headers
        )
        response.raise_for_status()
        
        users = response.json().get('data', [])
        iprint(f"✅ Found {len(users)} users in FireHydrant")
        return users
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching FireHydrant users: {e}")
        return []


def get_team_members(team_id):
    """Get members of a specific Opsgenie team"""
    headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    
    try:
        # Try the correct Opsgenie API endpoint for team members
        response = requests.get(
            f"{OPSGENIE_BASE_URL}/teams/{team_id}",
            headers=headers
        )
        response.raise_for_status()
        
        team_data = response.json().get('data', {})
        members = team_data.get('members', [])
        return members
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching team members for team {team_id}: {e}")
        return []


def get_all_schedules():
    """Get all on-call schedules from Opsgenie"""
    headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    
    try:
        response = requests.get(
            f"{OPSGENIE_BASE_URL}/schedules",
            headers=headers
        )
        vprint(f"    📡 Schedules API Response: {response.status_code}")
        if response.status_code != 200:
            vprint(f"    📡 Schedules API Response Text: {response.text}")
        
        response.raise_for_status()
        schedules_data = response.json()
        schedules = schedules_data.get('data', [])
        vprint(f"    📋 Raw schedules data: {len(schedules)} schedules found")
        return schedules
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching all schedules: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return []


def get_team_schedules(team_id):
    """Get on-call schedules from Opsgenie for a team"""
    # First get all schedules, then filter by team
    all_schedules = get_all_schedules()
    team_schedules = []
    
    vprint(f"    🔍 Filtering schedules for team {team_id}...")
    vprint(f"    📋 Total schedules available: {len(all_schedules)}")
    
    if all_schedules:
        vprint(f"    📋 Available schedule names:")
        for schedule in all_schedules:
            vprint(f"      - {schedule.get('name', 'Unknown')} (ID: {schedule.get('id', 'Unknown')})")
    
    for schedule in all_schedules:
        schedule_id = schedule.get('id', 'Unknown')
        schedule_name = schedule.get('name', 'Unknown')
        
        # Check if this schedule is owned by the team
        owner_team = schedule.get('ownerTeam', {})
        owner_team_id = owner_team.get('id', 'None')
        owner_team_name = owner_team.get('name', 'Unknown')
        
        vprint(f"    🔍 Checking schedule '{schedule_name}' - Owner: {owner_team_name} (ID: {owner_team_id})")
        
        if owner_team_id == team_id:
            vprint(f"      ✅ Found schedule '{schedule_name}' owned by team")
            team_schedules.append(schedule)
            continue
        
        # Also check if any participants belong to this team
        schedule_details = get_schedule_details(schedule_id)
        if schedule_details:
            rotations = schedule_details.get('rotations', [])
            for rotation in rotations:
                participants = rotation.get('participants', [])
                for participant in participants:
                    if participant.get('type') == 'team':
                        participant_team = participant.get('team', {})
                        if participant_team.get('id') == team_id:
                            vprint(f"      ✅ Found schedule '{schedule_name}' with team participant")
                            team_schedules.append(schedule)
                            break
                if schedule in team_schedules:
                    break
        
        # Note: We intentionally avoid heuristic name-based matching to prevent cross-team leakage
    
    return team_schedules


@lru_cache(maxsize=256)
def get_schedule_details(schedule_id):
    """Get detailed information about an Opsgenie schedule"""
    headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    
    try:
        response = requests.get(
            f"{OPSGENIE_BASE_URL}/schedules/{schedule_id}",
            headers=headers
        )
        vprint(f"      📡 Schedule details API Response: {response.status_code}")
        if response.status_code != 200:
            vprint(f"      📡 Schedule details API Response Text: {response.text}")
        
        response.raise_for_status()
        schedule_data = response.json().get('data', {})
        schedule_name = schedule_data.get('name', 'Unknown')
        rotations = schedule_data.get('rotations', [])
        
        vprint(f"      📋 Schedule details: {schedule_name} - {len(rotations)} rotations")
        
        # Debug: Show rotation details for "Custom Interval" schedules and time restrictions
        if "custom" in schedule_name.lower() or "interval" in schedule_name.lower() or "time-restrictions" in schedule_name.lower():
            vprint(f"      🔍 Special schedule detected: {schedule_name}")
            for i, rotation in enumerate(rotations):
                rotation_type = rotation.get('type', 'unknown')
                length = rotation.get('length', 'unknown')
                participants = rotation.get('participants', [])
                time_restrictions = rotation.get('timeRestriction', {})
                
                vprint(f"        Rotation {i + 1}: type={rotation_type}, length={length}, participants={len(participants)}")
                
                # Show time restrictions if they exist
                if time_restrictions:
                    restriction_type = time_restrictions.get('type', 'unknown')
                    restriction = time_restrictions.get('restriction', {}) if time_restrictions else {}
                    start_hour = restriction.get('startHour', 'unknown')
                    end_hour = restriction.get('endHour', 'unknown')
                    vprint(f"          Time restrictions: type={restriction_type}, start={start_hour}, end={end_hour}")
                
                # Show participant details
                for j, participant in enumerate(participants):
                    participant_type = participant.get('type', 'unknown')
                    if participant_type == 'user':
                        username = participant.get('username', 'unknown')
                        vprint(f"          Participant {j + 1}: user={username}")
                    elif participant_type == 'team':
                        team_info = participant.get('team', {})
                        team_name = team_info.get('name', 'unknown')
                        vprint(f"          Participant {j + 1}: team={team_name}")
                
                # Show overrides if they exist (log clearly)
                overrides = rotation.get('overrides', [])
                if overrides:
                    vprint(f"          🔁 Overrides: {len(overrides)} found in Opsgenie rotation {i+1}")
                    for k, override in enumerate(overrides):
                        override_name = override.get('name', 'unknown')
                        override_user = override.get('user', {}).get('username', 'unknown')
                        start_time = override.get('startDate', 'unknown')
                        end_time = override.get('endDate', 'unknown')
                        vprint(f"            ▶ Override {k + 1}: {override_name} -> {override_user}")
                        vprint(f"              🕒 {start_time} to {end_time}")
                else:
                    vprint(f"          🔁 Overrides: None found in Opsgenie rotation {i+1}")
        
        return schedule_data
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching schedule details for {schedule_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return {}


def list_opsgenie_overrides(schedule_id):
    """List overrides for an Opsgenie schedule using the official Overrides API"""
    headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    try:
        url = f"{OPSGENIE_BASE_URL}/schedules/{schedule_id}/overrides"
        response = requests.get(url, headers=headers)
        vprint(f"      📡 Overrides API Response: {response.status_code} for schedule {schedule_id}")
        if response.status_code != 200:
            print(f"      📡 Overrides API Response Text: {response.text}")
        response.raise_for_status()
        data = response.json().get('data', [])
        # Record raw GET payload for final summary
        try:
            OVERRIDE_GET_PAYLOADS.append({
                "opsgenie_schedule_id": schedule_id,
                "payload": data
            })
        except Exception:
            pass
        vprint(f"      🔁 Found {len(data)} schedule-level overrides in Opsgenie")
        return data
    except requests.exceptions.RequestException as e:
        print(f"      ❌ Error listing overrides for schedule {schedule_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"        📡 Error Response: {e.response.text}")
        return []


def get_opsgenie_schedule_oncalls(schedule_id: str):
    """Return list of usernames currently on-call for a given Opsgenie schedule."""
    headers = {"Authorization": f"GenieKey {OPSGENIE_API_KEY}"}
    try:
        url = f"{OPSGENIE_BASE_URL}/schedules/{schedule_id}/on-calls"
        r = requests.get(url, headers=headers)
        vprint(f"      📡 OG on-calls {schedule_id} → {r.status_code}")
        r.raise_for_status()
        data = r.json().get('data') or {}
        recips = []
        # API shape variants: onCallRecipients or onCallParticipants
        if isinstance(data.get('onCallRecipients'), list):
            recips = data.get('onCallRecipients')
        elif isinstance(data.get('onCallParticipants'), list):
            recips = [p.get('name') for p in data.get('onCallParticipants') if p.get('name')]
        return [str(x).strip() for x in recips if x]
    except Exception as e:
        vprint(f"      ⚠️  OG on-calls lookup failed for {schedule_id}: {e}")
        return []

# ========================= Escalation Policies ========================= #
EP_ERRORS = []
DELETE_TEAM_IF_NO_EPS = (os.getenv('DELETE_TEAM_IF_NO_EPS', 'false').lower() in ('1', 'true', 'yes'))
EP_MIN_TIMEOUT_SECONDS = int(os.getenv('EP_MIN_TIMEOUT_SECONDS', '300'))  # default 5 minutes
def fetch_opsgenie_escalation_policies(team_id: str):
    headers = {"Authorization": f"GenieKey {OPSGENIE_API_KEY}"}
    try:
        # Opsgenie: GET /v2/escalations?teamId={team_id}
        url = f"{OPSGENIE_BASE_URL}/escalations?teamId={team_id}"
        resp = requests.get(url, headers=headers)
        vprint(f"  📡 OG EPs GET {url} → {resp.status_code}")
        if resp.status_code != 200:
            print(f"  📡 OG EPs Body: {resp.text}")
            resp.raise_for_status()
        data = resp.json().get('data', [])
        vprint(f"  ✅ Found {len(data)} Opsgenie escalation policy(ies)")
        return data
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error fetching Opsgenie escalation policies: {e}")
        return []


def filter_escalation_policies_for_team(og_team: dict, og_eps: list) -> list:
    """Return only the Opsgenie EPs that belong to the given team by
    strict ownership and target matching.
    - Keep EPs whose name equals "<team>_escalation" (exact, case-insensitive)
    - Or any rule targets a schedule that belongs to this team (by schedule id)
    - Or any rule targets this team (by team id)
    """
    team_id = og_team.get('id')
    team_name = (og_team.get('name') or '').strip()
    team_name_l = team_name.lower()
    # Build set of this team's schedule ids for precise matching
    try:
        scheds = get_team_schedules(team_id) or []
    except Exception:
        scheds = []
    team_sched_ids = set(s.get('id') for s in scheds if s.get('id'))

    filtered = []
    for ep in og_eps or []:
        ep_name = (ep.get('name') or '').strip()
        if ep_name.lower() == f"{team_name_l}_escalation":
            filtered.append(ep)
            continue
        rules = ep.get('rules') or []
        keep = False
        for r in rules:
            for tgt in r.get('targets') or []:
                ttype = (tgt.get('type') or '').lower()
                if ttype == 'schedule' and (tgt.get('id') in team_sched_ids):
                    keep = True; break
                if ttype == 'team' and (tgt.get('id') == team_id):
                    keep = True; break
            if keep:
                break
        if keep:
            filtered.append(ep)
    return filtered


def align_fh_schedule_to_og_oncall(fh_team_id: str, fh_schedule_id: str, og_schedule_id: str, firehydrant_users: list):
    """Ensure the active FH shift's user matches Opsgenie's current on-call for the given schedule.
    No-op if on-call cannot be determined or already matches.
    """
    try:
        og_on = get_opsgenie_schedule_oncalls(og_schedule_id)
        desired_email = og_on[0] if og_on else None
        if not desired_email:
            return False
        desired_user = find_user_by_email(desired_email, firehydrant_users)
        if not desired_user or not desired_user.get('id'):
            return False
        headers = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}", "Content-Type": "application/json"}
        # Fetch FH schedule and locate active shift
        ver = requests.get(
            f"{FIREHYDRANT_BASE_URL}/teams/{fh_team_id}/on_call_schedules/{fh_schedule_id}",
            headers=headers
        )
        if ver.status_code != 200:
            return False
        sj = ver.json()
        from datetime import datetime, timezone
        now_dt = datetime.now(timezone.utc)
        def parse_iso(z):
            return datetime.fromisoformat(z.replace('Z','+00:00'))
        for rot in sj.get('rotations', []) or []:
            for sh in rot.get('shifts', []) or []:
                st = sh.get('start_time'); et = sh.get('end_time')
                if not st or not et:
                    continue
                try:
                    st_dt, et_dt = parse_iso(st), parse_iso(et)
                except Exception:
                    continue
                if st_dt <= now_dt < et_dt:
                    cur = (sh.get('user') or {}).get('id')
                    if cur != desired_user['id']:
                        patch_url = f"{FIREHYDRANT_BASE_URL}/teams/{fh_team_id}/on_call_schedules/{fh_schedule_id}/shifts/{sh.get('id')}"
                        pr = requests.patch(patch_url, headers=headers, json={"user_id": desired_user['id']})
                        vprint(f"    🔁 Align FH active shift to OG on-call {desired_email} → {pr.status_code}")
                        return pr.status_code in (200, 204)
                    return False
        return False
    except Exception as _e:
        vprint(f"    ⚠️ Alignment check failed: {_e}")
        return False

def map_opsgenie_ep_to_fh(og_ep: dict, fh_users: list):
    """Map a single Opsgenie EP to a FireHydrant payload based on OG 'notifyType' and 'recipient'.
    This returns a payload with unresolved schedule targets (by name or id),
    which will be normalized in create_firehydrant_escalation_policy.
    """
    name = og_ep.get('name') or 'Escalation Policy'
    description = og_ep.get('description') or ''
    rules = og_ep.get('rules') or []

    def to_minutes(delay_obj):
        try:
            if isinstance(delay_obj, dict):
                amt = delay_obj.get('timeAmount', 0) or 0
                unit = (delay_obj.get('timeUnit') or 'minutes').lower()
                if unit.startswith('min'):
                    return int(amt)
                if unit.startswith('hour'):
                    return int(amt) * 60
                if unit.startswith('day'):
                    return int(amt) * 60 * 24
                return int(amt)
            return int(delay_obj or 0)
        except Exception:
            return 0

    fh_steps = []
    for idx, rule in enumerate(rules, 1):
        delay_minutes = to_minutes(rule.get('delay'))

        # Normalize recipients: Opsgenie may use 'recipient' (single) or 'recipients' (array)
        raw_recipients = []
        if rule.get('recipient'):
            raw_recipients = [rule['recipient']]
        elif isinstance(rule.get('recipients'), list):
            raw_recipients = rule['recipients']

        step_targets = []
        for r in raw_recipients:
            r_type = (r.get('type') or '').lower()
            if r_type == 'user' and r.get('username'):
                u = find_user_by_email(r['username'], fh_users)
                if u:
                    step_targets.append({"type": "user", "id": u.get('id')})
                else:
                    print(f"    ⚠️  EP step {idx}: user {r['username']} not found in FH; skipping target")
            elif r_type == 'schedule':
                # Preserve schedule target for resolution stage (by id or name)
                tgt = {"type": "schedule"}
                if r.get('id'):
                    tgt['id'] = r['id']
                if r.get('name'):
                    tgt['name'] = r['name']
                step_targets.append(tgt)
            elif r_type == 'team':
                # Try pass-through; FH may not support team targets in EPs directly
                if r.get('id'):
                    step_targets.append({"type": "team", "id": r['id']})
                else:
                    print(f"    ⚠️  EP step {idx}: team recipient missing id; skipped")
            else:
                print(f"    ⚠️  EP step {idx}: unsupported recipient type {r_type}")

        # If we have at least one valid target, add a step
        if step_targets:
            step_type = 'notify'  # Default mapping for Opsgenie notify rules
            fh_steps.append({
                "type": step_type,
                "timeout": delay_minutes,
                "targets": step_targets
            })

    payload = {
        "name": name,
        "description": description,
        "steps": fh_steps,
        # carry through Opsgenie repeat block for later normalization
        "repeat": og_ep.get('repeat') or {}
    }
    try:
        EP_MAPPING_SUMMARY.append({
            "og_name": name,
            "og_rules": og_ep.get('rules', []),
            "fh_payload": payload
        })
    except Exception:
        pass
    return payload


def _to_iso_duration_minutes(minutes: int) -> str:
    """Convert minutes to ISO-8601 duration, clamped to EP_MIN_TIMEOUT_SECONDS.
    If minutes is 0, we still return at least PT{EP_MIN_TIMEOUT_SECONDS}S.
    """
    try:
        m = int(minutes or 0)
        seconds = max(EP_MIN_TIMEOUT_SECONDS, m * 60)
        if seconds % 60 == 0:
            return f"PT{seconds // 60}M"
        return f"PT{seconds}S"
    except Exception:
        return f"PT{max(EP_MIN_TIMEOUT_SECONDS, 0)}S"


def create_firehydrant_escalation_policy(team_id: str, payload: dict, set_default: bool = False):
    headers = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}", "Content-Type": "application/json"}
    # Resolve schedule names in targets to FH schedule IDs for this team
    try:
        def resolve_schedule_id(schedule_name: str) -> str:
            try:
                r = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=headers)
                if r.status_code != 200:
                    return None
                data = r.json()
                items = data.get('data') if isinstance(data, dict) else data
                for sc in items or []:
                    if sc.get('name') == schedule_name:
                        return sc.get('id')
                return None
            except Exception:
                return None

        steps = payload.get('steps') or []
        normalized_steps = []
        for i, st in enumerate(steps, 0):
            st_timeout = st.get('timeout', 0)
            targets = st.get('targets') or []
            resolved_targets = []
            for t in targets:
                t_type = (t.get('type') or '').lower()
                if t_type == 'schedule':
                    sched_id = resolve_schedule_id(t.get('name', ''))
                    if sched_id:
                        resolved_targets.append({"type": "OnCallSchedule", "id": sched_id})
                    else:
                        msg = f"EP step {i+1}: schedule target not found in FH: {t}"
                        print(f"  ⚠️  {msg}")
                        EP_ERRORS.append(msg)
                elif t_type == 'user' and t.get('id'):
                    resolved_targets.append({"type": "User", "id": t['id']})
                elif t_type == 'team' and t.get('id'):
                    # Unsupported in Signals EP; skip but log
                    msg = f"EP step {i+1}: team target unsupported; skipped: {t}"
                    print(f"  ⚠️  {msg}")
                    EP_ERRORS.append(msg)
                else:
                    msg = f"EP step {i+1}: unsupported target format: {t}"
                    print(f"  ⚠️  {msg}")
                    EP_ERRORS.append(msg)
            if not resolved_targets:
                msg = f"EP step {i+1}: no valid targets; step skipped"
                print(f"  ⚠️  {msg}")
                EP_ERRORS.append(msg)
                continue
            normalized_steps.append({
                "parent_position": i,
                "targets": resolved_targets,
                "timeout": _to_iso_duration_minutes(st_timeout),
                "distribution_type": "unspecified",
                "priorities": ["HIGH", "MEDIUM", "LOW"]
            })

        # Compute repeats/handoff from Opsgenie repeat data (if any)
        og_repeat = payload.get('repeat') or {}
        og_repeat_count = og_repeat.get('count') or 0
        og_wait = og_repeat.get('waitInterval') or 0  # minutes
        # Clamp repeat wait interval to FH minimum timeout if needed
        repeat_timeout_iso = _to_iso_duration_minutes(og_wait)

        # Determine default flag: only set True when caller indicates this is the single EP
        # AND there are no existing EPs for the team already
        is_default = False
        try:
            vr = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/escalation_policies", headers=headers)
            if vr.status_code == 200:
                items = (vr.json().get('data') if isinstance(vr.json(), dict) else vr.json()) or []
                if set_default and len(items) == 0:
                    is_default = True
        except Exception:
            # On any error determining existing EPs, fall back to caller intent
            is_default = set_default

        # Build FH EP payload per network trace
        fh_payload = {
            "name": payload.get('name') or 'Escalation Policy',
            "default": bool(is_default),
            "step_strategy": "static",
            "steps": normalized_steps,
            "handoff_step": None,
            "repetitions": int(og_repeat_count or 0),
            "prioritized_settings": {
                "high": {"repetitions": None, "handoff_step": None},
                "medium": {"repetitions": None, "handoff_step": None},
                "low": {"repetitions": None, "handoff_step": None}
            },
            "teamId": team_id
        }
        # If repeats exist and there is at least one step, use the last step timeout as repeat cadence if OG provided none
        if og_repeat_count and normalized_steps:
            if not og_wait:
                # ensure last step timeout is at least the minimum
                normalized_steps[-1]['timeout'] = _to_iso_duration_minutes(EP_MIN_TIMEOUT_SECONDS // 60)
            else:
                normalized_steps[-1]['timeout'] = repeat_timeout_iso
        vprint("  🔧 Final FH EP payload (normalized)")
        vprint("  " + json.dumps(fh_payload, indent=2).replace("\n", "\n  "))
    except Exception as ex:
        print(f"  ⚠️  Failed to resolve EP schedule targets: {ex}")
    try:
        url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/escalation_policies"
        vprint(f"  ➤ POST {url} (normalized)")
        vprint(f"    Payload: {json.dumps(fh_payload, indent=2)}")
        if DRY_RUN or VERIFY_ONLY:
            vprint("  🧪 DRY-RUN: would POST escalation policy")
            return None
        resp = requests.post(url, headers=headers, json=fh_payload)
        vprint(f"    ⇦ Status: {resp.status_code}")
        if resp.status_code not in [200, 201]:
            vprint(f"    Body: {resp.text}")
            EP_ERRORS.append(f"Create EP failed ({payload.get('name')}): {resp.status_code} {resp.text}")
        else:
            vprint(f"  ✅ Created FH EP: {resp.json().get('name')}")
        return resp
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error creating FH escalation policy: {e}")
        return None


def migrate_team_escalation_policies(og_team: dict, fh_team: dict, fh_users: list):
    vprint(f"📈 Migrating escalation policies for {fh_team['name']}…")
    og_eps = fetch_opsgenie_escalation_policies(og_team['id'])
    if not og_eps:
        vprint("  ℹ️  No escalation policies found in Opsgenie")
        return
    # Strictly filter EPs that belong to this team
    filtered_eps = filter_escalation_policies_for_team(og_team, og_eps)

    if not filtered_eps:
        vprint("  ℹ️  No matching escalation policies for this team; skipping")
        return

    # If there will be exactly one EP created, mark it default
    set_default_flag = (len(filtered_eps) == 1)
    # Avoid duplicate names that already exist in FH
    existing_names = set()
    try:
        ver = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/escalation_policies", headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"})
        if ver.status_code == 200:
            items = (ver.json().get('data') if isinstance(ver.json(), dict) else ver.json()) or []
            existing_names = { (it.get('name') or '').strip().lower() for it in items }
    except Exception:
        pass
    created_ct = 0
    skipped_ct = 0
    for ep in filtered_eps:
        payload = map_opsgenie_ep_to_fh(ep, fh_users)
        ep_name = (payload.get('name') or '').strip().lower()
        if ep_name in existing_names:
            vprint(f"  ⏭️  Skipping EP '{payload.get('name')}' (already exists)")
            skipped_ct += 1
            continue
        resp = create_firehydrant_escalation_policy(fh_team['id'], payload, set_default=set_default_flag)
        created_ct += 1

    # Verify EPs exist; optionally delete team if none found for fast reruns
    try:
        verify_url = f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/escalation_policies"
        vr = requests.get(verify_url, headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"})
        count = 0
        if vr.status_code == 200:
            data = vr.json()
            items = data.get('data') if isinstance(data, dict) else data
            count = len(items or [])
        vprint(f"  🔎 Verify EPs GET → {vr.status_code}; count={count}")
        if count == 0:
            msg = f"No EPs present for team {fh_team['name']} ({fh_team['id']})."
            print(f"  ⚠️  {msg}")
            EP_ERRORS.append(msg)
            if DELETE_TEAM_IF_NO_EPS:
                try:
                    del_url = f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}"
                    dr = requests.delete(del_url, headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"})
                    print(f"  🗑️  Deleted team due to empty EPs → {dr.status_code}")
                except Exception as dx:
                    print(f"  ⚠️  Failed to delete team: {dx}")
    except Exception as vx:
        print(f"  ⚠️  EP verification failed: {vx}")
    return {"created": created_ct, "skipped": skipped_ct}


def create_firehydrant_schedule(team_id, opsgenie_schedule, firehydrant_users, rotation_index=0):
    """Create an on-call schedule in FireHydrant based on Opsgenie schedule"""
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Get detailed schedule information from Opsgenie
    schedule_details = get_schedule_details(opsgenie_schedule['id'])
    if not schedule_details:
        print(f"    ⚠️  Could not get details for schedule {opsgenie_schedule['name']}")
        return None
    
    # Map Opsgenie schedule to FireHydrant format
    schedule_name = opsgenie_schedule['name']
    if rotation_index > 0:
        schedule_name = f"{schedule_name} - Rotation {rotation_index + 1}"
    
    base_tz = schedule_details.get('timezone', 'UTC')
    payload = {
        "name": schedule_name,
        "team_id": team_id,
        "time_zone": TIMEZONE_OVERRIDE or base_tz
    }
    
    # Add description if available
    if opsgenie_schedule.get('description'):
        payload['description'] = opsgenie_schedule['description']
    
    # Map schedule strategy based on Opsgenie rotation type
    rotations = schedule_details.get('rotations', [])
    if rotations and rotation_index < len(rotations):
        rotation = rotations[rotation_index]
        rotation_type = rotation.get('type', 'custom')
        
        print(f"      🔄 Mapping rotation type: {rotation_type} (rotation {rotation_index + 1})")
        
        # Get rotation start time and length for accurate mapping (clamp to now if >30d old)
        start_date = rotation.get('startDate', '')
        length = rotation.get('length', 1)
        
        # Check for time restrictions in the rotation
        time_restrictions = rotation.get('timeRestriction', {})
        has_time_restrictions = time_restrictions.get('restriction', {}) if time_restrictions else {}
        rotation_restrictions = None  # Will hold FH rotation restrictions if present
        
        # Store strategy for use in rotation (not in main payload)
        rotation_strategy = None
        
        if rotation_type == 'weekly':
            # Extract handoff day and time from start_date if available
            handoff_day = "monday"  # Default
            handoff_time = "09:00:00"  # Default
            
            if start_date:
                try:
                    from datetime import datetime, timezone, timedelta
                    start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                    if (datetime.now(timezone.utc) - start_dt) <= timedelta(days=30):
                        handoff_day = start_dt.strftime('%A').lower()
                        handoff_time = start_dt.strftime('%H:%M:%S')
                    else:
                        start_date = ''
                except Exception:
                    start_date = ''
            
            rotation_strategy = {
                "type": "weekly",
                "handoff_day": handoff_day,
                "handoff_time": handoff_time
            }
            
        elif rotation_type == 'daily':
            # Extract handoff time from start_date if available
            handoff_time = "09:00:00"  # Default
            
            if start_date:
                try:
                    from datetime import datetime, timezone, timedelta
                    start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                    if (datetime.now(timezone.utc) - start_dt) <= timedelta(days=30):
                        handoff_time = start_dt.strftime('%H:%M:%S')
                    else:
                        start_date = ''
                except Exception:
                    start_date = ''
            
            rotation_strategy = {
                "type": "daily",
                "handoff_time": handoff_time
            }
            
        else:
            # Custom interval - derive ISO-8601 duration precisely using Opsgenie length + unit
            unit = (rotation.get('timeUnit') or rotation.get('intervalUnit') or '').strip().lower()
            shift_duration_iso = None
            try:
                if unit in ('minute','minutes'):
                    mins = int(length)
                    shift_duration_iso = f"PT{mins}M"
                elif unit in ('hour','hours',''):  # default to hours when unit is missing
                    hrs = int(length)
                    shift_duration_iso = f"PT{hrs}H"
                elif unit in ('day','days'):
                    hrs = int(length) * 24
                    shift_duration_iso = f"PT{hrs}H"
                else:
                    # Fallback: assume hours
                    hrs = int(length)
                    shift_duration_iso = f"PT{hrs}H"
            except Exception:
                shift_duration_iso = "PT24H"

            rotation_strategy = {
                "type": "custom",
                "shift_duration": shift_duration_iso
            }
        
        # Add time restrictions if they exist: map to rotation-level restrictions
        schedule_restrictions = None
        if time_restrictions:
            restriction_type = time_restrictions.get('type', '')
            # Opsgenie may return a single 'restriction' object OR an array 'restrictions'
            restriction = time_restrictions.get('restriction', {})
            restrictions_array = time_restrictions.get('restrictions', [])
            start_hour = restriction.get('startHour')
            end_hour = restriction.get('endHour')
            # Day bounds may not exist in Opsgenie restriction; default Monday->Friday when missing
            # Default to full week when day bounds are not provided by Opsgenie
            start_day_raw = (time_restrictions.get('startDay') or restriction.get('startDay') or 'monday')
            end_day_raw = (time_restrictions.get('endDay') or restriction.get('endDay') or 'sunday')
            provided_start_day = (time_restrictions.get('startDay') is not None) or (restriction.get('startDay') is not None)
            provided_end_day = (time_restrictions.get('endDay') is not None) or (restriction.get('endDay') is not None)
            _DAY_MAP = {1:'monday',2:'tuesday',3:'wednesday',4:'thursday',5:'friday',6:'saturday',7:'sunday',0:'sunday'}
            def _normalize_day(val):
                if isinstance(val, int):
                    return _DAY_MAP.get(val, 'monday')
                return str(val).strip().lower()
            start_day = _normalize_day(start_day_raw)
            end_day = _normalize_day(end_day_raw)
            # Helper to expand a day range into per-day windows (FH often expects per-day windows)
            def _expand_day_range(sd: str, ed: str, st: str, et: str):
                ordered_days = [
                    'monday','tuesday','wednesday','thursday','friday','saturday','sunday'
                ]
                try:
                    sidx = ordered_days.index(sd)
                    eidx = ordered_days.index(ed)
                except ValueError:
                    sidx = 0
                    eidx = 6
                days = []
                idx = sidx
                # Walk inclusive from start to end; wrap if needed
                while True:
                    days.append(ordered_days[idx])
                    if idx == eidx:
                        break
                    idx = (idx + 1) % 7
                return [
                    {"start_day": d, "start_time": st, "end_day": d, "end_time": et}
                    for d in days
                ]

            # If Opsgenie returned multiple restrictions, emit per configured strategy
            if isinstance(restrictions_array, list) and len(restrictions_array) > 0:
                weekly_windows = []
                per_day_windows = []
                last_end_time = None
                def _next_day(d: str) -> str:
                    ordered_days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
                    try:
                        idx = ordered_days.index(d)
                        return ordered_days[(idx + 1) % 7]
                    except ValueError:
                        return 'tuesday' if d == 'monday' else 'monday'
                def _prev_day(d: str) -> str:
                    ordered_days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
                    try:
                        idx = ordered_days.index(d)
                        return ordered_days[(idx - 1) % 7]
                    except ValueError:
                        return 'sunday'
                for r in restrictions_array:
                    sd = _normalize_day(r.get('startDay', start_day))
                    ed = _normalize_day(r.get('endDay', end_day))
                    sh = r.get('startHour'); sm = r.get('startMin', 0)
                    eh = r.get('endHour'); em = r.get('endMin', 0)
                    if sh is None or eh is None:
                        continue
                    st_str = f"{int(sh):02d}:{int(sm):02d}:00"
                    et_str = f"{int(eh):02d}:{int(em):02d}:00"
                    if (int(eh), int(em)) <= (int(sh), int(sm)):
                        # Overnight
                        end_day_week = sd if (r.get('startDay') is not None or r.get('endDay') is not None) else _prev_day(sd)
                        weekly_windows.append({"start_day": sd, "start_time": st_str, "end_day": end_day_week, "end_time": et_str})
                        # Per-day split across day boundary
                        per_day_windows.extend(_expand_day_range(sd, sd, st_str, "23:59:59"))
                        per_day_windows.extend(_expand_day_range(_next_day(sd), _next_day(sd), "00:00:00", et_str))
                        last_end_time = et_str
                    else:
                        weekly_windows.append({"start_day": sd, "start_time": st_str, "end_day": ed, "end_time": et_str})
                        per_day_windows.extend(_expand_day_range(sd, ed, st_str, et_str))
                        last_end_time = et_str
                chosen = per_day_windows if (RESTRICTION_STRATEGY == 'per-day') else weekly_windows
                if chosen:
                    rotation_restrictions = chosen
                    schedule_restrictions = chosen
                    print(f"      ⏰ Added array-based restrictions ({RESTRICTION_STRATEGY}): {json.dumps(rotation_restrictions, indent=2)}")
                    if rotation_strategy and rotation_strategy.get('type') == 'daily' and last_end_time:
                        rotation_strategy['handoff_time'] = last_end_time
                        print(f"      🔁 Aligned daily handoff_time to restriction end_time: {last_end_time}")
            # Otherwise fall back to legacy single 'restriction' object handling
            if restriction_type in ('time-of-day', 'weekday-and-time-of-day') and start_hour is not None and end_hour is not None:
                # Include minute precision when available
                sm = int(restriction.get('startMin', 0) or 0)
                em = int(restriction.get('endMin', 0) or 0)
                sh = int(start_hour)
                eh = int(end_hour)
                start_time_str = f"{sh:02d}:{sm:02d}:00"
                end_time_str = f"{eh:02d}:{em:02d}:00"

                # Detect overnight windows (end <= start) and split into two segments per day
                def _next_day(d: str) -> str:
                    ordered_days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
                    try:
                        idx = ordered_days.index(d)
                        return ordered_days[(idx + 1) % 7]
                    except ValueError:
                        return 'tuesday' if d == 'monday' else 'monday'

                if (eh, em) <= (sh, sm):
                    if RESTRICTION_STRATEGY == 'per-day':
                        windows = _expand_day_range(start_day, start_day, start_time_str, "23:59:59") + \
                                  _expand_day_range(_next_day(start_day), _next_day(start_day), "00:00:00", end_time_str)
                        rotation_restrictions = windows
                        schedule_restrictions = windows
                        print(f"      ⏰ Added overnight per-day restrictions: {json.dumps(rotation_restrictions, indent=2)}")
                    else:
                        def _prev_day(d: str) -> str:
                            ordered_days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
                            try:
                                idx = ordered_days.index(d)
                                return ordered_days[(idx - 1) % 7]
                            except ValueError:
                                return 'sunday'
                        end_day_week = start_day if (provided_start_day or provided_end_day) else _prev_day(start_day)
                        windows = [{"start_day": start_day, "start_time": start_time_str, "end_day": end_day_week, "end_time": end_time_str}]
                        rotation_restrictions = windows
                        schedule_restrictions = windows
                        vprint(f"      ⏰ Added overnight weekly restriction (single window): {json.dumps(rotation_restrictions, indent=2)}")
                else:
                    if RESTRICTION_STRATEGY == 'per-day':
                        rotation_restrictions = _expand_day_range(start_day, end_day, start_time_str, end_time_str)
                        schedule_restrictions = rotation_restrictions
                        print(f"      ⏰ Added per-day restriction (expanded): {json.dumps(rotation_restrictions, indent=2)}")
                    else:
                        rotation_restrictions = [{"start_day": start_day, "start_time": start_time_str, "end_day": end_day, "end_time": end_time_str}]
                        schedule_restrictions = rotation_restrictions
                        print(f"      ⏰ Added weekly restriction (compact): {json.dumps(rotation_restrictions, indent=2)}")

                # If handoff is daily, align the daily handoff_time to the restriction end_time
                if rotation_strategy and rotation_strategy.get('type') == 'daily':
                    rotation_strategy['handoff_time'] = end_time_str
                    print(f"      🔁 Aligned daily handoff_time to restriction end_time: {end_time_str}")
    
    # Map participants to FireHydrant user IDs for this specific rotation
    member_ids = []
    unknown_user_emails = []
    
    if rotations and rotation_index < len(rotations):
        rotation = rotations[rotation_index]
        participants = rotation.get('participants', [])
        
        print(f"      🔍 Found {len(participants)} participants in rotation")
        
        for participant in participants:
            participant_type = participant.get('type', '')
            
            if participant_type == 'user':
                # Handle user participants
                user_email = participant.get('username', '')
                if user_email:
                    firehydrant_user = find_user_by_email(user_email, firehydrant_users)
                    if firehydrant_user:
                        member_ids.append(firehydrant_user['id'])
                        vprint(f"      👤 Mapped user: {user_email} -> ID: {firehydrant_user['id']}")
                    else:
                        print(f"      ⚠️  Could not find FireHydrant user for: {user_email}")
                        unknown_user_emails.append(user_email)
            
            elif participant_type == 'team':
                # Handle team participants by expanding to team members (map by email)
                team_id_opsgenie = participant.get('team', {}).get('id', '')
                if team_id_opsgenie:
                    print(f"      👥 Found team participant: {team_id_opsgenie}")
                    try:
                        og_team_members = get_team_members(team_id_opsgenie) or []
                        print(f"        🔎 Team participant has {len(og_team_members)} member(s)")
                        for tm in og_team_members:
                            og_user = tm.get('user', {})
                            email = og_user.get('username')
                            if not email:
                                continue
                            fh_user = find_user_by_email(email, firehydrant_users)
                            if not fh_user:
                                # Do NOT auto-create for schedules; FH requires real user IDs
                                print(f"        ⚠️  FH user missing for team participant email {email}; skipping in rotation member assignment")
                                unknown_user_emails.append(email)
                                continue
                            member_ids.append(fh_user['id'])
                            print(f"        👤 Added team member from participant: {email} -> {fh_user['id']}")
                    except Exception as e:
                        print(f"        ⚠️  Could not expand team participant members: {e}")
    
    # De-duplicate while preserving order
    if member_ids:
        seen_ids = set()
        deduped = []
        for uid in member_ids:
            if uid not in seen_ids:
                seen_ids.add(uid)
                deduped.append(uid)
        if len(deduped) != len(member_ids):
            print(f"      ⚠️  Duplicate user IDs found in rotation mapping; deduplicating")
        member_ids = deduped
        print(f"      📋 Total mapped user IDs: {member_ids}")
        if unknown_user_emails:
            print(f"      ⚠️  The following Opsgenie users do not exist in FireHydrant and were skipped: {', '.join(sorted(set(unknown_user_emails)))}")
            print(f"      ℹ️  Action taken: Only existing FH users were assigned to the rotation. No unclaimed shifts were created automatically.")
            print(f"      ℹ️  To include these users, create them in FH, then add them to the rotation or claim shifts manually.")
    else:
        print(f"      ⚠️  No users mapped to rotation!")
    
    # Add rotations with proper structure
    if rotations and rotation_index < len(rotations):
        rotation = rotations[rotation_index]
        start_date = rotation.get('startDate', '')
        
        # Create a proper ISO8601 timestamp for the rotation start time
        if start_date:
            try:
                from datetime import datetime
                # Parse the start date and ensure it's in ISO8601 format
                if start_date.endswith('Z'):
                    # Already in ISO format
                    rotation_start_time = start_date
                else:
                    # Convert to ISO format
                    start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                    rotation_start_time = start_dt.isoformat() + 'Z'
            except:
                # Fallback to current time if parsing fails
                from datetime import datetime, timezone
                rotation_start_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        else:
            # Use current time as fallback
            from datetime import datetime, timezone
            rotation_start_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        
        # Create rotation with all required fields
        rotation_data = {
            "name": f"Rotation {rotation_index + 1}",
            "start_time": rotation_start_time,
            "time_zone": TIMEZONE_OVERRIDE or schedule_details.get('timezone', 'UTC'),
            "strategy": rotation_strategy or {
                "type": "custom",
                "shift_duration": "PT24H"
            }
        }
        # Attach memberships so FH creates shifts with assigned users immediately
        try:
            if member_ids:
                rotation_data["memberships"] = [{"user_id": uid} for uid in member_ids]
                # keep compatible fields for older schemas
                rotation_data["members"] = [{"user_id": uid} for uid in member_ids]
                rotation_data["member_ids"] = list(member_ids)
        except Exception:
            pass
        if rotation_restrictions:
            # Ensure restrictions are always a list of windows
            windows = rotation_restrictions if isinstance(rotation_restrictions, list) else [rotation_restrictions]
            rotation_data["restrictions"] = windows
            # Also set at schedule level so FH UI shows custom Shift Hours
            try:
                payload["restrictions"] = windows
            except Exception:
                pass
        
        # Determine rotation start time (use now if missing/too old)
        if not start_date:
            try:
                from datetime import datetime, timezone
                start_date = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            except Exception:
                start_date = None
        rotation_start_time = start_date
        # Add rotation to payload
        payload['rotations'] = [rotation_data]
        
        vprint(f"      Writing rotation (members={len(member_ids)}) start={rotation_start_time}")
        vprint(f"      📋 Full rotation data: {json.dumps(rotation_data, indent=2)}")
        # No schedule-level time_restrictions; use rotation-level restrictions only for FH UI parity
    
    # Log the complete payload being sent
    vprint(f"    📋 Complete payload being sent to FireHydrant:\n{json.dumps(payload, indent=2)}")
    
    try:
        # If a schedule with the same name already exists for this team, reuse it
        try:
            existing = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=headers)
            if existing.status_code == 200:
                items = existing.json().get('data') if isinstance(existing.json(), dict) else existing.json()
                for sc in items or []:
                    if (sc.get('name') or '').strip().lower() == (payload['name'] or '').strip().lower():
                        print(f"    ⏭️  Reusing existing schedule: {sc.get('name')}")
                        return sc
        except Exception:
            pass
        if DRY_RUN or VERIFY_ONLY:
            vprint(f"    🧪 DRY-RUN: would create schedule {payload['name']} for team {team_id}")
            vprint(f"    Payload: {json.dumps(payload, indent=2)}")
            return None
        response = requests.post(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules",
            headers=headers,
            json=payload
        )
        
        vprint(f"    📡 Create schedule API Response: {response.status_code}")
        vprint(f"    📡 Full API Response: {response.text}")
        
        if response.status_code not in [200, 201]:
            print(f"    ❌ Error creating schedule")
        
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"    ❌ Error creating schedule: {e}")
            # Continue execution; do not abort whole run
            return None
        schedule_data = response.json()
        print(f"    ✅ Created schedule: {schedule_data.get('name', 'Unknown')}")
        # Ensure schedule-level restrictions persisted; if missing, PATCH them
        try:
            existing_sched_restrictions = schedule_data.get('restrictions') or []
            if 'restrictions' in payload and payload['restrictions'] and not existing_sched_restrictions:
                vprint("    ⏰ Schedule restrictions missing in response; applying via PATCH…")
                patch_body = {"restrictions": payload['restrictions'], "strategy": {"type": "custom"}}
                patch_resp = requests.patch(
                    f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}",
                    headers=headers,
                    json=patch_body
                )
                vprint(f"    ⇦ PATCH schedule restrictions status: {patch_resp.status_code}")
        except Exception as _se:
            vprint(f"    ⚠️ Failed to apply schedule restrictions via PATCH: {_se}")
        
        # Debug: Check if rotations were created with members
        created_rotations = schedule_data.get('rotations', [])
        vprint(f"    📋 Created {len(created_rotations)} rotation(s) in FireHydrant")
        vprint(f"    📋 Full API response rotation data: {json.dumps(created_rotations, indent=2)}")
        
        for i, rot in enumerate(created_rotations):
            rot_members = rot.get('member_ids', []) or rot.get('members', []) or rot.get('responders', [])
            rot_id = rot.get('id', 'unknown')
            print(f"      Rotation {i+1} (ID: {rot_id}): {len(rot_members)} members")

            # Ensure restrictions persisted; if missing but we had them, try updating
            try:
                current_restrictions = rot.get('restrictions') or []
                if rotation_restrictions and not current_restrictions:
                    vprint("        ⏰ Rotation restrictions missing in response; attempting to apply via PUT…")
                    put_payload = rot.copy()
                    put_payload['restrictions'] = rotation_restrictions if isinstance(rotation_restrictions, list) else [rotation_restrictions]
                    upr = requests.put(
                        f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}",
                        headers=headers,
                        json=put_payload
                    )
                    vprint(f"        ⇦ PUT restrictions status: {upr.status_code}")
                    if upr.status_code not in [200, 201]:
                        vprint(f"        Body: {upr.text}")
            except Exception as _re:
                vprint(f"        ⚠️ Restriction update attempt failed: {_re}")
            # If FH ignored member_ids, attempt to force-assign both users now
            if len(rot_members) < len(member_ids) and member_ids:
                print(f"        🔧 Members missing from rotation; attempting to assign {len(member_ids)} member(s) explicitly…")
                try:
                    assign_payload = {
                        "member_ids": member_ids,
                        "members": [{"user_id": uid} for uid in member_ids]
                    }
                    assign_resp = requests.post(
                        f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}/memberships",
                        headers=headers,
                        json=assign_payload
                    )
                    print(f"        ➤ POST …/memberships → {assign_resp.status_code}")
                    if assign_resp.status_code not in [200, 201]:
                        print(f"          Body: {assign_resp.text}")
                        # Try schedule-level memberships with rotation_id
                        try:
                            sl_payload = {"memberships": [{"user_id": uid, "rotation_id": rot_id} for uid in member_ids]}
                            sl_url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/memberships"
                            sl_resp = requests.post(sl_url, headers=headers, json=sl_payload)
                            print(f"        ➤ POST schedule-level memberships → {sl_resp.status_code}")
                            if sl_resp.status_code not in [200,201]:
                                print(f"          Body: {sl_resp.text}")
                        except Exception as _se:
                            print(f"          ⚠️ schedule-level memberships failed: {_se}")
                except Exception as e:
                    print(f"        ⚠️  Explicit member assignment failed: {e}")
            if len(rot_members) == 0:
                print(f"        ⚠️  WARNING: This rotation has NO MEMBERS!")
                print(f"        🔧 Attempting to update rotation with members...")
                
                # Try to update the rotation with members using PUT with full rotation object
                try:
                    # Get the full rotation data from the response
                    full_rotation = rot.copy()
                    full_rotation['member_ids'] = member_ids
                    
                    print(f"        📋 PUT payload (full rotation): {json.dumps(full_rotation, indent=2)}")
                    update_response = requests.put(
                        f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}",
                        headers=headers,
                        json=full_rotation
                    )
                    print(f"        📡 Update rotation API Response: {update_response.status_code}")
                    print(f"        📡 Update rotation API Full Response: {update_response.text}")
                    
                    if update_response.status_code not in [200, 201]:
                        print(f"        ❌ PUT rotation failed")
                    else:
                        # Verify the update worked by fetching the rotation again
                        verify_response = requests.get(
                            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}",
                            headers=headers
                        )
                        if verify_response.status_code == 200:
                            verify_data = verify_response.json()
                            verified_members = verify_data.get('members', []) or verify_data.get('member_ids', [])
                            print(f"        🔍 Verification - Rotation now has {len(verified_members)} members")
                            if len(verified_members) > 0:
                                print(f"        ✅ Successfully updated rotation with {len(verified_members)} members")
                            else:
                                print(f"        ⚠️  WARNING: Rotation still has NO members after PUT!")
                                print(f"        📋 Full verification response: {json.dumps(verify_data, indent=2)}")
                        else:
                            print(f"        ⚠️  Could not verify rotation update: {verify_response.status_code}")
                except Exception as e:
                    print(f"        ❌ Error updating rotation: {e}")
                    if hasattr(e, 'response') and e.response is not None:
                        print(f"        📡 Error Response: {e.response.text}")

                # Schedule-level PATCH fallback including rotations array
                try:
                    print(f"        🔁 Fallback: schedule-level PATCH with rotations including members...")
                    patch_rotation = {
                        "id": rot_id,
                        "name": rot.get('name') or f"Rotation",
                        "time_zone": rot.get('time_zone') or schedule_details.get('timezone', 'UTC'),
                        "strategy": rot.get('strategy') or rotation_strategy or {"type": "custom", "shift_duration": "PT24H"},
                    "memberships": [{"user_id": uid} for uid in member_ids],
                        "member_ids": member_ids,
                        "members": [{"user_id": uid} for uid in member_ids],
                        "responders": [{"type": "user", "id": uid} for uid in member_ids]
                    }
                    patch_payload = {"rotations": [patch_rotation]}
                    vprint(f"          Payload: {json.dumps(patch_payload, indent=2)}")
                    sched_patch = requests.patch(
                        f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}",
                        headers=headers,
                        json=patch_payload
                    )
                    print(f"          ⇦ Status: {sched_patch.status_code}")
                    print(f"          ⇦ Body: {sched_patch.text}")
                    if sched_patch.status_code in [200, 201]:
                        reget = requests.get(
                            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}",
                            headers=headers
                        )
                        if reget.status_code == 200:
                            sched_after = reget.json()
                            # Find rotation
                            for rr in sched_after.get('rotations', []):
                                if rr.get('id') == rot_id:
                                    vm = rr.get('members', []) or rr.get('member_ids', []) or rr.get('responders', [])
                                    print(f"          🔍 After schedule PATCH, rotation members: {len(vm)}")
                                    break
                except Exception as e:
                    print(f"        ⚠️  Schedule-level PATCH fallback failed: {e}")

                # Fallback attempts: known alternate endpoints/fields for assigning members
                try:
                    print(f"        🔁 Fallback: trying alternate member assignment endpoints...")
                    alt_endpoints = [
                        (
                            'POST',
                            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}/memberships",
                            {"member_ids": member_ids}
                        ),
                        (
                            'POST',
                            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}/memberships",
                            {"memberships": [{"user_id": uid} for uid in member_ids]}
                        ),
                        (
                            'POST',
                            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}/members",
                            {"member_ids": member_ids}
                        ),
                        (
                            'POST',
                            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}/members",
                            [{"user_id": uid} for uid in member_ids]
                        ),
                    ]
                    for method, url, body in alt_endpoints:
                        try:
                            print(f"          ➤ {method} {url}")
                            vprint(f"            Payload: {json.dumps(body, indent=2)}")
                            resp = requests.request(method, url, headers=headers, json=body)
                            print(f"            ⇦ Status: {resp.status_code}")
                            print(f"            ⇦ Body: {resp.text}")
                            if resp.status_code in [200, 201]:
                                # Verify again
                                verify_response = requests.get(
                                    f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/rotations/{rot_id}",
                                    headers=headers
                                )
                                if verify_response.status_code == 200:
                                    verify_data = verify_response.json()
                                    verified_members = verify_data.get('members', []) or verify_data.get('member_ids', [])
                                    print(f"            🔍 Verification after fallback - Rotation members: {len(verified_members)}")
                                    if len(verified_members) > 0:
                                        print(f"            ✅ Members successfully assigned via fallback endpoint")
                                        break
                        except Exception as alt_e:
                            print(f"            ⚠️  Alternate endpoint attempt failed: {alt_e}")
                except Exception as e:
                    print(f"        ⚠️  Fallback member assignment attempts failed: {e}")

                # Final fallback: claim unassigned shifts by re-fetching schedule and patching each shift
                try:
                    print(f"        🔁 Final fallback: attempting to claim unassigned shifts directly...")
                    sched_url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}"
                    sresp = requests.get(sched_url, headers=headers)
                    print(f"          ➤ GET {sched_url} → {sresp.status_code}")
                    if sresp.status_code == 200:
                        sched = sresp.json() or {}
                        # Gather all shifts from rotations; if none, fall back to root-level shifts
                        shifts = []
                        for rr in sched.get('rotations', []) or []:
                            for sh in rr.get('shifts', []) or []:
                                shifts.append(sh)
                        if not shifts:
                            shifts = sched.get('shifts', []) or []
                        print(f"            📋 Found {len(shifts)} shift(s) to inspect")
                        # Assign round-robin across provided members
                        for idx, sh in enumerate(shifts):
                            if not sh or not sh.get('id'):
                                continue
                            # Only claim if unassigned
                            if (sh.get('user') or {}).get('id'):
                                continue
                            chosen_user = member_ids[idx % max(1, len(member_ids))] if member_ids else None
                            if not chosen_user:
                                continue
                            update_shift_url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/shifts/{sh['id']}"
                            patch_resp = requests.patch(update_shift_url, headers=headers, json={"user_id": chosen_user})
                            print(f"              ➤ PATCH {update_shift_url} → {patch_resp.status_code}")
                    else:
                        print(f"            ⚠️  Could not fetch schedule to list shifts: {sresp.text}")
                except Exception as claim_e:
                    print(f"        ⚠️  Shift claiming fallback failed: {claim_e}")

            # Attempt override migration for this rotation if Opsgenie has overrides
            try:
                og_rotations = schedule_details.get('rotations', [])
                if rotation_index < len(og_rotations):
                    og_rotation = og_rotations[rotation_index]
                    migrate_schedule_overrides(schedule_data['id'], rot_id, og_rotation, firehydrant_users, team_id)
                    # Schedule-level overrides that apply to this rotation
                    migrated_count, ov_details = migrate_schedule_level_overrides(
                        schedule_data['id'], rot_id, opsgenie_schedule['id'], og_rotation, firehydrant_users, team_id
                    ) or (0, [])
                    # Stash for summary
                    schedule_data.setdefault('_override_details', []).extend(ov_details)
                # Also look for schedule-level overrides and log them clearly
                og_overrides = list_opsgenie_overrides(opsgenie_schedule['id'])
                if og_overrides:
                    vprint(f"      🔁 Schedule-level overrides present in Opsgenie: {len(og_overrides)}")
                    for idx, ov in enumerate(og_overrides):
                        ov_user = (ov.get('user') or {}).get('username') or (ov.get('user') or {}).get('id')
                        print(f"        ▶ Override {idx+1}: user={ov_user} {ov.get('startDate')} -> {ov.get('endDate')}")
            except Exception as e:
                print(f"      ⚠️  Override migration attempt failed: {e}")
        
        # Post-create: align current FH on-call to OG on-call if mismatched
        try:
            og_on = get_opsgenie_schedule_oncalls(opsgenie_schedule['id'])
            desired_email = og_on[0] if og_on else None
            if desired_email:
                desired_user = find_user_by_email(desired_email, firehydrant_users)
                if desired_user and desired_user.get('id'):
                    ver = requests.get(
                        f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}",
                        headers=headers
                    )
                    if ver.status_code == 200:
                        sj = ver.json()
                        from datetime import datetime, timezone
                        now_dt = datetime.now(timezone.utc)
                        def parse_iso(z):
                            return datetime.fromisoformat(z.replace('Z','+00:00'))
                        for rot in sj.get('rotations', []):
                            for sh in rot.get('shifts', []) or []:
                                st = sh.get('start_time'); et = sh.get('end_time')
                                if not st or not et:
                                    continue
                                try:
                                    st_dt, et_dt = parse_iso(st), parse_iso(et)
                                except Exception:
                                    continue
                                if st_dt <= now_dt < et_dt:
                                    cur = (sh.get('user') or {}).get('id')
                                    if cur != desired_user['id']:
                                        patch_url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_data['id']}/shifts/{sh.get('id')}"
                                        pr = requests.patch(patch_url, headers=headers, json={"user_id": desired_user['id']})
                                        print(f"    🔁 Align current shift to OG on-call {desired_email} → {pr.status_code}")
                                    break
        except Exception as _al:
            vprint(f"    ⚠️ Post-create on-call alignment failed: {_al}")

        print(f"      ℹ️  Override migration will be implemented after rotation creation")
        
        return schedule_data
        
    except requests.exceptions.RequestException as e:
        print(f"    ❌ Error creating schedule: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return None


def migrate_schedule_overrides(firehydrant_schedule_id, firehydrant_rotation_id, opsgenie_rotation, firehydrant_users, team_id):
    """Migrate overrides from Opsgenie rotation to FireHydrant schedule"""
    vprint(f"      🔄 Checking for overrides in rotation...")
    if NO_OVERRIDES:
        print(f"      ⏭️  Skipping rotation overrides due to --no-overrides")
        return
    
    # Check if the rotation has overrides
    overrides = opsgenie_rotation.get('overrides', [])
    if not overrides:
        print(f"      ℹ️  No overrides found in rotation")
        return
    
        print(f"      Writing {len(overrides)} rotation override(s)…")
    
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    for override in overrides:
        override_name = override.get('name', 'Override')
        start_time = override.get('startDate', '')
        end_time = override.get('endDate', '')
        
        print(f"        🔍 Processing override: {override_name}")
        print(f"        📅 Override times: {start_time} to {end_time}")
        
        # Find the user for this override
        override_user = override.get('user', {})
        user_email = override_user.get('username', '')
        
        print(f"        👤 Override user: {user_email}")
        
        if not user_email or not start_time or not end_time:
            print(f"        ⚠️  Skipping override with missing data: {override_name}")
            continue
        
        firehydrant_user = find_user_by_email(user_email, firehydrant_users)
        if not firehydrant_user:
            print(f"        ⚠️  Could not find FireHydrant user for override: {user_email}")
            continue
        
        # Create override payload using the correct FireHydrant API format
        override_payload = {
            "start_time": start_time,
            "end_time": end_time,
            "user_id": firehydrant_user['id']
        }
        
        vprint(f"        📋 Override payload: {override_payload}")
        
        try:
            # Use the correct FireHydrant API endpoint for overrides
            response = requests.post(
                f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{firehydrant_schedule_id}/rotations/{firehydrant_rotation_id}/overrides",
                headers=headers,
                json=override_payload
            )
            
            vprint(f"        📡 Create override API Response: {response.status_code}")
            if response.status_code not in [200, 201]:
                vprint(f"        📡 Create override API Response Text: {response.text}")
            
            response.raise_for_status()
            override_data = response.json()
            print(f"        ✅ Created override: {override_data.get('name', 'Unknown')}")
            
        except requests.exceptions.RequestException as e:
            print(f"        ❌ Error creating override: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"        📡 Error Response: {e.response.text}")


def migrate_schedule_level_overrides(fh_schedule_id, fh_rotation_id, og_schedule_id, og_rotation, firehydrant_users, team_id, apply_on_mismatch=True):
    """Migrate Opsgenie schedule-level overrides by posting FH rotation overrides (no shift lookup)."""
    vprint(f"      Writing schedule-level overrides…")
    if NO_OVERRIDES:
        vprint(f"      ⏭️  Skipping schedule-level overrides due to --no-overrides")
        return 0, []
    overrides = list_opsgenie_overrides(og_schedule_id)
    if not overrides:
        vprint(f"      ℹ️  No schedule-level overrides found")
        return 0, []

    headers = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}", "Content-Type": "application/json"}

    migrated = 0
    details = []
    for og_ov in overrides:
        start_ts = og_ov.get('startDate')
        end_ts = og_ov.get('endDate')
        og_user_email = (og_ov.get('user') or {}).get('username')
        if not start_ts or not end_ts:
            vprint(f"        ⚠️  Skipping override with missing times: {og_ov}")
            continue
        fh_user_id = None
        if og_user_email:
            fh_user = find_user_by_email(og_user_email, firehydrant_users)
            if fh_user:
                fh_user_id = fh_user.get('id')
            else:
                vprint(f"        ℹ️  Override user {og_user_email} not found in FH; creating unassigned override")

        url = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{fh_schedule_id}/rotations/{fh_rotation_id}/overrides"
        payload = {"start_time": start_ts, "end_time": end_ts}
        if fh_user_id:
            payload["user_id"] = fh_user_id
        try:
            vprint(f"        ➤ POST {url}")
            vprint(f"          Payload: {json.dumps(payload, indent=2)}")
            if DRY_RUN or VERIFY_ONLY:
                vprint(f"          🧪 DRY-RUN: would create schedule-level override")
                details.append({"status": "DRY_RUN", "post_url": url, "request_payload": payload, "response_payload": {}})
                migrated += 1
                continue
            resp = requests.post(url, headers=headers, json=payload)
            vprint(f"          ⇦ Status: {resp.status_code}")
            if resp.status_code in [200, 201]:
                migrated += 1
            else:
                vprint(f"          Body: {resp.text}")
            # Record in summary
            try:
                resp_body = resp.json() if resp.text else {}
            except Exception:
                resp_body = {"_note": "no body"}
            details.append({
                "status": resp.status_code,
                "post_url": url,
                "request_payload": payload,
                "response_payload": resp_body
            })
            # Also push to global accumulator
            try:
                OVERRIDE_PUT_DETAILS.append(details[-1])
            except Exception:
                pass
        except requests.exceptions.RequestException as e:
            print(f"          ❌ Rotation override POST failed: {e}")

    vprint(f"      ✅ Applied {migrated} rotation override POST(s) for schedule-level overrides")
    return migrated, details


def display_team_selector(opsgenie_teams, team_members_map, team_schedules_map):
    """Interactive team selector using pick library for multi-select"""
    
    # Prepare options for pick
    options = []
    for team in opsgenie_teams:
        # Get member count for this team
        member_count = len(team_members_map.get(team['id'], []))
        schedule_count = len(team_schedules_map.get(team['id'], []))
        
        # Create display text with team info
        display_text = f"{team['name']} ({member_count} members, {schedule_count} schedules)"
        if team.get('description'):
            display_text += f" - {team['description']}"
        
        # Add member preview if there are members
        if member_count > 0:
            members = team_members_map.get(team['id'], [])
            member_emails = [member.get('user', {}).get('username', 'Unknown') for member in members[:3]]
            if len(member_emails) > 0:
                display_text += f" | Members: {', '.join(member_emails)}"
                if len(members) > 3:
                    display_text += f" +{len(members) - 3} more"
        
        # Add schedule preview if there are schedules
        if schedule_count > 0:
            schedules = team_schedules_map.get(team['id'], [])
            schedule_names = [schedule.get('name', 'Unknown') for schedule in schedules[:2]]
            if len(schedule_names) > 0:
                display_text += f" | Schedules: {', '.join(schedule_names)}"
                if len(schedules) > 2:
                    display_text += f" +{len(schedules) - 2} more"
        
        options.append(display_text)
    
    # Use pick for multi-select
    title = "Select teams to migrate (SPACE to select, ENTER to confirm, ESC to exit):"
    result = pick(
        options,
        title,
        multiselect=True,
        min_selection_count=1
    )
    
    # Handle the result - pick returns a list of tuples (option, index) for multiselect
    selected_teams = []
    for option, index in result:
        selected_teams.append(opsgenie_teams[index])
    
    return selected_teams


def display_team_mapping_options(opsgenie_team, firehydrant_teams):
    """Interactive action selection (skip/create/map) using pick like the team selector"""
    if not SUMMARY_ONLY or VERBOSE:
        print(f"\n{'='*60}")
        print(f"Opsgenie Team: {opsgenie_team['name']}")
        print(f"ID: {opsgenie_team['id']}")
        if opsgenie_team.get('description'):
            print(f"Description: {opsgenie_team['description']}")
        print(f"{'='*60}")
    
    options = [
        ("Skip the team", "skip"),
        ("Create a new team", "create"),
        ("Match to an existing team", "map"),
        ("Preview what will migrate (read-only)", "preview"),
        ("Exit", "exit"),
    ]
    labels = [o[0] for o in options]
    label, index = pick(labels, f"{opsgenie_team['name']}: choose action (↑/↓, ENTER)", multiselect=False)
    action = options[index][1]
    if action == "skip":
        return {"action": "skip", "opsgenie_team": opsgenie_team}
    if action == "create":
        return {"action": "create", "opsgenie_team": opsgenie_team}
    if action == "preview":
        return {"action": "preview", "opsgenie_team": opsgenie_team}
    if action == "exit":
        return {"action": "exit", "opsgenie_team": opsgenie_team}
    # map to existing
        return select_existing_team(opsgenie_team, firehydrant_teams)


def debug_get_rotation_from_ui_url(url: str):
    """Given a FH UI URL, GET the rotation and print members/shifts."""
    try:
        parts = url.strip('/').split('/')
        team_id = parts[parts.index('teams') + 1]
        schedule_id = parts[parts.index('schedules') + 1]
        rotation_id = parts[parts.index('rotations') + 1]
    except Exception:
        print("⚠️  Could not parse rotation URL. Skipping debug GET.")
        return

    api = f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}"
    headers = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
    try:
        resp = requests.get(api, headers=headers)
        print(f"🔎 Debug rotation GET {api} → {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            members = data.get('members') or data.get('member_ids') or []
            print(f"   Members on rotation: {len(members)}")
            try:
                if isinstance(members, list):
                    for m in members[:5]:
                        if isinstance(m, dict):
                            mid = m.get('user_id') or m.get('id') or m.get('user', {}).get('id')
                            mname = m.get('name') or m.get('user', {}).get('name')
                            print(f"     - {mid} {mname or ''}")
                else:
                    print(f"     - {members}")
            except Exception:
                pass
            try:
                shifts_resp = requests.get(f"{api}/shifts", headers=headers)
                print(f"   Shifts GET → {shifts_resp.status_code}")
                if shifts_resp.status_code == 200:
                    sdata = shifts_resp.json()
                    shifts = sdata.get('data') if isinstance(sdata, dict) else sdata
                    print(f"   Shift count: {len(shifts or [])}")
            except Exception:
                pass
        else:
            print(resp.text)
    except Exception as ex:
        print(f"⚠️  Rotation debug GET failed: {ex}")


def select_existing_team(opsgenie_team, firehydrant_teams):
    """Auto-match by name if present; fallback to interactive list."""
    # Auto name-match (case-insensitive)
    og_name = (opsgenie_team.get('name') or '').strip().lower()
    exact = None
    for t in firehydrant_teams:
        if (t.get('name') or '').strip().lower() == og_name:
            exact = t
            break
    if exact:
        print(f"  🔗 Auto-matched to existing FH team by name: {exact['name']} ({exact['id']})")
        return {"action": "map", "opsgenie_team": opsgenie_team, "firehydrant_team": exact}

    # Interactive fallback
    print("\nAvailable FireHydrant teams:")
    for idx, team in enumerate(firehydrant_teams, 1):
        print(f"  {idx}. {team['name']} (ID: {team['id']})")
    choice = input(f"\nSelect team number (1-{len(firehydrant_teams)}) or 0 to cancel: ").strip()
    try:
        choice_num = int(choice)
        if choice_num == 0:
            return {"action": "skip", "opsgenie_team": opsgenie_team}
        elif 1 <= choice_num <= len(firehydrant_teams):
            selected_team = firehydrant_teams[choice_num - 1]
            return {"action": "map", "opsgenie_team": opsgenie_team, "firehydrant_team": selected_team}
        else:
            print("Invalid selection. Skipping team.")
            return {"action": "skip", "opsgenie_team": opsgenie_team}
    except ValueError:
        print("Invalid input. Skipping team.")
        return {"action": "skip", "opsgenie_team": opsgenie_team}


def find_user_by_email(email, firehydrant_users):
    """Find a user in FireHydrant by email"""
    for user in firehydrant_users:
        if user.get('email', '').lower() == email.lower():
            return user
    return None


def create_firehydrant_user(opsgenie_user):
    """Create a new user in FireHydrant"""
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "email": opsgenie_user.get('username', ''),
        "first_name": opsgenie_user.get('fullName', '').split(' ')[0] if opsgenie_user.get('fullName') else '',
        "last_name": ' '.join(opsgenie_user.get('fullName', '').split(' ')[1:]) if opsgenie_user.get('fullName') and len(opsgenie_user.get('fullName', '').split(' ')) > 1 else ''
    }
    
    try:
        response = requests.post(
            f"{FIREHYDRANT_BASE_URL}/users",
            headers=headers,
            json=payload
        )
        
        response.raise_for_status()
        
        new_user = response.json()
        print(f"  ✅ Created user: {new_user.get('email', 'Unknown')}")
        return new_user
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error creating user {opsgenie_user.get('username', 'Unknown')}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return None


def create_firehydrant_user_by_email(email):
    """Create a FireHydrant user from an email address (fallback when not present)."""
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"email": email}
    try:
        resp = requests.post(f"{FIREHYDRANT_BASE_URL}/users", headers=headers, json=payload)
        if resp.status_code not in [200, 201]:
            print(f"  ⚠️  Create FH user by email failed ({resp.status_code}): {resp.text}")
            return None
        user = resp.json()
        print(f"  ✅ Created FH user by email: {email}")
        return user
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error creating FH user by email {email}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return None

def add_user_to_team(user_id, team_id):
    """Add a user to a FireHydrant team"""
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        if DRY_RUN or VERIFY_ONLY:
            print(f"    🧪 DRY-RUN: would add user {user_id} to team {team_id}")
            return True
        # First, get the current team to see existing memberships
        get_response = requests.get(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}",
            headers=headers
        )
        
        if get_response.status_code != 200:
            print(f"    📡 Failed to get team: {get_response.status_code} - {get_response.text}")
            return False
        
        team_data = get_response.json()
        existing_memberships = team_data.get('memberships', [])
        
        # Check if user is already a member
        for membership in existing_memberships:
            if membership.get('user', {}).get('id') == user_id:
                print(f"    ℹ️  User already a member of team")
                return True
        
        # Add the new user to existing memberships
        new_membership = {"user_id": user_id}
        existing_memberships.append(new_membership)
        
        print(f"    📋 Updating team with {len(existing_memberships)} memberships")
        print(f"    📋 New membership: {new_membership}")
        
        # Update the team with new memberships
        update_response = requests.patch(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}",
            headers=headers,
            json={"memberships": existing_memberships}
        )
        
        print(f"    📡 Add user API Response: {update_response.status_code}")
        if update_response.status_code not in [200, 201]:
            print(f"    📡 Add user API Response Text: {update_response.text}")
        
        update_response.raise_for_status()
        
        # Verify the user was actually added
        verify_response = requests.get(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}",
            headers=headers
        )
        
        if verify_response.status_code == 200:
            updated_team = verify_response.json()
            updated_memberships = updated_team.get('memberships', [])
            user_found = False
            
            for membership in updated_memberships:
                if membership.get('user', {}).get('id') == user_id:
                    user_found = True
                    break
            
            if user_found:
                print(f"    ✅ User successfully added to team (verified)")
            else:
                print(f"    ❌ User was not actually added to team (verification failed)")
                return False
        
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error adding user to team: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return False


def add_multiple_users_to_team(user_ids, team_id):
    """Add multiple users to a FireHydrant team at once"""
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        # First, get the current team to see existing memberships
        get_response = requests.get(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}",
            headers=headers
        )
        
        if get_response.status_code != 200:
            print(f"    📡 Failed to get team: {get_response.status_code} - {get_response.text}")
            return False
        
        team_data = get_response.json()
        existing_memberships = team_data.get('memberships', [])
        
        # Get existing user IDs to avoid duplicates
        existing_user_ids = set()
        for membership in existing_memberships:
            if membership.get('user', {}).get('id'):
                existing_user_ids.add(membership['user']['id'])
        
        # Add new users to existing memberships
        new_memberships = []
        for user_id in user_ids:
            if user_id not in existing_user_ids:
                new_memberships.append({"user_id": user_id})
                existing_user_ids.add(user_id)
            else:
                print(f"    ℹ️  User {user_id} already a member of team")
        
        if not new_memberships:
            print(f"    ℹ️  All users already members of team")
            return True
        
        # Combine existing and new memberships
        all_memberships = existing_memberships + new_memberships
        
        print(f"    📋 Updating team with {len(all_memberships)} total memberships")
        print(f"    📋 Adding {len(new_memberships)} new members")
        
        # Update the team with all memberships
        update_response = requests.patch(
            f"{FIREHYDRANT_BASE_URL}/teams/{team_id}",
            headers=headers,
            json={"memberships": all_memberships}
        )
        
        print(f"    📡 Add users API Response: {update_response.status_code}")
        if update_response.status_code not in [200, 201]:
            print(f"    📡 Add users API Response Text: {update_response.text}")
        
        update_response.raise_for_status()
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error adding users to team: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    📡 Error Response: {e.response.text}")
        return False


def migrate_team_members(opsgenie_team, team_members, firehydrant_team, firehydrant_users):
    """Migrate team members from Opsgenie to FireHydrant team"""
    if not team_members:
        print(f"  ℹ️  No team members to migrate for {opsgenie_team['name']}")
        return
    
    print(f"👥 Migrating {len(team_members)} team members to {firehydrant_team['name']}...")
    
    # Collect all users to add to the team
    users_to_add = []
    
    for member in team_members:
        opsgenie_user = member.get('user', {})
        user_email = opsgenie_user.get('username', '')
        
        if not user_email:
            print(f"  ⚠️  Skipping member with no email")
            continue
        
        # Check if user already exists in FireHydrant
        firehydrant_user = find_user_by_email(user_email, firehydrant_users)
        
        if not firehydrant_user:
            # Create new user (prefer email-only creation first)
            print(f"  👤 Creating new user: {user_email}")
            if DRY_RUN or VERIFY_ONLY:
                print(f"  🧪 DRY-RUN: would create user {user_email}")
                firehydrant_user = {"id": f"DRY_{user_email}", "email": user_email}
            else:
                firehydrant_user = create_firehydrant_user_by_email(user_email) or create_firehydrant_user(opsgenie_user)
            if firehydrant_user:
                firehydrant_users.append(firehydrant_user)
            else:
                print(f"  ❌ Failed to create user: {user_email}")
                continue
        
        if firehydrant_user:
            users_to_add.append(firehydrant_user['id'])
            print(f"  ✅ Prepared {user_email} for team addition")
    
    # Add all users to the team at once, but ensure we don't cross-contaminate between teams
    added_count = 0
    if users_to_add:
        print(f"  🔄 Adding {len(users_to_add)} users to team...")
        if DRY_RUN or VERIFY_ONLY:
            print(f"  🧪 DRY-RUN: would add users {users_to_add} to team {firehydrant_team['name']}")
            added_count = len(users_to_add)
        else:
            if add_multiple_users_to_team(users_to_add, firehydrant_team['id']):
                print(f"  ✅ Successfully added all users to team")
                added_count = len(users_to_add)
            else:
                print(f"  ❌ Failed to add users to team")
    return added_count


def migrate_team_schedules(opsgenie_team, firehydrant_team, firehydrant_users):
    """Migrate on-call schedules from Opsgenie to FireHydrant team"""
    vprint(f"📅 Migrating on-call schedules for {firehydrant_team['name']}...")
    vprint(f"  🔍 Looking for schedules for Opsgenie team: {opsgenie_team['name']} (ID: {opsgenie_team['id']})")
    
    # Get schedules from Opsgenie
    opsgenie_schedules = get_team_schedules(opsgenie_team['id'])
    
    if not opsgenie_schedules:
        print(f"  ℹ️  No on-call schedules found for {opsgenie_team['name']}")
        print(f"  🔍 This could mean:")
        print(f"    - No schedules are owned by this team")
        print(f"    - No schedules have this team as a participant")
        print(f"    - Schedule names don't match team patterns")
        return
    
    print(f"  🔍 Found {len(opsgenie_schedules)} schedule(s) to migrate")
    
    created_schedules = []
    pending_unclaimed = []  # [(fh_team_id, fh_schedule_id, fh_rotation_id, unknown_email)]
    applied_overrides_summary = []
    applied_overrides_payloads = []
    for schedule in opsgenie_schedules:
        vprint(f"  📋 Processing schedule: {schedule.get('name', 'Unknown')}")
        
        # Get detailed schedule information to check rotations
        schedule_details = get_schedule_details(schedule['id'])
        if not schedule_details:
            print(f"    ⚠️  Could not get details for schedule {schedule.get('name')}")
            continue
        
        rotations = schedule_details.get('rotations', [])
        print(f"    🔄 Found {len(rotations)} rotation(s) in schedule")
        
        if len(rotations) == 1:
            # Single rotation - create one schedule
            print(f"    📅 Creating single schedule for rotation")
            firehydrant_schedule = create_firehydrant_schedule(
                firehydrant_team['id'], 
                schedule, 
                firehydrant_users,
                rotation_index=0
            )
            
            if firehydrant_schedule:
                created_schedules.append(firehydrant_schedule)
                print(f"    ✅ Successfully migrated schedule: {firehydrant_schedule.get('name')}")
                # Ensure 'now' matches OG on-call
                if ALIGN_NOW:
                    try:
                        align_fh_schedule_to_og_oncall(
                            firehydrant_team['id'],
                            firehydrant_schedule.get('id'),
                            schedule['id'],
                            firehydrant_users
                        )
                    except Exception as _e:
                        vprint(f"    ⚠️ Alignment after create failed: {_e}")
                # Optional verification (verbose only)
                if VERBOSE:
                    try:
                        ver = requests.get(
                            f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{firehydrant_schedule['id']}",
                            headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
                        )
                        vprint(f"    🔎 Verify FH schedule GET status: {ver.status_code}")
                        if ver.status_code == 200:
                            ver_json = ver.json()
                            vprint(f"    🔎 FH schedule (normalized): name={ver_json.get('name')} tz={ver_json.get('time_zone')} rotations={len(ver_json.get('rotations', []))}")
                            if ver_json.get('time_restrictions'):
                                vprint(f"    🔎 FH time_restrictions: {json.dumps(ver_json.get('time_restrictions'), indent=2)}")
                            for rr in ver_json.get('rotations', []):
                                if rr.get('overrides'):
                                    print(f"    🔎 FH overrides for rotation {rr.get('id')}: {len(rr.get('overrides', []))}")
                                    applied_overrides_summary.append({
                                        "schedule": ver_json.get('name'),
                                        "rotation_id": rr.get('id'),
                                        "count": len(rr.get('overrides', []))
                                    })
                            if firehydrant_schedule.get('_override_details'):
                                applied_overrides_payloads.extend(firehydrant_schedule.get('_override_details', []))
                        else:
                            print(f"    ⚠️ Verify FH schedule GET failed: {ver.text}")
                    except Exception as e:
                        print(f"    ⚠️ Verify FH schedule GET error: {e}")
            else:
                print(f"    ❌ Failed to migrate schedule: {schedule.get('name')}")
        
        elif len(rotations) > 1:
            # Multiple rotations - create separate schedules for each
            print(f"    📅 Creating separate schedules for {len(rotations)} rotations")
            for i, rotation in enumerate(rotations):
                print(f"    🔄 Processing rotation {i + 1}/{len(rotations)}")
                firehydrant_schedule = create_firehydrant_schedule(
                    firehydrant_team['id'], 
                    schedule, 
                    firehydrant_users,
                    rotation_index=i
                )
                
                if firehydrant_schedule:
                    created_schedules.append(firehydrant_schedule)
                    print(f"    ✅ Successfully migrated rotation {i + 1}: {firehydrant_schedule.get('name')}")
                    # Ensure 'now' matches OG on-call for the source schedule
                    if ALIGN_NOW:
                        try:
                            align_fh_schedule_to_og_oncall(
                                firehydrant_team['id'],
                                firehydrant_schedule.get('id'),
                                schedule['id'],
                                firehydrant_users
                            )
                        except Exception as _e:
                            vprint(f"    ⚠️ Alignment after create failed: {_e}")
                    # Verification GET per created schedule (verbose only)
                    if VERBOSE:
                        try:
                            ver = requests.get(
                                f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{firehydrant_schedule['id']}",
                                headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
                            )
                            vprint(f"    🔎 Verify FH schedule GET status: {ver.status_code}")
                            if ver.status_code == 200:
                                ver_json = ver.json()
                                vprint(f"    🔎 FH schedule (normalized): name={ver_json.get('name')} tz={ver_json.get('time_zone')} rotations={len(ver_json.get('rotations', []))}")
                                if ver_json.get('time_restrictions'):
                                    vprint(f"    🔎 FH time_restrictions: {json.dumps(ver_json.get('time_restrictions'), indent=2)}")
                                for rr in ver_json.get('rotations', []):
                                    if rr.get('overrides'):
                                        applied_overrides_summary.append({
                                            "schedule": ver_json.get('name'),
                                            "rotation_id": rr.get('id'),
                                            "count": len(rr.get('overrides', []))
                                        })
                                if firehydrant_schedule.get('_override_details'):
                                    applied_overrides_payloads.extend(firehydrant_schedule.get('_override_details', []))
                        except Exception as e:
                            print(f"    ⚠️ Verify FH schedule GET error: {e}")
                else:
                    print(f"    ❌ Failed to migrate rotation {i + 1}")
        
        else:
            print(f"    ⚠️  No rotations found in schedule {schedule.get('name')}")
    
    if created_schedules:
        print(f"  🎉 Successfully migrated {len(created_schedules)} schedule(s)")
        # Emit override summary
        if applied_overrides_summary:
            print("  🔁 Overrides applied:")
            for item in applied_overrides_summary:
                print(f"    - {item['schedule']} rotation {item['rotation_id']}: {item['count']} override(s)")
        else:
            print("  🔁 Overrides applied: 0")

        # Final pass (verbose only): apply overrides again after all schedules exist
        if VERBOSE:
          try:
            for sched in created_schedules:
                fh_sched_id = sched.get('id')
                # GET fresh to enumerate rotations
                ver = requests.get(
                    f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{fh_sched_id}",
                    headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
                )
                if ver.status_code != 200:
                    continue
                ver_json = ver.json()
                # Prompt for unknown schedule participants encountered earlier (unclaimed vs assign)
                # Build unknowns by contrasting OG participants and FH members
                try:
                    og_sched = next((s for s in opsgenie_schedules if s.get('name') == sched.get('name', '').split(' - Rotation')[0]), None)
                    if og_sched:
                        og_details = get_schedule_details(og_sched['id'])
                        og_rots = og_details.get('rotations', [])
                        fh_rots = ver_json.get('rotations', [])
                        from collections import defaultdict
                        unknowns_by_rot = defaultdict(list)
                        for idx, og_rot in enumerate(og_rots):
                            og_emails = []
                            for p in og_rot.get('participants', []):
                                if p.get('type') == 'user' and p.get('username'):
                                    og_emails.append(p['username'])
                            # Which of these are missing from FH users?
                            for em in og_emails:
                                if not find_user_by_email(em, firehydrant_users):
                                    unknowns_by_rot[idx].append(em)
                        # For any unknowns, prompt once
                        if unknowns_by_rot:
                            print("\n❓ Some Opsgenie rotation users are unknown in FireHydrant:")
                            for idx, emails in unknowns_by_rot.items():
                                print(f"   - Rotation {idx+1}: {', '.join(emails)}")
                            choice = input("Would you like to set their shifts as unclaimed (u) or assign to an existing FH user (a)? [u/a]: ").strip().lower()
                            if choice == 'a':
                                # Ask for a FH user email to assign unknown shifts
                                assign_email = input("Enter FireHydrant user email to assign these shifts to: ").strip()
                                assign_user = find_user_by_email(assign_email, firehydrant_users)
                                if not assign_user:
                                    print(f"  ⚠️  Could not find FH user {assign_email}; defaulting to unclaimed")
                                    choice = 'u'
                            # Apply per rotation; only target shifts that currently have a user not in FH users
                            # Build set of known FH user_ids for quick checks
                            known_user_ids = set([u.get('id') for u in firehydrant_users if u.get('id')])
                            for idx, fh_rot in enumerate(fh_rots):
                                if idx not in unknowns_by_rot:
                                    continue
                                # Fetch shifts at schedule level and filter by rotation
                                shifts_url = f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{fh_sched_id}/shifts"
                                sresp = requests.get(shifts_url, headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"})
                                if sresp.status_code != 200:
                                    continue
                                sdata = sresp.json()
                                shifts = sdata.get('data') if isinstance(sdata, dict) else sdata
                                for sh in shifts or []:
                                    rref = sh.get('rotation') or sh.get('on_call_rotation') or {}
                                    rid = rref.get('id') if isinstance(rref, dict) else None
                                    if rid != fh_rot.get('id'):
                                        continue
                                    sh_user = (sh.get('user') or {}).get('id')
                                    # If a shift is assigned to a user_id not in known FH users, it's a candidate
                                    if not sh_user or sh_user in known_user_ids:
                                        continue
                                    if choice == 'u':
                                        patch = requests.patch(
                                            f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{fh_sched_id}/shifts/{sh.get('id')}",
                                            headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}", "Content-Type": "application/json"},
                                            json={"user_id": None}
                                        )
                                        print(f"    ➤ Unclaim shift {sh.get('id')} → {patch.status_code}")
                                    else:
                                        patch = requests.patch(
                                            f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{fh_sched_id}/shifts/{sh.get('id')}",
                                            headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}", "Content-Type": "application/json"},
                                            json={"user_id": assign_user['id']}
                                        )
                                        print(f"    ➤ Assign shift {sh.get('id')} to {assign_user['email']} → {patch.status_code}")
                except Exception as e:
                    print(f"  ⚠️  Unknown-user prompt/apply step failed: {e}")
                # Map name back to OG schedule to fetch OG overrides and attempt PUTs
                og_base_name = sched.get('name', '').split(' - Rotation')[0]
                og_sched = next((s for s in opsgenie_schedules if s.get('name') == og_base_name), None)
                if not og_sched:
                    continue
                og_details = get_schedule_details(og_sched['id'])
                og_rots = og_details.get('rotations', [])
                fh_rots = ver_json.get('rotations', [])
                for idx, fh_rot in enumerate(fh_rots):
                    og_rot = og_rots[idx] if idx < len(og_rots) else {}
                    try:
                        migrated_count, ov_details = migrate_schedule_level_overrides(
                            fh_sched_id, fh_rot.get('id'), og_sched['id'], og_rot, firehydrant_users, firehydrant_team['id']
                        ) or (0, [])
                        if ov_details:
                            applied_overrides_payloads.extend(ov_details)
                    except Exception as e:
                        print(f"  ⚠️  Final override pass failed: {e}")
          except Exception as e:
            print(f"  ⚠️  Final override application step errored: {e}")
        # Emit override payloads for debug
        if applied_overrides_payloads:
            print("  🔁 Override request/response payloads:")
            for d in applied_overrides_payloads:
                try:
                    print(f"    - {d['status']} {d['put_url']}")
                    print("      Request:")
                    print("      " + json.dumps(d.get('request_payload', {}), indent=2).replace("\n", "\n      "))
                    print("      Response:")
                    print("      " + json.dumps(d.get('response_payload', {}), indent=2).replace("\n", "\n      "))
                except Exception:
                    pass
        # Deep comparison: Opsgenie vs FireHydrant (name, timezone, restrictions, rotations count)
        try:
            for sched in created_schedules:
                fh_get = requests.get(
                    f"{FIREHYDRANT_BASE_URL}/teams/{firehydrant_team['id']}/on_call_schedules/{sched['id']}",
                    headers={"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
                )
                if fh_get.status_code != 200:
                    print(f"  ⚠️  Compare fetch failed for {sched.get('name')}: {fh_get.text}")
                    continue
                fh = fh_get.json()
                og = get_schedule_details(next(s['id'] for s in opsgenie_schedules if s['name'] == sched['name'].split(' - Rotation')[0]))
                def tz(d):
                    return d.get('timezone') or d.get('time_zone')
                vprint(f"  🔎 Compare '{sched.get('name')}':")
                vprint(f"     - Timezone OG={tz(og)} FH={fh.get('time_zone')}")
                if fh.get('time_restrictions'):
                    print(f"     - FH time_restrictions: {json.dumps(fh.get('time_restrictions'), indent=2)}")
                og_rots = og.get('rotations', [])
                fh_rots = fh.get('rotations', [])
                print(f"     - Rotations OG={len(og_rots)} FH={len(fh_rots)}")
                # Print OG restriction summary
                for idx, og_rot in enumerate(og_rots[:1]):
                    tr = og_rot.get('timeRestriction', {})
                    r = tr.get('restriction', {}) if tr else {}
                    if tr or r:
                        print(f"     - OG rotation {idx+1} timeRestriction: type={tr.get('type')} start={r.get('startHour')} end={r.get('endHour')}")
                # Print FH rotation restriction summary
                for idx, rr in enumerate(fh_rots[:1]):
                    if rr.get('restrictions'):
                        print(f"     - FH rotation {idx+1} restrictions: {json.dumps(rr.get('restrictions'), indent=2)}")
                # Overrides present?
                for idx, og_rot in enumerate(og_rots[:1]):
                    og_over = og_rot.get('overrides', [])
                    print(f"     - Overrides OG={len(og_over)} FH={(len(rr.get('overrides', [])) if fh_rots else 0)}")
        except Exception as e:
            print(f"  ⚠️  Comparison step failed: {e}")
        # Summary return
        try:
            total_rotations = sum(len(s.get('rotations', []) or []) for s in created_schedules)
        except Exception:
            total_rotations = 0
        total_overrides = sum(it.get('count', 0) for it in applied_overrides_summary)
        return {"schedules": len(created_schedules), "rotations": total_rotations, "overrides": total_overrides}
    else:
        print(f"  ⚠️  No schedules were successfully migrated")
        return {"schedules": 0, "rotations": 0, "overrides": 0}


def create_firehydrant_team(opsgenie_team, team_members, firehydrant_users, existing_teams):
    """Create a new team in FireHydrant and migrate its members"""
    stage_print(f"\n🔨 Creating team '{opsgenie_team['name']}' in FireHydrant...")
    
    # Check if a team with similar name already exists
    team_name = opsgenie_team['name']
    vprint(f"  🔍 Checking for existing teams with name: '{team_name}'")
    
    for existing_team in existing_teams:
        existing_name = existing_team.get('name', '')
        if existing_name.lower() == team_name.lower():
            vprint(f"  ⚠️  Found existing team with same name (case-insensitive): '{existing_name}'")
            vprint(f"  📋 Existing team details: ID={existing_team.get('id')}, Slug={existing_team.get('slug')}")
            
            # Verify this team actually exists by making an API call
            verify_headers = {
                "Authorization": f"Bearer {FIREHYDRANT_API_KEY}"
            }
            try:
                verify_response = requests.get(
                    f"{FIREHYDRANT_BASE_URL}/teams/{existing_team.get('id')}",
                    headers=verify_headers
                )
                if verify_response.status_code == 200:
                    vprint(f"  ✅ Team verification: Team exists in FireHydrant")
                else:
                    vprint(f"  ❌ Team verification: Team does NOT exist (Status: {verify_response.status_code})")
                    vprint(f"  📡 Verification Response: {verify_response.text}")
            except Exception as e:
                vprint(f"  ❌ Team verification failed: {e}")
                
        elif team_name.lower() in existing_name.lower() or existing_name.lower() in team_name.lower():
            vprint(f"  ⚠️  Found similar team name: '{existing_name}' (similar to '{team_name}')")
    
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "name": opsgenie_team['name']
    }
    
    if opsgenie_team.get('description'):
        payload['description'] = opsgenie_team['description']
    
    try:
        if DRY_RUN or VERIFY_ONLY:
            print(f"  🧪 DRY-RUN: would create team with payload:\n  {json.dumps(payload, indent=2)}")
            return {"id": "DRY_RUN", "name": payload['name']}
        vprint(f"  ➤ POST {FIREHYDRANT_BASE_URL}/teams (creating team)")
        response = requests.post(
            f"{FIREHYDRANT_BASE_URL}/teams",
            headers=headers,
            json=payload
        )
        
        # Debug: Print response details (verbose only)
        vprint(f"  📡 API Response Status: {response.status_code}")
        vprint(f"  📡 API Response Text: {response.text}")
        
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"❌ Error creating team (non-fatal): {e}")
            if hasattr(e, 'response') and e.response is not None and hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None
        new_team = response.json()
        
        # Verify the team was actually created by checking if it has required fields
        if not new_team.get('id') or not new_team.get('name'):
            print(f"❌ Team creation failed - invalid response: {new_team}")
            return None
            
        vprint(f"✅ Created team: {new_team.get('name', 'Unknown')} (ID: {new_team.get('id', 'Unknown')})")
        
        # Verify the team actually exists by fetching it
        verify_response = requests.get(
            f"{FIREHYDRANT_BASE_URL}/teams/{new_team['id']}",
            headers=headers
        )
        
        if verify_response.status_code != 200:
            print(f"❌ Team creation verification failed - team not found: {verify_response.status_code}")
            vprint(f"  📡 Verification Response: {verify_response.text}")
            return None
        
        vprint(f"✅ Team creation verified - team exists in FireHydrant")
        
        # Record team in ledger
        if not (DRY_RUN or VERIFY_ONLY):
            _record_created_team(new_team['id'], new_team.get('name'))
        
        # Migrate team members
        stage_print("👥 Creating team members…")
        with SilentPrint(SUMMARY_ONLY and not VERBOSE):
            members_added = migrate_team_members(opsgenie_team, team_members, new_team, firehydrant_users)
        
        # Migrate on-call schedules (scoped to this Opsgenie team only)
        stage_print("📅 Creating schedules and rotations…")
        with SilentPrint(SUMMARY_ONLY and not VERBOSE):
            sched_summary = migrate_team_schedules(opsgenie_team, new_team, firehydrant_users) or {"schedules":0,"rotations":0,"overrides":0}
        
        # EPs
        stage_print("📈 Creating escalation policies…")
        with SilentPrint(SUMMARY_ONLY and not VERBOSE):
            ep_summary = migrate_team_escalation_policies(opsgenie_team, new_team, firehydrant_users) or {"created":0,"skipped":0}
        
        # Summary-only output
        if SUMMARY_ONLY:
            stage_print(f"✅ Finishing up… Team: {new_team.get('name')} | Users: +{members_added} | Schedules: {sched_summary['schedules']} (rots {sched_summary['rotations']}) | Overrides: {sched_summary['overrides']} | EPs: +{ep_summary['created']}/⏭️{ep_summary['skipped']}")
        
        return new_team
    except requests.exceptions.RequestException as e:
        print(f"❌ Error creating team: {e}")
        if hasattr(e, 'response') and e.response is not None and hasattr(e.response, 'text'):
            print(f"Response: {e.response.text}")
            
            # If it's a name conflict, try with a modified name
            if "name is already being used" in str(e.response.text):
                print(f"  🔄 Attempting to create team with modified name...")
                modified_name = f"{opsgenie_team['name']}"
                payload['name'] = modified_name
                
                try:
                    retry_response = requests.post(
                        f"{FIREHYDRANT_BASE_URL}/teams",
                        headers=headers,
                        json=payload
                    )
                    retry_response.raise_for_status()
                    new_team = retry_response.json()
                    print(f"✅ Created team with modified name: {new_team.get('name')} (ID: {new_team.get('id')})")
                    
                    # Migrate team members
                    migrate_team_members(opsgenie_team, team_members, new_team, firehydrant_users)
                    return new_team
                except requests.exceptions.RequestException as retry_e:
                    print(f"❌ Retry also failed: {retry_e}")
                    if hasattr(retry_e, 'response') and retry_e.response is not None:
                        print(f"Retry Response: {retry_e.response.text}")
        
        return None


def main():
    """Main migration workflow"""
    iprint("=" * 60)
    iprint("Opsgenie to FireHydrant Team Migration Tool v2")
    iprint("=" * 60)
    
    # CLI: quick actions
    argv = sys.argv[1:]
    # Config setters run before validation
    if '--set-fh-token' in argv:
        try:
            val = argv[argv.index('--set-fh-token') + 1]
        except Exception:
            print("⚠️  Missing value for --set-fh-token"); return
        _write_config_pairs({"FIREHYDRANT_API_KEY": val})
        print("✅ Saved FIREHYDRANT_API_KEY to config.env")
        return
    if '--set-og-token' in argv:
        try:
            val = argv[argv.index('--set-og-token') + 1]
        except Exception:
            print("⚠️  Missing value for --set-og-token"); return
        _write_config_pairs({"OPSGENIE_API_KEY": val})
        print("✅ Saved OPSGENIE_API_KEY to config.env")
        return
    if '--configure' in argv:
        try:
            fh = input("FireHydrant API key: ").strip()
            og = input("Opsgenie API key: ").strip()
            pairs = {}
            if fh: pairs["FIREHYDRANT_API_KEY"] = fh
            if og: pairs["OPSGENIE_API_KEY"] = og
            if pairs:
                _write_config_pairs(pairs)
                print("✅ Saved to config.env")
            else:
                print("ℹ️  No changes written")
        except KeyboardInterrupt:
            print("\n✋ Aborted")
        return

    # Validate API keys
    if not OPSGENIE_API_KEY or not FIREHYDRANT_API_KEY:
        print("❌ Missing required API keys. Please set them via --configure or --set-fh-token/--set-og-token or config.env.")
        return
    
    # Debug: Show which API keys are being used
    vprint(f"🔑 Using Opsgenie API Key: {OPSGENIE_API_KEY[:8]}...")
    vprint(f"🔑 Using FireHydrant API Key: {FIREHYDRANT_API_KEY}")
    
    # Test FireHydrant API connection
    vprint("\n🔍 Testing FireHydrant API connection...")
    headers = {
        "Authorization": f"Bearer {FIREHYDRANT_API_KEY}"
    }
    try:
        test_response = requests.get(f"{FIREHYDRANT_BASE_URL}/teams", headers=headers)
        vprint(f"  📡 FireHydrant API Test - Status: {test_response.status_code}")
        if test_response.status_code == 200:
            vprint("  ✅ FireHydrant API connection successful")
        else:
            print(f"  ❌ FireHydrant API connection failed: {test_response.text}")
            return
    except Exception as e:
        print(f"  ❌ FireHydrant API connection error: {e}")
        return

    if ('-h' in argv) or ('--help' in argv):
        print("Usage: python3 migrate-teams.py [options]\n\n"
              "Options:\n"
              "  --set-fh-token VALUE          Save FIREHYDRANT_API_KEY into config.env\n"
              "  --set-og-token VALUE          Save OPSGENIE_API_KEY into config.env\n"
              "  --configure                   Interactive prompt to set FH and Opsgenie tokens\n"
              "  --delete-team VALUE[,VALUE…]  Delete FH team(s) by exact name or id (CSV supported)\n"
              "  --delete VALUE[,VALUE…]       Alias for --delete-team\n"
              "  --revert-all                  Delete all teams created by this migrator (destructive)\n"
              "  --preview-team VALUE[,VALUE…] Preview what will migrate for OG team(s) by name or id (CSV)\n"
              "  --delete-schedule ID          Delete FH schedule (requires --team-id)\n"
              "  --team-id ID                  Team id for schedule deletes\n"
              "  --overrides [FILTER]          Preview/apply Opsgenie overrides to FH. FILTER may be a team\n"
              "                                name (apply to all its schedules) or a schedule name. Combine\n"
              "                                with --yes to apply without prompt.\n"
              "  --restriction-strategy STR    weekly | per-day (default: weekly)\n"
              "  --timezone-override Tz        Force schedule timezone\n"
              "  --no-overrides                Skip migrating overrides\n"
              "  --align / --no-align          Toggle OG→FH on-call alignment (default on)\n"
              "  --dry-run                     Print actions; no writes\n"
              "  --verify-only                Fetch and compare; no writes\n"
              "  --verbose                    Show debug payloads and full API responses\n"
              "  --output PATH                Write preview JSON to this path (for --dry-run/--preview-team)\n"
              "  --force                       Skip confirmations for destructive actions\n")
        return

    # Parse feature flags
    global DRY_RUN, VERIFY_ONLY, NO_OVERRIDES, RESTRICTION_STRATEGY, TIMEZONE_OVERRIDE, ALIGN_NOW, OUTPUT_PATH, SUMMARY_ONLY
    DRY_RUN = ('--dry-run' in argv)
    VERIFY_ONLY = ('--verify-only' in argv)
    NO_OVERRIDES = ('--no-overrides' in argv)
    if '--restriction-strategy' in argv:
        try:
            RESTRICTION_STRATEGY = argv[argv.index('--restriction-strategy') + 1].strip().lower()
        except Exception:
            print("⚠️  Missing value for --restriction-strategy; using default")
    if '--timezone-override' in argv:
        try:
            TIMEZONE_OVERRIDE = argv[argv.index('--timezone-override') + 1].strip()
        except Exception:
            print("⚠️  Missing value for --timezone-override; ignoring")
    if '--no-align' in argv:
        ALIGN_NOW = False
    if '--align' in argv:
        ALIGN_NOW = True
    SUMMARY_ONLY = ('--summary-only' in argv)
    if '--output' in argv:
        try:
            OUTPUT_PATH = argv[argv.index('--output') + 1].strip()
        except Exception:
            print("⚠️  Missing value for --output; ignoring")

    # Utility: parse a CSV list possibly spread across multiple argv tokens until next flag
    def _parse_csv_values(start_idx: int):
        vals = []
        buf = []
        i = start_idx
        while i < len(argv) and not argv[i].startswith('-'):
            buf.append(argv[i])
            i += 1
        joined = ' '.join(buf)
        for part in joined.split(','):
            v = part.strip().strip('"').strip("'")
            if v:
                vals.append(v)
        return vals, i

    # Dedicated override backfill: --overrides [FILTER] [--yes]
    if '--overrides' in argv:
        # Helper: apply all OG overrides for a FH schedule (by matching schedule name to OG)
        def _apply_overrides_for_fh_schedule(fh_team: dict, fh_schedule: dict, og_schedules_by_name: dict, fh_users: list, assume_yes: bool):
            sched_name = fh_schedule.get('name') or ''
            og = og_schedules_by_name.get(sched_name.strip().lower())
            if not og:
                print(f"  ⚠️  No Opsgenie schedule matched by name for FH schedule '{sched_name}'")
                return 0
            og_overrides = list_opsgenie_overrides(og.get('id')) or []
            if not og_overrides:
                print(f"  ℹ️  No overrides found in Opsgenie for '{sched_name}'")
                return 0
            # Preview
            print(f"  🔁 Found {len(og_overrides)} Opsgenie override(s) for '{sched_name}':")
            preview = []
            for it in og_overrides[:10]:
                preview.append(f"    - { (it.get('user') or {}).get('username') } {it.get('startDate')} → {it.get('endDate')}")
            for line in preview:
                print(line)
            if len(og_overrides) > 10:
                print(f"    … and {len(og_overrides) - 10} more")
            do_apply = assume_yes
            if not do_apply:
                try:
                    ans = input("  Apply these to FireHydrant? (y/N): ").strip().lower()
                    do_apply = (ans in ('y','yes'))
                except Exception:
                    do_apply = False
            if not do_apply:
                print("  ⏭️  Skipping apply (preview only).")
                return 0
            # Resolve FH rotation id
            rh = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
            det = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/on_call_schedules/{fh_schedule['id']}", headers=rh)
            if det.status_code != 200:
                print(f"  ❌ Could not fetch FH schedule detail: {det.status_code}")
                return 0
            rots = (det.json().get('rotations') or [])
            if not rots:
                print("  ⚠️  No rotations on this FH schedule; cannot apply overrides.")
                return 0
            fh_rot_id = rots[0].get('id')
            # FH user lookup by email
            fh_by_email = {}
            for u in fh_users or []:
                em = (u.get('email') or '').strip().lower()
                if em and u.get('id'):
                    fh_by_email[em] = u['id']
            headers_post = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}", "Content-Type": "application/json"}
            applied = 0
            for ov in og_overrides:
                em = ((ov.get('user') or {}).get('username') or '').strip().lower()
                uid = fh_by_email.get(em)
                body = {"start_time": ov.get('startDate'), "end_time": ov.get('endDate')}
                if uid:
                    body["user_id"] = uid
                url = f"{FIREHYDRANT_BASE_URL}/teams/{fh_team['id']}/on_call_schedules/{fh_schedule['id']}/rotations/{fh_rot_id}/overrides"
                resp = requests.post(url, headers=headers_post, json=body)
                if resp.status_code in (200,201):
                    applied += 1
                else:
                    print(f"    ⚠️  Override POST failed → {resp.status_code}: {getattr(resp,'text','')[:200]}")
            print(f"  ✅ Applied {applied}/{len(og_overrides)} override(s) to '{sched_name}'")
            return applied

        # Parse optional filter and --yes
        assume_yes = ('--yes' in argv)
        idx = argv.index('--overrides') + 1
        filter_value = None
        if idx < len(argv) and not argv[idx].startswith('-'):
            filter_value = argv[idx].strip()
        # Load required data
        fh_teams = fetch_firehydrant_teams()
        fh_users = fetch_firehydrant_users()
        # Build OG schedules map by exact (lowercased) name
        og_schedules = get_all_schedules() or []
        og_by_name = { (s.get('name') or '').strip().lower(): s for s in og_schedules }
        targets = []
        # If filter matches a FH team name exactly, target all of its schedules
        chosen_team = find_firehydrant_team_by_name_or_id(filter_value, fh_teams) if filter_value else None
        if chosen_team:
            rh = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
            rs = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{chosen_team['id']}/on_call_schedules", headers=rh)
            items = rs.json().get('data') if rs.status_code == 200 else []
            for s in items or []:
                targets.append((chosen_team, s))
        else:
            # Otherwise, treat FILTER as schedule-name contains; across all teams
            rh = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
            for t in fh_teams or []:
                rs = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{t['id']}/on_call_schedules", headers=rh)
                items = rs.json().get('data') if rs.status_code == 200 else []
                for s in items or []:
                    nm = (s.get('name') or '')
                    if (filter_value is None) or (filter_value.strip().lower() in nm.strip().lower()):
                        targets.append((t, s))
        if not targets:
            print("ℹ️  No matching FH schedules found for the provided filter.")
            return
        total_applied = 0
        for (team_obj, sched_obj) in targets:
            print(f"\n▶ Schedule: {sched_obj.get('name')} (team {team_obj.get('name')})")
            total_applied += _apply_overrides_for_fh_schedule(team_obj, sched_obj, og_by_name, fh_users, assume_yes)
        print(f"\n🎉 Done. Overrides applied: {total_applied}")
        return

    # Handle: delete team by id or name (supports CSV)
    delete_team_values = []
    for flag in ('--delete-team', '--delete'):
        if flag in argv:
            idx = argv.index(flag) + 1
            if idx >= len(argv):
                print("⚠️  Missing value for", flag, "- continuing without delete")
                break
            delete_team_values, _next = _parse_csv_values(idx)
            break
    # Handle: revert-all (delete all teams)
    if '--revert-all' in argv:
        force = ('--force' in argv)
        ledger = _load_ledger()
        targets = ledger.get('teams', [])
        if not targets:
            print("ℹ️  No migrator-created teams recorded; nothing to revert.")
            return
        # Cross-check with current FH teams to avoid deleting unrelated teams
        existing = {t.get('id'): t for t in (fetch_firehydrant_teams() or [])}
        to_delete = [t for t in targets if t.get('id') in existing]
        if not to_delete:
            print("ℹ️  No recorded teams still exist in FireHydrant.")
            return
        print("⚠️  You are about to DELETE the following migrator-created teams:")
        for t in to_delete:
            print(f"  - {t.get('name')} ({t.get('id')})")
        if not force:
            confirm = input("Type DELETE or DELETE ALL to confirm: ").strip()
            if confirm not in ('DELETE', 'DELETE ALL'):
                print("✋ Aborted")
                return
        remaining = []
        for t in targets:
            if any(td.get('id') == t.get('id') for td in to_delete):
                ok, status, body = delete_firehydrant_team(t.get('id'))
                if ok:
                    print(f"✅ Deleted team {t.get('name')} ({t.get('id')}) → {status}")
                else:
                    print(f"❌ Delete failed for {t.get('name')} → {status}: {body}")
                    remaining.append(t)  # keep in ledger if failed
            else:
                remaining.append(t)  # keep unrelated records
        ledger['teams'] = remaining
        _save_ledger(ledger)
        return
    if delete_team_values:
        force = ('--force' in argv)
        fh_teams = fetch_firehydrant_teams()
        not_found = []
        to_delete = []
        for ident in delete_team_values:
            t = find_firehydrant_team_by_name_or_id(ident, fh_teams)
            if t:
                to_delete.append(t)
            else:
                not_found.append(ident)
        if not_found:
            print(f"❌ Team(s) not found in FireHydrant: {', '.join(not_found)}")
        if not to_delete:
            return
        print("⚠️  About to delete team(s):")
        for t in to_delete:
            print(f"  - {t.get('name')} ({t.get('id')})")
        if not force:
            confirm = input("Type DELETE to confirm: ").strip()
            if confirm != 'DELETE':
                print("✋ Aborted")
                return
        for t in to_delete:
            ok, status, body = delete_firehydrant_team(t.get('id'))
            if ok:
                print(f"✅ Deleted team {t.get('name')} ({t.get('id')}) → {status}")
            else:
                print(f"❌ Delete failed for {t.get('name')} → {status}: {body}")
        return

    # Handle: delete schedule
    if '--delete-schedule' in argv:
        try:
            first_idx = argv.index('--delete-schedule') + 1
        except Exception:
            print("⚠️  Missing value for --delete-schedule; ignoring")
            first_idx = None
        if first_idx is None:
            # fall-through to normal migration
            pass
        team_id_cli = None
        if '--team-id' in argv:
            try:
                team_id_cli = argv[argv.index('--team-id') + 1]
            except Exception:
                print("⚠️  --team-id is required with --delete-schedule; ignoring delete-schedule")
                team_id_cli = None
        else:
            print("⚠️  --team-id is required with --delete-schedule; ignoring delete-schedule")
            team_id_cli = None
        # Resolve schedules by name or id
        def find_schedule_by_name_or_id(team_id: str, ident: str):
            rh = {"Authorization": f"Bearer {FIREHYDRANT_API_KEY}"}
            rr = requests.get(f"{FIREHYDRANT_BASE_URL}/teams/{team_id}/on_call_schedules", headers=rh)
            if rr.status_code != 200:
                return None
            items = rr.json().get('data') or []
            ident_l = ident.strip().lower()
            for it in items:
                if (it.get('id') or '').lower() == ident_l:
                    return it
            for it in items:
                if (it.get('name') or '').strip().lower() == ident_l:
                    return it
            return None
        if first_idx is not None and team_id_cli is not None:
            sched_values, _ = _parse_csv_values(first_idx)
        else:
            sched_values = []
        if first_idx is None or team_id_cli is None or not sched_values:
            # fall-through to normal migration
            pass
        else:
            resolved = []
            not_found = []
            for ident in sched_values:
                s = find_schedule_by_name_or_id(team_id_cli, ident)
                if s:
                    resolved.append(s)
                else:
                    not_found.append(ident)
            if not_found:
                print(f"❌ Schedule(s) not found: {', '.join(not_found)}")
            if resolved:
                force = ('--force' in argv)
                print("⚠️  About to delete schedule(s):")
                for s in resolved:
                    print(f"  - {s.get('name')} ({s.get('id')})")
                if not force:
                    confirm = input("Type DELETE to confirm: ").strip()
                    if confirm != 'DELETE':
                        print("✋ Aborted")
                        return
                for s in resolved:
                    ok, status, body = delete_firehydrant_schedule(team_id_cli, s.get('id'))
                    if ok:
                        print(f"✅ Deleted schedule {s.get('name')} ({s.get('id')}) → {status}")
                    else:
                        print(f"❌ Delete failed for {s.get('name')} → {status}: {body}")
                return
        resolved = []
        not_found = []
        for ident in sched_values:
            s = find_schedule_by_name_or_id(team_id_cli, ident)
            if s:
                resolved.append(s)
            else:
                not_found.append(ident)
        if not_found:
            print(f"❌ Schedule(s) not found: {', '.join(not_found)}")
        if not resolved:
            return
        force = ('--force' in argv)
        print("⚠️  About to delete schedule(s):")
        for s in resolved:
            print(f"  - {s.get('name')} ({s.get('id')})")
        if not force:
            confirm = input("Type DELETE to confirm: ").strip()
            if confirm != 'DELETE':
                print("✋ Aborted")
                return
        for s in resolved:
            ok, status, body = delete_firehydrant_schedule(team_id_cli, s.get('id'))
            if ok:
                print(f"✅ Deleted schedule {s.get('name')} ({s.get('id')}) → {status}")
            else:
                print(f"❌ Delete failed for {s.get('name')} → {status}: {body}")
        return
    
    # Test Opsgenie API connection and schedules
    vprint("\n🔍 Testing Opsgenie API connection...")
    opsgenie_headers = {
        "Authorization": f"GenieKey {OPSGENIE_API_KEY}"
    }
    try:
        # Test basic API connection
        test_response = requests.get(f"{OPSGENIE_BASE_URL}/teams", headers=opsgenie_headers)
        vprint(f"  📡 Opsgenie Teams API Test - Status: {test_response.status_code}")
        if test_response.status_code == 200:
            vprint("  ✅ Opsgenie API connection successful")
        else:
            print(f"  ❌ Opsgenie API connection failed: {test_response.text}")
            return
        
        # Test schedules API specifically
        schedules_test = requests.get(f"{OPSGENIE_BASE_URL}/schedules", headers=opsgenie_headers)
        vprint(f"  📡 Opsgenie Schedules API Test - Status: {schedules_test.status_code}")
        if schedules_test.status_code == 200:
            schedules_data = schedules_test.json()
            schedule_count = len(schedules_data.get('data', []))
            vprint(f"  ✅ Opsgenie Schedules API accessible - Found {schedule_count} schedules")
        else:
            print(f"  ❌ Opsgenie Schedules API failed: {schedules_test.text}")
    except Exception as e:
        print(f"  ❌ Opsgenie API connection error: {e}")
        return
    
    # Fetch all data from both systems
    vprint("\n📡 Fetching data from Opsgenie and FireHydrant...")
    # Silent in minimal
    opsgenie_teams = fetch_opsgenie_teams()
    firehydrant_teams = fetch_firehydrant_teams()
    opsgenie_users = fetch_opsgenie_users()
    firehydrant_users = fetch_firehydrant_users()

    

    # Verify-only mode: summarize and exit
    if VERIFY_ONLY:
        print("\n🔎 Verify-only mode: No changes will be made.")
        print(f"  OG teams: {len(opsgenie_teams)} | FH teams: {len(firehydrant_teams)} | OG users: {len(opsgenie_users)} | FH users: {len(firehydrant_users)}")
        return

    # Optional: debug a specific rotation via UI URL
    debug_url = os.getenv('DEBUG_ROTATION_URL')
    if debug_url:
        print("\n🔍 DEBUG_ROTATION_URL detected; inspecting rotation before migration…")
        debug_get_rotation_from_ui_url(debug_url)
    
    if not opsgenie_teams:
        print("\n❌ No Opsgenie teams found. Exiting.")
        return
    
    # Fetch team members for all teams (concise output)
    iprint("\n👥 Fetching team members…")
    team_members_map = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        future_to_team = {executor.submit(get_team_members, team['id']): team for team in opsgenie_teams}
        for future in as_completed(future_to_team):
            team = future_to_team[future]
            try:
                members = future.result()
            except Exception:
                members = []
            team_members_map[team['id']] = members

    # Non-interactive preview for specified OG teams
    if '--preview-team' in argv:
        idx = argv.index('--preview-team') + 1
        if idx >= len(argv):
            print("⚠️  Missing value for --preview-team; continuing with normal migration")
        else:
            preview_values, _ = _parse_csv_values(idx)
            def _find_og_team(ident: str):
                ident_l = ident.strip().lower()
                for t in opsgenie_teams:
                    if (t.get('id') or '').lower() == ident_l:
                        return t
                for t in opsgenie_teams:
                    if (t.get('name') or '').strip().lower() == ident_l:
                        return t
                return None
            for ident in preview_values:
                og_team = _find_og_team(ident)
                if not og_team:
                    print(f"❌ Opsgenie team not found: {ident}")
                    continue
                try:
                    preview = build_team_preview(og_team, team_members_map)
                    print("\n🧪 Preview (no changes):\n" + json.dumps(preview, indent=2))
                except Exception as _pe:
                    print(f"  ⚠️ Preview failed: {_pe}")
            return
    
    # DRY-RUN without interactive preview: emit full preview JSON and exit
    if DRY_RUN:
        try:
            from datetime import datetime, timezone
            # Build detailed previews
            previews = []
            for og_team in opsgenie_teams:
                previews.append(build_team_preview(og_team, team_members_map))
            # If --output points to a file, write consolidated; otherwise, emit one file per team
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            if OUTPUT_PATH and OUTPUT_PATH.strip().lower().endswith('.json'):
                out_path = OUTPUT_PATH
                with open(out_path, 'w') as f:
                    json.dump({"generated_at":"utc","teams":previews}, f, indent=2)
                print(f"\n🧪 DRY-RUN preview written to: {out_path}")
            else:
                # Determine output directory (either OUTPUT_PATH dir or ./previews)
                out_dir = (OUTPUT_PATH or "previews").rstrip('/')
                try:
                    os.makedirs(out_dir, exist_ok=True)
                except Exception:
                    pass
                for p in previews:
                    name = _slugify(p.get('team'))
                    path = f"{out_dir}/preview-{name}-{stamp}.json"
                    try:
                        with open(path, 'w') as f:
                            json.dump(p, f, indent=2)
                        print(f"📄 Wrote team preview: {path}")
                    except Exception as _we:
                        print(f"  ⚠️ Could not write {path}: {_we}")
                # Also write a small index file
                idx_path = f"{out_dir}/index-{stamp}.json"
                try:
                    with open(idx_path, 'w') as f:
                        json.dump({"generated_at":"utc","teams":[{"team":p.get('team'),"file":f"preview-{_slugify(p.get('team'))}-{stamp}.json"} for p in previews]}, f, indent=2)
                    print(f"📚 Wrote previews index: {idx_path}")
                except Exception as _ie:
                    print(f"  ⚠️ Could not write index file: {_ie}")
        except Exception as _e:
            print(f"❌ Failed to write dry-run preview: {_e}")
        return
    
    # Fetch all schedules once, quietly
    # Silent in minimal
    vprint("\n📅 Checking schedules…")
    all_schedules = get_all_schedules()
    vprint(f"  Found {len(all_schedules)} total schedule(s) in Opsgenie")
    
    # Now fetch team-specific schedules (parallel)
    vprint("\n📅 Mapping schedules to teams…")
    team_schedules_map = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        future_to_team = {executor.submit(get_team_schedules, team['id']): team for team in opsgenie_teams}
        for future in as_completed(future_to_team):
            team = future_to_team[future]
            try:
                schedules = future.result()
            except Exception:
                schedules = []
            team_schedules_map[team['id']] = schedules
    
    # Let user select which teams to migrate
    # Show all teams to select
    selected_teams = display_team_selector(opsgenie_teams, team_members_map, team_schedules_map)
    
    if not selected_teams:
        print("\n❌ No teams selected. Exiting.")
        return
    
    vprint(f"\n✅ Selected {len(selected_teams)} teams for migration")
    input("Press ENTER to continue...")
    
    # Store mapping decisions
    mapping_decisions = []
    
    # Process each selected Opsgenie team
    for i, opsgenie_team in enumerate(selected_teams, 1):
        vprint(f"\nProcessing team {i}/{len(selected_teams)}")
        decision = display_team_mapping_options(opsgenie_team, firehydrant_teams)
        mapping_decisions.append(decision)
        
        # Handle team creation or mapping with member migration
        team_members = team_members_map.get(opsgenie_team['id'], [])
        
        # If preview was requested, show preview then re-prompt this team for action
        while decision['action'] == 'preview':
            try:
                preview = build_team_preview(opsgenie_team, team_members_map)
                print("\n🧪 Preview (no changes):\n" + json.dumps(preview, indent=2))
                # Ask whether to save to file and with what name
                choice = input("\nSave this preview to a JSON file? [y/N]: ").strip().lower()
                if choice == 'y' or choice == 'yes':
                    from datetime import datetime, timezone
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                    default_name = f"preview-{_slugify(opsgenie_team['name'])}-{stamp}.json"
                    path = input(f"Filename (ENTER for {default_name}): ").strip() or default_name
                    try:
                        with open(path, 'w') as f:
                            json.dump(preview, f, indent=2)
                        print(f"📄 Wrote preview to: {path}")
                    except Exception as _we:
                        print(f"  ⚠️ Could not write file: {_we}")
                input("\nPress ENTER to return to action menu for this team…")
            except Exception as _pe:
                print(f"  ⚠️ Preview failed: {_pe}")
            # Re-open action menu for this team
            decision = display_team_mapping_options(opsgenie_team, firehydrant_teams)
            if decision.get('action') == 'exit':
                print("\n👋 Exit selected. Stopping migration.")
                return
        
        if decision['action'] == 'exit':
            print("\n👋 Exit selected. Stopping migration.")
            return
        if decision['action'] == 'create':
            new_team = create_firehydrant_team(opsgenie_team, team_members, firehydrant_users, firehydrant_teams)
            if new_team:
                decision['firehydrant_team'] = new_team
                # Add to list of available teams for future mappings
                firehydrant_teams.append(new_team)
        elif decision['action'] == 'map':
            firehydrant_team = decision['firehydrant_team']
            stage_print("👥 Creating team members…")
            with SilentPrint(SUMMARY_ONLY and not VERBOSE):
                members_added = migrate_team_members(opsgenie_team, team_members, firehydrant_team, firehydrant_users)
            stage_print("📅 Creating schedules and rotations…")
            with SilentPrint(SUMMARY_ONLY and not VERBOSE):
                sched_summary = migrate_team_schedules(opsgenie_team, firehydrant_team, firehydrant_users) or {"schedules":0,"rotations":0,"overrides":0}
            stage_print("📈 Creating escalation policies…")
            with SilentPrint(SUMMARY_ONLY and not VERBOSE):
                ep_summary = migrate_team_escalation_policies(opsgenie_team, firehydrant_team, firehydrant_users) or {"created":0,"skipped":0}
            if SUMMARY_ONLY:
                stage_print(f"✅ Finishing up… Team: {firehydrant_team.get('name')} | Users: +{members_added} | Schedules: {sched_summary['schedules']} (rots {sched_summary['rotations']}) | Overrides: {sched_summary['overrides']} | EPs: +{ep_summary['created']}/⏭️{ep_summary['skipped']}")
    
    # Display summary (suppressed in minimal mode)
    if not MINIMAL:
        print("\n" + "=" * 60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)