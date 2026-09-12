import os
import io
import re
import zipfile
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

@dataclass
class StorageResult:
    provider: str  # "supabase" or "local"
    file_id: str
    file_path: str
    view_link: str
    download_url: str
    size: int

class StorageService:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.uploads_dir = os.path.join(self.base_dir, "uploads")
        os.makedirs(self.uploads_dir, exist_ok=True)
        
        # Supabase Storage Bucket configuration (default: tax-documents)
        self.bucket_name = os.environ.get("SUPABASE_STORAGE_BUCKET", "tax-documents")
        print(f"[StorageService] Primary storage target: Supabase Storage bucket '{self.bucket_name}'")

    def _sanitize(self, name: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_\-\. ]', '_', name).strip()

    def _get_admin_client(self):
        try:
            from app.database import get_supabase_admin_client
            return get_supabase_admin_client()
        except Exception as e:
            print(f"[StorageService] Failed to load Supabase admin client: {e}")
            return None

    # --- Public Storage Operations ---

    def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        company_name: str = "ABC (Pvt) Ltd",
        tax_year: str = "2025/26",
        doc_type: str = "Financial Statements"
    ) -> StorageResult:
        clean_company = self._sanitize(company_name) or "Company"
        clean_tax_year = self._sanitize(tax_year.replace("/", "-"))
        clean_filename = self._sanitize(filename)

        # 1. Primary: Supabase Storage Bucket
        admin_client = self._get_admin_client()
        if admin_client:
            try:
                storage_path = f"{clean_company}/{clean_tax_year}/{clean_filename}"
                admin_client.storage.from_(self.bucket_name).upload(
                    path=storage_path,
                    file=file_bytes,
                    file_options={"content-type": content_type, "upsert": "true"}
                )
                public_url = admin_client.storage.from_(self.bucket_name).get_public_url(storage_path)

                print(f"[StorageService] Successfully uploaded to Supabase Storage: {storage_path}")
                return StorageResult(
                    provider="supabase",
                    file_id=storage_path,
                    file_path=storage_path,
                    view_link=public_url,
                    download_url=public_url,
                    size=len(file_bytes)
                )
            except Exception as e:
                print(f"[StorageService] Supabase Storage upload failed: {e}. Falling back to local vault.")

        # 2. Fallback: Local Vault Upload
        company_vault_dir = os.path.join(self.uploads_dir, clean_company, clean_tax_year)
        os.makedirs(company_vault_dir, exist_ok=True)

        target_file_path = os.path.join(company_vault_dir, clean_filename)
        with open(target_file_path, "wb") as f:
            f.write(file_bytes)

        rel_path = os.path.relpath(target_file_path, self.base_dir).replace("\\", "/")
        download_url = f"/api/documents/download/{clean_filename}"

        return StorageResult(
            provider="local",
            file_id=f"local_{clean_filename}",
            file_path=rel_path,
            view_link=download_url,
            download_url=download_url,
            size=len(file_bytes)
        )

    def download_file_bytes(self, file_path_or_id: str) -> Optional[bytes]:
        """
        Retrieves raw bytes of a file from Supabase Storage or local disk.
        """
        if not file_path_or_id:
            return None

        # 1. Try Supabase Storage
        admin_client = self._get_admin_client()
        if admin_client and not file_path_or_id.startswith("uploads/"):
            try:
                data = admin_client.storage.from_(self.bucket_name).download(file_path_or_id)
                if data:
                    return data
            except Exception:
                pass

        # 2. Try local disk
        clean_path = file_path_or_id.replace("/", os.sep)
        full_path = os.path.join(self.base_dir, clean_path)
        if os.path.exists(full_path) and os.path.isfile(full_path):
            try:
                with open(full_path, "rb") as f:
                    return f.read()
            except Exception:
                pass

        return None

    def delete_file(self, file_path_or_id: str) -> bool:
        if not file_path_or_id:
            return False

        import urllib.parse
        raw = urllib.parse.unquote(str(file_path_or_id).replace("\\", "/"))

        # Extract bucket-relative path
        cleaned = raw
        if "supabase.co/storage/v1/object/public/" in cleaned:
            cleaned = cleaned.split("supabase.co/storage/v1/object/public/", 1)[1]
        if f"{self.bucket_name}/" in cleaned:
            cleaned = cleaned.split(f"{self.bucket_name}/", 1)[1]
        if cleaned.startswith("uploads/"):
            cleaned = cleaned.replace("uploads/", "", 1)
        cleaned = cleaned.lstrip("/")

        deleted = False
        # 1. Try Supabase Storage deletion
        admin_client = self._get_admin_client()
        if admin_client:
            try:
                targets = list({t for t in [cleaned, raw, file_path_or_id] if t})
                admin_client.storage.from_(self.bucket_name).remove(targets)
                print(f"[StorageService] Deleted from Supabase Storage '{self.bucket_name}': {targets}")
                deleted = True
            except Exception as e:
                print(f"[StorageService] Failed to delete from Supabase storage: {e}")

        # 2. Local disk cleanup
        for p in [file_path_or_id, raw, cleaned]:
            full_path = os.path.join(self.base_dir, p) if not os.path.isabs(p) else p
            if os.path.exists(full_path) and os.path.isfile(full_path):
                try:
                    os.remove(full_path)
                    deleted = True
                except Exception as e:
                    print(f"[StorageService] Failed to delete local file {full_path}: {e}")

        return deleted

    def share_company_folder(
        self,
        company_name: str,
        tax_year: str,
        auditor_email: str
    ) -> Dict[str, Any]:
        """
        Returns cloud access details for the company's document folder in Supabase Storage.
        """
        clean_company = self._sanitize(company_name) or "Company"
        clean_tax_year = self._sanitize(tax_year.replace("/", "-"))

        folder_prefix = f"{clean_company}/{clean_tax_year}"
        admin_client = self._get_admin_client()
        if admin_client:
            supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
            folder_link = f"{supabase_url}/storage/v1/object/public/{self.bucket_name}/{folder_prefix}"
            return {
                "success": True,
                "shared_with": auditor_email,
                "folder_link": folder_link,
                "mode": "supabase_storage"
            }

        return {
            "success": True,
            "shared_with": auditor_email,
            "folder_link": f"/api/documents?company_name={clean_company}",
            "mode": "local"
        }

    def bundle_audit_pack(
        self,
        company_name: str,
        tax_year: str,
        documents: List[Dict[str, Any]]
    ) -> io.BytesIO:
        """
        Bundles all company documents into an in-memory ZIP archive for 1-click auditor download.
        Fetches directly from Supabase Storage bucket.
        """
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for doc in documents:
                file_path = doc.get("file_path") or ""
                doc_name = doc.get("name") or "document.pdf"
                written = False

                # 1. Fetch file bytes (from Supabase or local)
                data = self.download_file_bytes(file_path)
                if data:
                    zip_file.writestr(doc_name, data)
                    written = True

                # 2. Fallback manifest entry if raw bytes cannot be read
                if not written:
                    manifest_text = (
                        f"TaxEaseLK Document Manifest\n\n"
                        f"Name: {doc_name}\n"
                        f"Type: {doc.get('type')}\n"
                        f"Status: {doc.get('status')}\n"
                        f"AI Confidence: {doc.get('ai_confidence_percent')}%\n"
                        f"Cloud Link: {doc.get('view_link')}\n"
                    )
                    zip_file.writestr(f"manifest_{doc_name}.txt", manifest_text)

        zip_buffer.seek(0)
        return zip_buffer

# Global singleton
storage_service = StorageService()
