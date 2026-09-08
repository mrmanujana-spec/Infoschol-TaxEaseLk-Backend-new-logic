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
DEFAULT_TEAM = [
    {
        "id": "ft_1",
        "name": "Ruwan Silva",
        "initials": "RS",
        "email": "ruwan.silva@abcholdings.lk",
        "role": "Finance Director",
        "status": "Active",
        "lastActive": "Today, 10:15 AM",
        "canSignReturns": True,
        "company_name": "ABC Holdings (Pvt) Ltd"
    },
    {
        "id": "ft_2",
        "name": "Dinithi Perera",
        "initials": "DP",
        "email": "dinithi.p@abcholdings.lk",
        "role": "Senior Accountant",
        "status": "Active",
        "lastActive": "Yesterday",
        "canSignReturns": False,
        "company_name": "ABC Holdings (Pvt) Ltd"
    },
]

# Baseline assigned auditor mapping
DEFAULT_ASSIGNED = {
    "ABC Holdings (Pvt) Ltd": {
        "company_name": "ABC Holdings (Pvt) Ltd",
        "auditor_email": "audit@karunaratne.lk",
        "firm_name": "Karunaratne & Associates",
        "auditor_name": "Mr. A. Karunaratne (FCA)",
        "status": "Connected",
        "invited_at": "01 Aug 2026",
    }
}

@router.post("/auditor-review/invite", response_model=AuditorInviteResponse)
@router.post("/business/auditor/invite", response_model=AuditorInviteResponse)
def invite_auditor(request: AuditorInviteRequest, authorization: Optional[str] = Header(None)):
    """
    Sends an engagement invitation to an auditor and establishes them as the
    active assigned auditor for the company.
    """
    firm = request.firmName or request.firm_name or "Certified Tax Auditor"
    auditor = request.auditorName or request.auditor_name or firm
    company = request.company_name.strip() if request.company_name and request.company_name.strip() else "ABC Holdings (Pvt) Ltd"

    invite_id = f"inv_aud_{int(time.time() * 1000)}"
    now_str = datetime.now().strftime("%d %b %Y at %I:%M %p")

    invite_record = {
        "id": invite_id,
        "company_name": company,
        "email": request.email.strip().lower(),
        "firm_name": firm,
        "auditor_name": auditor,
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
        "auditor_email": request.email.strip().lower(),
        "firm_name": firm,
        "auditor_name": auditor,
        "status": "Invited",
        "invited_at": now_str,
    }
    _save_json(ASSIGNED_AUDITORS_FILE, assigned_map)

    # 3. Persist to Supabase if table exists
    admin_client = get_supabase_admin_client()
    try:
        admin_client.table("invitations").insert({
            "id": invite_id,
            "company_name": company,
            "invite_type": "AUDITOR",
            "email": request.email.strip().lower(),
            "name": auditor,
            "firm_name": firm,
            "status": "PENDING",
        }).execute()
    except Exception as e:
        print(f"[Supabase] Invitations table note: {e}")

    return AuditorInviteResponse(
        success=True,
        message=f"Engagement invitation successfully dispatched to {firm} ({request.email})",
        invitation_id=invite_id,
        status="Invited",
        assigned_auditor=assigned_map[company]
    )

@router.get("/auditor-review/assigned-auditor", response_model=AssignedAuditorResponse)
def get_assigned_auditor(company_name: Optional[str] = None):
    """
    Retrieves the currently assigned / invited auditor for a given company.
    """
    target_company = company_name.strip() if company_name and company_name.strip() else "ABC Holdings (Pvt) Ltd"

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
    company = request.company_name or "ABC Holdings (Pvt) Ltd"

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
        team = [m for m in team if (m.get("company_name") or "ABC Holdings (Pvt) Ltd").lower() == target]

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
