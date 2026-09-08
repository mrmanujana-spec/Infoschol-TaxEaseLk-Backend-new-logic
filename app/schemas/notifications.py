from pydantic import BaseModel
from typing import Optional, List

class NotificationSchema(BaseModel):
    id: str
    user_id: Optional[str] = None
    recipient_role: str  # "business" or "auditor"
    company_name: Optional[str] = None
    thread_id: Optional[str] = None
    title: str
    message: str
    type: str  # "critical", "warning", "info", "success"
    link: Optional[str] = None
    is_read: bool = False
    created_at: str

class NotificationsListResponse(BaseModel):
    notifications: List[NotificationSchema]
    unread_count: int

class MarkReadRequest(BaseModel):
    notification_id: Optional[str] = None
