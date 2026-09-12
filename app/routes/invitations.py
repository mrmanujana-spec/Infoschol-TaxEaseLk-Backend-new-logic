import os
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, cast

from fastapi import APIRouter, Header, HTTPException, Query, status
from app.database import get_supabase_client, get_supabase_admin_client
from app.services.storage import storage_service
from app.routes.notifications import create_notification
from app.schemas.invitations import (
    AuditorInviteRequest,
    AuditorInviteResponse,
    AssignedAuditorResponse,
    TeamInviteRequest,
    TeamMemberResponse,
    TeamListResponse,
    ClientInvitationItem,
    ClientInvitationsListResponse,
)

router = APIRouter(prefix="/api", tags=["Invitations"])

INVITES_DB_FILE = os.path.join(storage_service.uploads_dir, "invitations_db.json")
ASSIGNED_AUDITORS_FILE = os.path.join(storage_service.uploads_dir, "assigned_auditors_db.json")
ENGAGEMENTS_DB_FILE = os.path.join(storage_service.uploads_dir, "engagements_db.json")
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
                rows = cast(List[Dict[str, Any]], res.data)
                p = rows[0]
                result["email"] = str(p.get("email") or result["email"]).lower()
                result["name"] = str(p.get("display_name") or "")
                result["id"] = str(p.get("id"))
                return result

        if len(val) == 36 and "-" in val:
            res = admin_client.table("profiles").select("id, email, display_name, role").eq("id", val).execute()
            if res.data and len(res.data) > 0:
                rows = cast(List[Dict[str, Any]], res.data)
                p = rows[0]
                result["email"] = str(p.get("email") or result["email"]).lower()
                result["name"] = str(p.get("display_name") or "")
                result["id"] = str(p.get("id"))
                return result

        if "@" in val:
            res = admin_client.table("profiles").select("id, email, display_name, role").ilike("email", val).execute()
            if res.data and len(res.data) > 0:
                rows = cast(List[Dict[str, Any]], res.data)
                p = rows[0]
                result["email"] = str(p.get("email") or val).lower()
                result["name"] = str(p.get("display_name") or "")
                result["id"] = str(p.get("id"))
                return result
    except Exception:
        pass

    return result

