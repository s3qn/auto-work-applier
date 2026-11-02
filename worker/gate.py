"""Submit gate for the worker's browser agent.

The gate decides every tool call from the page snapshot the browser tool
itself returned (the accessible name behind a ref), never from the model's
own description of what it is clicking.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

MCP_PREFIX = "mcp__playwright__"

# Pre-approved tools skip can_use_tool entirely (the SDK allows them before
# the callback runs), so nothing is pre-approved: every call reaches
# Gate.decide and counts against the budget.
PREAPPROVED: tuple[str, ...] = ()
FILL_TOOLS = (
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_check",
    "browser_uncheck",
    "browser_hover",
    "browser_file_upload",
    "browser_take_screenshot",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_tabs",
    "browser_click",
    "browser_press_key",
)
KNOWN_TOOLS = frozenset(FILL_TOOLS)

# Matched against a normalized label (lowercase, single spaces, trailing
# arrows and punctuation removed) by prefix.
FINAL_RE = re.compile(
    r"^(?:(?:submit|send|finish|complete)\b|שלח|שליח|סיום|סיים)", re.IGNORECASE
)
# An apply control opens the form from a listing, so it is allowed until the
# page looks like a form (3+ text boxes) or a file was uploaded.
APPLY_RE = re.compile(r"^(?:apply\b|הגש)", re.IGNORECASE)
TRAILING_RE = re.compile(r"[\s\W_]+$")
SUBMIT_KEYS = frozenset(("enter", "return", "numpadenter", "space", "spacebar", " "))
REF_LINE_RE = re.compile(r"-\s+([\w-]+)(?:\s+\"(.*?)\")?[^\n]*?\[ref=([\w]+)\]")
URL_RE = re.compile(r"Page URL:\s*(\S+)")
MD_IMAGE_RE = re.compile(r"\(([^()\n]+\.(?:png|jpe?g))\)", re.IGNORECASE)
BARE_IMAGE_RE = re.compile(r"([^\s()\[\]\"']+\.(?:png|jpe?g))", re.IGNORECASE)


@dataclass
class Allow:
    pass


@dataclass
class Deny:
    reason: str


def normalize_label(label: str) -> str:
    """Lowercase, collapse whitespace, drop trailing arrows and punctuation."""
    text = " ".join((label or "").split()).lower()
    return TRAILING_RE.sub("", text)


def _path_key(path) -> str:
    return os.path.normcase(str(Path(str(path)).resolve()))


def short_name(tool_name: str) -> str:
    return tool_name[len(MCP_PREFIX):] if tool_name.startswith(MCP_PREFIX) else tool_name


class Gate:
    def __init__(self, submit_allowed: bool, max_tool_calls: int = 45, tools=None, upload_paths=()):
        self.submit_allowed = submit_allowed
        self.upload_paths = frozenset(_path_key(p) for p in upload_paths if p)
        self.uploaded = False
        self.max_tool_calls = max_tool_calls
        self.tools = frozenset(tools) if tools is not None else KNOWN_TOOLS
        self.refs: dict[str, tuple[str, str]] = {}
        self.url = ""
        self.screenshots: list[str] = []
        self.tool_calls = 0
        self.denied = 0
        self.denials: list[str] = []

    def observe(self, tool_name: str, tool_input: dict, result_text: str) -> None:
        """Record what the browser tool returned (snapshot refs, URL, screenshots)."""
        name = short_name(tool_name)
        text = result_text or ""
        url = URL_RE.search(text)
        if url:
            self.url = url.group(1)
        refs = {m.group(3): (m.group(1), m.group(2) or "") for m in REF_LINE_RE.finditer(text)}
        if refs:
            self.refs = refs
        if name == "browser_take_screenshot":
            paths = MD_IMAGE_RE.findall(text) or BARE_IMAGE_RE.findall(text)
            filename = (tool_input or {}).get("filename")
            if not paths and filename:
                paths = [str(filename)]
            for path in paths:
                if path not in self.screenshots:
                    self.screenshots.append(path)

    def _deny(self, reason: str) -> Deny:
        self.denied += 1
        self.denials.append(reason)
        return Deny(reason)

    def _form_field_count(self) -> int:
        return sum(1 for role, _ in self.refs.values() if role in ("textbox", "combobox"))

    def decide(self, tool_name: str, tool_input: dict, context=None):
        name = short_name(tool_name)
        tool_input = tool_input or {}
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            return self._deny("Tool budget exhausted, report now")
        if name not in self.tools:
            return self._deny(f"Tool {name} is not allowed in this session")
        if name == "browser_navigate":
            scheme = urlparse(str(tool_input.get("url", ""))).scheme.lower()
            if scheme not in ("http", "https"):
                return self._deny("Only http(s) URLs may be opened")
        if name == "browser_type" and tool_input.get("submit") and not self.submit_allowed:
            return self._deny(
                "Submitting is not allowed in this session; stop and report filled"
            )
        if name == "browser_file_upload":
            paths = tool_input.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            if any(_path_key(p) not in self.upload_paths for p in paths):
                return self._deny("Only the CV and the cover letter file may be uploaded")
            if paths:
                self.uploaded = True
            return Allow()
        if name == "browser_press_key":
            raw = str(tool_input.get("key", "")).lower()
            key = raw.strip() or raw[:1]  # " " is the Space key
            last = key.rsplit("+", 1)[-1].strip() or " "
            if (key in SUBMIT_KEYS or last in SUBMIT_KEYS) and not self.submit_allowed:
                return self._deny(
                    "Enter and Space are not allowed in this session; stop and report filled"
                )
            return Allow()
        if name == "browser_click":
            ref = tool_input.get("ref")
            if not ref or ref not in self.refs:
                return self._deny("Take a snapshot first")
            _, label = self.refs[ref]
            normalized = normalize_label(label)
            host = (urlparse(self.url).hostname or "").lower()
            on_linkedin = host == "linkedin.com" or host.endswith(".linkedin.com")
            if on_linkedin and "easy apply" in normalized:
                return self._deny(
                    "Easy Apply is never used; follow the external apply link instead"
                )
            if not self.submit_allowed:
                if FINAL_RE.match(normalized) or (
                    APPLY_RE.match(normalized)
                    and (self.uploaded or self._form_field_count() >= 3)
                ):
                    return self._deny(
                        "Submitting is not allowed in this session; stop and report filled"
                    )
        return Allow()
