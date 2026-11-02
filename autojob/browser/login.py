"""Interactive one-time login helper, and the shared login-session runner
the `/browser` dashboard page's "Open login" buttons also call.

Run with `uv run python -m autojob.browser.login <site>` for
`site in {linkedin, alljobs, jobmaster}`. Starts (or reuses) the Xvfb +
x11vnc stack managed by `autojob.browser.display`, then opens a
headed Chromium window on that shared virtual display, backed by the
persistent profile at `data/chrome-profile/`, so Sean can log in by hand.
The cookies and local storage then persist for every later headless scan or
apply run against that profile.

There is nothing to attach to locally and no `DISPLAY` to set by hand: watch
and type into the login window at https://autojob.sean.build/browser (or,
run directly on this machine, any VNC client pointed at
`127.0.0.1:<x11vnc port>`), since both paths render the exact same Xvfb
display.
"""

from __future__ import annotations

import sys
import time

from autojob.browser.context import LOGIN_URLS, browser_page
from autojob.browser.display import DisplayStackError, load_display_config, start_all
from autojob.config import load_config
from autojob.pipeline.lock import RunInProgress, hold_run_lock

LOGIN_WAIT_SECONDS = 600
POLL_SECONDS = 2


def run_login_session(config, site: str, *, wait_seconds: int = LOGIN_WAIT_SECONDS) -> None:
    """Opens `site`'s login page in a headed Chromium window on the shared
    Xvfb display and waits until Sean closes the window himself or
    `wait_seconds` elapses, whichever comes first. Shared by `main` (below)
    and the `/browser` dashboard's "Open login" buttons (run in a background
    thread there), so both paths log in on the exact same profile and
    display. Does not start the display stack itself: callers that need it
    reachable over VNC/noVNC call `autojob.browser.display.start_all` first.
    """
    url = LOGIN_URLS[site]
    with browser_page(config, visible=True, lock_timeout=wait_seconds + 30) as page:
        page.goto(url, wait_until="domcontentloaded")
        context = page.context
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if not context.pages:
                break  # Sean closed the window himself
            time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in LOGIN_URLS:
        sites = ", ".join(sorted(LOGIN_URLS))
        print(f"Usage: python -m autojob.browser.login <site>\nSites: {sites}")
        return 1
    site = argv[0]
    config = load_config()
    display_config = load_display_config(config)
    try:
        start_all(config)
    except DisplayStackError as exc:
        print(str(exc))
        return 1

    print(f"Opening the {site} login page in a real browser window on the shared virtual display.")
    print(
        "Watch and log in by hand at https://autojob.sean.build/browser, or, on this machine, "
        f"any VNC client pointed at 127.0.0.1:{display_config.x11vnc_port}."
    )
    print("The session is saved to the shared Chrome profile and reused for every later scan.")
    print(f"This closes itself after {LOGIN_WAIT_SECONDS // 60} minutes, or press Ctrl+C once you are done.")
    try:
        # Held for the whole session so a scheduled run never opens the same
        # Chromium profile at the same time.
        with hold_run_lock(config):
            run_login_session(config, site)
    except RunInProgress:
        print("A run is in progress; try the login after it finishes.")
        return 1
    except KeyboardInterrupt:
        print("Stopping early. The session up to this point is already saved.")
    print("Done. The browser profile now holds this login for future runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
