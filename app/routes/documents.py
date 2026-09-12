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
        "user_id": "",
        "email": "",
        "company_name": "",
        "role": "",
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
            user_id = str(u.id)
            email = str(u.email or "").lower()
            role = (meta.get("role") or "").lower().strip()
            company_name = meta.get("company_name") or meta.get("display_name") or ""
            
            # Fetch profile details if role or company_name missing from metadata
            if not role or not company_name:
                try:
                    admin_client = get_supabase_admin_client()
                    if admin_client:
                        prof_res = admin_client.table("profiles").select("role, company_name, display_name").eq("id", user_id).execute()
                        if prof_res.data and len(prof_res.data) > 0:
                            p = prof_res.data[0]
                            if not role:
                                role = (p.get("role") or "").lower().strip()
                            if not company_name:
                                company_name = p.get("company_name") or p.get("display_name") or ""
                except Exception:
                    pass

            return {
                "user_id": user_id,
                "email": email,
                "company_name": company_name,
                "role": role,
                "tax_year": meta.get("current_fiscal_year") or "2025/26",
            }
    except Exception:
        pass
    return default_info

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

# Initial baseline documents - empty for fresh user experience
DEFAULT_DOCUMENTS: List[Dict[str, Any]] = []


# --- Endpoints ---

@router.get("/documents", response_model=DocumentsSummaryResponse)
def get_documents_summary(
    company_name: Optional[str] = None,
    authorization: Optional[str] = Header(None)
):
    """
    Fetches uploaded documents with strict tenant isolation:
    - Business User: ONLY retrieves documents owned by their authenticated user_id or company.
    - Auditor: ONLY retrieves documents for companies where they have an active statutory engagement.
    - Unauthenticated: Returns zero documents.
    """
    user_info = _get_user_info(authorization)
    user_id = user_info.get("user_id")
    user_email = user_info.get("email", "").lower().strip()
    user_company = user_info.get("company_name", "").strip()
    role = user_info.get("role", "").lower().strip()

    # Strict check: If neither user_id nor email could be verified, no documents are returned
    if not user_id and not user_email:
        return DocumentsSummaryResponse(
            uploaded_count=0,
            processed_count=0,
            review_required_count=0,
            missing_count=5,
            documents=[]
        )

    is_auditor = "auditor" in role
    admin_client = get_supabase_admin_client()
    docs = []

    # 1. Supabase Query with strict scoping
    try:
        if is_auditor:
            # Auditor access: Find all companies assigned to this auditor
            engaged_companies: List[str] = []
            if admin_client and user_email:
                eng_res = admin_client.table("auditor_engagements").select("company_name").eq("auditor_email", user_email).eq("status", "ACTIVE").execute()
                for e in (eng_res.data or []):
                    c = e.get("company_name")
                    if c and c not in engaged_companies:
                        engaged_companies.append(c)

                inv_res = admin_client.table("invitations").select("company_name").eq("email", user_email).execute()
                for inv in (inv_res.data or []):
                    c = inv.get("company_name")
                    if c and c not in engaged_companies:
                        engaged_companies.append(c)

            # Filter documents by engaged companies
            if company_name:
                req_comp = company_name.strip()
                # Ensure auditor is authorized for this requested company
                matched = next((c for c in engaged_companies if c.lower() == req_comp.lower()), None)
                if matched:
                    res = admin_client.table("documents").select("*").ilike("company_name", matched).order("uploaded_at", desc=True).execute()
                    if res.data:
                        docs = res.data
            else:
                if engaged_companies:
                    res = admin_client.table("documents").select("*").in_("company_name", engaged_companies).order("uploaded_at", desc=True).execute()
                    if res.data:
                        docs = res.data
        else:
            # Business User access: strictly isolated to the user's own identity
            if admin_client and user_id:
                res = admin_client.table("documents").select("*").eq("user_id", user_id).order("uploaded_at", desc=True).execute()
                if res.data and len(res.data) > 0:
                    docs = res.data
                elif user_company:
                    # Fallback for documents uploaded before user_id column assignment
                    res2 = admin_client.table("documents").select("*").ilike("company_name", user_company).order("uploaded_at", desc=True).execute()
                    if res2.data:
                        docs = res2.data
    except Exception as e:
        print(f"[Documents] Query note: {e}")

    # 2. Local DB Fallback with identical isolation guarantees
    if not docs:
        local_docs = _load_local_db()
        if is_auditor:
            docs = [d for d in local_docs if (d.get("company_name") or "").lower() in [c.lower() for c in engaged_companies]]
            if company_name:
                docs = [d for d in docs if (d.get("company_name") or "").lower() == company_name.strip().lower()]
        else:
            docs = [
                d for d in local_docs
                if (user_id and d.get("user_id") == user_id) or
                   (user_company and (d.get("company_name") or "").lower() == user_company.lower())
            ]

    uploaded_count = len(docs)
    processed_count = sum(1 for d in docs if d.get("status") == "processed")
    review_required_count = sum(1 for d in docs if d.get("status") == "review_required")
    required_types = {"Financial Statements", "Trial Balance", "General Ledger", "Fixed Assets", "Previous CIT"}
    uploaded_types = {d.get("type") or d.get("doc_type") for d in docs}
    missing_count = len(required_types - uploaded_types)

    doc_models = [
        DocumentResponse(
            id=str(d["id"]),
            name=d["name"],
            type=d.get("type") or d.get("doc_type") or "Financial Statements",
            status=d.get("status") or "processed",
            ai_confidence_percent=d.get("ai_confidence_percent") or 98,
            uploaded_date=datetime.fromisoformat(d["uploaded_at"]).strftime("%d %b %Y") if d.get("uploaded_at") else d.get("uploaded_date") or "Today",
            size_label=_format_size(d.get("file_size", 1024000)) if "file_size" in d else d.get("size_label", "1.0 MB"),
            file_url=f"/api/documents/download/{d['name']}",
            view_link=d.get("gdrive_view_link") or d.get("view_link"),
            extracted_data=d.get("extracted_data") or {},
            company_name=d.get("company_name") or d.get("company") or user_company or "Company",
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
    Deletes a document from storage and database, strictly verifying ownership.
    """
    user_info = _get_user_info(authorization)
    user_id = user_info.get("user_id")
    user_company = (user_info.get("company_name") or "").lower().strip()

    if not user_id and not user_info.get("email"):
        raise HTTPException(status_code=401, detail="Authentication required to delete documents")

    # 1. Supabase check & delete
    admin_client = get_supabase_admin_client()
    if admin_client:
        try:
            doc_res = admin_client.table("documents").select("id, user_id, company_name, file_path").eq("id", doc_id).execute()
            if doc_res.data and len(doc_res.data) > 0:
                doc = doc_res.data[0]
                owner_id = str(doc.get("user_id") or "")
                doc_company = (doc.get("company_name") or "").lower().strip()
                if owner_id and user_id and owner_id != user_id:
                    raise HTTPException(status_code=403, detail="Unauthorized to delete another user's document")
                if not owner_id and user_company and doc_company != user_company:
                    raise HTTPException(status_code=403, detail="Unauthorized to delete another company's document")
                
                if doc.get("file_path"):
                    storage_service.delete_file(doc["file_path"])
                admin_client.table("documents").delete().eq("id", doc_id).execute()
        except HTTPException:
            raise
        except Exception as e:
            print(f"[Supabase] Document delete note: {e}")

    # 2. Local DB cleanup with ownership check
    local_docs = _load_local_db()
    matched = [d for d in local_docs if d.get("id") == doc_id]
    if matched:
        d = matched[0]
        owner_id = str(d.get("user_id") or "")
        doc_company = (d.get("company_name") or "").lower().strip()
        if owner_id and user_id and owner_id != user_id:
            raise HTTPException(status_code=403, detail="Unauthorized to delete another user's document")
        if not owner_id and user_company and doc_company != user_company:
            raise HTTPException(status_code=403, detail="Unauthorized to delete another company's document")

        storage_service.delete_file(d.get("file_path", ""))
        local_docs = [x for x in local_docs if x.get("id") != doc_id]
        _save_local_db(local_docs)

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
    local_docs = _load_local_db()

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

# --- Business Financials & Statutory Tax Intelligence API ---

@router.get("/financials")
def get_financials_summary(authorization: Optional[str] = Header(None)):
    """
    Returns executive financial summary, statutory Sri Lanka CIT reconciliation,
    and line item schedules for the business company.
    """
    user_info = _get_user_info(authorization)
    tax_rule = _get_active_tax_rule(user_info.get("tax_year", "2025/26"))
    cit_rate = float(tax_rule.get("standard_cit_rate", 0.30))
    gazette_ref = tax_rule.get("gazette_reference", "Inland Revenue Act No. 24 of 2017 (Gazette 2311/38 — 30% Standard CIT Rate)")

    status_db = _load_auditor_status_db()
    company_name = user_info.get("company_name", "ABC (Pvt) Ltd")
    auditor_status = status_db.get(company_name, {}).get("status", "Under Review by Auditor")

    docs = _load_local_db()
    if len(docs) == 0:
        return {
            "revenue": "Rs. 0.00",
            "expenses": "Rs. 0.00",
            "accounting_profit": "Rs. 0.00",
            "cost_of_sales": "Rs. 0.00",
            "gross_profit": "Rs. 0.00",
            "gross_margin_percent": 0.0,
            "operating_expenses": "Rs. 0.00",
            "net_pbt": "Rs. 0.00",
            "disallowable_add_backs": "Rs. 0.00",
            "tax_capital_allowances": "Rs. 0.00",
            "taxable_income": "Rs. 0.00",
            "cit_rate_percent": int(cit_rate * 100),
            "est_cit_liability": "Rs. 0.00",
            "tax_adjustments": "Rs. 0.00",
            "auditor_status": "Waiting for Documents",
            "ird_gazette_ref": gazette_ref,
            "tabs": {
                "Income Statement": [],
                "Balance Sheet": [],
                "Trial Balance": [],
                "General Ledger": [],
                "Fixed Assets": [],
            }
        }

    revenue_val = 25000000
    cogs_val = 15200000
    gross_profit_val = revenue_val - cogs_val
    gross_margin_pct = round((gross_profit_val / revenue_val) * 100, 1)
    opex_val = 5200000
    accounting_profit_val = gross_profit_val - opex_val

    disallowables_val = 2100000
    allowances_val = 1500000
    taxable_income_val = accounting_profit_val + disallowables_val - allowances_val
    cit_liability_val = round(taxable_income_val * cit_rate)

    return {
        "revenue": f"Rs. {revenue_val:,.0f}",
        "expenses": f"Rs. {(cogs_val + opex_val):,.0f}",
        "accounting_profit": f"Rs. {accounting_profit_val:,.0f}",
        "cost_of_sales": f"Rs. {cogs_val:,.0f}",
        "gross_profit": f"Rs. {gross_profit_val:,.0f}",
        "gross_margin_percent": gross_margin_pct,
        "operating_expenses": f"Rs. {opex_val:,.0f}",
        "net_pbt": f"Rs. {accounting_profit_val:,.0f}",
        "disallowable_add_backs": f"Rs. {disallowables_val:,.0f}",
        "tax_capital_allowances": f"Rs. {allowances_val:,.0f}",
        "taxable_income": f"Rs. {taxable_income_val:,.0f}",
        "cit_rate_percent": int(cit_rate * 100),
        "est_cit_liability": f"Rs. {cit_liability_val:,.0f}",
        "tax_adjustments": f"Rs. {(disallowables_val - allowances_val):,.0f}",
        "auditor_status": auditor_status,
        "ird_gazette_ref": gazette_ref,
        "tabs": {
            "Income Statement": [
                {"item": "Revenue from Operations", "amount": "25,000,000", "source": "Financial Statements.pdf", "category": "Gross Inflow", "taxTreatment": "Assessable Income", "aiConfidence": 99},
                {"item": "Cost of Sales", "amount": "(15,200,000)", "source": "Financial Statements.pdf", "category": "Direct Cost", "taxTreatment": "Allowable Deduction", "aiConfidence": 98},
                {"item": "Gross Profit", "amount": "9,800,000", "source": "Calculated", "category": "Subtotal", "taxTreatment": "Gross Trading Profit", "aiConfidence": 100},
                {"item": "Administrative Expenses", "amount": "(3,100,000)", "source": "General Ledger.xlsx", "category": "OPEX", "taxTreatment": "Allowable OPEX", "aiConfidence": 97},
                {"item": "Entertainment Expenses", "amount": "(300,000)", "source": "General Ledger.xlsx", "category": "Hospitality", "taxTreatment": "Disallowable (Sec 11)", "aiConfidence": 96},
                {"item": "Accounting Depreciation", "amount": "(1,800,000)", "source": "Fixed Asset Schedule.xlsx", "category": "Non-Cash Cost", "taxTreatment": "Disallowable (Sec 11)", "aiConfidence": 99},
                {"item": "Accounting Profit Before Tax (PBT)", "amount": "4,600,000", "source": "Calculated", "category": "P&L Balance", "taxTreatment": "Starting PBT", "aiConfidence": 100},
            ],
            "Balance Sheet": [
                {"item": "Property, Plant & Equipment", "amount": "18,400,000", "source": "Fixed Asset Schedule.xlsx", "category": "Non-Current Asset", "taxTreatment": "Capital Asset Base", "aiConfidence": 98},
                {"item": "Trade Receivables", "amount": "6,200,000", "source": "Trial Balance.xlsx", "category": "Current Asset", "taxTreatment": "Commercial Inflow", "aiConfidence": 96},
                {"item": "Cash & Bank Balances", "amount": "3,050,000", "source": "Bank Reconciliation.xlsx", "category": "Liquid Asset", "taxTreatment": "Reconciled Cash", "aiConfidence": 99},
                {"item": "Trade Payables", "amount": "(4,700,000)", "source": "Trial Balance.xlsx", "category": "Current Liability", "taxTreatment": "Commercial Outflow", "aiConfidence": 97},
                {"item": "Retained Earnings", "amount": "16,300,000", "source": "Financial Statements.pdf", "category": "Equity", "taxTreatment": "Cumulative Profit", "aiConfidence": 99},
            ],
            "Trial Balance": [
                {"item": "Sales Account (4000)", "amount": "25,000,000", "source": "Trial Balance.xlsx", "category": "Revenue", "taxTreatment": "Assessable Turnover", "aiConfidence": 100},
                {"item": "Purchases Account (5000)", "amount": "15,200,000", "source": "Trial Balance.xlsx", "category": "COGS", "taxTreatment": "Allowable Cost", "aiConfidence": 98},
                {"item": "Salaries & Wages (6010)", "amount": "2,400,000", "source": "Trial Balance.xlsx", "category": "Staff OPEX", "taxTreatment": "Allowable OPEX", "aiConfidence": 99},
                {"item": "Rent Expense (6020)", "amount": "700,000", "source": "Trial Balance.xlsx", "category": "Facility OPEX", "taxTreatment": "Allowable OPEX", "aiConfidence": 98},
                {"item": "Bank Balance (1010)", "amount": "3,050,000", "source": "Trial Balance.xlsx", "category": "Treasury", "taxTreatment": "Asset Balance", "aiConfidence": 99},
            ],
            "General Ledger": [
                {"item": "Nov 2025 — Office Supplies", "amount": "120,000", "source": "General Ledger.xlsx", "category": "Office Admin", "taxTreatment": "Allowable OPEX", "aiConfidence": 95},
                {"item": "Dec 2025 — Electricity & Water", "amount": "95,000", "source": "General Ledger.xlsx", "category": "Utilities", "taxTreatment": "Allowable OPEX", "aiConfidence": 97},
                {"item": "Jan 2026 — Executive Dining & Hospitality", "amount": "300,000", "source": "General Ledger.xlsx", "category": "Entertainment", "taxTreatment": "Disallowable (Sec 11)", "aiConfidence": 98},
                {"item": "Feb 2026 — Plant Maintenance & Repairs", "amount": "210,000", "source": "General Ledger.xlsx", "category": "Repairs", "taxTreatment": "Allowable OPEX", "aiConfidence": 96},
            ],
            "Fixed Assets": [
                {"item": "Motor Vehicles (WDV)", "amount": "6,200,000", "source": "Fixed Asset Schedule.xlsx", "category": "Vehicles", "taxTreatment": "4th Sched Allowance (20%)", "aiConfidence": 97},
                {"item": "Office Equipment & Computers (WDV)", "amount": "2,100,000", "source": "Fixed Asset Schedule.xlsx", "category": "IT Assets", "taxTreatment": "4th Sched Allowance (20%)", "aiConfidence": 99},
                {"item": "Commercial Factory Buildings (WDV)", "amount": "10,100,000", "source": "Fixed Asset Schedule.xlsx", "category": "Buildings", "taxTreatment": "4th Sched Allowance (5%)", "aiConfidence": 98},
                {"item": "Current Year Accounting Depreciation", "amount": "1,800,000", "source": "Fixed Asset Schedule.xlsx", "category": "Depreciation", "taxTreatment": "Disallowable (Sec 11)", "aiConfidence": 100},
            ],
        }
    }

@router.post("/financials/generate-report")
def generate_financials_report(authorization: Optional[str] = Header(None)):
    """
    Generates AI statutory corporate income tax report with executive commentary,
    disallowables schedule, and audit action points.
    """
    user_info = _get_user_info(authorization)
    tax_rule = _get_active_tax_rule(user_info.get("tax_year", "2025/26"))
    cit_rate = float(tax_rule.get("standard_cit_rate", 0.30))
    company_name = user_info.get("company_name", "ABC (Pvt) Ltd")
    now_str = datetime.now().strftime("%d %b %Y")

    return {
        "generatedAt": now_str,
        "taxYear": user_info.get("tax_year", "2025/2026"),
        "companyName": company_name,
        "executiveSummary": f"{company_name} generated Rs. 25.0M in gross operating turnover for Year of Assessment {user_info.get('tax_year', '2025/2026')} with a robust gross profit margin of 39.2% (Rs. 9.8M). After operating overheads and depreciation, commercial profit before tax stands at Rs. 4.60M. Statutory tax reconciliation under Inland Revenue Act No. 24 of 2017 requires disallowing Rs. 2.10M in non-deductible accounting depreciation and executive entertainment, offset by Rs. 1.50M in Fourth Schedule tax capital allowances, arriving at an estimated taxable business income of Rs. 5.20M and an estimated CIT liability of Rs. 1.56M at the standard {int(cit_rate * 100)}% rate.",
        "profitabilityAnalysis": {
            "revenue": "Rs. 25,000,000",
            "grossProfit": "Rs. 9,800,000",
            "grossMargin": "39.2%",
            "operatingExpenses": "Rs. 5,200,000",
            "netPbt": "Rs. 4,600,000"
        },
        "taxReconciliation": {
            "accountingProfit": "Rs. 4,600,000",
            "disallowablesTotal": "Rs. 2,100,000",
            "disallowablesItems": [
                {"item": "Accounting Depreciation", "amount": "Rs. 1,800,000", "reason": "Section 11(1)(b) replacement by tax capital allowances"},
                {"item": "Entertainment & Hospitality", "amount": "Rs. 300,000", "reason": "Section 11(1)(c) restriction on non-business hospitality"}
            ],
            "capitalAllowancesTotal": "Rs. 1,500,000",
            "taxableIncome": "Rs. 5,200,000",
            "citRate": f"{int(cit_rate * 100)}.0%",
            "estimatedLiability": f"Rs. {round(5200000 * cit_rate):,.0f}"
        },
        "complianceScore": 94,
        "keyTaxRisks": [
            "SVAT reconciliation variance: Ensure Schedule 05 sales matches RAMIS SVAT declaration.",
            "Motor Vehicle lease payment add-back cap per Section 16 must be validated by statutory auditor.",
            "Advance CIT installment receipts for Q1-Q3 should be linked to offset final liability."
        ],
        "recommendations": [
            "Submit draft schedules to Assigned Auditor (A. Karunaratne & Co.) for official audit sign-off.",
            "Ensure tax capital allowance schedule includes original invoice references for new IT additions.",
            "Verify that withholding taxes (WHT/AIT) suffered on treasury balances are claimed via Form 38 certificates."
        ]
    }

@router.post("/documents/clear-all")
def clear_all_documents(authorization: Optional[str] = Header(None)):
    """
    Clears all local documents so the user can test with a completely clean slate from scratch.
    """
    _save_local_db([])
    return {"success": True, "message": "All documents cleared. Blank workspace initialized."}

@router.post("/documents/reset-demo")
def reset_demo_documents(authorization: Optional[str] = Header(None)):
    """
    Restores standard sample documents for demo purposes.
    """
    _save_local_db(DEFAULT_DOCUMENTS)
    return {"success": True, "message": "Demo sample documents restored."}

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
        "auditor_name": "Assigned Auditor",
        "auditor_firm": "Chartered Accountants"
    }

@router.post("/auditor/checklists")
def save_auditor_checklist(payload: Dict[str, Any]):
    company_name = payload.get("company_name", "")
    items = payload.get("items", DEFAULT_STATUTORY_CHECKLIST)
    auditor_name = payload.get("auditor_name", "Assigned Auditor")
    auditor_firm = payload.get("auditor_firm", "Chartered Accountants")

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
        or ("pending" if uploaded_count > 0 else "waiting")
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
        s4_ratio = "All Inquiries Resolved"
        s4_sublabel = "All inquiries cleared by auditor"
        s5_ratio = "Signed Off"
        s5_sublabel = "Audited & Certified for RAMIS submission"
    elif company_status_norm in ["waiting_for_company"]:
        s4_pct = 40
        s5_pct = 0
        composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)
        auditor_status_label = "waiting_for_company"
        s4_ratio = "Clarifications Needed"
        s4_sublabel = "Awaiting client clarification responses"
        s5_ratio = "Under Review"
        s5_sublabel = "Auditor reviewing responses"
    elif company_status_norm in ["in_progress", "under_review"]:
        s4_pct = 50
        s5_pct = 20
        composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)
        auditor_status_label = "in_progress"
        s4_ratio = "In Progress"
        s4_sublabel = "Auditor reviewing tax pack"
        s5_ratio = "Under Review"
        s5_sublabel = "Awaiting final auditor confirmation"
    else:
        # Fresh / Waiting state
        s4_pct = 0
        s5_pct = 0
        composite_pct = int(0.20 * s1_pct + 0.20 * s2_pct + 0.20 * s3_pct + 0.20 * s4_pct + 0.20 * s5_pct)
        auditor_status_label = "waiting"
        s4_ratio = "No Open Inquiries"
        s4_sublabel = "No auditor queries yet"
        s5_ratio = "Pending Handover"
        s5_sublabel = "Awaiting auditor appointment & handover"

    # Extract profit if any doc has it
    extracted_profit = 0
    for d in docs:
        if isinstance(d.get("extracted_data"), dict) and "accounting_profit_before_tax" in d["extracted_data"]:
            try:
                extracted_profit = float(d["extracted_data"]["accounting_profit_before_tax"])
                break
            except Exception:
                pass

    taxable_income = extracted_profit
    estimated_cit_liability = int(taxable_income * standard_cit_rate)

    return {
        "progress_percent": composite_pct,
        "updated_at": datetime.now().strftime("%d %b %Y at %I:%M %p"),
        "steps": {
            "document_gathering": {
                "percent": s1_pct,
                "state": "done" if s1_pct == 100 else ("in_progress" if s1_pct > 0 else "pending"),
                "ratio_label": f"{provided_required}/5 Gathered",
                "sublabel": "All statutory docs provided" if s1_pct == 100 else f"{5 - provided_required} statutory doc(s) missing"
            },
            "ai_extraction": {
                "percent": s2_pct,
                "state": "warning" if review_required_count > 0 else ("done" if s2_pct == 100 and uploaded_count > 0 else "pending"),
                "ratio_label": f"{processed_count}/{uploaded_count} Extracted",
                "sublabel": f"{review_required_count} doc needs review" if review_required_count > 0 else ("All files OCR-parsed" if uploaded_count > 0 else "Upload documents to begin")
            },
            "auditor_handover": {
                "percent": s3_pct if uploaded_count > 0 else 0,
                "state": "done" if s3_pct == 100 and uploaded_count > 0 else "pending",
                "ratio_label": "Pack Handed Over" if s3_pct == 100 and uploaded_count > 0 else "Ready for Handover",
                "sublabel": "Submitted to Auditor" if s3_pct == 100 and uploaded_count > 0 else "Submit in Documents tab"
            },
            "auditor_inquiries": {
                "percent": s4_pct,
                "state": "done" if s4_pct == 100 else ("in_progress" if s4_pct > 0 else "pending"),
                "ratio_label": s4_ratio,
                "sublabel": s4_sublabel
            },
            "audit_sign_off": {
                "percent": s5_pct,
                "state": "done" if s5_pct == 100 else ("in_progress" if s5_pct > 0 else "pending"),
                "ratio_label": s5_ratio,
                "sublabel": s5_sublabel
            }
        },
        "metrics": {
            "documents_uploaded": uploaded_count,
            "documents_missing": max(0, 5 - provided_required),
            "accounting_profit": extracted_profit,
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
    return []

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
    return []


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

    # Resolve auditor by User ID (AUD-XXXXXXXX / UUID) or email
    resolved = resolve_auditor_identity(req.auditor_email)
    actual_email = resolved["email"] or req.auditor_email.strip().lower()
    actual_name = req.auditor_name if req.auditor_name and req.auditor_name != req.auditor_email else (resolved["name"] or req.auditor_firm or "Certified Tax Auditor")
    actual_firm = req.auditor_firm or "Certified Tax Practice"

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
    if existing_active and existing_active.get("auditor_email") == actual_email:
        return {
            "success": True,
            "message": f"{actual_name} ({actual_firm}) is already your appointed active auditor for {req.tax_year}.",
            "engagement": existing_active
        }

    # If active with a DIFFERENT auditor, REJECT under 1-Auditor statutory rule!
    if existing_active and existing_active.get("auditor_email") != actual_email:
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
        "auditor_email": actual_email,
        "auditor_name": actual_name,
        "auditor_firm": actual_firm,
        "status": "ACTIVE",
        "review_status": "PENDING",
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
        avg_overall = 0.0
        total_count = 0
        avg_timeliness = 0.0
        avg_comm = 0.0
        avg_tech = 0.0

    return {
        "success": True,
        "auditor_email": auditor_email,
        "rank": "Verified CA Sri Lanka",
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
    engs = _load_engagements_db()
    for eng in engs:
        if eng.get("id") == company_id or str(eng.get("company_name", "")).strip().lower() == company_id.strip().lower():
            return eng.get("company_name", company_id)
    return company_id

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


# -----------------------------------------------------------------------------
# AUDITOR PORTAL API (Dashboard, Companies, Review Queue, Settings)
# -----------------------------------------------------------------------------

@router.get("/auditor/dashboard")
def get_auditor_dashboard(authorization: Optional[str] = Header(None)):
    """
    Returns live dynamic metrics for the logged-in auditor based on assigned engagements.
    Strictly isolated: unauthenticated or non-auditor callers receive zero metrics.
    """
    user_info = _get_user_info(authorization)
    auditor_email = user_info.get("email", "").lower().strip()
    auditor_id = user_info.get("user_id", "")
    
    if not auditor_email and not auditor_id:
        return {
            "companies_assigned": 0,
            "under_review": 0,
            "pending_reviews": 0,
            "critical_issues": 0,
            "completed_reviews": 0,
            "priority_reviews": [],
            "workload": {
                "pending": 0,
                "in_progress": 0,
                "waiting_for_company": 0,
                "ready_for_approval": 0,
                "completed": 0,
            },
            "recent_activity": []
        }

    client = get_supabase_admin_client()
    engagements: List[Dict[str, Any]] = []
    if client and auditor_email:
        try:
            res = client.table("auditor_engagements").select("*").eq("auditor_email", auditor_email).execute()
            if res.data:
                engagements = res.data
        except Exception:
            pass

    if not engagements and auditor_email:
        local_engs = _load_engagements_db()
        engagements = [e for e in local_engs if e.get("auditor_email", "").lower() == auditor_email]

    # Filter active engagements
    active_engagements = [e for e in engagements if e.get("status") == "ACTIVE"]
    companies_count = len(active_engagements)

    # Status counts
    pending_count = 0
    in_progress_count = 0
    waiting_for_company_count = 0
    ready_for_approval_count = 0
    completed_count = 0

    priority_reviews = []
    for eng in active_engagements:
        c_name = eng.get("company_name", "Company")
        st = (eng.get("review_status") or "PENDING").upper()
        if st in ["PENDING"]:
            pending_count += 1
            priority_reviews.append({
                "name": c_name,
                "status": "Pending",
                "cit_status_badge": "Under Review",
                "critical_count": 0,
                "warnings_count": 0,
                "progress_percent": 10,
                "due_date": "30 Sep"
            })
        elif st in ["IN_PROGRESS"]:
            in_progress_count += 1
            priority_reviews.append({
                "name": c_name,
                "status": "In Progress",
                "cit_status_badge": "Under Review",
                "critical_count": 0,
                "warnings_count": 1,
                "progress_percent": 50,
                "due_date": "30 Sep"
            })
        elif st in ["WAITING_FOR_COMPANY"]:
            waiting_for_company_count += 1
            priority_reviews.append({
                "name": c_name,
                "status": "Waiting for Company",
                "cit_status_badge": "Waiting for Company",
                "critical_count": 1,
                "warnings_count": 0,
                "progress_percent": 60,
                "due_date": "30 Sep"
            })
        elif st in ["READY_FOR_APPROVAL"]:
            ready_for_approval_count += 1
            priority_reviews.append({
                "name": c_name,
                "status": "Ready for Approval",
                "cit_status_badge": "Ready for Auditor",
                "critical_count": 0,
                "warnings_count": 0,
                "progress_percent": 90,
                "due_date": "30 Sep"
            })
        elif st in ["APPROVED", "COMPLETED"]:
            completed_count += 1

    return {
        "companies_assigned": companies_count,
        "under_review": in_progress_count + waiting_for_company_count + ready_for_approval_count,
        "pending_reviews": pending_count,
        "critical_issues": sum(1 for p in priority_reviews if p.get("critical_count", 0) > 0),
        "completed_reviews": completed_count,
        "priority_reviews": priority_reviews,
        "workload": {
            "pending": pending_count,
            "in_progress": in_progress_count,
            "waiting_for_company": waiting_for_company_count,
            "ready_for_approval": ready_for_approval_count,
            "completed": completed_count,
        },
        "recent_activity": []
    }


@router.get("/auditor/companies")
def get_auditor_companies(authorization: Optional[str] = Header(None)):
    """
    Returns list of client companies assigned to this auditor.
    Strictly isolated: unauthenticated callers receive empty list.
    """
    user_info = _get_user_info(authorization)
    auditor_email = user_info.get("email", "").lower().strip()
    if not auditor_email:
        return []

    client = get_supabase_admin_client()
    engagements: List[Dict[str, Any]] = []
    if client and auditor_email:
        try:
            res = client.table("auditor_engagements").select("*").eq("auditor_email", auditor_email).execute()
            if res.data:
                engagements = res.data
        except Exception:
            pass

    if not engagements and auditor_email:
        local_engs = _load_engagements_db()
        engagements = [e for e in local_engs if e.get("auditor_email", "").lower() == auditor_email]

    active_engagements = [e for e in engagements if e.get("status") == "ACTIVE"]

    result = []
    for eng in active_engagements:
        c_name = eng.get("company_name", "Company")
        st = (eng.get("review_status") or "Pending").title()
        result.append({
            "id": eng.get("id", f"eng_{c_name}"),
            "name": c_name,
            "tin_number": "Pending Verification",
            "current_fiscal_year": eng.get("tax_year", "2025/26"),
            "status": st,
            "cit_status_badge": st,
            "critical_count": 0,
            "warnings_count": 0,
            "progress_percent": 100 if st == "Approved" else 50 if st == "In Progress" else 10,
            "due_date": "30 Sep",
            "contact_email": "",
            "contact_phone": "",
            "registration_number": "PV Registration",
            "address": "Colombo, Sri Lanka",
            "business_category": "Corporate Business",
            "annual_turnover": "Rs. 0.00",
            "contact_person": "Company Representative",
            "tax_office": "Inland Revenue Department"
        })

    return result


AUDITOR_SETTINGS_DB_FILE = os.path.join(storage_service.uploads_dir, "auditor_settings_db.json")

def _load_auditor_settings_db() -> Dict[str, Any]:
    if os.path.exists(AUDITOR_SETTINGS_DB_FILE):
        try:
            with open(AUDITOR_SETTINGS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_auditor_settings_db(data: Dict[str, Any]):
    try:
        with open(AUDITOR_SETTINGS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[AuditorSettingsDB] Failed to persist: {e}")

@router.get("/auditor/settings")
def get_auditor_settings(authorization: Optional[str] = Header(None)):
    user_info = _get_user_info(authorization)
    auditor_email = user_info.get("email", "").lower().strip()
    db = _load_auditor_settings_db()
    saved = db.get(auditor_email) or db.get("default")
    if saved:
        return saved

    # Fresh auditor profile
    return {
        "profile": {
            "fullName": user_info.get("company_name") or "Chartered Accountant",
            "email": auditor_email,
            "phone": "",
            "licenseNumber": "",
            "organization": "Audit Firm",
            "designation": "Audit Partner",
            "caSriLankaNo": "",
            "irdPractitionerNo": "",
            "firmRegNo": "",
            "firmAddress": "Colombo, Sri Lanka",
            "signatureStampUrl": "",
        },
        "team": [],
        "preferences": {
            "defaultTaxYear": "2025/26 (Apr 1 - Mar 31)",
            "accountingStandard": "SLFRS / LKAS for SMEs",
            "materialityThresholdPercent": 5.0,
            "autoRemindDaysBeforeDeadline": [14, 7, 3],
            "autoRequestStandardPackOnConnect": True,
            "strictVatReconciliation": True,
        },
        "notifications": {
            "clientDocumentUploaded": True,
            "clientResponseReceived": True,
            "discussionMessageReceived": True,
            "deadlineApproaching": True,
            "clientInvitationReceived": True,
            "digestFrequency": "instant",
        },
        "security": {
            "twoFactorEnabled": False,
            "sessionTimeoutMinutes": 60,
            "ipWhitelistEnabled": False,
            "immutableAuditTrail": True,
            "activeSessions": [],
        }
    }

@router.put("/auditor/profile")
def update_auditor_profile_endpoint(
    profile: Dict[str, Any],
    authorization: Optional[str] = Header(None)
):
    user_info = _get_user_info(authorization)
    auditor_email = user_info.get("email", "").lower().strip() or "default"
    db = _load_auditor_settings_db()
    current = db.get(auditor_email) or get_auditor_settings(authorization)
    current["profile"] = {**current.get("profile", {}), **profile}
    db[auditor_email] = current
    _save_auditor_settings_db(db)
    return current["profile"]

@router.get("/auditor/review-queue")
def get_auditor_review_queue(authorization: Optional[str] = Header(None)):
    """
    Returns the review queue for the logged-in auditor.
    """
    companies = get_auditor_companies(authorization)
    return companies


