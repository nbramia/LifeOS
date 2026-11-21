"""Vault file browser and intake API.

The page at /vault uses these endpoints: list one folder's contents, upload any
file into a folder (it is stored as-is and processed in the background into a
sidecar note), follow a processing job, download a stored file, and move a file
(with its sidecar note) to another folder.

Every path is vault-relative and must resolve inside the vault root. Files are
served as attachments, except a short allowlist of types that are safe to show
inline, and always with `nosniff` and a sandboxing CSP, so a stored HTML or SVG
file cannot run script in the site's origin.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.routes.vault import _RESERVED_PREFIX, _journal_enabled
from api.services import vault_ingest
from api.services.vault_ingest import VaultPathError
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vault", tags=["vault"])

MAX_TREE_ENTRIES = 1000
_CHUNK = 1024 * 1024


class TreeFolder(BaseModel):
    name: str
    path: str


class TreeFile(BaseModel):
    name: str
    path: str
    size: int
    modified: str
    kind: str
    sidecar: str | None = None


class TreeResponse(BaseModel):
    path: str
    folders: list[TreeFolder]
    files: list[TreeFile]
    truncated: bool = False


class UploadResult(BaseModel):
    name: str
    ok: bool
    path: str | None = None
    job_id: str | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    folder: str
    results: list[UploadResult]


class MoveRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Vault-relative path of the file to move.")
    to_folder: str = Field("", description="Vault-relative destination folder; empty for the vault root.")


class MoveResponse(BaseModel):
    path: str
    moved: list[str]


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


def _check_writable_folder(rel: str) -> Path:
    try:
        folder = vault_ingest.resolve_inside_vault(rel)
    except VaultPathError as exc:
        raise _bad(exc)
    parts = Path(rel.strip().replace("\\", "/")).parts
    if parts[: len(_RESERVED_PREFIX)] == _RESERVED_PREFIX and _journal_enabled():
        raise HTTPException(400, "Personal/Journal/ is reserved for the generated journal")
    if folder.exists() and not folder.is_dir():
        raise HTTPException(400, "destination is not a folder")
    return folder


def _entry_modified(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")


@router.get("/tree", response_model=TreeResponse)
async def vault_tree(path: str = Query("", description="Vault-relative folder; empty for the vault root.")) -> TreeResponse:
    """One folder's immediate subfolders and files (all types, hidden entries skipped)."""
    try:
        folder = vault_ingest.resolve_inside_vault(path)
    except VaultPathError as exc:
        raise _bad(exc)
    if not folder.is_dir():
        raise HTTPException(404, "folder not found")
    root = settings.vault_path.resolve()
    folders: list[TreeFolder] = []
    files: list[TreeFile] = []
    truncated = False
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        raise HTTPException(500, "folder could not be read")
    names = {p.name for p in entries}
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if len(folders) + len(files) >= MAX_TREE_ENTRIES:
            truncated = True
            break
        try:
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir():
                folders.append(TreeFolder(name=entry.name, path=rel))
            elif entry.is_file():
                sidecar = f"{entry.name}.md"
                files.append(
                    TreeFile(
                        name=entry.name,
                        path=rel,
                        size=entry.stat().st_size,
                        modified=_entry_modified(entry),
                        kind=vault_ingest.file_kind(entry),
                        sidecar=(folder / sidecar).relative_to(root).as_posix() if sidecar in names else None,
                    )
                )
        except OSError:
            continue
    folders.sort(key=lambda f: f.name.lower())
    files.sort(key=lambda f: f.name.lower())
    return TreeResponse(path=path.strip().strip("/"), folders=folders, files=files, truncated=truncated)


async def _save_upload(folder: Path, upload: UploadFile) -> Path:
    """Write one upload into `folder` under a safe, unused name and return its path."""
    name = vault_ingest.safe_filename(upload.filename or "")
    limit = settings.vault_upload_max_mb * _CHUNK
    folder.mkdir(parents=True, exist_ok=True)
    target = vault_ingest.unique_path(folder, name)
    partial = folder / f".upload-{os.getpid()}-{target.name}.part"
    written = 0
    try:
        with partial.open("wb") as out:
            while True:
                chunk = await upload.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"file is larger than {settings.vault_upload_max_mb} MB")
                out.write(chunk)
        if written == 0:
            raise ValueError("file is empty")
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)
    return target


@router.post("/upload", response_model=UploadResponse)
async def vault_upload(
    folder: str = Form("", description="Vault-relative destination folder; created if missing."),
    files: list[UploadFile] = File(...),  # noqa: B008
) -> UploadResponse:
    """Store each file in the folder and start processing it into a sidecar note."""
    dest = _check_writable_folder(folder)
    if len(files) > 25:
        raise HTTPException(400, "at most 25 files per upload")
    root = settings.vault_path.resolve()
    results: list[UploadResult] = []
    for upload in files:
        label = upload.filename or "(unnamed)"
        try:
            saved = await _save_upload(dest, upload)
        except (ValueError, OSError) as exc:
            results.append(UploadResult(name=label, ok=False, error=str(exc)))
            continue
        rel = saved.relative_to(root).as_posix()
        job = vault_ingest.start_job(saved, rel)
        logger.info("vault upload: %s (%d bytes)", rel, saved.stat().st_size)
        results.append(UploadResult(name=label, ok=True, path=rel, job_id=job["id"]))
    return UploadResponse(folder=folder.strip().strip("/"), results=results)


@router.get("/jobs/{job_id}")
async def vault_job(job_id: str) -> dict:
    job = vault_ingest.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return vault_ingest.public_job(job)


@router.get("/download")
async def vault_download(
    path: str = Query(..., min_length=1),
    inline: bool = Query(False, description="Show in the browser when the type is safe to display."),
) -> FileResponse:
    try:
        target = vault_ingest.resolve_inside_vault(path)
    except VaultPathError as exc:
        raise _bad(exc)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    ext = target.suffix.lower()
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        "Cache-Control": "private, no-store",
    }
    if inline and ext in vault_ingest.INLINE_TYPES:
        return FileResponse(str(target), media_type=vault_ingest.INLINE_TYPES[ext], headers=headers)
    return FileResponse(str(target), filename=target.name, media_type="application/octet-stream", headers=headers)


@router.post("/move", response_model=MoveResponse)
async def vault_move(request: MoveRequest) -> MoveResponse:
    """Move a file, and its sidecar note when it has one, into another folder. Never overwrites."""
    try:
        source = vault_ingest.resolve_inside_vault(request.path)
    except VaultPathError as exc:
        raise _bad(exc)
    if not source.is_file():
        raise HTTPException(404, "file not found")
    dest = _check_writable_folder(request.to_folder)
    root = settings.vault_path.resolve()
    if source.parent == dest:
        raise HTTPException(400, "the file is already in that folder")

    pairs = [(source, dest / source.name)]
    sidecar = source.with_name(source.name + ".md")
    if sidecar.is_file() and source.suffix.lower() != ".md":
        pairs.append((sidecar, dest / sidecar.name))
    for _, target in pairs:
        if target.exists():
            raise HTTPException(409, f"{target.name} already exists in the destination folder")
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    try:
        for src, target in pairs:
            shutil.move(str(src), str(target))
            moved.append(target.relative_to(root).as_posix())
    except OSError as exc:
        logger.exception("vault move failed for %s", request.path)
        raise HTTPException(500, f"move failed: {exc}")
    logger.info("vault move: %s -> %s", request.path, request.to_folder)
    return MoveResponse(path=moved[0], moved=moved)
