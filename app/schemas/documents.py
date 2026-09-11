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

class AuditorEngagementRequest(BaseModel):
    company_name: str
    auditor_email: str
    auditor_name: Optional[str] = "Mr. A. Karunaratne (FCA)"
    auditor_firm: Optional[str] = "Karunaratne & Associates"
    tax_year: Optional[str] = "2025/26"

class AuditorDisengageRequest(BaseModel):
    company_name: str
    tax_year: Optional[str] = "2025/26"
    reason: Optional[str] = None

class AuditorReviewRequest(BaseModel):
    auditor_email: str
    auditor_name: Optional[str] = "Mr. A. Karunaratne (FCA)"
    auditor_firm: Optional[str] = "Karunaratne & Associates"
    company_name: str
    tax_year: Optional[str] = "2025/26"
    rating: int
    timeliness_rating: Optional[int] = 5
    communication_rating: Optional[int] = 5
    technical_rating: Optional[int] = 5
    review_comment: Optional[str] = None
    client_reviewer_name: Optional[str] = "Finance Representative"

class TaxRuleModel(BaseModel):
    tax_year: str
    standard_cit_rate: float
    sin_tax_rate: float
    concession_rate: Optional[float] = 0.15
    capital_allowance_rates: Optional[Dict[str, float]] = None
    entertainment_disallowable_pct: Optional[float] = 1.00
    gazette_reference: Optional[str] = None
    notes: Optional[str] = None

class UpdateTaxRuleRequest(BaseModel):
    standard_cit_rate: Optional[float] = None
    sin_tax_rate: Optional[float] = None
    concession_rate: Optional[float] = None
    gazette_reference: Optional[str] = None
    notes: Optional[str] = None

class UpdateAuditorStatusRequest(BaseModel):
    company_name: Optional[str] = None
    new_status: str
    notes: Optional[str] = None
