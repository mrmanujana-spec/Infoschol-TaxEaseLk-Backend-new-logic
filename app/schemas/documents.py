from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class DocumentResponse(BaseModel):
    id: str
    name: str
    type: str
    status: str
    ai_confidence_percent: Optional[int] = None
    uploaded_date: str
    size_label: Optional[str] = None
    file_url: Optional[str] = None
    view_link: Optional[str] = None
    extracted_data: Optional[Dict[str, Any]] = None
    company_name: Optional[str] = None

class DocumentsSummaryResponse(BaseModel):
    uploaded_count: int
    processed_count: int
    review_required_count: int
    missing_count: int
    documents: List[DocumentResponse]

class SubmitToAuditorRequest(BaseModel):
    company_name: Optional[str] = None
    auditor_email: Optional[str] = None

class SubmitToAuditorResponse(BaseModel):
    success: bool
    message: str
    status: str
    shared_with: Optional[str] = None
    company_name: Optional[str] = None
    folder_link: Optional[str] = None
    submission_timestamp: str
