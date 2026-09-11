import os
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, cast

from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse

from app.database import get_supabase_client, get_supabase_admin_client
from app.services.storage import storage_service
from app.services.document_parser import document_parser
from app.schemas.documents import (
    DocumentResponse,
    DocumentsSummaryResponse,
    SubmitToAuditorRequest,
    SubmitToAuditorResponse,
    AuditorEngagementRequest,
    AuditorDisengageRequest,
    AuditorReviewRequest,
    TaxRuleModel,
    UpdateTaxRuleRequest,
    UpdateAuditorStatusRequest,
)

router = APIRouter(prefix="/api", tags=["Documents"])

DB_FILE = os.path.join(storage_service.uploads_dir, "documents_db.json")
ENGAGEMENTS_DB_FILE = os.path.join(storage_service.uploads_dir, "engagements_db.json")
REVIEWS_DB_FILE = os.path.join(storage_service.uploads_dir, "reviews_db.json")
TAX_RULES_DB_FILE = os.path.join(storage_service.uploads_dir, "tax_rules_db.json")
AUDITOR_STATUS_DB_FILE = os.path.join(storage_service.uploads_dir, "auditor_status_db.json")

def _load_local_db() -> List[Dict[str, Any]]:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def _save_local_db(docs: List[Dict[str, Any]]):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(docs, f, indent=2)
    except Exception as e:
        print(f"[DocumentsDB] Failed to persist local documents db: {e}")

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def _get_user_info(authorization: Optional[str]) -> Dict[str, str]:
    default_info = {
        "user_id": "usr_default_company",
        "email": "admin@abc.lk",
        "company_name": "ABC (Pvt) Ltd",
        "tax_year": "2025/26",
    }
    if not authorization or not authorization.startswith("Bearer "):
        return default_info

    token = authorization.split(" ")[1]
    try:
        client = get_supabase_client()
        user_res = client.auth.get_user(token)
        if user_res and user_res.user:
            u = user_res.user
            meta = u.user_metadata or {}
            return {
                "user_id": str(u.id),
                "email": str(u.email),
                "company_name": meta.get("company_name") or meta.get("display_name") or "ABC (Pvt) Ltd",
                "tax_year": meta.get("current_fiscal_year") or "2025/26",
            }
    except Exception:
        pass
    return default_info

# Initial baseline documents to display on first run
DEFAULT_DOCUMENTS = [
    {
        "id": "doc_1",
        "name": "Financial Statements.pdf",
        "type": "Financial Statements",
        "status": "processed",
        "ai_confidence_percent": 99,
        "uploaded_date": "16 Aug 2026",
        "size_label": "4.2 MB",
        "file_path": "uploads/ABC (Pvt) Ltd/2025-26/Financial Statements.pdf",
        "view_link": "https://drive.google.com/file/d/gdrive_financial_statements/view",
        "company_name": "ABC Holdings (Pvt) Ltd",
        "extracted_data": {"accounting_profit_before_tax": 24500000.0, "revenue": 128500000.0}
    },
    {
        "id": "doc_2",
        "name": "Trial Balance.xlsx",
        "type": "Trial Balance",
        "status": "processed",
        "ai_confidence_percent": 99,
        "uploaded_date": "16 Aug 2026",
        "size_label": "1.8 MB",
        "file_path": "uploads/ABC (Pvt) Ltd/2025-26/Trial Balance.xlsx",
        "view_link": "https://drive.google.com/file/d/gdrive_trial_balance/view",
        "company_name": "ABC Holdings (Pvt) Ltd",
        "extracted_data": {"total_debits": 142580400.0, "total_credits": 142580400.0, "is_balanced": True}
    },
    {
        "id": "doc_3",
        "name": "General Ledger.xlsx",
        "type": "General Ledger",
        "status": "review_required",
        "ai_confidence_percent": 91,
        "uploaded_date": "16 Aug 2026",
        "size_label": "3.1 MB",
        "file_path": "uploads/ABC (Pvt) Ltd/2025-26/General Ledger.xlsx",
        "view_link": "https://drive.google.com/file/d/gdrive_general_ledger/view",
        "company_name": "ABC Holdings (Pvt) Ltd",
        "extracted_data": {"review_reasons": ["Discrepancy detected in November ledger balance"]}
    },
    {
        "id": "doc_4",
        "name": "Fixed Asset Schedule.xlsx",
        "type": "Fixed Assets",
        "status": "review_required",
        "ai_confidence_percent": 87,
        "uploaded_date": "16 Aug 2026",
        "size_label": "1.4 MB",
        "file_path": "uploads/ABC (Pvt) Ltd/2025-26/Fixed Asset Schedule.xlsx",
        "view_link": "https://drive.google.com/file/d/gdrive_fixed_assets/view",
        "company_name": "ABC Holdings (Pvt) Ltd",
        "extracted_data": {"review_reasons": ["Depreciation method consistency requires auditor confirmation"]}
    },
    {
        "id": "doc_5",
        "name": "Previous CIT Return.pdf",
        "type": "Previous CIT",
        "status": "processed",
        "ai_confidence_percent": 99,
        "uploaded_date": "16 Aug 2026",
        "size_label": "2.8 MB",
        "file_path": "uploads/ABC (Pvt) Ltd/2025-26/Previous CIT Return.pdf",
        "view_link": "https://drive.google.com/file/d/gdrive_previous_cit/view",
        "company_name": "ABC Holdings (Pvt) Ltd",
        "extracted_data": {"prior_year_loss_brought_forward": 1200000.0}
    },
]

# --- Endpoints ---

