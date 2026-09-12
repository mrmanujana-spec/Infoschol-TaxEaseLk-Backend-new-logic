from pydantic import BaseModel
from typing import Optional, Dict, Any, List

class AuditorInviteRequest(BaseModel):
    email: str
    firmName: Optional[str] = None
    firm_name: Optional[str] = None
    auditorName: Optional[str] = None
    auditor_name: Optional[str] = None
    company_name: Optional[str] = None

class AuditorInviteResponse(BaseModel):
    success: bool
    message: str
    invitation_id: str
    status: str
    assigned_auditor: Dict[str, Any]

class AssignedAuditorResponse(BaseModel):
    company_name: str
    has_assigned_auditor: bool
    auditor: Optional[Dict[str, Any]] = None

class TeamInviteRequest(BaseModel):
    name: str
    email: str
    role: str
    can_sign_returns: bool = False
    canSignReturns: Optional[bool] = None
    company_name: Optional[str] = None

class TeamMemberResponse(BaseModel):
    id: str
    name: str
    initials: str
    email: str
    role: str
    status: str
    lastActive: str
    canSignReturns: bool

class TeamListResponse(BaseModel):
    team: List[TeamMemberResponse]

class ClientInvitationItem(BaseModel):
    id: str
    company_name: str
    email: str
    firm_name: Optional[str] = None
    auditor_name: Optional[str] = None
    tax_year: Optional[str] = "2025/26"
    status: str
    created_at: str

class ClientInvitationsListResponse(BaseModel):
    invitations: List[ClientInvitationItem]
    total_count: int
    pending_count: int
