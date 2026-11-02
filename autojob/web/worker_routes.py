"""HTTP API for the PC worker (`/api/worker/*`). Bearer-token auth, no
cookies, so the session login and CSRF checks do not apply. The logic lives
in `autojob.apply.remote`; these routes stay thin."""

from __future__ import annotations

import hmac
import json
import re
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, Response

from autojob import db, worker_token
from autojob.apply import proof, remote
from autojob.config import Config
from autojob.llm.cv import latest_cv_pdf
from autojob.web.deps import get_config, get_db

MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_SCREENSHOTS = 10
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Claims used to run one at a time on the event loop; the lock keeps that
# order now that they run in the threadpool (the pilot cap counts on it).
_CLAIM_LOCK = threading.Lock()


def require_worker_token(request: Request, config: Config = Depends(get_config), conn=Depends(get_db)) -> None:
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not presented or not worker_token.verify_worker_token(config, presented):
        raise HTTPException(status_code=401, detail="worker token required")
    db.set_setting(conn, "worker_last_seen", db.now_iso())


router = APIRouter(prefix="/api/worker", dependencies=[Depends(require_worker_token)])


def _leased_job(conn, job_id: int, lease_token: str):
    job = db.get_job_with_company(conn, job_id)
    held = job["worker_lease_token"] if job is not None else None
    if (
        job is None
        or not held
        or not hmac.compare_digest(held, lease_token or "")
        or job["status"] != "applying"
        or (job["worker_lease_until"] or "") < db.now_iso()
    ):
        raise HTTPException(status_code=409, detail="lease is not held")
    return job


@router.post("/claim")
async def claim(request: Request, config: Config = Depends(get_config), conn=Depends(get_db)):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    worker = str(body.get("worker", "")) if isinstance(body, dict) else ""
    # claim_next can make a sonnet call (the cover letter): keep it off the event loop.
    claimed = await run_in_threadpool(_claim_locked, conn, config, worker)
    if claimed is None:
        return Response(status_code=204)
    return JSONResponse(claimed)


def _claim_locked(conn, config: Config, worker: str):
    with _CLAIM_LOCK:
        return remote.claim_next(conn, config, worker)


@router.get("/cv")
def download_cv(config: Config = Depends(get_config)):
    cv = latest_cv_pdf(config)
    if cv is None:
        raise HTTPException(status_code=404, detail="no CV uploaded")
    return FileResponse(cv, media_type="application/pdf", filename=cv.name)


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat(job_id: int, request: Request, conn=Depends(get_db)):
    body = await request.json()
    token = str(body.get("lease_token", "")) if isinstance(body, dict) else ""
    _leased_job(conn, job_id, token)
    lease_until = remote._lease_until()
    if not db.extend_lease(conn, job_id, token, lease_until):
        raise HTTPException(status_code=409, detail="lease is not held")
    return {"lease_until": lease_until}


def _safe_name(raw: str, prefix: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw or "shot.png").name)[:80] or "shot.png"
    if not name.lower().endswith(".png"):
        name += ".png"
    return f"{prefix}{name}"


@router.post("/jobs/{job_id}/attempts")
async def post_attempt(
    job_id: int,
    lease_token: str = Form(...),
    payload: str = Form(...),
    screenshots: list[UploadFile] = File(default=[]),
    config: Config = Depends(get_config),
    conn=Depends(get_db),
):
    job = await run_in_threadpool(_leased_job, conn, job_id, lease_token)
    try:
        data = json.loads(payload)
        remote.validate_payload(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad payload: {exc}") from exc
    if len(screenshots) > MAX_SCREENSHOTS:
        raise HTTPException(status_code=400, detail="too many screenshots")
    contents = []
    for upload in screenshots:
        blob = await upload.read(MAX_SCREENSHOT_BYTES + 1)
        if len(blob) > MAX_SCREENSHOT_BYTES:
            raise HTTPException(status_code=400, detail="screenshot too large")
        if not blob.startswith(PNG_SIGNATURE):
            raise HTTPException(status_code=400, detail="screenshot is not a PNG")
        contents.append((upload.filename, blob))
    # Saving files and record_attempt (which can send email over SMTP) run
    # in the threadpool so they never block the event loop.
    return await run_in_threadpool(_store_attempt, conn, config, job, data, contents)


def _store_attempt(conn, config: Config, job, data: dict, contents: list) -> dict:
    application_id = job["worker_application_id"]
    out_dir = proof.application_dir(config, application_id)
    kind = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("kind") or "x"))
    phase = re.sub(r"[^A-Za-z0-9_-]", "", str(job["worker_phase"] or "fill"))
    prefix = f"attempt-{data['index']}-{kind}-{phase}-"
    saved: list[Path] = []
    for filename, blob in contents:
        path = out_dir / _safe_name(filename, prefix)
        path.write_bytes(blob)
        db.create_proof(conn, application_id=application_id, kind="screenshot", path=str(path))
        saved.append(path)
    if data["status"] == "submitted" and saved:
        db.create_proof(conn, application_id=application_id, kind="confirmation", path=str(saved[-1]))

    return remote.record_attempt(conn, config, job, application_id, data)


@router.post("/jobs/{job_id}/release")
async def release(job_id: int, request: Request, conn=Depends(get_db)):
    body = await request.json()
    token = str(body.get("lease_token", "")) if isinstance(body, dict) else ""
    reason = str(body.get("reason", "")) if isinstance(body, dict) else ""
    job = _leased_job(conn, job_id, token)
    application_id = job["worker_application_id"]
    if job["worker_phase"] == "fill" and application_id is not None:
        if db.list_proofs(conn, application_id):
            db.update_application(
                conn, application_id, status="failed", notes=f"PC worker released the job: {reason}"
            )
        else:
            db.delete_application(conn, application_id)
    db.release_lease(conn, job_id)
    return {"status": "queued"}
