"""
Vault write API — lets MCP-only clients (cloud agents) put files into the vault.

Read access is already covered by `lifeos_search`, `lifeos_ask`, etc. There was
no write path, which made tasks like "produce X.md in the vault" unsolvable for
the cloud agent (its tool surface is read-only RAG queries). This endpoint
closes that gap; the MCP server picks it up automatically as `lifeos_vault_write`
via the OpenAPI→curated-endpoint bridge.

Path validation: requested paths must be relative, must not contain `..`, and
must resolve under `settings.vault_path` after `Path.resolve()`. Parent dirs
are created on demand so a single call can write into a new subtree.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vault", tags=["vault"])


class VaultWriteRequest(BaseModel):
    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Vault-relative path (e.g., `Inbox/news monitoring prompt.md`). "
            "Must not start with `/` or contain `..`. Parent directories are "
            "created on demand."
        ),
    )
    content: str = Field(
        ...,
        description="UTF-8 text to write. Markdown is conventional; any text is allowed.",
    )
    mode: Literal["create", "overwrite", "append"] = Field(
        default="create",
        description=(
            "`create` (default) fails if the file exists. `overwrite` replaces. "
            "`append` adds to the end (creates the file if missing)."
        ),
    )


class VaultWriteResponse(BaseModel):
    path: str
    abs_path: str
    bytes_written: int
    mode: str
    created: bool


@router.post("/write", response_model=VaultWriteResponse)
async def vault_write(request: VaultWriteRequest) -> VaultWriteResponse:
    rel = request.path.strip()
    if rel.startswith("/") or rel.startswith("~"):
        raise HTTPException(400, "path must be vault-relative (no leading / or ~)")
    if ".." in Path(rel).parts:
        raise HTTPException(400, "path must not contain `..`")

    vault_root = settings.vault_path.resolve()
    target = (vault_root / rel).resolve()
    try:
        target.relative_to(vault_root)
    except ValueError:
        raise HTTPException(400, "path resolves outside the vault root")

    existed = target.exists()
    if request.mode == "create" and existed:
        raise HTTPException(
            409,
            f"file already exists at {rel}; use mode=overwrite or mode=append",
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        if request.mode == "append":
            with target.open("a", encoding="utf-8") as f:
                bytes_written = f.write(request.content)
        else:
            target.write_text(request.content, encoding="utf-8")
            bytes_written = len(request.content.encode("utf-8"))
    except OSError as e:
        logger.exception("vault write failed for %s", rel)
        raise HTTPException(500, f"write failed: {e}")

    logger.info("vault write: %s (%d bytes, mode=%s)", rel, bytes_written, request.mode)
    return VaultWriteResponse(
        path=rel,
        abs_path=str(target),
        bytes_written=bytes_written,
        mode=request.mode,
        created=not existed,
    )
