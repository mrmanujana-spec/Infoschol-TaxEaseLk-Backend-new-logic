from supabase import create_client, Client
from app.config import settings
from fastapi import HTTPException

_supabase_client: Client | None = None
_supabase_admin_client: Client | None = None

def get_supabase_client() -> Client:
    """Returns standard Supabase client with Anon Key."""
    global _supabase_client
    if _supabase_client is None:
        if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY or "your-project-id" in settings.SUPABASE_URL:
            raise HTTPException(
                status_code=500,
                detail="Supabase credentials not configured. Please set SUPABASE_URL and SUPABASE_ANON_KEY in the backend .env file."
            )
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
    return _supabase_client

def get_supabase_admin_client() -> Client:
    """Returns Supabase admin client with Service Role Key (bypasses RLS)."""
    global _supabase_admin_client
    if _supabase_admin_client is None:
        key = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_ANON_KEY
        if not settings.SUPABASE_URL or not key or "your-project-id" in settings.SUPABASE_URL:
            raise HTTPException(
                status_code=500,
                detail="Supabase credentials not configured. Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the backend .env file."
            )
        _supabase_admin_client = create_client(settings.SUPABASE_URL, key)
    return _supabase_admin_client
