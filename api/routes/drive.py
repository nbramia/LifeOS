"""
Google Drive API endpoints for LifeOS.

Provides search and retrieval of Drive files.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.services.drive import get_drive_service, DriveFile, get_drive_link
from api.services.google_auth import GoogleAccount

router = APIRouter(prefix="/api/drive", tags=["drive"])


class FileResponse(BaseModel):
    """Response model for a Drive file."""
    file_id: str
    name: str
    mime_type: str
    modified_time: str
    web_link: str
    size: Optional[int] = None
    source_account: str
    content: Optional[str] = None


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    files: list[FileResponse]
    count: int
    query: Optional[str] = None


def _file_to_response(file: DriveFile, include_content: bool = False) -> FileResponse:
    """Convert DriveFile to API response."""
    return FileResponse(
        file_id=file.file_id,
        name=file.name,
        mime_type=file.mime_type,
        modified_time=file.modified_time.isoformat(),
        web_link=file.web_link or get_drive_link(file.file_id, file.mime_type),
        size=file.size,
        source_account=file.source_account,
        content=file.content if include_content else None,
    )


@router.get("/search", response_model=SearchResponse)
async def search_files(
    q: Optional[str] = Query(default=None, description="Search by name or content"),
    name: Optional[str] = Query(default=None, description="Search by filename only"),
    content: Optional[str] = Query(default=None, description="Search by file content (fullText)"),
    account: str = Query(default="personal", description="Account: personal or work"),
    max_results: int = Query(default=20, ge=1, le=100, description="Maximum results"),
):
    """
    **Search Google Drive** for documents, spreadsheets, and files.

    Use this for:
    - "Find the Q4 budget spreadsheet" → `q=Q4 budget`
    - "Find docs about project roadmap" → `content=project roadmap`
    - "Find files named 'meeting notes'" → `name=meeting notes`

    Returns file name, type, modified date, and web link.
    Use `drive_file_content` to get full text content of a specific file.
    Query both personal and work accounts for complete results.
    """
    # q is a general search - applies to both name and content
    search_name = name or q
    search_content = content or (q if not name else None)

    if not any([search_name, search_content]):
        raise HTTPException(
            status_code=400,
            detail="At least one search parameter is required (q, name, content)"
        )

    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_drive_service(account_type)

        files = service.search(
            name=search_name,
            full_text=search_content,
            max_results=max_results,
        )

        return SearchResponse(
            files=[_file_to_response(f) for f in files],
            count=len(files),
            query=q or name or content,
        )

    except FileNotFoundError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search Drive: {e}")


@router.get("/file/{file_id}", response_model=FileResponse)
async def get_file(
    file_id: str,
    account: str = Query(default="personal", description="Account: personal or work"),
    include_content: bool = Query(default=False, description="Include file content"),
):
    """Get a specific file by ID."""
    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_drive_service(account_type)

        file = service.get_file(file_id)
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        # Optionally fetch content
        if include_content:
            content = service.get_file_content(file_id, file.mime_type)
            file.content = content

        return _file_to_response(file, include_content=include_content)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch file: {e}")


@router.get("/file/{file_id}/content")
async def get_file_content(
    file_id: str,
    account: str = Query(default="personal", description="Account: personal or work"),
):
    """Get file content as plain text."""
    try:
        account_type = GoogleAccount.PERSONAL if account == "personal" else GoogleAccount.WORK
        service = get_drive_service(account_type)

        file = service.get_file(file_id)
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        content = service.get_file_content(file_id, file.mime_type)
        if content is None:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot extract text from {file.mime_type}"
            )

        return {
            "file_id": file_id,
            "name": file.name,
            "content": content,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch content: {e}")