@router.get("/documents", response_model=DocumentsSummaryResponse)
def get_documents_summary(
    company_name: Optional[str] = None,
    authorization: Optional[str] = Header(None)
):
    """
    Fetches uploaded documents. If company_name is provided, filters for that company.
    Otherwise returns all documents (e.g. for the auditor view).
    """
    user_info = _get_user_info(authorization)
    
    # 1. Check Supabase 'documents' table
    admin_client = get_supabase_admin_client()
    docs = []
    try:
        query = admin_client.table("documents").select("*")
        if company_name:
            query = query.ilike("company_name", company_name.strip())
        res = query.order("uploaded_at", desc=True).execute()
        if res.data and len(res.data) > 0:
            docs_data = cast(List[Dict[str, Any]], res.data)
            docs = [
                {
                    "id": str(d["id"]),
                    "name": d["name"],
                    "type": d.get("doc_type") or "Financial Statements",
                    "status": d.get("status") or "processed",
                    "ai_confidence_percent": d.get("ai_confidence_percent") or 98,
                    "uploaded_date": datetime.fromisoformat(d["uploaded_at"]).strftime("%d %b %Y") if d.get("uploaded_at") else "Today",
                    "size_label": _format_size(d.get("file_size", 1024000)),
                    "file_path": d.get("file_path"),
                    "view_link": d.get("gdrive_view_link") or d.get("view_link"),
                    "extracted_data": d.get("extracted_data") or {},
                    "company_name": d.get("company_name") or "ABC Holdings (Pvt) Ltd",
                }
                for d in docs_data
            ]
    except Exception:
        pass

    # 2. If Supabase table was not populated, read local vault DB
    if not docs:
        local_docs = _load_local_db()
        if not local_docs:
            _save_local_db(DEFAULT_DOCUMENTS)
            docs = list(DEFAULT_DOCUMENTS)
        else:
            docs = local_docs

        if company_name:
            target_filter = company_name.strip().lower()
            filtered = [d for d in docs if (d.get("company_name") or "ABC Holdings (Pvt) Ltd").lower() == target_filter]
            docs = filtered

    uploaded_count = len(docs)
    processed_count = sum(1 for d in docs if d.get("status") == "processed")
    review_required_count = sum(1 for d in docs if d.get("status") == "review_required")
    missing_count = max(0, 10 - uploaded_count)

    doc_models = [
        DocumentResponse(
            id=d["id"],
            name=d["name"],
            type=d.get("type") or d.get("doc_type") or "Financial Statements",
            status=d.get("status") or "processed",
            ai_confidence_percent=d.get("ai_confidence_percent"),
            uploaded_date=d.get("uploaded_date") or "Today",
            size_label=d.get("size_label"),
            file_url=f"/api/documents/download/{d['name']}",
            view_link=d.get("view_link"),
            extracted_data=d.get("extracted_data"),
            company_name=d.get("company_name") or "ABC Holdings (Pvt) Ltd",
        )
        for d in docs
    ]

    return DocumentsSummaryResponse(
        uploaded_count=uploaded_count,
        processed_count=processed_count,
        review_required_count=review_required_count,
        missing_count=missing_count,
        documents=doc_models
    )

@router.post("/documents/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form("Financial Statements"),
    company_name: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None)
):
    """
    Accepts a document upload, stores it via Google Drive (or local vault fallback),
    runs the AI extraction/confidence parser, and registers it.
    """
    user_info = _get_user_info(authorization)
    target_company = company_name.strip() if company_name and company_name.strip() else user_info["company_name"]
    file_bytes = await file.read()
    filename = file.filename or "uploaded_document.pdf"
    content_type = file.content_type or "application/octet-stream"

    # 1. Store via Storage Service
    storage_res = storage_service.upload_file(
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
        company_name=target_company,
        tax_year=user_info["tax_year"],
        doc_type=doc_type
    )

    # 2. Parse content & compute AI confidence
    analysis = document_parser.analyze_document(
        filename=filename,
        file_bytes=file_bytes,
        declared_doc_type=doc_type
    )

    new_doc_id = f"doc_{int(time.time() * 1000)}"
    uploaded_date_str = datetime.now().strftime("%d %b %Y")
    size_label = _format_size(len(file_bytes))

    doc_record = {
        "id": new_doc_id,
        "name": filename,
        "type": analysis["doc_type"],
        "status": analysis["status"],
        "ai_confidence_percent": analysis["ai_confidence_percent"],
        "uploaded_date": uploaded_date_str,
        "size_label": size_label,
        "file_path": storage_res.file_path,
        "view_link": storage_res.view_link,
        "download_url": storage_res.download_url,
        "extracted_data": analysis["extracted_data"],
        "user_id": user_info["user_id"],
        "company_name": target_company
    }

    # 3. Persist to Supabase if table exists
    admin_client = get_supabase_admin_client()
    valid_uuid = user_info["user_id"] if "-" in user_info.get("user_id", "") else None
    try:
        admin_client.table("documents").insert({
            "id": new_doc_id,
            "user_id": valid_uuid,
            "company_name": target_company,
            "name": filename,
            "file_path": storage_res.file_path,
            "file_size": len(file_bytes),
            "doc_type": analysis["doc_type"],
            "status": analysis["status"],
            "ai_confidence_percent": analysis["ai_confidence_percent"],
            "extracted_data": analysis["extracted_data"],
            "gdrive_file_id": storage_res.file_id if storage_res.provider == "gdrive" else None,
            "gdrive_view_link": storage_res.view_link,
        }).execute()
    except Exception as e:
        print(f"[Supabase] Document insert note: {e}")

    # 4. Also persist to local vault DB
    local_docs = _load_local_db()
    local_docs.insert(0, doc_record)
    _save_local_db(local_docs)

    return DocumentResponse(
        id=new_doc_id,
        name=filename,
        type=analysis["doc_type"],
        status=analysis["status"],
        ai_confidence_percent=analysis["ai_confidence_percent"],
        uploaded_date=uploaded_date_str,
        size_label=size_label,
        file_url=storage_res.download_url,
        view_link=storage_res.view_link,
        extracted_data=analysis["extracted_data"],
        company_name=target_company
    )

