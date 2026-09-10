from pydantic import BaseModel, EmailStr
from typing import Optional

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str
    role: str = "COMPANY_ADMIN"
    category: Optional[str] = None
    specialization: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    password: str

class UserResponse(BaseModel):
    id: str
    email: str
    display_name: Optional[str] = None
    role: Optional[str] = None
    category: Optional[str] = None
    specialization: Optional[str] = None

class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse

class MessageResponse(BaseModel):
    message: str
