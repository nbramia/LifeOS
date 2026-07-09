"""
Monarch Money API routes for LifeOS.

Live query endpoints for financial data (accounts, transactions, cashflow, budgets).
"""
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException

from api.services.monarch import get_monarch_client, get_session_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monarch", tags=["monarch"])


@router.get("/session_status")
async def session_status():
    """Report Monarch session age + expiry-soon warnings.

    Used by /health/services and dashboards to surface the impending re-auth
    requirement *before* the monthly sync hits 401/525. The session is just a
    pickle on disk — checking its age is cheap and doesn't make any network
    calls.
    """
    return get_session_status()


@router.get("/accounts")
async def list_accounts():
    """List all financial accounts with current balances."""
    try:
        client = get_monarch_client()
        accounts = await client.get_accounts()
        return {"accounts": accounts, "count": len(accounts)}
    except Exception as e:
        logger.error(f"Failed to fetch Monarch accounts: {e}")
        raise HTTPException(status_code=502, detail=f"Monarch API error: {e}")


@router.get("/holdings")
async def account_holdings(account_id: str):
    """Investment holdings for one account (empty list if the institution
    does not supply holdings through Plaid)."""
    try:
        client = get_monarch_client()
        holdings = await client.get_holdings(account_id)
        return {"holdings": holdings, "count": len(holdings)}
    except Exception as e:
        logger.error(f"Failed to fetch Monarch holdings for {account_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Monarch API error: {e}")


@router.get("/history")
async def account_history(account_id: str, start_date: Optional[str] = None):
    """Daily balance snapshots for one account (date, balance)."""
    try:
        client = get_monarch_client()
        snaps = await client.get_history(account_id)
        if start_date:
            snaps = [s for s in snaps if (s.get("date") or "") >= start_date]
        return {"history": snaps, "count": len(snaps)}
    except Exception as e:
        logger.error(f"Failed to fetch Monarch history for {account_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Monarch API error: {e}")


@router.get("/transactions")
async def list_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    account_id: Optional[str] = None,
):
    """
    Search/filter recent transactions.

    Query parameters:
    - start_date: Start date (YYYY-MM-DD), defaults to 30 days ago
    - end_date: End date (YYYY-MM-DD), defaults to today
    - category: Filter by category name
    - search: Search by merchant name
    - limit: Max results (default 100)
    - account_id: Filter by Monarch account ID
    """
    if not start_date:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    if not end_date:
        end_date = date.today().isoformat()

    try:
        client = get_monarch_client()
        transactions = await client.get_transactions(
            start_date=start_date,
            end_date=end_date,
            search=search or "",
            category=category,
            limit=min(limit, 500),
            account_ids=[account_id] if account_id else None,
        )
        return {
            "transactions": transactions,
            "count": len(transactions),
            "start_date": start_date,
            "end_date": end_date,
        }
    except Exception as e:
        logger.error(f"Failed to fetch Monarch transactions: {e}")
        raise HTTPException(status_code=502, detail=f"Monarch API error: {e}")


@router.get("/cashflow")
async def cashflow_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """
    Get cashflow summary for a date range.

    Query parameters:
    - start_date: Start date (YYYY-MM-DD), defaults to first of current month
    - end_date: End date (YYYY-MM-DD), defaults to today
    """
    if not start_date:
        start_date = date.today().replace(day=1).isoformat()
    if not end_date:
        end_date = date.today().isoformat()

    try:
        client = get_monarch_client()
        summary = await client.get_cashflow_summary(start_date, end_date)
        categories = await client.get_cashflow_by_category(start_date, end_date)
        return {
            **summary,
            "categories": categories,
            "start_date": start_date,
            "end_date": end_date,
        }
    except Exception as e:
        logger.error(f"Failed to fetch Monarch cashflow: {e}")
        raise HTTPException(status_code=502, detail=f"Monarch API error: {e}")


@router.get("/budgets")
async def budget_status(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """
    Get current budget status.

    Query parameters:
    - start_date: Start date (YYYY-MM-DD), defaults to first of current month
    - end_date: End date (YYYY-MM-DD), defaults to today
    """
    if not start_date:
        start_date = date.today().replace(day=1).isoformat()
    if not end_date:
        end_date = date.today().isoformat()

    try:
        client = get_monarch_client()
        budgets = await client.get_budgets(start_date, end_date)
        return {
            "budgets": budgets,
            "count": len(budgets),
            "start_date": start_date,
            "end_date": end_date,
        }
    except Exception as e:
        logger.error(f"Failed to fetch Monarch budgets: {e}")
        raise HTTPException(status_code=502, detail=f"Monarch API error: {e}")