@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, authorization: Optional[str] = Header(None)):
    """
    Deletes a document from storage and database.
    """
    # 1. Remove from local DB
    local_docs = _load_local_db()
    matched = [d for d in local_docs if d.get("id") == doc_id]
    if matched:
        storage_service.delete_file(matched[0].get("file_path", ""))
        local_docs = [d for d in local_docs if d.get("id") != doc_id]
        _save_local_db(local_docs)

    # 2. Remove from Supabase
    admin_client = get_supabase_admin_client()
    try:
        admin_client.table("documents").delete().eq("id", doc_id).execute()
    except Exception:
        pass

    return {"success": True, "message": "Document deleted successfully"}

@router.get("/documents/download/{filename}")
def download_document_by_name(filename: str):
    """
    Direct download stream for documents stored in the local vault.
    """
    local_docs = _load_local_db()
    matched = next((d for d in local_docs if d.get("name") == filename), None)
    if matched and matched.get("file_path"):
        full_path = os.path.join(storage_service.base_dir, matched["file_path"])
        if os.path.exists(full_path):
            return FileResponse(full_path, filename=filename)

    # Fallback to search in uploads dir
    for root, _, files in os.walk(storage_service.uploads_dir):
        if filename in files:
            return FileResponse(os.path.join(root, filename), filename=filename)

    raise HTTPException(status_code=404, detail="File not found")

@router.post("/financials/submit-to-auditor", response_model=SubmitToAuditorResponse)
@router.post("/documents/submit-to-auditor", response_model=SubmitToAuditorResponse)
def submit_to_auditor(
    request_data: Optional[SubmitToAuditorRequest] = None,
    authorization: Optional[str] = Header(None)
):
    """
    Organizes company documents, updates CIT status to Ready for Auditor,
    and shares the company folder with the auditor via Google Drive.
    """
    user_info = _get_user_info(authorization)
    target_company = (request_data.company_name.strip() if request_data and request_data.company_name and request_data.company_name.strip() else None) or user_info["company_name"]
    target_auditor = (request_data.auditor_email.strip() if request_data and request_data.auditor_email and request_data.auditor_email.strip() else None) or "audit@karunaratne.lk"

    # Share company folder with auditor
    share_result = storage_service.share_company_folder(
        company_name=target_company,
        tax_year=user_info["tax_year"],
        auditor_email=target_auditor
    )

    now_str = datetime.now().strftime("%d %b %Y at %I:%M %p")
    return SubmitToAuditorResponse(
        success=True,
        message=f"Document pack for {target_company} successfully organized and submitted to auditor {target_auditor}",
        status="Ready for Auditor",
        shared_with=target_auditor,
        company_name=target_company,
        folder_link=share_result.get("folder_link"),
        submission_timestamp=now_str
    )

@router.get("/documents/export-pack")
def export_audit_pack(authorization: Optional[str] = Header(None)):
    """
    Bundles all company documents into an in-memory ZIP archive for 1-click auditor download.
    """
    user_info = _get_user_info(authorization)
    local_docs = _load_local_db() or DEFAULT_DOCUMENTS

    zip_buffer = storage_service.bundle_audit_pack(
        company_name=user_info["company_name"],
        tax_year=user_info["tax_year"],
        documents=local_docs
    )

    clean_name = user_info["company_name"].replace(" ", "_")
    zip_filename = f"{clean_name}_CIT_Audit_Pack_2025-26.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
    )

# --- Auditor Document Checklist API ---
CHECKLISTS_DB_FILE = os.path.join(storage_service.uploads_dir, "checklists_db.json")

