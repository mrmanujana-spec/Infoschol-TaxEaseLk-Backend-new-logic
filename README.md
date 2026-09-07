# TaxEaseLK — FastAPI Backend Logic

FastAPI backend integrated with Supabase Authentication and PostgreSQL database for TaxEaseLK.

## Features Built
- `POST /api/auth/register` — Registers new business owners or auditors in Supabase Auth and creates their profile in `public.profiles`.
- `POST /api/auth/login` — Authenticates email & password against Supabase Auth, returning JWT access token and user role.
- `POST /api/auth/forgot-password` — Triggers password reset email via Supabase Auth.
- `POST /api/auth/reset-password` — Updates password for authenticated user.
- `GET /api/auth/me` — Fetches profile and role of authenticated user.

## Quick Start

### 1. Activate the Virtual Environment
```bash
# In Windows PowerShell:
.\.venv\Scripts\Activate.ps1
```

### 2. Configure `.env`
Open `.env` and fill in your Supabase project values:
```env
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_ANON_KEY=your-supabase-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-supabase-service-role-key
PORT=8000
HOST=0.0.0.0
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

### 3. Run the Server
```bash
uvicorn app.main:app --reload --port 8000
```
API Documentation will be live at `http://localhost:8000/docs`.

## Supabase Setup
Run the SQL in `supabase_schema.sql` inside your Supabase SQL Editor.
