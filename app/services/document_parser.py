import io
import re
from typing import Dict, Any, List, Optional
try:
    import pypdf
except ImportError:
    pypdf = None

class DocumentParserService:
    """
    Parses financial documents (Trial Balance, P&L, Fixed Asset Schedule, General Ledger, CIT Returns)
    using pypdf and intelligent text extraction to extract real corporate financial metrics
    under Sri Lanka Inland Revenue Act standards.
    """

    def _extract_text(self, filename: str, file_bytes: bytes) -> str:
        fn_lower = filename.lower()
        if fn_lower.endswith(".pdf") and pypdf is not None:
            try:
                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                pages_text = []
                for p in reader.pages:
                    txt = p.extract_text()
                    if txt:
                        pages_text.append(txt)
                return "\n".join(pages_text)
            except Exception as e:
                print(f"[DocumentParser] PDF parse error: {e}")
                return ""
        elif fn_lower.endswith((".txt", ".csv", ".tsv", ".json", ".log")):
            try:
                return file_bytes.decode("utf-8", errors="ignore")
            except Exception:
                return file_bytes.decode("latin-1", errors="ignore")
        return ""

    def _parse_number(self, s: str) -> Optional[float]:
        clean = s.replace(",", "").replace("LKR", "").replace("Rs.", "").replace("Rs", "").strip()
        is_neg = False
        if clean.startswith("(") and clean.endswith(")"):
            is_neg = True
            clean = clean[1:-1].strip()
        elif clean.startswith("-"):
            is_neg = True
            clean = clean[1:].strip()
        m = re.search(r"^\d+(?:\.\d+)?", clean)
        if m:
            val = float(m.group(0))
            return -val if is_neg else val
        return None

    def analyze_document(
        self,
        filename: str,
        file_bytes: bytes,
        declared_doc_type: str = "Financial Statements"
    ) -> Dict[str, Any]:
        fn_lower = filename.lower()
        file_size = len(file_bytes)

        # 1. Infer/Refine Document Type
        doc_type = declared_doc_type
        if "trial" in fn_lower or "tb" in fn_lower:
            doc_type = "Trial Balance"
        elif "ledger" in fn_lower or "gl" in fn_lower:
            doc_type = "General Ledger"
        elif "asset" in fn_lower or "depreciation" in fn_lower:
            doc_type = "Fixed Assets"
        elif "cit" in fn_lower or "tax" in fn_lower or "return" in fn_lower:
            doc_type = "Previous CIT"
        elif "statement" in fn_lower or "financial" in fn_lower or "p&l" in fn_lower or "pnl" in fn_lower:
            doc_type = "Financial Statements"

        # 2. Extract Text
        extracted_text = self._extract_text(filename, file_bytes)
        lines = [l.strip() for l in extracted_text.split("\n") if l.strip()]

        review_reasons = []
        confidence = 98
        status = "processed"

        if file_size < 100:
            confidence = 65
            status = "review_required"
            review_reasons.append("File size appears unusually small (< 100 bytes)")
        elif not lines:
            confidence = 80
            status = "review_required"
            review_reasons.append("No extractable digital text found (scanned image or unsupported format). Manual auditor review required.")
        elif "draft" in fn_lower or "unreconciled" in fn_lower or "prelim" in fn_lower:
            confidence = 88
            status = "review_required"
            review_reasons.append("Document labeled as draft or unreconciled")

        # Base extracted data payload
        extracted_data: Dict[str, Any] = {
            "document_name": filename,
            "detected_type": doc_type,
            "file_size_bytes": file_size,
            "review_reasons": review_reasons,
        }

        # Detect Company Name in document header
        for l in lines[:10]:
            if any(w in l.lower() for w in ["(pvt) ltd", "limited", "plc", "holdings", "enterprises", "trading"]):
                if "sample" not in l.lower() and "fictional" not in l.lower():
                    extracted_data["detected_company_name"] = l.strip()
                    break

        # 3. Dynamic Parser Logic by Document Type
        if doc_type == "Financial Statements":
            fin_metrics = self._parse_financial_statements(lines)
            extracted_data.update(fin_metrics)
        elif doc_type == "Trial Balance":
            tb_metrics = self._parse_trial_balance(lines)
            extracted_data.update(tb_metrics)
        elif doc_type == "Fixed Assets":
            fa_metrics = self._parse_fixed_assets(lines)
            extracted_data.update(fa_metrics)
        elif doc_type == "Previous CIT":
            cit_metrics = self._parse_previous_cit(lines)
            extracted_data.update(cit_metrics)
        elif doc_type == "General Ledger":
            gl_metrics = self._parse_general_ledger(lines)
            extracted_data.update(gl_metrics)

        return {
            "doc_type": doc_type,
            "status": status,
            "ai_confidence_percent": confidence,
            "extracted_data": extracted_data
        }

    def _parse_financial_statements(self, lines: List[str]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for i, line in enumerate(lines):
            ll = line.lower()
            val = None
            for j in range(1, 4):
                if i + j < len(lines):
                    num = self._parse_number(lines[i + j])
                    if num is not None:
                        val = num
                        break
            if val is None:
                continue

            if (ll == "revenue" or ll.startswith("revenue")) and "assessable" not in ll:
                if "revenue" not in data: data["revenue"] = abs(val)
            elif "cost of sales" in ll:
                if "cost_of_sales" not in data: data["cost_of_sales"] = abs(val)
            elif ll == "gross profit" or ll.startswith("gross profit"):
                if "gross_profit" not in data: data["gross_profit"] = abs(val)
            elif "operating expenses" in ll:
                if "operating_expenses" not in data: data["operating_expenses"] = abs(val)
            elif "operating profit" in ll:
                if "operating_profit" not in data: data["operating_profit"] = abs(val)
            elif "finance costs" in ll:
                if "finance_costs" not in data: data["finance_costs"] = abs(val)
            elif "profit before tax" in ll or "accounting profit before tax" in ll or "pbt" in ll:
                if "accounting_profit_before_tax" not in data: data["accounting_profit_before_tax"] = val
            elif "income tax expense" in ll or "tax expense" in ll:
                if "income_tax_expense" not in data: data["income_tax_expense"] = abs(val)
            elif "profit for the year" in ll or "net profit" in ll:
                if "profit_for_year" not in data: data["profit_for_year"] = val
            elif "total assets" in ll and "current" not in ll:
                if "total_assets" not in data: data["total_assets"] = abs(val)
            elif "property, plant & equipment" in ll or "property, plant and equipment" in ll:
                if "ppe" not in data: data["ppe"] = abs(val)
            elif "trade receivables" in ll:
                if "trade_receivables" not in data: data["trade_receivables"] = abs(val)
            elif "cash and cash equivalents" in ll or "cash and bank" in ll:
                if "cash_and_bank" not in data: data["cash_and_bank"] = abs(val)
            elif "inventories" in ll or "inventory" in ll:
                if "inventories" not in data: data["inventories"] = abs(val)
            elif "total equity" in ll and "liabilities" not in ll:
                if "total_equity" not in data: data["total_equity"] = abs(val)

        # Derived metrics if some are present
        if "gross_profit" in data and "operating_expenses" in data and "accounting_profit_before_tax" not in data:
            data["accounting_profit_before_tax"] = data["gross_profit"] - data["operating_expenses"]
        elif "revenue" in data and "cost_of_sales" in data and "gross_profit" not in data:
            data["gross_profit"] = data["revenue"] - data["cost_of_sales"]

        return data

    def _parse_trial_balance(self, lines: List[str]) -> Dict[str, Any]:
        accounts = []
        total_debits = 0.0
        total_credits = 0.0

        i = 0
        while i < len(lines):
            line = lines[i]
            # Match 4-digit code e.g. 1000, 2000, 4000
            if re.match(r"^\d{4}$", line) and i + 2 < len(lines):
                code = line
                name = lines[i + 1]
                num = self._parse_number(lines[i + 2])
                if num is not None:
                    is_credit = int(code) in range(2000, 4999)
                    if is_credit:
                        total_credits += abs(num)
                    else:
                        total_debits += abs(num)
                    accounts.append({
                        "code": code,
                        "name": name,
                        "amount": abs(num),
                        "type": "credit" if is_credit else "debit"
                    })
                    i += 3
                    continue
            i += 1

        is_balanced = total_debits > 0 and (total_debits == total_credits or abs(total_debits - total_credits) < 1.0)
        return {
            "extracted_accounts_count": len(accounts),
            "total_debits": total_debits,
            "total_credits": total_credits,
            "is_balanced": is_balanced,
            "sample_accounts": accounts[:10]
        }

    def _parse_fixed_assets(self, lines: List[str]) -> Dict[str, Any]:
        assets = []
        total_cost = 0.0
        total_dep = 0.0
        total_nbv = 0.0

        i = 0
        while i < len(lines):
            if re.match(r"^FA-\d+$", lines[i]) and i + 5 < len(lines):
                asset_id = lines[i]
                desc = lines[i + 1]
                cost = self._parse_number(lines[i + 2]) or 0.0
                accum_dep = self._parse_number(lines[i + 3]) or 0.0
                cur_dep = self._parse_number(lines[i + 4]) or 0.0
                nbv = self._parse_number(lines[i + 5]) or 0.0
                total_cost += cost
                total_dep += cur_dep
                total_nbv += nbv
                assets.append({
                    "id": asset_id,
                    "description": desc,
                    "cost": cost,
                    "accum_dep": accum_dep,
                    "current_dep": cur_dep,
                    "nbv": nbv
                })
                i += 6
                continue
            i += 1

        return {
            "extracted_assets_count": len(assets),
            "total_asset_cost": total_cost,
            "total_carrying_value": total_nbv,
            "accounting_depreciation_expense": total_dep,
            "tax_capital_allowances_claimable": round(total_cost * 0.20, 2),  # 20% standard capital allowance rate
            "assets": assets[:10]
        }

    def _parse_previous_cit(self, lines: List[str]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for i, line in enumerate(lines):
            ll = line.lower()
            if i + 1 < len(lines):
                num = self._parse_number(lines[i + 1])
                if num is not None:
                    if "accounting profit before tax" in ll or "pbt" in ll:
                        data["prior_accounting_profit"] = num
                    elif "non-deductible" in ll or "disallowable" in ll:
                        data["prior_disallowables"] = num
                    elif "tax depreciation" in ll or "capital allowances" in ll:
                        data["prior_capital_allowances"] = abs(num)
                    elif "taxable income" in ll:
                        data["prior_taxable_income"] = num
                    elif "corporate income tax" in ll:
                        data["prior_cit_tax"] = num
                    elif "balance tax payable" in ll or "tax payable" in ll:
                        data["prior_balance_payable"] = num
        return data

    def _parse_general_ledger(self, lines: List[str]) -> Dict[str, Any]:
        # Count transactions and find total turnover or expense entries
        tx_count = sum(1 for l in lines if re.search(r"^\d{2}/\d{2}/\d{4}", l) or "JV-" in l or "INV-" in l)
        return {
            "extracted_transactions_count": max(tx_count, 1),
            "ledger_type": "Selected Fiscal Year Transactions",
        }

# Global singleton
document_parser = DocumentParserService()
