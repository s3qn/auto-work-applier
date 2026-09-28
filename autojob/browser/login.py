"""Interactive one-time login helper.

Run with `uv run python -m autojob.browser.login <site>` for
`site in {linkedin, alljobs, jobmaster}`. Opens a headed Chromium window on
the persistent profile at `data/chrome-profile/` so Sean can log in by hand;
the cookies and local storage then persist for every later headless scan or
apply run against that profile.

This machine has no display by default. If `DISPLAY` is not set, this
re-launches itself under `xvfb-run` (a virtual framebuffer), so the browser
still runs headed as far as Playwright is concerned, on a virtual display
Sean is not looking at. That is fine: a headed browser behaves more like a
real user than a headless one for login flows with bot checks, even though
nobody is watching. If `xvfb-run` is not installed, this prints how to
install it instead of failing silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from autojob.browser.context import LOGIN_URLS, USER_AGENT, VIEWPORT, profile_dir
from autojob.config import load_config

LOGIN_WAIT_SECONDS = 600
POLL_SECONDS = 2


def _relaunch_under_xvfb(site: str) -> int:
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        print(
            "No DISPLAY is set and xvfb-run is not installed.\n"
            "Install it with:\n"
            "  sudo apt-get install -y xvfb\n"
            "then run this command again."
        )
        return 1
    # -u: unbuffered, so the status lines below show up immediately even
    # though this whole invocation is not attached to a real terminal.
    cmd = [xvfb_run, "-a", sys.executable, "-u", "-m", "autojob.browser.login", site]
    return subprocess.call(cmd)


def _run_login(site: str) -> int:
    from playwright.sync_api import sync_playwright

    config = load_config()
    url = LOGIN_URLS[site]
    target_dir = profile_dir(config)
    print(f"Opening the {site} login page in a real browser window.")
    print(f"Log in by hand. The session is saved to {target_dir} and reused for every later scan.")
    print(f"This closes itself after {LOGIN_WAIT_SECONDS // 60} minutes, or press Ctrl+C once you are done.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(target_dir),
            headless=False,
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            deadline = time.time() + LOGIN_WAIT_SECONDS
            while time.time() < deadline:
                if not context.pages:
                    break  # Sean closed the window himself
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("Stopping early. Saving the session.")
        finally:
            context.close()
    print("Done. The browser profile now holds this login for future runs.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in LOGIN_URLS:
        sites = ", ".join(sorted(LOGIN_URLS))
        print(f"Usage: python -m autojob.browser.login <site>\nSites: {sites}")
        return 1
    site = argv[0]
    if not os.environ.get("DISPLAY"):
        return _relaunch_under_xvfb(site)
    return _run_login(site)


if __name__ == "__main__":
    raise SystemExit(main())
