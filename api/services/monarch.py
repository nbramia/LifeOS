"""
Monarch Money integration service.

Provides authenticated access to Monarch Money financial data:
- Account balances
- Transaction history
- Cashflow summaries
- Budget status
- Monthly vault report generation

Session caching avoids repeated login/MFA. First login must be interactive
(see CLAUDE.md setup instructions), after which the cached session persists.
"""
import asyncio
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

SESSION_PATH = Path(__file__).parent.parent.parent / "data" / "monarch_session.pickle"


class MonarchClient:
    """Thin wrapper around monarchmoney with session caching."""

    def __init__(self):
        self._mm = None

    async def _get_client(self):
        """Get authenticated MonarchMoney client, reusing cached session."""
        if self._mm is not None:
            return self._mm

        from monarchmoney import MonarchMoney

        mm = MonarchMoney()

        # Try loading cached session first
        if SESSION_PATH.exists():
            try:
                mm.load_session(str(SESSION_PATH))
                # Verify session is still valid with a lightweight call
                await mm.get_accounts()
                self._mm = mm
                logger.info("Loaded cached Monarch Money session")
                return self._mm
            except Exception as e:
                logger.warning(f"Cached session invalid, re-authenticating: {e}")

        # Fall back to credential-based login
        if not settings.monarch_email or not settings.monarch_password:
            raise RuntimeError(
                "Monarch Money credentials not configured. "
                "Set MONARCH_EMAIL and MONARCH_PASSWORD in .env"
            )

        await mm.login(
            email=settings.monarch_email,
            password=settings.monarch_password,
            save_session=False,
        )

        # Save session for future use
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        mm.save_session(str(SESSION_PATH))
        logger.info("Authenticated with Monarch Money and saved session")

        self._mm = mm
        return self._mm

    async def get_accounts(self) -> list[dict]:
        """Get all accounts with current balances."""
        mm = await self._get_client()
        data = await mm.get_accounts()
        accounts = data.get("accounts", [])
        result = []
        for acct in accounts:
            result.append({
                "id": acct.get("id"),
                "name": acct.get("displayName", ""),
                "type": acct.get("type", {}).get("display", "") if isinstance(acct.get("type"), dict) else str(acct.get("type", "")),
                "subtype": acct.get("subtype", {}).get("display", "") if isinstance(acct.get("subtype"), dict) else str(acct.get("subtype", "")),
                "balance": acct.get("currentBalance") or acct.get("displayBalance") or 0,
                "institution": acct.get("credential", {}).get("institution", {}).get("name", "") if isinstance(acct.get("credential"), dict) else "",
                "last_updated": acct.get("updatedAt", ""),
            })
        return result

    async def get_transactions(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        search: str = "",
        category: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        """Get transactions, optionally filtered by date range and category."""
        mm = await self._get_client()
        kwargs = {"limit": limit, "offset": 0, "search": search}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        data = await mm.get_transactions(**kwargs)

        # Navigate response structure
        all_txns = data.get("allTransactions", data)
        txn_list = all_txns.get("results", all_txns.get("transactions", []))
        if isinstance(txn_list, dict):
            txn_list = txn_list.get("results", [])

        result = []
        for txn in txn_list:
            cat_name = ""
            if isinstance(txn.get("category"), dict):
                cat_name = txn["category"].get("name", "")
            elif isinstance(txn.get("category"), str):
                cat_name = txn["category"]

            # Apply category filter client-side if needed
            if category and cat_name.lower() != category.lower():
                continue

            merchant_name = ""
            if isinstance(txn.get("merchant"), dict):
                merchant_name = txn["merchant"].get("name", "")
            elif isinstance(txn.get("merchant"), str):
                merchant_name = txn["merchant"]

            account_name = ""
            if isinstance(txn.get("account"), dict):
                account_name = txn["account"].get("displayName", "")

            result.append({
                "id": txn.get("id"),
                "date": txn.get("date", ""),
                "merchant": merchant_name,
                "category": cat_name,
                "amount": txn.get("amount", 0),
                "account": account_name,
                "notes": txn.get("notes", ""),
                "pending": txn.get("pending", False),
            })

        return result

    async def get_cashflow_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        """Get cashflow summary (income, expenses, savings)."""
        mm = await self._get_client()
        kwargs = {}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        data = await mm.get_cashflow_summary(**kwargs)

        # Response: {"summary": [{"summary": {"sumIncome": ..., "sumExpense": ..., ...}}]}
        summary_list = data.get("summary", [])
        if isinstance(summary_list, list) and summary_list:
            inner = summary_list[0].get("summary", {})
        elif isinstance(summary_list, dict):
            inner = summary_list.get("summary", summary_list)
        else:
            inner = {}

        return {
            "total_income": abs(float(inner.get("sumIncome", 0))),
            "total_expenses": abs(float(inner.get("sumExpense", 0))),
            "savings": float(inner.get("savings", 0)),
            "savings_rate": float(inner.get("savingsRate", 0)),
        }

    async def get_cashflow_by_category(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Get spending breakdown by category."""
        mm = await self._get_client()
        kwargs = {}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        data = await mm.get_cashflow(**kwargs)

        # Response: {"byCategory": [{"groupBy": {"category": {"name": ...}}, "summary": {"sum": ...}}]}
        categories = []
        by_category = data.get("byCategory", [])
        if isinstance(by_category, list):
            for item in by_category:
                group_by = item.get("groupBy", {})
                cat_info = group_by.get("category", {})
                cat_name = cat_info.get("name", "") if isinstance(cat_info, dict) else str(cat_info)
                cat_group = cat_info.get("group", {})
                cat_type = cat_group.get("type", "") if isinstance(cat_group, dict) else ""
                amount = abs(float(item.get("summary", {}).get("sum", 0)))
                if amount > 0 and cat_name and cat_type == "expense":
                    categories.append({"category": cat_name, "amount": amount})

        categories.sort(key=lambda x: x["amount"], reverse=True)
        return categories

    async def get_budgets(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Get budget status (budgeted vs actual)."""
        mm = await self._get_client()
        kwargs = {}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        data = await mm.get_budgets(**kwargs)

        # Build category ID -> name lookup from categoryGroups
        cat_names = {}
        for group in data.get("categoryGroups", []):
            for cat in group.get("categories", []):
                cat_names[cat.get("id", "")] = cat.get("name", "")

        # Parse budgetData.monthlyAmountsByCategory
        budgets = []
        budget_data = data.get("budgetData", {})
        monthly_by_cat = budget_data.get("monthlyAmountsByCategory", []) if isinstance(budget_data, dict) else []
        for item in monthly_by_cat:
            cat_id = item.get("category", {}).get("id", "")
            cat_name = cat_names.get(cat_id, cat_id)
            monthly = item.get("monthlyAmounts", [])
            if not monthly:
                continue
            amt = monthly[0]  # First (and usually only) month in range
            budgeted = abs(float(amt.get("plannedCashFlowAmount", 0)))
            actual = abs(float(amt.get("actualAmount", 0)))
            remaining = float(amt.get("remainingAmount", budgeted - actual))
            if budgeted > 0 or actual > 0:
                budgets.append({
                    "category": cat_name,
                    "budgeted": budgeted,
                    "actual": actual,
                    "remaining": remaining,
                })

        return budgets

    async def generate_monthly_report(self, year: int, month: int) -> str:
        """
        Generate a Markdown financial summary for a given month.

        Returns the Markdown content string.
        """
        from calendar import monthrange

        last_day = monthrange(year, month)[1]
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"
        period = f"{year}-{month:02d}"
        month_name = date(year, month, 1).strftime("%B %Y")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch all data concurrently
        cashflow_task = asyncio.create_task(self.get_cashflow_summary(start_date, end_date))
        categories_task = asyncio.create_task(self.get_cashflow_by_category(start_date, end_date))
        accounts_task = asyncio.create_task(self.get_accounts())
        budgets_task = asyncio.create_task(self.get_budgets(start_date, end_date))
        transactions_task = asyncio.create_task(self.get_transactions(start_date, end_date, limit=1000))

        cashflow = await cashflow_task
        categories = await categories_task
        accounts = await accounts_task
        budgets = await budgets_task
        transactions = await transactions_task

        income = cashflow["total_income"]
        expenses = cashflow["total_expenses"]
        savings = income - expenses
        savings_rate = (savings / income * 100) if income > 0 else 0

        # Build Markdown
        lines = []

        # Frontmatter
        lines.append("---")
        lines.append("type: finance")
        lines.append("source: monarch")
        lines.append(f'date: "{end_date}"')
        lines.append(f'period: "{period}"')
        lines.append(f"total_income: {income:.2f}")
        lines.append(f"total_expenses: {expenses:.2f}")
        lines.append(f"savings_rate: {savings_rate / 100:.2f}")
        lines.append("tags:")
        lines.append("  - finance")
        lines.append("  - monthly-review")
        lines.append("monarch_sync: true")
        lines.append(f'synced_at: "{now_iso}"')
        lines.append("---")
        lines.append("")
        lines.append("> [!info] Auto-Synced from Monarch Money")
        lines.append("> This file is automatically synced monthly. **Do not edit locally.**")
        lines.append("")

        # Title
        lines.append(f"# Financial Summary — {month_name}")
        lines.append("")

        # Cashflow
        lines.append("## Cashflow")
        lines.append(f"- **Income**: ${income:,.2f}")
        lines.append(f"- **Expenses**: ${expenses:,.2f}")
        lines.append(f"- **Net Savings**: ${savings:,.2f}")
        lines.append(f"- **Savings Rate**: {savings_rate:.1f}%")
        lines.append("")

        # Spending by Category
        if categories:
            total_spend = sum(c["amount"] for c in categories)
            lines.append("## Spending by Category")
            lines.append("| Category | Amount | % of Total |")
            lines.append("|----------|--------|------------|")
            for cat in categories:
                pct = (cat["amount"] / total_spend * 100) if total_spend > 0 else 0
                lines.append(f"| {cat['category']} | ${cat['amount']:,.2f} | {pct:.1f}% |")
            lines.append("")

        # Account Balances
        if accounts:
            lines.append(f"## Account Balances (as of {date(year, month, last_day).strftime('%b %d')})")
            lines.append("| Account | Balance |")
            lines.append("|---------|---------|")
            for acct in sorted(accounts, key=lambda a: a.get("balance", 0), reverse=True):
                bal = acct["balance"]
                lines.append(f"| {acct['name']} | ${bal:,.2f} |")
            lines.append("")

        # Budget Status
        if budgets:
            lines.append("## Budget Status")
            lines.append("| Budget | Budgeted | Actual | Remaining |")
            lines.append("|--------|----------|--------|-----------|")
            for b in budgets:
                lines.append(f"| {b['category']} | ${b['budgeted']:,.2f} | ${b['actual']:,.2f} | ${b['remaining']:,.2f} |")
            lines.append("")

        # Transactions
        if transactions:
            lines.append("## Transactions")
            lines.append("| Date | Merchant | Category | Amount |")
            lines.append("|------|----------|----------|--------|")
            # Sort by date descending
            sorted_txns = sorted(transactions, key=lambda t: t["date"], reverse=True)
            for txn in sorted_txns:
                txn_date = txn["date"][5:] if len(txn["date"]) >= 10 else txn["date"]  # MM-DD
                amount = txn["amount"]
                sign = "" if amount >= 0 else "-"
                lines.append(f"| {txn_date} | {txn['merchant']} | {txn['category']} | {sign}${abs(amount):,.2f} |")
            lines.append("")

        return "\n".join(lines)

    async def write_monthly_report(self, year: int, month: int, dry_run: bool = False) -> dict:
        """
        Generate and write monthly report to vault.

        Returns stats dict with file path and counts.
        """
        content = await self.generate_monthly_report(year, month)
        period = f"{year}-{month:02d}"

        vault_path = settings.vault_path / "Personal" / "Finance" / "Monarch"
        file_path = vault_path / f"{period}.md"

        if dry_run:
            logger.info(f"DRY RUN: Would write {len(content)} chars to {file_path}")
            return {"status": "dry_run", "file": str(file_path), "size": len(content)}

        vault_path.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Wrote monthly report to {file_path}")

        return {"status": "success", "file": str(file_path), "size": len(content)}


# Singleton instance
_client: Optional[MonarchClient] = None


def get_monarch_client() -> MonarchClient:
    """Get or create the singleton MonarchClient."""
    global _client
    if _client is None:
        _client = MonarchClient()
    return _client
