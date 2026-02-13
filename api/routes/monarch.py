"""
Monarch Money API routes for LifeOS.

Live query endpoints for financial data (accounts, transactions, cashflow, budgets).
"""
import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException

from api.services.monarch import get_monarch_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monarch", tags=["monarch"])


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


@router.get("/transactions")
async def list_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
):
    """
    Search/filter recent transactions.

    Query parameters:
    - start_date: Start date (YYYY-MM-DD), defaults to 30 days ago
    - end_date: End date (YYYY-MM-DD), defaults to today
    - category: Filter by category name
    - search: Search by merchant name
    - limit: Max results (default 100)
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
