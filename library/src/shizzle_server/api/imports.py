"""Authenticated, bounded completed-media contribution API."""

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..db import create_session_factory
from ..db.models import CompletedImport
from ..publish.completed import (
    MAX_MANIFEST_BYTES,
    ImportRepository,
    InvalidCandidate,
    track_id,
    upload_instructions,
)
from .auth import require_auth
from .media import s3_client

router = APIRouter(prefix="/api/imports")


def contribution_auth(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.auth_enabled:
        raise HTTPException(503, "Contribution requires configured authentication")
    require_auth(request)
    if not settings.shizzle_completed_imports_enabled:
        raise HTTPException(503, "Completed-media contribution is not enabled")


def repository(request: Request) -> ImportRepository:
    return ImportRepository(create_session_factory(request.app.state.engine))


def response(item: CompletedImport) -> dict[str, Any]:
    return {
        "importId": str(item.id),
        "status": item.status,
        "errorCode": item.error_code,
        "trackId": str(track_id(item)) if item.status == "ready" else None,
        "generation": 1 if item.status == "ready" else None,
    }


async def lookup(request: Request, identifier: uuid.UUID) -> CompletedImport:
    item = await repository(request).get(identifier)
    if item is None:
        raise HTTPException(404, "Import not found")
    return item


@router.post("", dependencies=[Depends(contribution_auth)])
async def create_import(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise HTTPException(413, "Manifest exceeds the size limit")
    try:
        manifest = json.loads(raw)
        item = await repository(request).create(manifest)
    except (InvalidCandidate, ValueError, TypeError, RecursionError):
        raise HTTPException(422, "Invalid completed-media manifest") from None
    return response(item)


@router.get("/{identifier}", dependencies=[Depends(contribution_auth)])
async def import_status(request: Request, identifier: uuid.UUID) -> dict[str, Any]:
    return response(await lookup(request, identifier))


@router.post("/{identifier}/uploads", dependencies=[Depends(contribution_auth)])
async def import_uploads(request: Request, identifier: uuid.UUID) -> dict[str, Any]:
    item = await lookup(request, identifier)
    settings = request.app.state.settings
    try:
        uploads = await asyncio.to_thread(
            upload_instructions, item, s3_client(settings), settings.s3_media_bucket
        )
    except Exception:
        raise HTTPException(503, "Upload instructions temporarily unavailable") from None
    return {**response(item), "uploads": uploads}


@router.post("/{identifier}/finalize", dependencies=[Depends(contribution_auth)])
async def finalize_import(request: Request, identifier: uuid.UUID) -> dict[str, Any]:
    item = await lookup(request, identifier)
    if item.status == "uploading":
        settings = request.app.state.settings
        try:
            missing = await asyncio.to_thread(
                upload_instructions, item, s3_client(settings), settings.s3_media_bucket
            )
        except Exception:
            raise HTTPException(503, "Could not verify completed uploads") from None
        if missing:
            raise HTTPException(409, "Upload incomplete; request upload instructions to resume")
    result = await repository(request).finalize(identifier)
    if result is None:
        raise HTTPException(404, "Import not found")
    return response(result)
