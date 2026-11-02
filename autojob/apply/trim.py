"""Deterministic cover letter trimming. Only ever removes text, never
rewrites it, so a trim cannot add a claim the letter did not make."""

from __future__ import annotations

import re

_JUNIOR = re.compile(r"junior|ג['\u2019\u05f3]וניור|\bjr\.?", re.IGNORECASE)
_NO_EXPERIENCE = re.compile(
    r"\bno (professional|paid|commercial)\b|\bdo not have\b|\bdon['\u2019]t have\b|\bnot yet\b"
    r"|\blimited experience\b|\black(s|ed|ing)?\b|\bclose to no\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.?!\n]+(?:[.?!]+|$)\s*")


def _sentences(paragraph: str) -> list[str]:
    return [m.group(0).strip() for m in _SENTENCE.finditer(paragraph) if m.group(0).strip()]


def _paragraphs(text: str) -> list[list[str]]:
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        sentences: list[str] = []
        for line in lines:
            found = _sentences(line)
            if found and sentences:
                # A leading newline marks a line break kept inside the paragraph.
                found[0] = "\n" + found[0]
            sentences.extend(found)
        paragraphs.append(sentences)
    return paragraphs


def _join_sentences(sentences: list[str]) -> str:
    out = ""
    for s in sentences:
        out += s if s.startswith("\n") or not out else " " + s
    return out.strip()


def _join(paragraphs: list[list[str]]) -> str:
    return "\n\n".join(_join_sentences(p) for p in paragraphs if p)


def trim_cover_letter(text: str) -> str:
    kept_experience = False
    result: list[list[str]] = []
    for sentences in _paragraphs(text or ""):
        out = []
        for sentence in sentences:
            if _JUNIOR.search(sentence):
                continue
            if _NO_EXPERIENCE.search(sentence):
                if kept_experience:
                    continue
                kept_experience = True
            out.append(sentence)
        result.append(out)
    return _join(result)


def short_variant(text: str, limit: int = 1000) -> str:
    paragraphs = _paragraphs(trim_cover_letter(text))
    paragraphs = [p for p in paragraphs if p]
    while len(_join(paragraphs)) > limit:
        # Greeting, first paragraph, closing and signature stay: only the
        # paragraphs strictly between the first body paragraph and the last
        # two blocks (closing, signature) are candidates.
        middle = [i for i in range(2, len(paragraphs) - 2) if paragraphs[i]]
        if not middle:
            break
        index = middle[-1]
        paragraphs[index].pop()
    joined = _join(paragraphs)
    if len(joined) <= limit:
        return joined
    cut = joined[:limit]
    ends = [cut.rfind(c) for c in ".?!"]
    best = max(ends)
    return cut[: best + 1] if best > 0 else cut.rstrip()
