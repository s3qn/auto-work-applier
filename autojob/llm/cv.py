"""Extracts Sean's CV text from the newest PDF in the CV folder, with a
disk cache so a run doesn't re-parse the PDF every time nothing changed.
"""

from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

from autojob.config import Config

CACHE_FILENAME = "cv_text_cache.json"


def latest_cv_pdf(config: Config) -> Path | None:
    candidates = sorted(config.cv_dir.glob("*.pdf"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def get_cv_text(config: Config) -> str:
    """Return the extracted text of Sean's newest CV PDF, or "" if none is
    uploaded yet."""
    cv_file = latest_cv_pdf(config)
    if cv_file is None:
        return ""
    cache_path = config.data_dir / CACHE_FILENAME
    stamp = f"{cv_file.name}:{cv_file.stat().st_mtime_ns}"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            cached = {}
        if cached.get("stamp") == stamp and cached.get("text"):
            return cached["text"]
    reader = PdfReader(str(cv_file))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    try:
        cache_path.write_text(json.dumps({"stamp": stamp, "text": text}))
    except OSError:
        pass
    return text
