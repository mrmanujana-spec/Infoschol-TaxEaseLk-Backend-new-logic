import os
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, cast

from fastapi import APIRouter, Header, HTTPException, status
from app.database import get_supabase_admin_client
from app.services.storage import storage_service
from app.schemas.discussions import (
    DiscussionMessageSchema,
    DiscussionThreadSchema,
    CreateDiscussionRequest,
    SendDiscussionMessageRequest,
    DiscussionsSummaryResponse,
)

router = APIRouter(prefix="/api", tags=["Discussions"])

DISCUSSIONS_DB_FILE = os.path.join(storage_service.uploads_dir, "discussions_db.json")

def _load_discussions() -> List[Dict[str, Any]]:
    if os.path.exists(DISCUSSIONS_DB_FILE):
        try:
            with open(DISCUSSIONS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def _save_discussions(threads: List[Dict[str, Any]]):
    try:
        with open(DISCUSSIONS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(threads, f, indent=2)
    except Exception as e:
        print(f"[DiscussionsDB] Failed to save discussions: {e}")

DEFAULT_DISCUSSIONS = []


def _sync_supabase_thread(thread: Dict[str, Any]):
    try:
        client = get_supabase_admin_client()
        client.table("discussion_threads").upsert({
            "id": thread["id"],
            "company_name": thread.get("companyName") or thread.get("company_name"),
            "topic": thread["topic"],
            "category": thread.get("category", "General Audit Inquiry"),
            "status": thread.get("status", "Open"),
            "last_message": thread.get("lastMessage", ""),
        }).execute()
    except Exception as e:
        print(f"[Supabase] Discussion thread sync note: {e}")

def _sync_supabase_message(thread_id: str, msg: Dict[str, Any]):
    try:
        client = get_supabase_admin_client()
        client.table("discussion_messages").upsert({
            "id": msg["id"],
            "thread_id": thread_id,
            "sender": msg["sender"],
            "sender_role": msg["senderRole"],
            "text": msg["text"],
        }).execute()
    except Exception as e:
        print(f"[Supabase] Discussion message sync note: {e}")

# --- Endpoints ---

@router.get("/business/discussions", response_model=DiscussionsSummaryResponse)
@router.get("/auditor/discussions", response_model=DiscussionsSummaryResponse)
def get_discussions(
    company_name: Optional[str] = None,
    authorization: Optional[str] = Header(None)
):
    """
    Returns discussion threads. If company_name is provided, filters for that company.
    If not provided (auditor view), returns all threads across companies.
    """
    # 1. Try reading from Supabase
    threads = []
    supabase_queried = False
    try:
        client = get_supabase_admin_client()
        if client:
            query = client.table("discussion_threads").select("*")
            if company_name:
                query = query.ilike("company_name", company_name.strip())
            res = query.order("last_updated", desc=True).execute()
            if res.data is not None:
                supabase_queried = True
                threads_data = cast(List[Dict[str, Any]], res.data)
                for t in threads_data:
                    # fetch messages
                    m_res = client.table("discussion_messages").select("*").eq("thread_id", t["id"]).order("created_at").execute()
                    messages_data = cast(List[Dict[str, Any]], m_res.data or [])
                    messages = [
                        {
                            "id": m["id"],
                            "sender": m["sender"],
                            "senderRole": m["sender_role"],
                            "text": m["text"],
                            "timestamp": datetime.fromisoformat(m["created_at"]).strftime("%d %b, %H:%M") if m.get("created_at") else "Recently"
                        }
                        for m in messages_data
                    ]
                    threads.append({
                        "id": t["id"],
                        "companyName": t["company_name"],
                        "company_name": t["company_name"],
                        "topic": t["topic"],
                        "category": t.get("category") or "General Audit Inquiry",
                        "lastMessage": t.get("last_message") or (messages[-1]["text"] if messages else ""),
                        "lastUpdated": datetime.fromisoformat(t["last_updated"]).strftime("%d %b, %H:%M") if t.get("last_updated") else "Recently",
                        "unreadCount": 0,
                        "status": t.get("status") or "Open",
                        "messages": messages
                    })
    except Exception as e:
        print(f"[Discussions] Query note: {e}")

    # 2. Local DB Fallback ONLY if Supabase is offline/unreachable
    if not supabase_queried:
        local_threads = _load_discussions()
        if not local_threads:
            _save_discussions(DEFAULT_DISCUSSIONS)
            threads = list(DEFAULT_DISCUSSIONS)
        else:
            threads = local_threads

        if company_name:
            target = company_name.strip().lower()
            threads = [t for t in threads if (t.get("companyName") or t.get("company_name", "")).lower() == target]

    models = [
        DiscussionThreadSchema(
            id=t["id"],
            companyName=t.get("companyName") or t.get("company_name") or "Company",
            company_name=t.get("companyName") or t.get("company_name"),
            topic=t["topic"],
            category=t.get("category") or "General Audit Inquiry",
            lastMessage=t.get("lastMessage") or (t["messages"][-1]["text"] if t.get("messages") else ""),
            lastUpdated=t.get("lastUpdated") or "Just now",
            unreadCount=t.get("unreadCount", 0),
            status=t.get("status", "Open"),
            messages=[DiscussionMessageSchema(**m) for m in t.get("messages", [])]
        )
        for t in threads
    ]

    return DiscussionsSummaryResponse(threads=models)

@router.post("/business/discussions", response_model=DiscussionThreadSchema)
def create_business_discussion(
    request: CreateDiscussionRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Creates a new discussion thread started by the business client.
    """
    company = request.company_name.strip() if request.company_name and request.company_name.strip() else ""
    now_str = datetime.now().strftime("%d %b, %H:%M")
    thread_id = f"disc_{int(time.time() * 1000)}"

    init_msg_text = request.initialMessage or request.initial_message or request.message or "Started a new discussion topic."
    initial_msg = {
        "id": f"m_{int(time.time() * 1000)}",
        "sender": "Admin User (You)",
        "senderRole": "Company",
        "text": init_msg_text,
        "timestamp": "Just now"
    }

    new_thread = {
        "id": thread_id,
        "companyName": company,
        "company_name": company,
        "topic": request.topic.strip(),
        "category": request.category or "General Audit Inquiry",
        "lastMessage": f"You: {init_msg_text}",
        "lastUpdated": "Just now",
        "unreadCount": 0,
        "status": "Open",
        "messages": [initial_msg]
    }

    # Save local
    all_threads = _load_discussions()
    if not all_threads:
        all_threads = list(DEFAULT_DISCUSSIONS)
    all_threads.insert(0, new_thread)
    _save_discussions(all_threads)

    # Sync Supabase
    _sync_supabase_thread(new_thread)
    _sync_supabase_message(thread_id, initial_msg)

    return DiscussionThreadSchema(
        id=new_thread["id"],
        companyName=company,
        company_name=company,
        topic=new_thread["topic"],
        category=new_thread["category"],
        lastMessage=new_thread["lastMessage"],
        lastUpdated=new_thread["lastUpdated"],
        unreadCount=0,
        status="Open",
        messages=[DiscussionMessageSchema(**initial_msg)]
    )

@router.post("/business/discussions/{thread_id}/messages", response_model=DiscussionMessageSchema)
def send_business_message(
    thread_id: str,
    request: SendDiscussionMessageRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Appends a message from the business company to an existing discussion thread.
    """
    msg_text = (request.message or request.text or "").strip()
    if not msg_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    sender_name = request.sender or "Admin User (You)"
    msg_id = f"m_{int(time.time() * 1000)}"
    new_msg = {
        "id": msg_id,
        "sender": sender_name,
        "senderRole": "Company",
        "text": msg_text,
        "timestamp": "Just now"
    }

    all_threads = _load_discussions()
    if not all_threads:
        all_threads = list(DEFAULT_DISCUSSIONS)

    matched = False
    for t in all_threads:
        if t["id"] == thread_id:
            t.setdefault("messages", []).append(new_msg)
            t["lastMessage"] = f"You: {msg_text}"
            t["lastUpdated"] = "Just now"
            matched = True
            _sync_supabase_thread(t)
            break

    if not matched:
        raise HTTPException(status_code=404, detail="Thread not found")

    _save_discussions(all_threads)
    _sync_supabase_message(thread_id, new_msg)

    return DiscussionMessageSchema(**new_msg)

@router.post("/auditor/discussions/{thread_id}/messages", response_model=DiscussionMessageSchema)
def send_auditor_message(
    thread_id: str,
    request: SendDiscussionMessageRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Appends an auditor reply to an existing discussion thread.
    """
    msg_text = (request.message or request.text or "").strip()
    if not msg_text:
        raise HTTPException(status_code=400, detail="Message text is required")

    sender_name = request.sender or "Professional Auditor"
    msg_id = f"m_{int(time.time() * 1000)}"
    new_msg = {
        "id": msg_id,
        "sender": sender_name,
        "senderRole": "Auditor",
        "text": msg_text,
        "timestamp": "Just now"
    }

    all_threads = _load_discussions()
    if not all_threads:
        all_threads = list(DEFAULT_DISCUSSIONS)

    matched = False
    for t in all_threads:
        if t["id"] == thread_id:
            t.setdefault("messages", []).append(new_msg)
            t["lastMessage"] = f"Auditor: {msg_text}"
            t["lastUpdated"] = "Just now"
            t["unreadCount"] = t.get("unreadCount", 0) + 1
            matched = True
            _sync_supabase_thread(t)
            break

    if not matched:
        raise HTTPException(status_code=404, detail="Thread not found")

    _save_discussions(all_threads)
    _sync_supabase_message(thread_id, new_msg)

    return DiscussionMessageSchema(**new_msg)

@router.post("/business/discussions/{thread_id}/resolve")
@router.post("/auditor/discussions/{thread_id}/resolve")
def resolve_discussion(
    thread_id: str,
    authorization: Optional[str] = Header(None)
):
    """
    Resolves and closes a discussion thread.
    """
    all_threads = _load_discussions()
    if not all_threads:
        all_threads = list(DEFAULT_DISCUSSIONS)

    matched = False
    for t in all_threads:
        if t["id"] == thread_id:
            t["status"] = "Closed"
            matched = True
            _sync_supabase_thread(t)
            break

    if not matched:
        raise HTTPException(status_code=404, detail="Thread not found")

    _save_discussions(all_threads)
    return {"success": True, "message": "Discussion topic marked as resolved"}
