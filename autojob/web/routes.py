"""All page and form routes for the dashboard."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from autojob import auth, db
from autojob.config import Config
from autojob.pipeline import run as run_module
from autojob.web.deps import (
    base_context,
    get_auto_submit,
    get_config,
    get_db,
    get_limiter,
    get_profile_answers,
    get_targets,
    get_templates,
    verify_csrf_or_403,
)

router = APIRouter()

STATUS_CHOICES = ("sent", "replied", "interview", "rejected", "not_interested")
CV_ALLOWED_EXTENSIONS = {".pdf", ".docx"}
CV_MAX_BYTES = 10 * 1024 * 1024
PROOF_MEDIA_TYPES = {".png": "image/png", ".html": "text/html", ".pdf": "application/pdf", ".txt": "text/plain"}


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned or "file"


# --- auth -----------------------------------------------------------------

@router.get("/login")
def login_get(request: Request, config: Config = Depends(get_config), templates: Jinja2Templates = Depends(get_templates)):
    if not auth.password_is_set(config):
        return templates.TemplateResponse(request, "login.html", {"no_password": True})
    token = auth.csrf_token_for_session(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"no_password": False, "csrf_token": token, "error": None},
    )


@router.post("/login")
def login_post(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
    config: Config = Depends(get_config),
    limiter: auth.LoginLimiter = Depends(get_limiter),
    templates: Jinja2Templates = Depends(get_templates),
):
    if not auth.password_is_set(config):
        return templates.TemplateResponse(request, "login.html", {"no_password": True})
    if not auth.verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid session token. Reload the page and try again.")
    ip = auth.get_client_ip(request)
    if limiter.is_locked(ip):
        return PlainTextResponse(
            "Too many failed login attempts. Wait 15 minutes and try again.", status_code=429
        )
    if auth.verify_password(config, password):
        limiter.reset(ip)
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    limiter.record_failure(ip)
    new_token = auth.csrf_token_for_session(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"no_password": False, "csrf_token": new_token, "error": "Incorrect password."},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    verify_csrf_or_403(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


# --- applications -----------------------------------------------------

@router.get("/")
def applications_home(
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    ctx["applications"] = db.list_applications_with_jobs(conn)
    ctx["stats"] = db.get_dashboard_stats(conn)
    return templates.TemplateResponse(request, "applications.html", ctx)


@router.get("/applications/{application_id}")
def application_detail(
    application_id: int,
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    application = db.get_application_with_job(conn, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    ctx = base_context(request, conn)
    ctx["application"] = application
    ctx["proofs"] = db.list_proofs(conn, application_id)
    return templates.TemplateResponse(request, "application_detail.html", ctx)


@router.post("/applications/{application_id}/status")
def update_application_status(
    application_id: int,
    request: Request,
    status: str = Form(...),
    csrf_token: str = Form(...),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    verify_csrf_or_403(request, csrf_token)
    if status not in STATUS_CHOICES:
        raise HTTPException(status_code=400, detail="Unknown status.")
    if db.get_application(conn, application_id) is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    db.update_application(conn, application_id, status=status)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/status_chip.html", {"status": status})
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@router.post("/applications/{application_id}/notes")
def update_application_notes(
    application_id: int,
    request: Request,
    notes: str = Form(""),
    csrf_token: str = Form(...),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    verify_csrf_or_403(request, csrf_token)
    if db.get_application(conn, application_id) is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    db.update_application(conn, application_id, notes=notes)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/notes_saved.html", {})
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@router.get("/applications/{application_id}/proofs/{proof_id}")
def application_proof_file(application_id: int, proof_id: int, conn=Depends(get_db)):
    """Serves one proof file to the logged-in user only. Never mounted as a
    static directory: RequireLoginMiddleware guards every route that isn't
    explicitly public, and this checks the proof actually belongs to the
    application in the URL before reading anything off disk."""
    proof_row = db.get_proof(conn, proof_id)
    if proof_row is None or proof_row["application_id"] != application_id:
        raise HTTPException(status_code=404, detail="Proof not found.")
    path = Path(proof_row["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Proof file is missing on disk.")
    media_type = PROOF_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=path.name)


def _release_application_and_release_lock(application_id: int, lock: threading.Lock) -> None:
    from autojob.apply.apply import release_application

    try:
        release_application(application_id)
    finally:
        lock.release()


@router.post("/applications/{application_id}/release")
def application_release(
    application_id: int,
    request: Request,
    csrf_token: str = Form(...),
    conn=Depends(get_db),
):
    verify_csrf_or_403(request, csrf_token)
    application = db.get_application(conn, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    if application["status"] != "ready_to_submit":
        raise HTTPException(status_code=400, detail="Only a ready-to-submit application can be released.")
    lock: threading.Lock = request.app.state.run_lock
    if not lock.acquire(blocking=False):
        return RedirectResponse(f"/applications/{application_id}?busy=1", status_code=303)
    thread = threading.Thread(
        target=_release_application_and_release_lock, args=(application_id, lock), daemon=True
    )
    thread.start()
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@router.post("/applications/{application_id}/discard")
def application_discard(
    application_id: int,
    request: Request,
    csrf_token: str = Form(...),
    conn=Depends(get_db),
):
    verify_csrf_or_403(request, csrf_token)
    application = db.get_application(conn, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    if application["status"] != "ready_to_submit":
        raise HTTPException(status_code=400, detail="Only a ready-to-submit application can be discarded.")
    db.update_application(conn, application_id, status="discarded")
    db.update_job_status(conn, application["job_id"], "failed")
    return RedirectResponse("/", status_code=303)


# --- jobs found ------------------------------------------------------------

JOB_STATUS_TABS = ("all", "queued", "filtered_out")


@router.get("/jobs")
def jobs_list(
    request: Request,
    status: str = "all",
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    status_filter = status if status in ("queued", "filtered_out") else None
    ctx["jobs"] = db.list_jobs_with_company(conn, status=status_filter)
    ctx["status"] = status if status in JOB_STATUS_TABS else "all"
    return templates.TemplateResponse(request, "jobs.html", ctx)


@router.get("/companies/{company_id}/tldr")
def company_tldr(
    company_id: int,
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    company = db.get_company(conn, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    return templates.TemplateResponse(request, "partials/company_tldr.html", {"request": request, "company": company})


@router.get("/jobs/{job_id}")
def job_detail(
    job_id: int,
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    job = db.get_job_with_company(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    ctx = base_context(request, conn)
    ctx["job"] = job
    ctx["applications"] = [a for a in db.list_applications_with_jobs(conn) if a["job_id"] == job_id]
    return templates.TemplateResponse(request, "job_detail.html", ctx)


def _apply_job_in_background(job, auto_submit: bool) -> None:
    from autojob.apply.apply import apply_to_job

    try:
        apply_to_job(job, auto_submit=auto_submit)
    except Exception:  # noqa: BLE001 - apply_to_job already handles its own failures; last-resort net
        pass


@router.post("/jobs/{job_id}/apply")
def job_apply_now(
    job_id: int,
    request: Request,
    csrf_token: str = Form(...),
    conn=Depends(get_db),
):
    verify_csrf_or_403(request, csrf_token)
    job = db.get_job_with_company(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    auto_submit = get_auto_submit(conn)
    thread = threading.Thread(target=_apply_job_in_background, args=(job, auto_submit), daemon=True)
    thread.start()
    redirect_to = request.headers.get("referer") or f"/jobs/{job_id}"
    return RedirectResponse(redirect_to, status_code=303)


# --- needs help -----------------------------------------------------------

@router.get("/needs-help")
def needs_help_list(
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    ctx["items"] = db.list_open_help_items(conn)
    return templates.TemplateResponse(request, "needs_help.html", ctx)


@router.post("/help/{help_id}/resolve")
def help_resolve(help_id: int, request: Request, csrf_token: str = Form(...), conn=Depends(get_db)):
    verify_csrf_or_403(request, csrf_token)
    if db.get_help_item(conn, help_id) is None:
        raise HTTPException(status_code=404, detail="Help item not found.")
    db.resolve_help_item(conn, help_id, "done_manually")
    return RedirectResponse("/needs-help", status_code=303)


@router.post("/help/{help_id}/skip")
def help_skip(help_id: int, request: Request, csrf_token: str = Form(...), conn=Depends(get_db)):
    verify_csrf_or_403(request, csrf_token)
    if db.get_help_item(conn, help_id) is None:
        raise HTTPException(status_code=404, detail="Help item not found.")
    db.resolve_help_item(conn, help_id, "skipped")
    return RedirectResponse("/needs-help", status_code=303)


@router.post("/help/{help_id}/answer")
def help_answer(
    help_id: int,
    request: Request,
    answer: str = Form(...),
    csrf_token: str = Form(...),
    conn=Depends(get_db),
):
    verify_csrf_or_403(request, csrf_token)
    item = db.get_help_item(conn, help_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Help item not found.")
    db.resolve_help_item(conn, help_id, f"answered: {answer}")
    db.update_job_status(conn, item["job_id"], "queued")
    return RedirectResponse("/needs-help", status_code=303)


# --- submit a link ------------------------------------------------------

@router.get("/submit")
def submit_get(
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    ctx["recent"] = db.list_jobs(conn, source="manual")
    ctx["error"] = None
    return templates.TemplateResponse(request, "submit.html", ctx)


@router.post("/submit")
def submit_post(
    request: Request,
    url: str = Form(...),
    csrf_token: str = Form(...),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    verify_csrf_or_403(request, csrf_token)
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    error = None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        error = "Enter a valid http or https URL."
    else:
        job_id = db.create_job(conn, source="manual", url=cleaned, title=cleaned, status="queued")
        if job_id is None:
            error = "This link was already submitted."
    ctx = base_context(request, conn)
    ctx["recent"] = db.list_jobs(conn, source="manual")
    ctx["error"] = error
    return templates.TemplateResponse(request, "submit.html", ctx)


# --- targets -------------------------------------------------------------

@router.get("/targets")
def targets_get(
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    ctx["targets"] = get_targets(conn)
    ctx["auto_submit"] = get_auto_submit(conn)
    ctx["saved"] = False
    return templates.TemplateResponse(request, "targets.html", ctx)


@router.post("/targets")
def targets_post(
    request: Request,
    titles: str = Form(""),
    locations: str = Form(""),
    max_years: int = Form(3),
    excluded_companies: str = Form(""),
    priority_companies: str = Form(""),
    daily_cap_per_source: int = Form(20),
    scan_times: str = Form(""),
    auto_submit: bool = Form(False),
    csrf_token: str = Form(...),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    verify_csrf_or_403(request, csrf_token)

    def lines(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]

    new_targets = {
        "titles": lines(titles),
        "locations": lines(locations),
        "max_years": max_years,
        "excluded_companies": lines(excluded_companies),
        "priority_companies": lines(priority_companies),
        "daily_cap_per_source": daily_cap_per_source,
        "scan_times": lines(scan_times),
    }
    db.set_setting(conn, "targets", new_targets)
    db.set_setting(conn, "auto_submit", auto_submit)
    ctx = base_context(request, conn)
    ctx["targets"] = get_targets(conn)
    ctx["auto_submit"] = get_auto_submit(conn)
    ctx["saved"] = True
    return templates.TemplateResponse(request, "targets.html", ctx)


# --- run log -------------------------------------------------------------

@router.get("/runs")
def runs_list(
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    ctx["runs"] = db.list_runs(conn)
    return templates.TemplateResponse(request, "runs.html", ctx)


@router.get("/runs/{run_id}")
def run_detail(
    run_id: int,
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    run_row = db.get_run(conn, run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    ctx = base_context(request, conn)
    ctx["run"] = run_row
    return templates.TemplateResponse(request, "run_detail.html", ctx)


@router.get("/runs/{run_id}/log")
def run_log_partial(
    run_id: int,
    request: Request,
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    run_row = db.get_run(conn, run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return templates.TemplateResponse(request, "partials/run_log_card.html", {"request": request, "run": run_row})


def _run_scan_and_release(lock: threading.Lock) -> None:
    try:
        run_module.run_scan(trigger="manual")
    finally:
        lock.release()


@router.post("/runs/now")
def run_now(request: Request, csrf_token: str = Form(...), conn=Depends(get_db)):
    verify_csrf_or_403(request, csrf_token)
    lock: threading.Lock = request.app.state.run_lock
    if db.run_in_progress(conn) or not lock.acquire(blocking=False):
        return RedirectResponse("/runs?busy=1", status_code=303)
    thread = threading.Thread(target=_run_scan_and_release, args=(lock,), daemon=True)
    thread.start()
    return RedirectResponse("/runs", status_code=303)


# --- profile and CV -------------------------------------------------------

@router.get("/profile")
def profile_get(
    request: Request,
    config: Config = Depends(get_config),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    ctx = base_context(request, conn)
    ctx["cv_files"] = sorted(p.name for p in config.cv_dir.glob("*") if p.is_file())
    ctx["answers"] = get_profile_answers(conn)
    ctx["cv_error"] = None
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/profile/cv")
async def profile_cv_upload(
    request: Request,
    csrf_token: str = Form(...),
    cv_file: UploadFile = File(...),
    config: Config = Depends(get_config),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    verify_csrf_or_403(request, csrf_token)
    filename = cv_file.filename or ""
    ext = Path(filename).suffix.lower()
    content = await cv_file.read()
    error = None
    if ext not in CV_ALLOWED_EXTENSIONS:
        error = "Only PDF and DOCX files are allowed."
    elif len(content) > CV_MAX_BYTES:
        error = "File is larger than 10 MB."
    else:
        stamp = db.now_iso().replace(":", "").replace("-", "")
        safe_name = f"{stamp}_{sanitize_filename(filename)}"
        (config.cv_dir / safe_name).write_bytes(content)
    ctx = base_context(request, conn)
    ctx["cv_files"] = sorted(p.name for p in config.cv_dir.glob("*") if p.is_file())
    ctx["answers"] = get_profile_answers(conn)
    ctx["cv_error"] = error
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/profile/cv/delete")
def profile_cv_delete(
    request: Request,
    filename: str = Form(...),
    csrf_token: str = Form(...),
    config: Config = Depends(get_config),
):
    verify_csrf_or_403(request, csrf_token)
    target = (config.cv_dir / filename).resolve()
    cv_dir_resolved = config.cv_dir.resolve()
    if target.parent == cv_dir_resolved and target.exists():
        target.unlink()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/answers")
def profile_answers_post(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    city: str = Form(""),
    date_of_birth: str = Form(""),
    military_service: str = Form(""),
    driving_licence: str = Form(""),
    salary_expectation: str = Form(""),
    availability_notice: str = Form(""),
    willing_to_relocate: str = Form(""),
    work_mode_preference: str = Form(""),
    linkedin_url: str = Form(""),
    github_url: str = Form(""),
    htb_profile_url: str = Form(""),
    certifications: str = Form(""),
    languages: str = Form(""),
    about_me: str = Form(""),
    csrf_token: str = Form(...),
    config: Config = Depends(get_config),
    conn=Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    verify_csrf_or_403(request, csrf_token)
    answers = {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "city": city,
        "date_of_birth": date_of_birth,
        "military_service": military_service,
        "driving_licence": driving_licence,
        "salary_expectation": salary_expectation,
        "availability_notice": availability_notice,
        "willing_to_relocate": willing_to_relocate,
        "work_mode_preference": work_mode_preference,
        "linkedin_url": linkedin_url,
        "github_url": github_url,
        "htb_profile_url": htb_profile_url,
        "certifications": certifications,
        "languages": languages,
        "about_me": about_me,
    }
    db.set_setting(conn, "profile.answers", answers)
    ctx = base_context(request, conn)
    ctx["cv_files"] = sorted(p.name for p in config.cv_dir.glob("*") if p.is_file())
    ctx["answers"] = get_profile_answers(conn)
    ctx["cv_error"] = None
    return templates.TemplateResponse(request, "profile.html", ctx)
