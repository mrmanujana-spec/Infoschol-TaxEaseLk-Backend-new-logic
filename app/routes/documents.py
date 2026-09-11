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
)

router = APIRouter(prefix="/api", tags=["Documents"])

DB_FILE = os.path.join(storage_service.uploads_dir, "documents_db.json")

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

@router.get("/dashboard")
def get_dashboard(authorization: Optional[str] = Header(None)):
    user_info = _get_user_info(authorization)
    company_name = user_info.get("company_name", "ABC (Pvt) Ltd")
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
    s4_pct = 60   # 3 of 5 auditor review points cleared
    s5_pct = 60   # In review

    composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)

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
                "state": "in_progress",
                "ratio_label": "3/5 Resolved",
                "sublabel": "2 open clarification point(s)"
            },
            "audit_sign_off": {
                "percent": s5_pct,
                "state": "in_progress",
                "ratio_label": "Under Review",
                "sublabel": "Awaiting final auditor confirmation"
            }
        },
        "metrics": {
            "documents_uploaded": uploaded_count,
            "documents_missing": max(0, 10 - uploaded_count),
            "accounting_profit": 4600000,
            "taxable_income": 26100000,
            "estimated_cit_liability": 7830000,
            "auditor_status": "under_review"
        }
    }


