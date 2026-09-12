import os
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Header, HTTPException, status
from app.database import get_supabase_admin_client
from app.services.storage import storage_service
from app.schemas.invitations import (
    AuditorInviteRequest,
    AuditorInviteResponse,
    AssignedAuditorResponse,
    TeamInviteRequest,
    TeamMemberResponse,
    TeamListResponse,
)

router = APIRouter(prefix="/api", tags=["Invitations"])

INVITES_DB_FILE = os.path.join(storage_service.uploads_dir, "invitations_db.json")
ASSIGNED_AUDITORS_FILE = os.path.join(storage_service.uploads_dir, "assigned_auditors_db.json")
TEAM_DB_FILE = os.path.join(storage_service.uploads_dir, "team_members_db.json")

def _load_json(filepath: str) -> Any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def _save_json(filepath: str, data: Any):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Invitations] Failed to persist {filepath}: {e}")

# Baseline default team members
DEFAULT_TEAM = []

# Baseline assigned auditor mapping
DEFAULT_ASSIGNED = {}


def resolve_auditor_identity(auditor_input: str) -> Dict[str, str]:
    """
    Resolves an auditor by User ID (e.g. AUD-XXXXXXXX or UUID) or email.
    Returns a dict with 'email', 'name', 'firm', and 'id'.
    """
    val = auditor_input.strip()
    result = {
        "email": val.lower(),
        "name": "",
        "firm": "",
        "id": "",
    }
    admin_client = get_supabase_admin_client()
    if not admin_client:
        return result

    try:
        clean_id = val.upper()
        if clean_id.startswith("AUD-"):
            prefix = clean_id.replace("AUD-", "").lower()
            res = admin_client.table("profiles").select("id, email, display_name, role").ilike("id", f"{prefix}%").execute()
            if res.data and len(res.data) > 0:
                p = res.data[0]
                result["email"] = (p.get("email") or result["email"]).lower()
                result["name"] = p.get("display_name") or ""
                result["id"] = str(p.get("id"))
                return result

        if len(val) == 36 and "-" in val:
            res = admin_client.table("profiles").select("id, email, display_name, role").eq("id", val).execute()
            if res.data and len(res.data) > 0:
                p = res.data[0]
                result["email"] = (p.get("email") or result["email"]).lower()
                result["name"] = p.get("display_name") or ""
                result["id"] = str(p.get("id"))
                return result

        if "@" in val:
            res = admin_client.table("profiles").select("id, email, display_name, role").ilike("email", val).execute()
            if res.data and len(res.data) > 0:
                p = res.data[0]
                result["email"] = (p.get("email") or val).lower()
                result["name"] = p.get("display_name") or ""
                result["id"] = str(p.get("id"))
                return result
    except Exception:
        pass

    return result

@router.post("/auditor-review/invite", response_model=AuditorInviteResponse)
@router.post("/business/auditor/invite", response_model=AuditorInviteResponse)
def invite_auditor(request: AuditorInviteRequest, authorization: Optional[str] = Header(None)):
    """
    Sends an engagement invitation to an auditor and establishes them as the
    active assigned auditor for the company.
    Supports invitation by Email or Auditor User ID (AUD-XXXXXXXX).
    """
    # Resolve auditor by User ID or email
    resolved = resolve_auditor_identity(request.email)
    actual_email = resolved["email"] or request.email.strip().lower()
    actual_firm = request.firmName or request.firm_name or resolved["firm"] or "Certified Tax Auditor"
    actual_name = request.auditorName or request.auditor_name or resolved["name"] or actual_firm
    company = request.company_name.strip() if request.company_name and request.company_name.strip() else ""

    invite_id = f"inv_aud_{int(time.time() * 1000)}"
    now_str = datetime.now().strftime("%d %b %Y at %I:%M %p")

    invite_record = {
        "id": invite_id,
        "company_name": company,
        "email": actual_email,
        "firm_name": actual_firm,
        "auditor_name": actual_name,
        "invite_type": "AUDITOR",
        "status": "Invited",
        "created_at": now_str,
    }

    # 1. Save to invitations list
    invites = _load_json(INVITES_DB_FILE) or []
    if isinstance(invites, list):
        invites.insert(0, invite_record)
    else:
        invites = [invite_record]
    _save_json(INVITES_DB_FILE, invites)

    # 2. Update active assigned auditor mapping for this company
    assigned_map = _load_json(ASSIGNED_AUDITORS_FILE)
    if not isinstance(assigned_map, dict):
        assigned_map = dict(DEFAULT_ASSIGNED)

    assigned_map[company] = {
        "company_name": company,
        "auditor_email": actual_email,
        "firm_name": actual_firm,
        "auditor_name": actual_name,
        "status": "Active",
        "invited_at": now_str,
    }
    _save_json(ASSIGNED_AUDITORS_FILE, assigned_map)

    # 3. Persist to Supabase invitations and auditor_engagements tables
    admin_client = get_supabase_admin_client()
    if admin_client:
        try:
            admin_client.table("invitations").insert({
                "id": invite_id,
                "company_name": company,
                "invite_type": "AUDITOR",
                "email": actual_email,
                "name": actual_name,
                "firm_name": actual_firm,
                "status": "PENDING",
            }).execute()
        except Exception as e:
            print(f"[Supabase] Invitations table note: {e}")

        try:
            admin_client.table("auditor_engagements").upsert({
                "id": f"eng_{int(time.time() * 1000)}",
                "company_name": company,
                "tax_year": "2025/26",
                "auditor_email": actual_email,
                "auditor_name": actual_name,
                "auditor_firm": actual_firm,
                "status": "ACTIVE",
                "review_status": "PENDING",
                "appointed_date": datetime.now().isoformat(),
                "created_at": datetime.now().isoformat(),
            }, on_conflict="company_name, tax_year").execute()
        except Exception as e:
            print(f"[Supabase] auditor_engagements sync note: {e}")

    return AuditorInviteResponse(
        success=True,
        message=f"Engagement invitation successfully dispatched to {actual_firm} ({actual_email})",
        invitation_id=invite_id,
        status="Active",
        assigned_auditor=assigned_map[company]
    )

