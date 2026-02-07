"""
Gmail API endpoints for LifeOS.

Provides search, retrieval, and draft creation for emails.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.services.gmail import get_gmail_service, EmailMessage, DraftMessage
from api.services.google_auth import GoogleAccount

router = APIRouter(prefix="/api/gmail", tags=["gmail"])


class EmailResponse(BaseModel):
    """Response model for an email message."""
    message_id: str
    thread_id: str
    subject: str
    sender: str
    sender_name: str
    date: str
    snippet: str
    body: Optional[str] = None
    source_account: str


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    messages: list[EmailResponse]
    count: int
    query: Optional[str] = None


class DraftCreateRequest(BaseModel):
    """Request model for creating a draft."""
    to: str = Field(..., description="Recipient email address(es), comma-separated for multiple")
    subject: str = Field(..., description="Email subject line")
    body: str = Field(..., description="Email body content")
    cc: Optional[str] = Field(default=None, description="CC recipients, comma-separated")
    bcc: Optional[str] = Field(default=None, description="BCC recipients, comma-separated")
    html: bool = Field(default=False, description="If true, body is treated as HTML")


class DraftResponse(BaseModel):
    """Response model for a draft."""
    draft_id: str
    message_id: str
    subject: str
    to: str
    body: Optional[str] = None
    cc: Optional[str] = None
    bcc: Optional[str] = None
    source_account: str
    gmail_url: str = Field(..., description="URL to open draft in Gmail")


def _message_to_response(msg: EmailMessage) -> EmailResponse:
    """Convert EmailMessage to API response."""
    return EmailResponse(
        message_id=msg.message_id,
        thread_id=msg.thread_id,
        subject=msg.subject,
        sender=msg.sender,
        sender_name=msg.sender_name,
        date=msg.date.isoformat(),
        snippet=msg.snippet,
        body=msg.body,
        source_account=msg.source_account,
    )


@router.get("/search", response_model=SearchResponse)
async def search_emails(
    q: Optional[str] = Query(default=None, description="Gmail search query - supports 'to:email', 'from:email', 'subject:text', 'newer_than:1d', etc."),
    from_email: Optional[str] = Query(default=None, alias="from", description="Filter by sender email"),
    after: Optional[str] = Query(default=None, description="Emails after date (YYYY-MM-DD)"),
    before: Optional[str] = Query(default=None, description="Emails before date (YYYY-MM-DD)"),
    account: str = Query(default="personal", description="Account: personal or work"),
    max_results: int = Query(default=20, ge=1, le=100, description="Maximum results"),
):
    """
    **Search Gmail for emails.** Queries both personal and work accounts by default.

    **IMPORTANT**: To search emails to/from a specific person, first use `people_v2_resolve` to get their
    email address, then search with `q=to:email` or `q=from:email`.

    Examples:
    - Find emails from someone: `q=from:john@example.com`
    - Find emails to someone: `q=to:jane@example.com`
    - Recent emails: `q=newer_than:7d`
    - Subject search: `q=subject:quarterly report`
    - Combined: `q=from:boss@work.com newer_than:30d`

    Returns message_id, subject, sender, date, and snippet for each email.
    Use `gmail_message` with message_id to get full body if needed.
    """
    if not any([q, from_email, after, before]):
        raise HTTPException(
            status_code=400,
            detail="At least one search parameter is required (q, from, after, before)"
        )

    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_gmail_service(account_type)

        # Parse dates if provided
        after_dt = datetime.fromisoformat(after) if after else None
        before_dt = datetime.fromisoformat(before) if before else None

        messages = service.search(
            keywords=q,
            from_email=from_email,
            after=after_dt,
            before=before_dt,
            max_results=max_results,
        )

        return SearchResponse(
            messages=[_message_to_response(m) for m in messages],
            count=len(messages),
            query=q,
        )

    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search emails: {e}")


@router.get("/message/{message_id}", response_model=EmailResponse)
async def get_email(
    message_id: str,
    account: str = Query(default="personal", description="Account: personal or work"),
    include_body: bool = Query(default=True, description="Include full email body"),
):
    """Get a specific email by message ID."""
    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_gmail_service(account_type)

        message = service.get_message(message_id, include_body=include_body)
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        return _message_to_response(message)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch email: {e}")


@router.post("/drafts", response_model=DraftResponse)
async def create_draft(
    request: DraftCreateRequest,
    account: str = Query(default="personal", description="Account: personal or work"),
):
    """
    **Create a draft email in Gmail.**

    Creates a draft that can be reviewed and sent from Gmail.
    Returns the draft ID and a direct link to open it in Gmail.

    Example:
    ```json
    {
        "to": "john@example.com",
        "subject": "Following up on our meeting",
        "body": "Hi John,\\n\\nGreat meeting today..."
    }
    ```

    For multiple recipients, use comma-separated emails:
    ```json
    {
        "to": "john@example.com, jane@example.com",
        "cc": "boss@example.com",
        "subject": "Team update"
    }
    ```
    """
    if not request.to or not request.to.strip():
        raise HTTPException(status_code=400, detail="Recipient (to) is required")
    if not request.subject or not request.subject.strip():
        raise HTTPException(status_code=400, detail="Subject is required")
    if not request.body or not request.body.strip():
        raise HTTPException(status_code=400, detail="Body is required")

    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_gmail_service(account_type)

        draft = service.create_draft(
            to=request.to.strip(),
            subject=request.subject.strip(),
            body=request.body,
            cc=request.cc.strip() if request.cc else None,
            bcc=request.bcc.strip() if request.bcc else None,
            html=request.html,
        )

        if not draft:
            raise HTTPException(status_code=500, detail="Failed to create draft")

        # Generate Gmail URL to open the draft
        gmail_url = f"https://mail.google.com/mail/u/0/#drafts?compose={draft.draft_id}"

        return DraftResponse(
            draft_id=draft.draft_id,
            message_id=draft.message_id,
            subject=draft.subject,
            to=draft.to,
            body=draft.body,
            cc=draft.cc,
            bcc=draft.bcc,
            source_account=draft.source_account,
            gmail_url=gmail_url,
        )

    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create draft: {e}")
