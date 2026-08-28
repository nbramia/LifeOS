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
import time
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

SESSION_PATH = Path(__file__).parent.parent.parent / "data" / "monarch_session.pickle"

# Empirically observed Monarch session lifetime. Re-auth before this is hit
# so the monthly sync doesn't silently 401/525. See issue #199 §3.
SESSION_EXPIRY_DAYS = 30
SESSION_WARNING_DAYS = 25  # Surface in health endpoints before things break.


def is_monarch_configured() -> bool:
    """Return True if Monarch Money has any way to authenticate.

    "Configured" mirrors exactly what ``MonarchClient._get_client()`` tries,
    in order: a cached session (``SESSION_PATH``) or, failing that, both
    ``MONARCH_EMAIL``/``MONARCH_PASSWORD`` set. A fresh install with neither
    is not configured — the nightly sync should skip quietly rather than
    fail. Anything else (a stale/invalid session, a wrong password, a
    network outage) still reaches ``_get_client()`` and raises for real,
    which must keep surfacing as a failure — issue #687.
    """
    return SESSION_PATH.exists() or bool(settings.monarch_email and settings.monarch_password)


def get_session_age_days() -> Optional[float]:
    """Return the cached Monarch session's age in days, or None if not present.

    Uses the file mtime of the saved pickle — monarchmoney writes the file
    fresh on each successful authentication, so mtime is a faithful proxy
    for "last good login". Returns ``None`` (not 0) when no session exists so
    callers can distinguish "fresh install" from "session just rotated".
    """
    if not SESSION_PATH.exists():
        return None
    age_seconds = time.time() - SESSION_PATH.stat().st_mtime
    return age_seconds / 86400.0


def get_session_status() -> dict:
    """Summarise Monarch session freshness for /health and dashboards."""
    age = get_session_age_days()
    if age is None:
        return {
            "exists": False,
            "age_days": None,
            "status": "missing",
            "message": (
                "No cached Monarch session at data/monarch_session.pickle. "
                "Run the interactive login documented in AGENTS.md to authenticate."
            ),
        }

    if age >= SESSION_EXPIRY_DAYS:
        status = "expired"
        message = f"Monarch session is {age:.0f}d old — likely expired (>={SESSION_EXPIRY_DAYS}d). Re-authenticate."
    elif age >= SESSION_WARNING_DAYS:
        status = "expiring_soon"
        message = f"Monarch session is {age:.0f}d old — re-auth recommended before it expires (~{SESSION_EXPIRY_DAYS}d)."
    else:
        status = "ok"
        message = f"Monarch session is {age:.0f}d old."
    return {
        "exists": True,
        "age_days": round(age, 1),
        "status": status,
        "message": message,
    }


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

    async def get_holdings(self, account_id: str) -> list[dict]:
        """Investment holdings for one account (via Plaid, where supported).

        Added 2026-07-09 for the Schwab-portfolio dashboard: Guideline 401(k)
        has no consumer API, but Plaid supplies fund-level holdings through
        Monarch for many institutions. Returns [] rather than erroring when
        the institution provides no holdings data.
        """
        mm = await self._get_client()
        data = await mm.get_account_holdings(int(account_id))
        holdings = []
        for edge in (data.get("portfolio", {}).get("aggregateHoldings", {}).get("edges", []) or []):
            node = edge.get("node", {}) or {}
            sec = node.get("security") or {}
            holdings.append({
                "ticker": sec.get("ticker") or "",
                "name": sec.get("name") or node.get("name") or "",
                "quantity": node.get("quantity"),
                "price": (sec.get("currentPrice") if sec else None) or node.get("lastSyncedPrice"),
                "value": node.get("totalValue"),
            })
        return holdings

    async def get_history(self, account_id: str) -> list[dict]:
        """Daily balance snapshots for one account.

        Added 2026-07-10 for the Schwab-portfolio dashboard: external
        accounts (Guideline 401(k)) have no ledger, but Monarch records a
        balance snapshot per day, which the dashboard folds into its
        wealth-over-time history.
        """
        mm = await self._get_client()
        snaps = await mm.get_account_history(int(account_id))
        return [{"date": s.get("date"), "balance": s.get("signedBalance")}
                for s in (snaps or []) if s.get("date")]

    async def get_transactions(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        search: str = "",
        category: Optional[str] = None,
        limit: int = 500,
        account_ids: Optional[list[str]] = None,
        sort_order: Optional[str] = None,
    ) -> list[dict]:
        """Get transactions, optionally filtered by date range and category.

        sort_order: "asc" or "desc" to sort by date; None (default) leaves
        results in whatever order the Monarch API returns — unchanged for
        callers that don't ask for a specific order (#779).
        """
        mm = await self._get_client()
        kwargs = {"limit": limit, "offset": 0, "search": search}
        if account_ids:
            kwargs["account_ids"] = account_ids
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        if sort_order in ("asc", "desc"):
            # The underlying monarchmoney client always requests offset=0 and
            # hardcodes its own ordering with no direction control. A plain
            # limit-capped fetch therefore only ever returns *some* `limit`
            # matches in whatever order the server picks — not necessarily
            # the range's true oldest/newest. Sorting that limit-truncated
            # subset client-side would just reorder the same arbitrary
            # matches, not surface the actual oldest/newest transactions in
            # the full range. So: probe how many transactions match the
            # filters, fetch all of them, sort client-side, then apply the
            # caller's limit against the full sorted range.
            probe_kwargs = dict(kwargs)
            probe_kwargs["limit"] = 1
            probe = await mm.get_transactions(**probe_kwargs)
            total_count = probe.get("allTransactions", probe).get("totalCount", 0) or 0
            fetch_kwargs = dict(kwargs)
            fetch_kwargs["limit"] = max(total_count, 1)
            data = await mm.get_transactions(**fetch_kwargs)
        else:
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

            tags = []
            if isinstance(txn.get("tags"), list):
                tags = [t.get("name", "") for t in txn["tags"] if isinstance(t, dict)]

            result.append({
                "id": txn.get("id"),
                "date": txn.get("date", ""),
                "merchant": merchant_name,
                "category": cat_name,
                "amount": txn.get("amount", 0),
                "account": account_name,
                "notes": txn.get("notes", ""),
                "pending": txn.get("pending", False),
                "is_recurring": txn.get("isRecurring", False),
                "is_split": txn.get("isSplitTransaction", False),
                "tags": tags,
            })

        if sort_order in ("asc", "desc"):
            result.sort(key=lambda t: t.get("date") or "", reverse=(sort_order == "desc"))
            result = result[:limit]

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

        vault_path = settings.vault_path / settings.monarch_vault_dir
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