@router.get("/auditor-review/assigned-auditor", response_model=AssignedAuditorResponse)
def get_assigned_auditor(company_name: Optional[str] = None):
    """
    Retrieves the currently assigned / invited auditor for a given company.
    Queries Supabase auditor_engagements first, falling back to local storage.
    """
    target_company = company_name.strip() if company_name and company_name.strip() else ""

    admin_client = get_supabase_admin_client()
    if admin_client and target_company:
        try:
            res = admin_client.table("auditor_engagements").select("*").eq("company_name", target_company).eq("status", "ACTIVE").order("created_at", desc=True).limit(1).execute()
            if res.data and len(res.data) > 0:
                eng = res.data[0]
                return AssignedAuditorResponse(
                    company_name=target_company,
                    has_assigned_auditor=True,
                    auditor={
                        "company_name": target_company,
                        "auditor_email": eng.get("auditor_email"),
                        "firm_name": eng.get("auditor_firm"),
                        "auditor_name": eng.get("auditor_name"),
                        "status": "Active",
                        "invited_at": eng.get("appointed_date") or eng.get("created_at"),
                    }
                )
        except Exception:
            pass

    assigned_map = _load_json(ASSIGNED_AUDITORS_FILE)
    if not isinstance(assigned_map, dict):
        _save_json(ASSIGNED_AUDITORS_FILE, DEFAULT_ASSIGNED)
        assigned_map = DEFAULT_ASSIGNED

    if target_company in assigned_map:
        return AssignedAuditorResponse(
            company_name=target_company,
            has_assigned_auditor=True,
            auditor=assigned_map[target_company]
        )

    return AssignedAuditorResponse(
        company_name=target_company,
        has_assigned_auditor=False,
        auditor=None
    )

@router.post("/business/team/invite", response_model=TeamMemberResponse)
def invite_team_member(request: TeamInviteRequest):
    """
    Invites a new finance team member with specific role and signing permissions.
    """
    can_sign = request.can_sign_returns if request.canSignReturns is None else request.canSignReturns
    company = request.company_name or ""

    member_id = f"ft_{int(time.time() * 1000)}"
    initials = "".join([part[0] for part in request.name.strip().split() if part]).upper()[:2] or "TM"

    new_member = {
        "id": member_id,
        "name": request.name.strip(),
        "initials": initials,
        "email": request.email.strip().lower(),
        "role": request.role,
        "status": "Invited",
        "lastActive": "Invitation sent",
        "canSignReturns": bool(can_sign),
        "company_name": company
    }

    team = _load_json(TEAM_DB_FILE) or []
    if not isinstance(team, list) or len(team) == 0:
        team = list(DEFAULT_TEAM)
    team.append(new_member)
    _save_json(TEAM_DB_FILE, team)

    return TeamMemberResponse(**new_member)

@router.get("/business/team", response_model=TeamListResponse)
def get_team_members(company_name: Optional[str] = None):
    """
    Lists all finance team members for a company.
    """
    team = _load_json(TEAM_DB_FILE) or []
    if not isinstance(team, list) or len(team) == 0:
        _save_json(TEAM_DB_FILE, DEFAULT_TEAM)
        team = list(DEFAULT_TEAM)

    if company_name:
        target = company_name.strip().lower()
        team = [m for m in team if (m.get("company_name") or "").lower() == target]

    return TeamListResponse(team=[TeamMemberResponse(**m) for m in team])

@router.delete("/business/team/{member_id}")
def remove_team_member(member_id: str):
    """
    Revokes team member access.
    """
    team = _load_json(TEAM_DB_FILE) or []
    if isinstance(team, list):
        team = [m for m in team if m.get("id") != member_id]
        _save_json(TEAM_DB_FILE, team)
    return {"success": True, "message": "Team member removed successfully"}
