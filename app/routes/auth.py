from fastapi import APIRouter, HTTPException, Header, status
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    AuthResponse,
    UserResponse,
    MessageResponse,
)
from app.database import get_supabase_client, get_supabase_admin_client

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

def normalize_role(role: str) -> str:
    role_upper = role.strip().upper()
    if role_upper in ["AUDITOR", "AUDITOR_PARTNER", "AUDITOR_STAFF"]:
        return "AUDITOR_PARTNER"
    if role_upper in ["BUSINESS", "COMPANY", "COMPANY_ADMIN", "BUSINESS_OWNER"]:
        return "COMPANY_ADMIN"
    return role_upper

@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest):
    """
    Register a new user in Supabase Auth and create their profile in profiles table.
    """
    client = get_supabase_client()
    admin_client = get_supabase_admin_client()

    role = normalize_role(req.role)

    try:
        # 1. Register with Supabase Auth
        res = client.auth.sign_up({
            "email": str(req.email),
            "password": req.password,
            "options": {
                "data": {
                    "display_name": req.display_name,
                    "role": role,
                    "category": req.category,
                    "specialization": req.specialization,
                }
            }
        })

        if not res.user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration failed. Unable to create user."
            )

        user_id = str(res.user.id)

        # 2. Insert or update record in public.profiles table using admin_client
        profile_data = {
            "id": user_id,
            "email": str(req.email),
            "display_name": req.display_name,
            "role": role,
            "category": req.category,
            "specialization": req.specialization,
        }

        try:
            admin_client.table("profiles").upsert(profile_data).execute()
        except Exception as profile_err:
            # Non-blocking if profiles trigger is already handling it
            print(f"Warning inserting profile: {profile_err}")

        # 3. Retrieve session access token
        access_token = ""
        if res.session and res.session.access_token:
            access_token = res.session.access_token
        else:
            # If email confirmation is disabled or auto-login is permitted
            try:
                login_res = client.auth.sign_in_with_password({
                    "email": str(req.email),
                    "password": req.password
                })
                if login_res.session:
                    access_token = login_res.session.access_token
            except Exception:
                # If email confirmation is required, provide user id token placeholder
                access_token = f"temp_token_{user_id}"

        return AuthResponse(
            access_token=access_token,
            token_type="bearer",
            user=UserResponse(
                id=user_id,
                email=str(req.email),
                display_name=req.display_name,
                role=role,
                category=req.category,
                specialization=req.specialization,
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        # Parse common Supabase auth messages
        if "User already registered" in err_msg or "already exists" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account with this email already exists."
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg
        )

@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest):
    """
    Authenticate user using Supabase Auth and return session token + user profile.
    """
    client = get_supabase_client()
    admin_client = get_supabase_admin_client()

    try:
        res = client.auth.sign_in_with_password({
            "email": str(req.email),
            "password": req.password
        })

        if not res.user or not res.session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password."
            )

        user_id = str(res.user.id)
        email = str(res.user.email)
        meta = res.user.user_metadata or {}
        display_name = meta.get("display_name", "")
        role = normalize_role(meta.get("role", "COMPANY_ADMIN"))
        category = meta.get("category", None)
        specialization = meta.get("specialization", None)

        # Query database profile for up-to-date role and details
        try:
            profile_query = admin_client.table("profiles").select("*").eq("id", user_id).execute()
            if profile_query.data and len(profile_query.data) > 0:
                p = profile_query.data[0]
                display_name = p.get("display_name") or display_name
                role = normalize_role(p.get("role") or role)
                category = p.get("category") or category
                specialization = p.get("specialization") or specialization
            else:
                # If profile row missing, create it
                admin_client.table("profiles").upsert({
                    "id": user_id,
                    "email": email,
                    "display_name": display_name,
                    "role": role,
                    "category": category,
                    "specialization": specialization
                }).execute()
        except Exception as pe:
            print(f"Notice reading profile: {pe}")

        return AuthResponse(
            access_token=res.session.access_token,
            token_type="bearer",
            user=UserResponse(
                id=user_id,
                email=email,
                display_name=display_name,
                role=role,
                category=category,
                specialization=specialization
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        if "Invalid login credentials" in err_msg or "invalid_grant" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password."
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg
        )

@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(req: ForgotPasswordRequest):
    """
    Send password reset instructions via Supabase Auth.
    """
    client = get_supabase_client()
    try:
        client.auth.reset_password_for_email(
            str(req.email),
            {"redirect_to": "http://localhost:3000/sign-in"}
        )
        return MessageResponse(
            message="Password reset instructions have been sent to your email."
        )
    except Exception as e:
        # Don't leak if email exists or not, but report if configured error
        err_msg = str(e)
        if "rate limit" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please wait a moment before trying again."
            )
        return MessageResponse(
            message="If an account exists for this email, password reset instructions have been sent."
        )

@router.post("/reset-password", response_model=MessageResponse)
def reset_password(req: ResetPasswordRequest, authorization: str = Header(None)):
    """
    Update password for authenticated user.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization token."
        )

    token = authorization.split(" ")[1]
    client = get_supabase_client()

    try:
        # Set session / update user with token
        client.auth.set_session(token, "")
        client.auth.update_user({"password": req.password})
        return MessageResponse(message="Password has been successfully updated.")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to reset password: {str(e)}"
        )

@router.get("/me", response_model=UserResponse)
def get_current_user(authorization: str = Header(None)):
    """
    Retrieve profile for current authenticated user.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization token."
        )

    token = authorization.split(" ")[1]
    client = get_supabase_client()
    admin_client = get_supabase_admin_client()

    try:
        user_res = client.auth.get_user(token)
        if not user_res or not user_res.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or invalid."
            )

        u = user_res.user
        user_id = str(u.id)
        email = str(u.email)
        meta = u.user_metadata or {}
        display_name = meta.get("display_name", "")
        role = normalize_role(meta.get("role", "COMPANY_ADMIN"))
        category = meta.get("category", None)
        specialization = meta.get("specialization", None)

        try:
            profile_query = admin_client.table("profiles").select("*").eq("id", user_id).execute()
            if profile_query.data and len(profile_query.data) > 0:
                p = profile_query.data[0]
                display_name = p.get("display_name") or display_name
                role = normalize_role(p.get("role") or role)
                category = p.get("category") or category
                specialization = p.get("specialization") or specialization
        except Exception:
            pass

        return UserResponse(
            id=user_id,
            email=email,
            display_name=display_name,
            role=role,
            category=category,
            specialization=specialization
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token."
        )
