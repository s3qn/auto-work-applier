"""Persistent Chromium context and the one-browser-at-a-time lock.

`browser_page()` is the entry point every caller (the AllJobs source, the
apply handlers, the login helper) should use. It yields a Playwright `Page`
backed by the persistent profile at `data/chrome-profile/`, so cookies and
local storage survive between calls: Sean logs into a site once through
`python -m autojob.browser.login <site>` and every later scan or application
reuses that session.

Playwright's sync API is not thread-safe against a shared profile, so only
one caller may hold the browser at a time. `browser_page()` blocks (with a
timeout) until it can acquire that lock, then releases it when the `with`
block exits.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from autojob.config import Config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1366, "height": 900}
DEFAULT_TIMEOUT_MS = 30_000
NAV_TIMEOUT_MS = 45_000
DEFAULT_LOCK_TIMEOUT_S = 300.0

# Best-known login page for each site the browser needs a saved session for.
# These only matter for the interactive `login` helper; a wrong guess still
# opens a real browser window Sean can navigate from by hand.
LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "alljobs": "https://www.alljobs.co.il/User/Login.aspx",
    "jobmaster": "https://www.jobmaster.co.il/Login/Login",
}

_BROWSER_LOCK = threading.Lock()


class BrowserBusyError(Exception):
    """Raised when another caller is already holding the browser lock."""


def profile_dir(config: Config) -> Path:
    path = config.data_dir / "chrome-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def visible_launch_env(config: Config) -> dict[str, str]:
    """The environment for launching Chromium against the Xvfb display that
    backs the `/browser` view (see `autojob.browser.display`), so it shows up
    there instead of on this process's own `DISPLAY` (there usually isn't
    one). Starts from a copy of the current environment rather than
    replacing it outright: Playwright's `env` argument, when given, is the
    whole environment the browser process sees, and dropping `PATH` and
    everything else would break the browser launch itself."""
    from autojob.browser.display import load_display_config

    env = dict(os.environ)
    env["DISPLAY"] = load_display_config(config).display
    return env


@contextmanager
def browser_page(
    config: Config,
    *,
    headless: bool = True,
    visible: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> Iterator["Page"]:  # noqa: F821 - Playwright type, imported lazily below
    """Yield a `Page` from the persistent Chromium profile.

    Acquires the process-wide browser lock first (raising `BrowserBusyError`
    if it cannot within `lock_timeout` seconds), launches a persistent
    context against `data/chrome-profile/`, and closes it again on exit.

    `visible=True` overrides `headless` to `False` and points Chromium at the
    shared Xvfb display managed by `autojob.browser.display`, so the window
    can be watched and interacted with through the `/browser` page's noVNC
    view (or any VNC client, on the box itself). This is how the one-time
    login helper (`autojob.browser.login`) and the apply pipeline's optional
    "watch it fill forms" mode both work. Unattended scans and applies keep
    the default, `headless=True`, `visible=False`.
    """
    if not _BROWSER_LOCK.acquire(timeout=lock_timeout):
        raise BrowserBusyError(
            "Another browser session is already running. Try again shortly."
        )
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch_kwargs: dict = dict(
                headless=False if visible else headless,
                user_agent=USER_AGENT,
            )
            if visible:
                # No fixed viewport, so the page tracks the window size and
                # fills the whole Xvfb screen in the noVNC view.
                launch_kwargs["no_viewport"] = True
                launch_kwargs["env"] = visible_launch_env(config)
                from autojob.browser.display import load_display_config

                width, height = load_display_config(config).geometry.split("x")[:2]
                launch_kwargs["args"] = [
                    "--window-position=0,0",
                    f"--window-size={width},{height}",
                ]
            else:
                launch_kwargs["viewport"] = VIEWPORT
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir(config)), **launch_kwargs
            )
            context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                yield page
            finally:
                context.close()
    finally:
        _BROWSER_LOCK.release()
