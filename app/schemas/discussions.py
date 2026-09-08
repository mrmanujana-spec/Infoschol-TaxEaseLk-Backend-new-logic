from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class DiscussionMessageSchema(BaseModel):
    id: str
    sender: str
    senderRole: str
    text: str
    timestamp: str

class DiscussionThreadSchema(BaseModel):
    id: str
    companyName: str
    company_name: Optional[str] = None
    topic: str
    category: Optional[str] = "General Audit Inquiry"
    lastMessage: str
    lastUpdated: str
    unreadCount: int = 0
    status: str = "Open"
    messages: List[DiscussionMessageSchema] = []

class CreateDiscussionRequest(BaseModel):
    topic: str
    category: Optional[str] = "General Audit Inquiry"
    initialMessage: Optional[str] = None
    initial_message: Optional[str] = None
    message: Optional[str] = None
    company_name: Optional[str] = None

class SendDiscussionMessageRequest(BaseModel):
    message: Optional[str] = None
    text: Optional[str] = None
    sender: Optional[str] = None
    senderRole: Optional[str] = None

class DiscussionsSummaryResponse(BaseModel):
    threads: List[DiscussionThreadSchema]
