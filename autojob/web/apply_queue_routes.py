"""HTTP API for the daily PC apply session. Bearer token (`apply_token`),
no cookies. Plain `def` routes run in the threadpool: building a batch can
call the claude CLI and must never block the event loop."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import FileResponse

from autojob import apply_token, db
from autojob.apply import queue
from autojob.config import Config
from autojob.llm.cv import latest_cv_pdf
from autojob.web.deps import get_config, get_db


def require_apply_token(request: Request, config: Config = Depends(get_config)) -> None:
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not presented or not apply_token.verify_apply_token(config, presented):
        raise HTTPException(status_code=401, detail="apply token required")


router = APIRouter(dependencies=[Depends(require_apply_token)])


class ApplyResult(BaseModel):
    id: int
    status: Literal["submitted", "blocked", "failed"]
    reason: Optional[str] = None
    cover_letter_used: Optional[str] = None
    cv_version: Optional[str] = None
    timestamp: Optional[str] = None


@router.get("/api/apply-queue")
def get_apply_queue(config: Config = Depends(get_config), conn=Depends(get_db)):
    db.set_setting(conn, "apply_queue_last_pull", db.now_iso())
    return queue.get_or_create_today(conn, config)


@router.post("/api/apply-result")
def post_apply_result(body: ApplyResult, config: Config = Depends(get_config), conn=Depends(get_db)):
    try:
        result = queue.record_result(
            conn, job_id=body.id, status=body.status, reason=body.reason,
            cover_letter_used=body.cover_letter_used, cv_version_used=body.cv_version, timestamp=body.timestamp,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return result


@router.get("/api/cv/latest")
def get_latest_cv(config: Config = Depends(get_config)):
    cv = latest_cv_pdf(config)
    if cv is None:
        raise HTTPException(status_code=404, detail="no CV uploaded")
    return FileResponse(cv, media_type="application/pdf", filename=cv.name,
                        headers={"X-CV-Version": queue.cv_version(config)})
