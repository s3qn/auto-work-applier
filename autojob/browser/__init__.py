"""Playwright browser infrastructure: one persistent Chromium profile shared
by every site so Sean logs in once and stays logged in, plus one process-wide
lock so at most one browser session runs at a time (Playwright's sync API is
not safe to run concurrently from several threads against the same profile).
"""

from __future__ import annotations
