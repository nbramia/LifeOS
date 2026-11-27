"""Approve/Deny requests from a local caller, decided on the primary bot.

Both routes answer only a direct loopback caller that is not proxied, not
a browser and not a relayed LifeOS call (`external_approval.refusal_reason`);
anyone else gets 403 and no message is sent. They are not MCP tools.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from api.services import external_approval as ea
from api.services.telegram import send_message_with_buttons

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def require_local_caller(request: Request) -> None:
    peer = request.client.host if request.client else None
    reason = ea.refusal_reason(peer, request.headers)
    if reason is not None:
        logger.info("external approval refused: %s", reason)
        raise HTTPException(status_code=403, detail="Forbidden")


class ExternalApprovalRequest(BaseModel):
    summary: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ttl_seconds: int = Field(
        default=ea.TTL_DEFAULT_SECONDS, ge=ea.TTL_MIN_SECONDS, le=ea.TTL_MAX_SECONDS, strict=True,
    )
    source: Optional[str] = None

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        value = ea.clean_text(value, keep_newlines=True)
        if not 1 <= len(value) <= ea.SUMMARY_MAX_CHARS:
            raise ValueError(f"summary must be 1..{ea.SUMMARY_MAX_CHARS} characters of text")
        return value

    @field_validator("source")
    @classmethod
    def _source(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = ea.clean_text(value, keep_newlines=False)
        if not 1 <= len(value) <= ea.SOURCE_MAX_CHARS:
            raise ValueError(f"source must be 1..{ea.SOURCE_MAX_CHARS} characters of text")
        return value


@router.post("/external", dependencies=[Depends(require_local_caller)])
def open_external_approval(body: ExternalApprovalRequest):
    store = ea.get_store()
    record = store.open(body.request_hash, body.summary, body.source, body.ttl_seconds)
    token = record["token"]
    text = ea.request_text(body.summary, body.source, body.request_hash, record["expires_at"])
    try:
        message_id = send_message_with_buttons(
            text,
            [("Approve", ea.callback_data(True, token)), ("Deny", ea.callback_data(False, token))],
        )
    except Exception:  # noqa: BLE001 - an unsent request is discarded below
        message_id = None
    if message_id is None:
        store.discard(token, body.request_hash, "send failed")
        raise HTTPException(status_code=503, detail="Telegram is unavailable; ask in chat.")
    store.set_message_id(token, message_id)
    return {"token": token, "expires_at": ea.iso_utc(record["expires_at"])}


@router.get("/external/{token}", dependencies=[Depends(require_local_caller)])
def read_external_approval(token: str):
    record = ea.get_store().get(token)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": "unknown or lost"})
    return record