def _load_checklists_db() -> Dict[str, Any]:
    if os.path.exists(CHECKLISTS_DB_FILE):
        try:
            with open(CHECKLISTS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_checklists_db(data: Dict[str, Any]):
    try:
        with open(CHECKLISTS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ChecklistsDB] Failed to persist: {e}")

DEFAULT_STATUTORY_CHECKLIST = [
    {
        "id": "chk_fs",
        "name": "Financial Statements",
        "category": "Financial Statements",
        "description": "Audited Balance Sheet, Income Statement & Notes",
        "required": True
    },
    {
        "id": "chk_tb",
        "name": "Final Trial Balance",
        "category": "Trial Balance",
        "description": "Balanced debit/credit year-end closing trial balance",
        "required": True
    },
    {
        "id": "chk_gl",
        "name": "General Ledger Dump",
        "category": "General Ledger",
        "description": "Detailed ledger transactions for expense verification",
        "required": True
    },
    {
        "id": "chk_fa",
        "name": "Fixed Asset Schedule",
        "category": "Fixed Assets",
        "description": "Tax depreciation schedule & capital allowances",
        "required": True,
        "auditorNote": "Required for RAMIS capital allowance claims"
    },
    {
        "id": "chk_cit",
        "name": "Previous CIT Return",
        "category": "Previous CIT",
        "description": "Prior year assessment & tax losses brought forward",
        "required": True
    },
    {
        "id": "chk_bank",
        "name": "Bank Reconciliation",
        "category": "Bank Reconciliation",
        "description": "Year-end bank confirmation & reconciliation statements",
        "required": False
    }
]

@router.get("/checklists/{company_name}")
def get_company_checklist(company_name: str):
    db = _load_checklists_db()
    c_key = company_name.strip().lower()
    for stored_name, val in db.items():
        if stored_name.strip().lower() == c_key:
            return val
    return {
        "company_name": company_name,
        "items": DEFAULT_STATUTORY_CHECKLIST,
        "auditor_name": "Mr. A. Karunaratne (FCA)",
        "auditor_firm": "Karunaratne & Associates"
    }

@router.post("/auditor/checklists")
def save_auditor_checklist(payload: Dict[str, Any]):
    company_name = payload.get("company_name", "ABC Holdings (Pvt) Ltd")
    items = payload.get("items", DEFAULT_STATUTORY_CHECKLIST)
    auditor_name = payload.get("auditor_name", "Mr. A. Karunaratne (FCA)")
    auditor_firm = payload.get("auditor_firm", "Karunaratne & Associates")

    db = _load_checklists_db()
    db[company_name] = {
        "company_name": company_name,
        "items": items,
        "auditor_name": auditor_name,
        "auditor_firm": auditor_firm,
        "updated_at": datetime.now().isoformat()
    }
    _save_checklists_db(db)

    return {
        "success": True,
        "message": f"Checklist for {company_name} updated successfully with {len(items)} requirements.",
        "data": db[company_name]
    }

# -----------------------------------------------------------------------------
# DYNAMIC IRD TAX RULES & AUDITOR STATUS HELPERS
# -----------------------------------------------------------------------------

def _load_tax_rules_db() -> List[Dict[str, Any]]:
    if os.path.exists(TAX_RULES_DB_FILE):
        try:
            with open(TAX_RULES_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Statutory Sri Lanka IRD tax rules
    default_rules = [
        {
            "tax_year": "2024/25",
            "standard_cit_rate": 0.30,
            "sin_tax_rate": 0.40,
            "concession_rate": 0.15,
            "capital_allowance_rates": {
                "computers_software": 0.20,
                "plant_machinery": 0.20,
                "commercial_buildings": 0.05
            },
            "entertainment_disallowable_pct": 1.00,
            "gazette_reference": "Inland Revenue (Amendment) Act No. 45 of 2022",
            "notes": "Unified standard corporate tax rate 30%"
        },
        {
            "tax_year": "2025/26",
            "standard_cit_rate": 0.30,
            "sin_tax_rate": 0.40,
            "concession_rate": 0.15,
            "capital_allowance_rates": {
                "computers_software": 0.20,
                "plant_machinery": 0.20,
                "commercial_buildings": 0.05
            },
            "entertainment_disallowable_pct": 1.00,
            "gazette_reference": "Inland Revenue Act No. 24 of 2017 as amended",
            "notes": "Current statutory assessment year"
        },
        {
            "tax_year": "2026/27",
            "standard_cit_rate": 0.30,
            "sin_tax_rate": 0.40,
            "concession_rate": 0.15,
            "capital_allowance_rates": {
                "computers_software": 0.20,
                "plant_machinery": 0.20,
                "commercial_buildings": 0.05
            },
            "entertainment_disallowable_pct": 1.00,
            "gazette_reference": "Subject to upcoming National Budget Gazette",
            "notes": "Provisional rate setting"
        }
    ]
    _save_tax_rules_db(default_rules)
    return default_rules

def _save_tax_rules_db(rules: List[Dict[str, Any]]):
    try:
        with open(TAX_RULES_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2)
    except Exception as e:
        print(f"[TaxRulesDB] Failed to persist tax rules db: {e}")

def _get_active_tax_rule(tax_year: str = "2025/26") -> Dict[str, Any]:
    client = get_supabase_admin_client()
    if client:
        try:
            res = client.table("ird_tax_rules").select("*").eq("tax_year", tax_year).limit(1).execute()
            if res.data and len(res.data) > 0:
                return res.data[0]
        except Exception as e:
            print(f"[TaxRules] Supabase fetch error: {e}")

    rules = _load_tax_rules_db()
    for r in rules:
        if r.get("tax_year") == tax_year:
            return r
    return rules[1] if len(rules) > 1 else {"tax_year": tax_year, "standard_cit_rate": 0.30, "gazette_reference": "Current IRD Law"}

def _load_auditor_status_db() -> Dict[str, Any]:
    if os.path.exists(AUDITOR_STATUS_DB_FILE):
        try:
            with open(AUDITOR_STATUS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_auditor_status_db(data: Dict[str, Any]):
    try:
        with open(AUDITOR_STATUS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[AuditorStatusDB] Failed to persist: {e}")


@router.get("/dashboard")
def get_dashboard(authorization: Optional[str] = Header(None)):
    user_info = _get_user_info(authorization)
    company_name = user_info.get("company_name", "ABC (Pvt) Ltd")
    tax_year = user_info.get("tax_year", "2025/26")
    docs = _load_local_db() or DEFAULT_DOCUMENTS

    uploaded_count = len(docs)
    processed_count = sum(1 for d in docs if d.get("status") == "processed")
    review_required_count = sum(1 for d in docs if "review" in str(d.get("status", "")).lower())

    # Check statutory fulfillment
    required_keys = ["financial", "trial", "ledger", "asset", "cit"]
    provided_required = 0
    for rk in required_keys:
        if any(rk in (d.get("type", "") + d.get("name", "")).lower() for d in docs):
            provided_required += 1

    if provided_required == 0 and uploaded_count > 0:
        provided_required = min(5, max(1, uploaded_count - 2))

    s1_pct = min(100, int((provided_required / 5) * 100))
    s2_pct = min(100, int((processed_count / max(1, uploaded_count)) * 100))
    s3_pct = 100 if s1_pct >= 80 else 50  # Handover readiness

    # 1. Fetch active IRD statutory tax rule
    tax_rule = _get_active_tax_rule(tax_year)
    standard_cit_rate = float(tax_rule.get("standard_cit_rate", 0.30))

    # 2. Check current auditor status for company
    status_db = _load_auditor_status_db()
    company_status = (
        status_db.get(company_name)
        or status_db.get(company_name.lower())
        or status_db.get("co_1")
        or "in_progress"
    )

    client = get_supabase_admin_client()
    if client:
        try:
            eng_res = client.table("auditor_engagements").select("review_status,status").eq("company_name", company_name).eq("status", "ACTIVE").limit(1).execute()
            if eng_res.data and len(eng_res.data) > 0:
                db_st = eng_res.data[0].get("review_status")
                if db_st:
                    company_status = db_st.lower().replace(" ", "_")
        except Exception:
            pass

    company_status_norm = str(company_status).lower().replace(" ", "_")

    if company_status_norm in ["approved", "completed", "ready_for_approval"]:
        s4_pct = 100
        s5_pct = 100
        composite_pct = 100
        auditor_status_label = "approved"
        s4_ratio = "5/5 Resolved"
        s4_sublabel = "All inquiries cleared by auditor"
        s5_ratio = "Signed Off"
        s5_sublabel = "Audited & Certified for RAMIS submission"
    elif company_status_norm in ["waiting_for_company"]:
        s4_pct = 40
        s5_pct = 50
        composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)
        auditor_status_label = "waiting_for_company"
        s4_ratio = "2/5 Resolved"
        s4_sublabel = "Awaiting client clarification responses"
        s5_ratio = "In Progress"
        s5_sublabel = "Auditor reviewing responses"
    elif company_status_norm in ["pending"]:
        s4_pct = 20
        s5_pct = 20
        composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)
        auditor_status_label = "pending"
        s4_ratio = "Queued"
        s4_sublabel = "Initial auditor review pending"
        s5_ratio = "Queued"
        s5_sublabel = "Awaiting auditor pickup"
    else:
        s4_pct = 60
        s5_pct = 60
        composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)
        auditor_status_label = "in_progress"
        s4_ratio = "3/5 Resolved"
        s4_sublabel = "2 open clarification point(s)"
        s5_ratio = "Under Review"
        s5_sublabel = "Awaiting final auditor confirmation"

    taxable_income = 26100000
    estimated_cit_liability = int(taxable_income * standard_cit_rate)

    return {
        "progress_percent": composite_pct,
        "updated_at": datetime.now().strftime("%d %b %Y at %I:%M %p"),
        "steps": {
            "document_gathering": {
                "percent": s1_pct,
                "state": "done" if s1_pct == 100 else "in_progress",
                "ratio_label": f"{provided_required}/5 Gathered",
                "sublabel": "All statutory docs provided" if s1_pct == 100 else f"{5 - provided_required} statutory doc(s) missing"
            },
            "ai_extraction": {
                "percent": s2_pct,
                "state": "warning" if review_required_count > 0 else "done",
                "ratio_label": f"{processed_count}/{uploaded_count} Extracted",
                "sublabel": f"{review_required_count} doc needs review" if review_required_count > 0 else "All files OCR-parsed"
            },
            "auditor_handover": {
                "percent": s3_pct,
                "state": "done" if s3_pct == 100 else "in_progress",
                "ratio_label": "Pack Handed Over" if s3_pct == 100 else "Ready for Handover",
                "sublabel": "Submitted to Karunaratne & Assoc" if s3_pct == 100 else "Submit in Documents tab"
            },
            "auditor_inquiries": {
                "percent": s4_pct,
                "state": "done" if s4_pct == 100 else "in_progress",
                "ratio_label": s4_ratio,
                "sublabel": s4_sublabel
            },
            "audit_sign_off": {
                "percent": s5_pct,
                "state": "done" if s5_pct == 100 else "in_progress",
                "ratio_label": s5_ratio,
                "sublabel": s5_sublabel
            }
        },
        "metrics": {
            "documents_uploaded": uploaded_count,
            "documents_missing": max(0, 10 - uploaded_count),
            "accounting_profit": 4600000,
            "taxable_income": taxable_income,
            "standard_cit_rate": standard_cit_rate,
            "cit_rate_label": f"{int(standard_cit_rate * 100)}%",
            "estimated_cit_liability": estimated_cit_liability,
            "gazette_reference": tax_rule.get("gazette_reference", "Inland Revenue Act No. 24 of 2017"),
            "auditor_status": auditor_status_label
        }
    }


# -----------------------------------------------------------------------------
# AUDITOR ENGAGEMENT & REVIEW HELPERS
# -----------------------------------------------------------------------------

def _load_engagements_db() -> List[Dict[str, Any]]:
    if os.path.exists(ENGAGEMENTS_DB_FILE):
        try:
            with open(ENGAGEMENTS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    # Default seed engagement for demo
    default_seed = [
        {
            "id": "eng_seed_abc_karunaratne",
            "company_name": "ABC (Pvt) Ltd",
            "tax_year": "2025/26",
            "auditor_email": "audit@karunaratne.lk",
            "auditor_name": "Mr. A. Karunaratne (FCA)",
            "auditor_firm": "Karunaratne & Associates",
            "status": "ACTIVE",
            "appointed_date": "2025-04-01T09:00:00Z",
            "concluded_date": None,
            "created_at": "2025-04-01T09:00:00Z",
        }
    ]
    _save_engagements_db(default_seed)
    return default_seed

def _save_engagements_db(engs: List[Dict[str, Any]]):
    try:
        with open(ENGAGEMENTS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(engs, f, indent=2)
    except Exception as e:
        print(f"[EngagementsDB] Failed to persist engagements db: {e}")

def _load_reviews_db() -> List[Dict[str, Any]]:
    if os.path.exists(REVIEWS_DB_FILE):
        try:
            with open(REVIEWS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    # Default seed reviews for Karunaratne & Associates (48 client reviews, avg 4.9)
    default_reviews = [
        {
            "id": "rev_seed_1",
            "company_name": "Lanka Logistics PLC",
            "tax_year": "2024/25",
            "auditor_email": "audit@karunaratne.lk",
            "auditor_name": "Mr. A. Karunaratne (FCA)",
            "auditor_firm": "Karunaratne & Associates",
            "rating": 5,
            "timeliness_rating": 5,
            "communication_rating": 5,
            "technical_rating": 5,
            "review_comment": "Exceptional thoroughness during our statutory CIT audit. Resolved complex transfer pricing questions within 48 hours.",
            "client_reviewer_name": "Head of Finance",
            "created_at": "2025-02-14T10:00:00Z",
            "updated_at": "2025-02-14T10:00:00Z",
        },
        {
            "id": "rev_seed_2",
            "company_name": "Ceylon Retail Holdings",
            "tax_year": "2024/25",
            "auditor_email": "audit@karunaratne.lk",
            "auditor_name": "Mr. A. Karunaratne (FCA)",
            "auditor_firm": "Karunaratne & Associates",
            "rating": 5,
            "timeliness_rating": 5,
            "communication_rating": 4,
            "technical_rating": 5,
            "review_comment": "Seamless handover through the TaxEase portal. Document verification was completed ahead of RAMIS deadline.",
            "client_reviewer_name": "Chief Financial Officer",
            "created_at": "2025-05-20T14:30:00Z",
            "updated_at": "2025-05-20T14:30:00Z",
        },
        {
            "id": "rev_seed_3",
            "company_name": "Colombo Tech Ventures",
            "tax_year": "2024/25",
            "auditor_email": "audit@karunaratne.lk",
            "auditor_name": "Mr. A. Karunaratne (FCA)",
            "auditor_firm": "Karunaratne & Associates",
            "rating": 5,
            "timeliness_rating": 5,
            "communication_rating": 5,
            "technical_rating": 5,
            "review_comment": "Highly recommended for IT exporters looking for clear BOI tax holiday guidance and CIT schedules.",
            "client_reviewer_name": "Finance Director",
            "created_at": "2025-08-11T09:15:00Z",
            "updated_at": "2025-08-11T09:15:00Z",
        }
    ]
    _save_reviews_db(default_reviews)
    return default_reviews

def _save_reviews_db(revs: List[Dict[str, Any]]):
    try:
        with open(REVIEWS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(revs, f, indent=2)
    except Exception as e:
        print(f"[ReviewsDB] Failed to persist reviews db: {e}")


# -----------------------------------------------------------------------------
# AUDITOR ENGAGEMENT ENDPOINTS (Strict 1-Auditor Per Business Rule)
# -----------------------------------------------------------------------------

@router.get("/auditor-engagement/{company_name}")
def get_auditor_engagement(company_name: str, tax_year: str = "2025/26"):
    """
    Retrieve the active auditor engagement for a given business and tax year.
    Enforces the rule that a business may only have ONE active auditor.
    """
    client = get_supabase_admin_client()
    if client:
        try:
            res = client.table("auditor_engagements").select("*").eq("company_name", company_name).eq("tax_year", tax_year).eq("status", "ACTIVE").order("created_at", desc=True).limit(1).execute()
            if res.data and len(res.data) > 0:
                return {"success": True, "has_active_auditor": True, "engagement": res.data[0]}
        except Exception as e:
            print(f"[Engagements] Supabase query error: {e}")

    # Fallback to local DB
    engs = _load_engagements_db()
    active_matches = [
        e for e in engs
        if e.get("company_name", "").lower() == company_name.lower()
        and e.get("tax_year") == tax_year
        and e.get("status") == "ACTIVE"
    ]
    if active_matches:
        return {"success": True, "has_active_auditor": True, "engagement": active_matches[0]}

    return {"success": True, "has_active_auditor": False, "engagement": None}


@router.post("/auditor-engagement")
def appoint_auditor_engagement(req: AuditorEngagementRequest):
    """
    Appoint an auditor for a business.
    CRITICAL STATUTORY RULE: Under Sri Lankan Companies Act No. 7 & Inland Revenue Act No. 24,
    a company can have strictly ONE appointed active auditor/audit firm per tax year.
    If an active appointment already exists for a different auditor, this endpoint rejects with 400.
    """
    client = get_supabase_admin_client()

    # 1. Check existing active auditor in Supabase or local
    existing_active = None
    if client:
        try:
            res = client.table("auditor_engagements").select("*").eq("company_name", req.company_name).eq("tax_year", req.tax_year).eq("status", "ACTIVE").execute()
            if res.data and len(res.data) > 0:
                existing_active = res.data[0]
        except Exception as e:
            print(f"[Engagements] Supabase check error: {e}")

    if not existing_active:
        engs = _load_engagements_db()
        matches = [
            e for e in engs
            if e.get("company_name", "").lower() == req.company_name.lower()
            and e.get("tax_year") == req.tax_year
            and e.get("status") == "ACTIVE"
        ]
        if matches:
            existing_active = matches[0]

    # If already active with SAME auditor, return success idempotent
    if existing_active and existing_active.get("auditor_email") == req.auditor_email:
        return {
            "success": True,
            "message": f"{req.auditor_name} ({req.auditor_firm}) is already your appointed active auditor for {req.tax_year}.",
            "engagement": existing_active
        }

    # If active with a DIFFERENT auditor, REJECT under 1-Auditor statutory rule!
    if existing_active and existing_active.get("auditor_email") != req.auditor_email:
        curr_auditor = existing_active.get("auditor_name", "another auditor")
        curr_firm = existing_active.get("auditor_firm", "an audit firm")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Statutory Constraint: {req.company_name} already has an active appointed auditor: "
                f"{curr_auditor} ({curr_firm}) for tax year {req.tax_year}. "
                "Under Sri Lankan Companies Act No. 7 / Inland Revenue Act No. 24, a registered business "
                "can appoint strictly ONE auditor or audit firm at a time. "
                "Please conclude or disengage the existing auditor first."
            )
        )

    # 2. Appoint new auditor
    new_engagement = {
        "id": f"eng_{int(time.time() * 1000)}",
        "company_name": req.company_name,
        "tax_year": req.tax_year,
        "auditor_email": req.auditor_email,
        "auditor_name": req.auditor_name,
        "auditor_firm": req.auditor_firm,
        "status": "ACTIVE",
        "appointed_date": datetime.now().isoformat(),
        "concluded_date": None,
        "created_at": datetime.now().isoformat(),
    }

    if client:
        try:
            client.table("auditor_engagements").insert(new_engagement).execute()
        except Exception as e:
            print(f"[Engagements] Supabase insert error: {e}")

    # Persist local
    engs = _load_engagements_db()
    engs.insert(0, new_engagement)
    _save_engagements_db(engs)

    return {
        "success": True,
        "message": f"Successfully appointed {req.auditor_name} ({req.auditor_firm}) as statutory tax auditor for {req.tax_year}.",
        "engagement": new_engagement
    }


@router.post("/auditor-engagement/disengage")
def disengage_auditor_engagement(req: AuditorDisengageRequest):
    """
    Formally disengage or conclude the active auditor appointment for a company.
    Allows the company to subsequently appoint a new auditor or conclude the tax year engagement.
    """
    client = get_supabase_admin_client()
    now_iso = datetime.now().isoformat()

    updated_record = None
    if client:
        try:
            res = client.table("auditor_engagements").update({
                "status": "TERMINATED",
                "concluded_date": now_iso
            }).eq("company_name", req.company_name).eq("tax_year", req.tax_year).eq("status", "ACTIVE").execute()
            if res.data and len(res.data) > 0:
                updated_record = res.data[0]
        except Exception as e:
            print(f"[Engagements] Supabase disengage error: {e}")

    engs = _load_engagements_db()
    for e in engs:
        if (e.get("company_name", "").lower() == req.company_name.lower()
            and e.get("tax_year") == req.tax_year
            and e.get("status") == "ACTIVE"):
            e["status"] = "TERMINATED"
            e["concluded_date"] = now_iso
            updated_record = e

    _save_engagements_db(engs)

    if not updated_record:
        return {"success": False, "message": "No active auditor engagement found to disengage."}

    return {
        "success": True,
        "message": f"Auditor appointment concluded for tax year {req.tax_year}. You may now appoint a new statutory auditor.",
        "engagement": updated_record
    }


# -----------------------------------------------------------------------------
# AUDITOR REVIEWS & RATINGS ENDPOINTS
# -----------------------------------------------------------------------------

@router.get("/auditors/{auditor_email}/reviews")
def get_auditor_reviews(auditor_email: str):
    """
    Get aggregated reviews and rating statistics for an auditor, dynamically feeding
    the TopBar badge, reviews count, and star rating.
    """
    client = get_supabase_admin_client()
    reviews: List[Dict[str, Any]] = []

    if client:
        try:
            res = client.table("auditor_reviews").select("*").eq("auditor_email", auditor_email).order("created_at", desc=True).execute()
            if res.data:
                reviews = res.data
        except Exception as e:
            print(f"[Reviews] Supabase query error: {e}")

    if not reviews:
        all_local = _load_reviews_db()
        reviews = [r for r in all_local if r.get("auditor_email") == auditor_email]

    total_count = len(reviews)
    if total_count > 0:
        avg_overall = round(sum(r.get("rating", 5) for r in reviews) / total_count, 1)
        avg_timeliness = round(sum(r.get("timeliness_rating", 5) for r in reviews) / total_count, 1)
        avg_comm = round(sum(r.get("communication_rating", 5) for r in reviews) / total_count, 1)
        avg_tech = round(sum(r.get("technical_rating", 5) for r in reviews) / total_count, 1)
    else:
        avg_overall = 4.9
        total_count = 48
        avg_timeliness = 4.9
        avg_comm = 4.8
        avg_tech = 5.0

    return {
        "success": True,
        "auditor_email": auditor_email,
        "rank": "#1 Verified CA Sri Lanka",
        "average_rating": avg_overall,
        "total_reviews": total_count,
        "subcategories": {
            "timeliness": avg_timeliness,
            "communication": avg_comm,
            "technical_rigor": avg_tech,
        },
        "reviews": reviews
    }


@router.post("/auditors/rate")
def rate_auditor(req: AuditorReviewRequest):
    """
    Submit or update a rating & review for the assigned auditor from a verified business client.
    Updates auditor ratings in real-time.
    """
    if req.rating < 1 or req.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5 stars.")

    client = get_supabase_admin_client()
    now_iso = datetime.now().isoformat()

    review_item = {
        "id": f"rev_{int(time.time() * 1000)}",
        "company_name": req.company_name,
        "tax_year": req.tax_year,
        "auditor_email": req.auditor_email,
        "auditor_name": req.auditor_name,
        "auditor_firm": req.auditor_firm,
        "rating": req.rating,
        "timeliness_rating": req.timeliness_rating or 5,
        "communication_rating": req.communication_rating or 5,
        "technical_rating": req.technical_rating or 5,
        "review_comment": req.review_comment or "",
        "client_reviewer_name": req.client_reviewer_name or "Finance Representative",
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    if client:
        try:
            # Upsert into auditor_reviews
            client.table("auditor_reviews").upsert(review_item, on_conflict="company_name,auditor_email,tax_year").execute()
        except Exception as e:
            print(f"[Reviews] Supabase upsert error: {e}")

    # Local fallback
    revs = _load_reviews_db()
    # Replace existing if same company, auditor, tax_year
    replaced = False
    for idx, r in enumerate(revs):
        if (r.get("company_name", "").lower() == req.company_name.lower()
            and r.get("auditor_email") == req.auditor_email
            and r.get("tax_year") == req.tax_year):
            revs[idx] = review_item
            replaced = True
            break
    if not replaced:
        revs.insert(0, review_item)
    _save_reviews_db(revs)

    # Compute updated stats
    auditor_revs = [r for r in revs if r.get("auditor_email") == req.auditor_email]
    total_count = len(auditor_revs)
    avg_overall = round(sum(r.get("rating", 5) for r in auditor_revs) / max(1, total_count), 1)

    return {
        "success": True,
        "message": f"Thank you! Your {req.rating}★ review for {req.auditor_name} has been submitted.",
        "review": review_item,
        "updated_stats": {
            "average_rating": avg_overall,
            "total_reviews": total_count,
        }
    }


# -----------------------------------------------------------------------------
# DYNAMIC IRD TAX RULES ENDPOINTS
# -----------------------------------------------------------------------------

@router.get("/tax-rules/current")
def get_current_tax_rule(tax_year: str = "2025/26"):
    """
    Get the active statutory IRD Corporate Income Tax rules for the given tax year.
    Enables dynamic tax calculations adhering to the latest Sri Lankan tax gazettes.
    """
    rule = _get_active_tax_rule(tax_year)
    return {"success": True, "data": rule}


@router.get("/tax-rules")
def get_all_tax_rules():
    """
    List all configured statutory tax years and corporate tax rates.
    """
    client = get_supabase_admin_client()
    if client:
        try:
            res = client.table("ird_tax_rules").select("*").order("tax_year", desc=True).execute()
            if res.data and len(res.data) > 0:
                return {"success": True, "data": res.data}
        except Exception as e:
            print(f"[TaxRules] Supabase get all error: {e}")

    return {"success": True, "data": _load_tax_rules_db()}


@router.patch("/tax-rules/{tax_year}")
def update_tax_rule(tax_year: str, req: UpdateTaxRuleRequest):
    """
    Update the statutory tax rate or gazette reference for a specific assessment year.
    Allows instant updates when the Inland Revenue Department publishes an amendment.
    """
    client = get_supabase_admin_client()
    now_iso = datetime.now().isoformat()
    
    update_data: Dict[str, Any] = {"updated_at": now_iso}
    if req.standard_cit_rate is not None:
        update_data["standard_cit_rate"] = req.standard_cit_rate
    if req.sin_tax_rate is not None:
        update_data["sin_tax_rate"] = req.sin_tax_rate
    if req.concession_rate is not None:
        update_data["concession_rate"] = req.concession_rate
    if req.gazette_reference is not None:
        update_data["gazette_reference"] = req.gazette_reference
    if req.notes is not None:
        update_data["notes"] = req.notes

    # Update in Supabase
    if client:
        try:
            client.table("ird_tax_rules").upsert({
                "tax_year": tax_year,
                **update_data
            }, on_conflict="tax_year").execute()
        except Exception as e:
            print(f"[TaxRules] Supabase update error: {e}")

    # Update local fallback
    rules = _load_tax_rules_db()
    found = False
    for r in rules:
        if r.get("tax_year") == tax_year:
            r.update(update_data)
            found = True
            break
    if not found:
        new_entry = {
            "tax_year": tax_year,
            "standard_cit_rate": req.standard_cit_rate if req.standard_cit_rate is not None else 0.30,
            "sin_tax_rate": req.sin_tax_rate if req.sin_tax_rate is not None else 0.40,
            "concession_rate": req.concession_rate if req.concession_rate is not None else 0.15,
            "gazette_reference": req.gazette_reference or "Inland Revenue Gazette",
            "notes": req.notes or "",
            "created_at": now_iso,
            "updated_at": now_iso
        }
        rules.append(new_entry)
    _save_tax_rules_db(rules)

    return {
        "success": True,
        "message": f"Statutory IRD tax rules for assessment year {tax_year} updated successfully.",
        "data": _get_active_tax_rule(tax_year)
    }


# -----------------------------------------------------------------------------
# AUDITOR STATUS UPDATE ENDPOINTS (Review Queue Lifecycle)
# -----------------------------------------------------------------------------

def _resolve_company_name_from_id(company_id: str) -> str:
    mapping = {
        "co_1": "ABC Holdings (Pvt) Ltd",
        "co_2": "Lanka Trading (Pvt) Ltd",
        "co_3": "Ocean Foods (Pvt) Ltd",
        "co_4": "Ceylon Retail Holdings",
        "co_5": "Colombo Tech Ventures"
    }
    return mapping.get(company_id, company_id)

@router.patch("/auditor/review-queue/{company_id}/status")
@router.post("/auditor/review-queue/{company_id}/status")
def update_company_audit_status(
    company_id: str,
    new_status: str,
    req_body: Optional[UpdateAuditorStatusRequest] = None
):
    """
    Auditor updates client company statutory review status.
    Transitions: 'Pending' -> 'In Progress' -> 'Waiting for Company' -> 'Ready for Approval' -> 'Approved'.
    Instantly reflects on Business Dashboard and Assigned Auditor card.
    """
    status_val = new_status or (req_body.new_status if req_body else "In Progress")
    company_name = _resolve_company_name_from_id(company_id)
    now_iso = datetime.now().isoformat()

    # 1. Update local auditor status DB
    status_db = _load_auditor_status_db()
    status_db[company_id] = status_val
    status_db[company_name] = status_val
    status_db[company_name.lower()] = status_val
    _save_auditor_status_db(status_db)

    # 2. Update Supabase auditor_engagements
    client = get_supabase_admin_client()
    if client:
        try:
            client.table("auditor_engagements").update({
                "review_status": status_val.upper().replace(" ", "_"),
            }).eq("company_name", company_name).eq("status", "ACTIVE").execute()
        except Exception as e:
            print(f"[AuditorStatus] Supabase engagement status update error: {e}")

        # 3. Create Notification for business client
        try:
            title_map = {
                "Approved": "CIT Return Approved & Signed Off",
                "Ready for Approval": "Audit Review Ready for Sign-Off",
                "In Progress": "Auditor Commenced Review",
                "Waiting for Company": "Action Required: Auditor Raised Inquiries",
            }
            msg_title = title_map.get(status_val, f"Audit Status Updated: {status_val}")
            client.table("notifications").insert({
                "id": f"notif_{int(time.time() * 1000)}",
                "recipient_role": "business",
                "company_name": company_name,
                "title": msg_title,
                "message": f"Your appointed auditor has updated your Corporate Income Tax status to '{status_val}'.",
                "type": "success" if status_val == "Approved" else "info",
                "link": "/auditor-review",
                "created_at": now_iso
            }).execute()
        except Exception as e:
            print(f"[AuditorStatus] Notification creation note: {e}")

    return {
        "success": True,
        "company_id": company_id,
        "company_name": company_name,
        "new_status": status_val,
        "message": f"Company status successfully transitioned to '{status_val}'.",
        "updated_at": now_iso
    }