@router.post("/auditor-review/invite", response_model=AuditorInviteResponse)
@router.post("/business/auditor/invite", response_model=AuditorInviteResponse)
def invite_auditor(request: AuditorInviteRequest, authorization: Optional[str] = Header(None)):
    """
    Sends an engagement invitation to an auditor.
    Initially establishes the relationship with 'Pending Acceptance' status.
    Once the auditor accepts, status transitions to 'Active'.
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
    now_iso = datetime.now().isoformat()

    invite_record = {
        "id": invite_id,
        "company_name": company,
        "email": actual_email,
        "firm_name": actual_firm,
        "auditor_name": actual_name,
        "invite_type": "AUDITOR",
        "tax_year": "2025/26",
        "status": "PENDING",
        "created_at": now_str,
    }

    # 1. Save to invitations list
    invites = _load_json(INVITES_DB_FILE) or []
    if isinstance(invites, list):
        invites.insert(0, invite_record)
    else:
        invites = [invite_record]
    _save_json(INVITES_DB_FILE, invites)

    # 2. Update assigned auditor mapping with "Pending Acceptance"
    assigned_map = _load_json(ASSIGNED_AUDITORS_FILE)
    if not isinstance(assigned_map, dict):
        assigned_map = dict(DEFAULT_ASSIGNED)

    assigned_map[company] = {
        "company_name": company,
        "auditor_email": actual_email,
        "firm_name": actual_firm,
        "auditor_name": actual_name,
        "status": "Pending Acceptance",
        "invited_at": now_str,
    }
    _save_json(ASSIGNED_AUDITORS_FILE, assigned_map)

    # 3. Update engagements_db.json with PENDING status
    engs = _load_json(ENGAGEMENTS_DB_FILE) or []
    if not isinstance(engs, list):
        engs = []
    
    eng_record = {
        "id": f"eng_{int(time.time() * 1000)}",
        "company_name": company,
        "tax_year": "2025/26",
        "auditor_email": actual_email,
        "auditor_name": actual_name,
        "auditor_firm": actual_firm,
        "status": "PENDING",
        "review_status": "AWAITING_ACCEPTANCE",
        "appointed_date": now_iso,
        "concluded_date": None,
        "created_at": now_iso,
    }
    replaced = False
    for idx, e in enumerate(engs):
        if e.get("company_name", "").lower() == company.lower() and e.get("tax_year") == "2025/26":
            engs[idx] = eng_record
            replaced = True
            break
    if not replaced:
        engs.insert(0, eng_record)
    _save_json(ENGAGEMENTS_DB_FILE, engs)

    # 4. Persist to Supabase invitations table
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

    # 5. Trigger in-app notification to the Auditor
    create_notification(
        recipient_role="auditor",
        company_name=company,
        title="New Client Invitation",
        message=f"{company} has invited you to be their statutory tax auditor for FY2025/26.",
        notif_type="info",
        link="/client-invitations",
    )

    return AuditorInviteResponse(
        success=True,
        message=f"Engagement invitation successfully dispatched to {actual_firm} ({actual_email}). Awaiting auditor acceptance.",
        invitation_id=invite_id,
        status="Pending Acceptance",
        assigned_auditor=assigned_map[company]
    )

@router.get("/auditor-review/assigned-auditor", response_model=AssignedAuditorResponse)
def get_assigned_auditor(company_name: Optional[str] = None):
    """
    Retrieves the currently assigned / invited auditor for a given company.
    Accurately reflects 'Active' vs 'Pending Acceptance'.
    """
    target_company = company_name.strip() if company_name and company_name.strip() else ""
    if not target_company:
        return AssignedAuditorResponse(company_name="", has_assigned_auditor=False, auditor=None)

    admin_client = get_supabase_admin_client()
    if admin_client:
        try:
            res = admin_client.table("auditor_engagements").select("*").eq("company_name", target_company).order("created_at", desc=True).limit(1).execute()
            if res.data and len(res.data) > 0:
                engs_data = cast(List[Dict[str, Any]], res.data)
                eng = engs_data[0]
                raw_st = str(eng.get("status") or "ACTIVE").upper()
                if raw_st in ["ACTIVE", "PENDING"]:
                    display_st = "Active" if raw_st == "ACTIVE" else "Pending Acceptance"
                    return AssignedAuditorResponse(
                        company_name=target_company,
                        has_assigned_auditor=True,
                        auditor={
                            "company_name": target_company,
                            "auditor_email": eng.get("auditor_email"),
                            "firm_name": eng.get("auditor_firm"),
                            "auditor_name": eng.get("auditor_name"),
                            "status": display_st,
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
        record = assigned_map[target_company]
        cur_st = record.get("status", "")
        if cur_st in ["Active", "Pending Acceptance"]:
            return AssignedAuditorResponse(
                company_name=target_company,
                has_assigned_auditor=True,
                auditor=record
            )

    return AssignedAuditorResponse(
        company_name=target_company,
        has_assigned_auditor=False,
        auditor=None
    )

# -----------------------------------------------------------------------------
# AUDITOR INVITATION MANAGEMENT ENDPOINTS (Client Invitations Box & Accept/Decline)
# -----------------------------------------------------------------------------

@router.get("/auditor/invitations", response_model=ClientInvitationsListResponse)
def get_auditor_invitations(
    auditor_email: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None)
):
    """
    Retrieves incoming client invitations for the auditor's 'Client Invitations' box.
    """
    target_email = auditor_email.strip().lower() if auditor_email else ""
    if not target_email and authorization and authorization.startswith("Bearer "):
        try:
            token = authorization.split(" ")[1]
            client = get_supabase_client()
            u = client.auth.get_user(token)
            if u and u.user and u.user.email:
                target_email = str(u.user.email).strip().lower()
        except Exception:
            pass

    invites: List[Dict[str, Any]] = []
    supabase_queried = False
    admin_client = get_supabase_admin_client()
    if admin_client:
        try:
            query = admin_client.table("invitations").select("*").eq("invite_type", "AUDITOR")
            if target_email:
                query = query.ilike("email", target_email)
            res = query.order("created_at", desc=True).execute()
            if res.data is not None:
                invites = cast(List[Dict[str, Any]], res.data)
                supabase_queried = True
        except Exception as e:
            print(f"[Invitations] Supabase query note: {e}")

    # Local fallback only if Supabase is unreachable
    if not supabase_queried:
        all_local = _load_json(INVITES_DB_FILE) or []
        if isinstance(all_local, list):
            if target_email:
                invites = [i for i in all_local if i.get("invite_type") == "AUDITOR" and (i.get("email") or "").lower() == target_email]
            else:
                invites = [i for i in all_local if i.get("invite_type") == "AUDITOR"]

    items: List[ClientInvitationItem] = []
    for inv in invites:
        st = str(inv.get("status") or "PENDING").upper()
        if st in ["INVITED"]:
            st = "PENDING"
        items.append(ClientInvitationItem(
            id=str(inv.get("id")),
            company_name=str(inv.get("company_name") or "Corporate Client"),
            email=str(inv.get("email") or ""),
            firm_name=inv.get("firm_name") or "Audit Firm",
            auditor_name=inv.get("auditor_name") or inv.get("name") or "Auditor",
            tax_year=inv.get("tax_year") or "2025/26",
            status=st,
            created_at=str(inv.get("created_at") or "Recently")
        ))

    pending_count = sum(1 for item in items if item.status == "PENDING")
    return ClientInvitationsListResponse(
        invitations=items,
        total_count=len(items),
        pending_count=pending_count
    )

@router.post("/auditor/invitations/{invitation_id}/accept")
def accept_auditor_invitation(invitation_id: str, authorization: Optional[str] = Header(None)):
    """
    Auditor accepts an incoming client engagement invitation.
    Transitions status to ACCEPTED and activates the engagement.
    Updates the business-side auditor tile to 'Active' and notifies the client.
    """
    now_str = datetime.now().strftime("%d %b %Y at %I:%M %p")
    now_iso = datetime.now().isoformat()

    # 1. Update invitations_db.json
    invites = _load_json(INVITES_DB_FILE) or []
    target_invite: Optional[Dict[str, Any]] = None
    if isinstance(invites, list):
        for inv in invites:
            if isinstance(inv, dict) and str(inv.get("id")) == str(invitation_id):
                inv["status"] = "ACCEPTED"
                target_invite = cast(Dict[str, Any], inv)
                break
        _save_json(INVITES_DB_FILE, invites)

    company: str = str(target_invite.get("company_name") or "") if target_invite else ""
    auditor_email: str = str(target_invite.get("email") or "") if target_invite else ""
    auditor_name: str = str(target_invite.get("auditor_name") or target_invite.get("name") or "Mr. A. Karunaratne (FCA)") if target_invite else "Mr. A. Karunaratne (FCA)"
    firm_name: str = str(target_invite.get("firm_name") or "Karunaratne & Associates") if target_invite else "Karunaratne & Associates"

    # 2. Update Supabase invitations
    admin_client = get_supabase_admin_client()
    if admin_client:
        try:
            admin_client.table("invitations").update({"status": "ACCEPTED"}).eq("id", invitation_id).execute()
            if not company:
                f_res = admin_client.table("invitations").select("*").eq("id", invitation_id).execute()
                if f_res.data and len(f_res.data) > 0:
                    f_rows = cast(List[Dict[str, Any]], f_res.data)
                    row = f_rows[0]
                    company = str(row.get("company_name") or company)
                    auditor_email = str(row.get("email") or auditor_email)
                    auditor_name = str(row.get("name") or auditor_name)
                    firm_name = str(row.get("firm_name") or firm_name)
        except Exception as e:
            print(f"[Invitations] Supabase accept note: {e}")

    if not company and isinstance(invites, list) and len(invites) > 0:
        first_inv = cast(Dict[str, Any], invites[0])
        company = str(first_inv.get("company_name", ""))
        auditor_email = str(first_inv.get("email", ""))
        auditor_name = str(first_inv.get("auditor_name") or "Auditor")
        firm_name = str(first_inv.get("firm_name") or "Audit Practice")

    if not company:
        raise HTTPException(status_code=404, detail="Invitation not found.")

    # 3. Update assigned_auditors_db.json to 'Active'
    assigned_map = cast(Dict[str, Any], _load_json(ASSIGNED_AUDITORS_FILE) or {})
    if not isinstance(assigned_map, dict):
        assigned_map = {}

    assigned_map[company] = {
        "company_name": company,
        "auditor_email": auditor_email,
        "firm_name": firm_name,
        "auditor_name": auditor_name,
        "status": "Active",
        "invited_at": now_str,
        "appointed_at": now_str,
    }
    _save_json(ASSIGNED_AUDITORS_FILE, assigned_map)

    # 4. Update engagements_db.json to 'ACTIVE'
    raw_engs = _load_json(ENGAGEMENTS_DB_FILE) or []
    engs_list: List[Dict[str, Any]] = cast(List[Dict[str, Any]], raw_engs) if isinstance(raw_engs, list) else []

    existing_eng: Optional[Dict[str, Any]] = None
    for e in engs_list:
        if str(e.get("company_name", "")).lower() == company.lower() and str(e.get("tax_year", "")) == "2025/26":
            e["status"] = "ACTIVE"
            e["review_status"] = "PENDING"
            e["appointed_date"] = now_iso
            existing_eng = e
            break

    if not existing_eng:
        existing_eng = {
            "id": f"eng_{int(time.time() * 1000)}",
            "company_name": company,
            "tax_year": "2025/26",
            "auditor_email": auditor_email,
            "auditor_name": auditor_name,
            "auditor_firm": firm_name,
            "status": "ACTIVE",
            "review_status": "PENDING",
            "appointed_date": now_iso,
            "concluded_date": None,
            "created_at": now_iso,
        }
        engs_list.insert(0, existing_eng)
    _save_json(ENGAGEMENTS_DB_FILE, engs_list)

    # 5. Persist to Supabase auditor_engagements table
    if admin_client:
        try:
            admin_client.table("auditor_engagements").upsert({
                "id": existing_eng.get("id") or f"eng_{int(time.time() * 1000)}",
                "company_name": company,
                "tax_year": "2025/26",
                "auditor_email": auditor_email,
                "auditor_name": auditor_name,
                "auditor_firm": firm_name,
                "status": "ACTIVE",
                "review_status": "PENDING",
                "appointed_date": now_iso,
                "created_at": now_iso,
            }, on_conflict="id").execute()
        except Exception as e:
            print(f"[Invitations] Supabase engagement upsert note: {e}")

    # 6. Notify Business Client in real time
    create_notification(
        recipient_role="business",
        company_name=company,
        title="Auditor Accepted Engagement",
        message=f"{auditor_name} ({firm_name}) has accepted your statutory tax audit appointment for FY2025/26.",
        notif_type="success",
        link="/auditor-review",
    )

    return {
        "success": True,
        "message": f"Successfully accepted engagement for {company}. You now have full audit access.",
        "status": "Active",
        "invitation_id": invitation_id,
        "assigned_auditor": assigned_map[company]
    }

@router.post("/auditor/invitations/{invitation_id}/decline")
def decline_auditor_invitation(invitation_id: str, authorization: Optional[str] = Header(None)):
    """
    Auditor declines an incoming client engagement invitation.
    Transitions status to DECLINED and frees the 1-auditor lock for the business.
    """
    invites = _load_json(INVITES_DB_FILE) or []
    target_invite: Optional[Dict[str, Any]] = None
    if isinstance(invites, list):
        for inv in invites:
            if isinstance(inv, dict) and str(inv.get("id")) == str(invitation_id):
                inv["status"] = "DECLINED"
                target_invite = cast(Dict[str, Any], inv)
                break
        _save_json(INVITES_DB_FILE, invites)

    company: str = str(target_invite.get("company_name") or "") if target_invite else ""
    auditor_name: str = str(target_invite.get("auditor_name") or target_invite.get("name") or "The invited auditor") if target_invite else "The invited auditor"

    admin_client = get_supabase_admin_client()
    if admin_client:
        try:
            admin_client.table("invitations").update({"status": "REVOKED"}).eq("id", invitation_id).execute()
            if not company:
                f_res = admin_client.table("invitations").select("*").eq("id", invitation_id).execute()
                if f_res.data and len(f_res.data) > 0:
                    f_rows = cast(List[Dict[str, Any]], f_res.data)
                    company = str(f_rows[0].get("company_name") or company)
        except Exception as e:
            print(f"[Invitations] Supabase decline note: {e}")

    if not company and isinstance(invites, list) and len(invites) > 0:
        first_inv = cast(Dict[str, Any], invites[0])
        company = str(first_inv.get("company_name", ""))

    if company:
        # Free assigned auditor status
        assigned_map = cast(Dict[str, Any], _load_json(ASSIGNED_AUDITORS_FILE) or {})
        if isinstance(assigned_map, dict) and company in assigned_map:
            assigned_map[company]["status"] = "Declined"
            _save_json(ASSIGNED_AUDITORS_FILE, assigned_map)

        # Terminate engagement
        raw_engs = _load_json(ENGAGEMENTS_DB_FILE) or []
        engs_list: List[Dict[str, Any]] = cast(List[Dict[str, Any]], raw_engs) if isinstance(raw_engs, list) else []
        for e in engs_list:
            if str(e.get("company_name", "")).lower() == company.lower():
                e["status"] = "TERMINATED"
        _save_json(ENGAGEMENTS_DB_FILE, engs_list)

        # Notify business
        create_notification(
            recipient_role="business",
            company_name=company,
            title="Auditor Declined Engagement",
            message=f"{auditor_name} was unable to accept the statutory audit appointment for FY2025/26.",
            notif_type="warning",
            link="/auditor-review",
        )

    return {
        "success": True,
        "message": "Engagement invitation declined.",
        "status": "Declined",
        "invitation_id": invitation_id
    }


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
