import os
import json
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, cast
from fastapi import APIRouter, Header, HTTPException, Query
from app.config import settings
from app.database import get_supabase_admin_client
from app.services.storage import storage_service
from app.schemas.notifications import NotificationSchema, NotificationsListResponse, MarkReadRequest

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])

LOCAL_NOTIFICATIONS_PATH = os.path.join(storage_service.uploads_dir, "notifications_db.json")

def _load_notifications() -> List[Dict[str, Any]]:
    if os.path.exists(LOCAL_NOTIFICATIONS_PATH):
        try:
            with open(LOCAL_NOTIFICATIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def _save_notifications(notifications: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(LOCAL_NOTIFICATIONS_PATH), exist_ok=True)
    with open(LOCAL_NOTIFICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(notifications, f, indent=2, ensure_ascii=False)

DEFAULT_NOTIFICATIONS = []


@router.get("", response_model=NotificationsListResponse)
def get_notifications(
    role: str = Query("business", description="Role: 'business' or 'auditor'"),
    company_name: Optional[str] = Query(None, description="Optional company name filter"),
    authorization: Optional[str] = Header(None)
):
    target_role = role.strip().lower()
    
    # 1. Try Supabase
    items: List[Dict[str, Any]] = []
    try:
        client = get_supabase_admin_client()
        query = client.table("notifications").select("*").eq("recipient_role", target_role)
        if company_name and target_role == "business":
            query = query.ilike("company_name", company_name.strip())
        res = query.order("created_at", desc=True).execute()
        if res.data and len(res.data) > 0:
            items = cast(List[Dict[str, Any]], res.data)
    except Exception:
        pass

    # 2. Local Vault Fallback
    if not items:
        all_local = _load_notifications()
        if not all_local:
            _save_notifications(DEFAULT_NOTIFICATIONS)
            all_local = list(DEFAULT_NOTIFICATIONS)

        filtered = [n for n in all_local if n.get("recipient_role", "").lower() == target_role]
        if company_name and target_role == "business":
            target_comp = company_name.strip().lower()
            filtered = [
                n for n in filtered
                if not n.get("company_name") or n.get("company_name", "").lower() == target_comp
            ]
        items = filtered

    models = [
        NotificationSchema(
            id=str(item.get("id")),
            user_id=str(item.get("user_id")) if item.get("user_id") else None,
            recipient_role=item.get("recipient_role", target_role),
            company_name=item.get("company_name"),
            thread_id=str(item.get("thread_id")) if item.get("thread_id") else None,
            title=item.get("title", "Notification"),
            message=item.get("message", ""),
            type=item.get("type", "info"),
            link=item.get("link", "/dashboard" if target_role == "business" else "/auditor-dashboard"),
            is_read=bool(item.get("is_read", False)),
            created_at=str(item.get("created_at", "Just now"))
        )
        for item in items
    ]

    unread_count = sum(1 for m in models if not m.is_read)
    return NotificationsListResponse(notifications=models, unread_count=unread_count)

@router.post("/{notification_id}/read")
def mark_notification_as_read(
    notification_id: str,
    authorization: Optional[str] = Header(None)
):
    # Try Supabase
    try:
        client = get_supabase_admin_client()
        client.table("notifications").update({"is_read": True}).eq("id", notification_id).execute()
    except Exception:
        pass

    # Local fallback
    all_local = _load_notifications()
    updated = False
    for n in all_local:
        if str(n.get("id")) == str(notification_id):
            n["is_read"] = True
            updated = True
            break

    if updated:
        _save_notifications(all_local)

    return {"success": True, "id": notification_id, "is_read": True}

@router.post("/mark-all-read")
def mark_all_notifications_read(
    role: str = Query("business"),
    company_name: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None)
):
    target_role = role.strip().lower()

    # Try Supabase
    try:
        client = get_supabase_admin_client()
        query = client.table("notifications").update({"is_read": True}).eq("recipient_role", target_role)
        if company_name and target_role == "business":
            query = query.ilike("company_name", company_name.strip())
        query.execute()
    except Exception:
        pass

    # Local fallback
    all_local = _load_notifications()
    for n in all_local:
        if n.get("recipient_role", "").lower() == target_role:
            if not company_name or target_role != "business" or (n.get("company_name") or "").lower() == company_name.strip().lower():
                n["is_read"] = True

    _save_notifications(all_local)
    return {"success": True, "unread_count": 0}
