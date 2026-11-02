"""FastAPI application factory for the auto-job dashboard."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from autojob import auth, db
from autojob.config import Config, load_config
from autojob.web.routes import router
from autojob.web.worker_routes import router as worker_router

WEB_DIR = Path(__file__).resolve().parent
PUBLIC_PATHS = {"/login", "/logout", "/healthz"}
# The PC worker authenticates with a bearer token inside the route instead.
TOKEN_AUTH_PREFIX = "/api/worker/"


class RequireLoginMiddleware(BaseHTTPMiddleware):
    """Redirects any request outside the public paths to /login when not signed in."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/") or path.startswith(TOKEN_AUTH_PREFIX):
            return await call_next(request)
        if request.session.get("logged_in"):
            return await call_next(request)
        return RedirectResponse("/login", status_code=303)


def create_app(config: Config | None = None) -> FastAPI:
    if config is None:
        config = load_config()
    db.init_db(config)
    secret_key = auth.load_or_create_secret_key(config)

    app = FastAPI(title="auto-job")
    app.state.config = config
    app.state.login_limiter = auth.LoginLimiter()
    app.state.run_lock = threading.Lock()
    app.state.browser_notice = None
    app.state.login_thread = None
    app.state.login_site = None
    app.state.templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # SessionMiddleware must be added after RequireLoginMiddleware: Starlette
    # runs the most recently added middleware first, and the login check
    # needs request.session already populated when it runs.
    app.add_middleware(RequireLoginMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="autojob_session",
        same_site="lax",
        https_only=config.secure_cookies,
    )

    app.include_router(router)
    app.include_router(worker_router)
    return app
