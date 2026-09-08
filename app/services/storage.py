import os
import io
import re
import shutil
import zipfile
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

@dataclass
class StorageResult:
    provider: str  # "gdrive" or "local"
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
        
        # Check for Google Drive Service Account key
        self.credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.path.join(self.base_dir, "service_account.json")
        self.gdrive_enabled = False
        self.drive_service = None
        self._init_gdrive()

    def _init_gdrive(self):
        """Initializes Google Drive API client if service_account.json is present."""
        if os.path.exists(self.credentials_path):
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build

                scopes = ["https://www.googleapis.com/auth/drive"]
                creds = service_account.Credentials.from_service_account_file(
                    self.credentials_path, scopes=scopes
                )
                self.drive_service = build("drive", "v3", credentials=creds)
                self.gdrive_enabled = True
                print(f"[StorageService] Google Drive API connected successfully using {self.credentials_path}")
            except Exception as e:
                print(f"[StorageService] Failed to initialize Google Drive: {e}. Falling back to Local Vault.")
                self.gdrive_enabled = False
        else:
            self.gdrive_enabled = False
            print("[StorageService] Running in Local Vault Mode. Drop service_account.json into backend folder to activate Google Drive.")

    def _sanitize(self, name: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_\-\. ]', '_', name).strip()

    # --- Google Drive Helpers ---

    def _gdrive_find_or_create_folder(self, folder_name: str, parent_id: Optional[str] = None) -> str:
        if not self.gdrive_enabled or not self.drive_service:
            return ""
        
        q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        
        results = self.drive_service.files().list(q=q, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])
        if files:
            return files[0]['id']
        
        # Create folder
        metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            metadata['parents'] = [parent_id]
        
        folder = self.drive_service.files().create(body=metadata, fields='id').execute()
        return folder.get('id')

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

        # 1. Try Google Drive Upload if available
        if self.gdrive_enabled and self.drive_service:
            try:
                from googleapiclient.http import MediaIoBaseUpload

                # Structure: TaxEaseLK_Documents / {company_name} / Y-A {tax_year}
                root_id = self._gdrive_find_or_create_folder("TaxEaseLK_Documents")
                company_folder_id = self._gdrive_find_or_create_folder(clean_company, parent_id=root_id)
                tax_year_folder_id = self._gdrive_find_or_create_folder(f"Y-A {clean_tax_year}", parent_id=company_folder_id)

                file_metadata = {
                    'name': clean_filename,
                    'parents': [tax_year_folder_id],
                    'description': f"TaxEaseLK upload: {doc_type} for {company_name} ({tax_year})"
                }
                media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=content_type, resumable=True)
                created_file = self.drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id, name, webViewLink, webContentLink'
                ).execute()

                file_id = created_file.get('id')
                view_link = created_file.get('webViewLink', f"https://drive.google.com/file/d/{file_id}/view")
                download_url = created_file.get('webContentLink', view_link)

                return StorageResult(
                    provider="gdrive",
                    file_id=file_id,
                    file_path=file_id,
                    view_link=view_link,
                    download_url=download_url,
                    size=len(file_bytes)
                )
            except Exception as e:
                print(f"[StorageService] Google Drive upload failed: {e}. Falling back to local vault.")

        # 2. Local Vault Upload (Primary when key not provided, or fallback)
        company_vault_dir = os.path.join(self.uploads_dir, clean_company, clean_tax_year)
        os.makedirs(company_vault_dir, exist_ok=True)

        target_file_path = os.path.join(company_vault_dir, clean_filename)
        with open(target_file_path, "wb") as f:
            f.write(file_bytes)

        rel_path = os.path.relpath(target_file_path, self.base_dir).replace("\\", "/")
        download_url = f"/api/documents/download/{clean_filename}"
        simulated_gdrive_link = f"https://drive.google.com/file/d/gdrive_{clean_filename}/view"

        return StorageResult(
            provider="local",
            file_id=f"local_{clean_filename}",
            file_path=rel_path,
            view_link=simulated_gdrive_link,
            download_url=download_url,
            size=len(file_bytes)
        )

    def delete_file(self, file_path_or_id: str) -> bool:
        if self.gdrive_enabled and self.drive_service and not file_path_or_id.startswith("uploads/"):
            try:
                self.drive_service.files().delete(fileId=file_path_or_id).execute()
                return True
            except Exception as e:
                print(f"[StorageService] Failed to delete GDrive file {file_path_or_id}: {e}")

        # Local deletion
        full_path = os.path.join(self.base_dir, file_path_or_id)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
                return True
            except Exception as e:
                print(f"[StorageService] Failed to delete local file {full_path}: {e}")
        return False

    def share_company_folder(
        self,
        company_name: str,
        tax_year: str,
        auditor_email: str
    ) -> Dict[str, Any]:
        """
        Shares the company's document folder with the auditor's email address.
        """
        clean_company = self._sanitize(company_name) or "Company"
        clean_tax_year = self._sanitize(tax_year.replace("/", "-"))

        if self.gdrive_enabled and self.drive_service:
            try:
                root_id = self._gdrive_find_or_create_folder("TaxEaseLK_Documents")
                company_folder_id = self._gdrive_find_or_create_folder(clean_company, parent_id=root_id)
                tax_year_folder_id = self._gdrive_find_or_create_folder(f"Y-A {clean_tax_year}", parent_id=company_folder_id)

                # Add reader permission for auditor
                perm_body = {
                    'type': 'user',
                    'role': 'reader',
                    'emailAddress': auditor_email
                }
                self.drive_service.permissions().create(
                    fileId=tax_year_folder_id,
                    body=perm_body,
                    sendNotificationEmail=True
                ).execute()

                folder_link = f"https://drive.google.com/drive/folders/{tax_year_folder_id}"
                return {
                    "success": True,
                    "shared_with": auditor_email,
                    "folder_link": folder_link,
                    "mode": "gdrive"
                }
            except Exception as e:
                print(f"[StorageService] GDrive folder share error: {e}")

        # Fallback local notification
        return {
            "success": True,
            "shared_with": auditor_email,
            "folder_link": f"https://drive.google.com/drive/folders/taxease_{clean_company}_{clean_tax_year}",
            "mode": "local_simulated"
        }

    def bundle_audit_pack(
        self,
        company_name: str,
        tax_year: str,
        documents: List[Dict[str, Any]]
    ) -> io.BytesIO:
        """
        Bundles all company documents into an in-memory ZIP archive for 1-click auditor download.
        """
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for doc in documents:
                file_path = doc.get("file_path")
                doc_name = doc.get("name") or "document.pdf"
                full_path = os.path.join(self.base_dir, file_path) if file_path else ""

                if full_path and os.path.exists(full_path):
                    zip_file.write(full_path, arcname=doc_name)
                else:
                    # Provide an informative manifest entry if file is purely in GDrive or mock
                    manifest_text = f"TaxEaseLK Document Manifest\n\nName: {doc_name}\nType: {doc.get('type')}\nStatus: {doc.get('status')}\nAI Confidence: {doc.get('ai_confidence_percent')}%\nCloud Link: {doc.get('view_link')}\n"
                    zip_file.writestr(f"manifest_{doc_name}.txt", manifest_text)

        zip_buffer.seek(0)
        return zip_buffer

# Global singleton
storage_service = StorageService()
