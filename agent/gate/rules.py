"""Deterministic request parsing: the fast path that needs no LLM.

Returns a ParsedRequest only when the email is unambiguous (exactly one matter, exactly one
document type). Anything else returns `None` with a reason and goes to the LLM classifier.
"""

import re
from dataclasses import dataclass

from agent.models import DocType, Intent, ParsedRequest

# "M12205", "m12205", "M-12205", "M 12205"; not inside longer tokens like "AM123456"
_MATTER_RE = re.compile(r"(?<![A-Za-z0-9])[Mm][-\s]?(\d{5})(?!\d)")
# "matter 12205", "matter no. 12205", "matter #12205"
_MATTER_WORD_RE = re.compile(r"\bmatter\s*(?:no\.?|number|#)?\s*:?\s*(\d{5})(?!\d)", re.IGNORECASE)

_DOC_TYPE_PATTERNS: dict[DocType, re.Pattern[str]] = {
    DocType.KEY_DOCUMENTS: re.compile(r"\bkey\s+(?:documents?|docs?|files?|filings?)\b", re.IGNORECASE),
    DocType.OTHER_DOCUMENTS: re.compile(r"\bother\s+(?:documents?|docs?|files?|filings?)\b", re.IGNORECASE),
    DocType.EXHIBITS: re.compile(r"\bexhibits?\b", re.IGNORECASE),
    DocType.TRANSCRIPTS: re.compile(r"\b(?:hearing\s+)?transcripts?\b", re.IGNORECASE),
    DocType.RECORDINGS: re.compile(r"\b(?:recordings?|audio|video)\b", re.IGNORECASE),
}

_COUNT_RE = re.compile(
    r"\b(?:first|latest|last|top|up\s+to|only|just)?\s*(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:of\s+the\s+)?(?:most\s+recent\s+|latest\s+|newest\s+)?(?:key\s+|other\s+)?"
    r"(?:documents?|docs?|files?|exhibits?|transcripts?|recordings?)\b",
    re.IGNORECASE,
)
_WORDS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"])}

# Phrases that need judgement (negation, conditions, comparisons): defer to the LLM.
_AMBIGUITY_RE = re.compile(
    r"\b(?:not|don't|dont|do\s+not|except|instead|rather\s+than|unless|either|or\s+the|"
    r"compare|difference|which\s+one|what\s+is|what's|why|how\s+many|summar)",
    re.IGNORECASE,
)
# Request phrasing: an email that names a matter and a tab but never asks for anything is
# probably a forward or a signature block, not a request.
_ASK_RE = re.compile(
    r"\b(?:send|give|get|fetch|share|forward|provide|email|need|want|pull|download|grab|"
    r"can\s+you|could\s+you|would\s+you|please|request|looking\s+for)\b",
    re.IGNORECASE,
)


# Anything that tries to steer the agent, or names another mailbox, gets a closer look from the
# classifier. (It is harmless either way: replies only ever go to the authenticated sender.)
_SUSPICIOUS_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+|\bignore\b.{0,40}\binstructions?\b|system\s+prompt|"
    r"\b(?:admin|developer|debug)\s+mode\b|\bact\s+as\b|\byou\s+are\s+now\b|\bjailbreak|"
    r"\b(?:bcc|cc)\b|https?://",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RuleResult:
    parsed: ParsedRequest | None
    reason: str
    matters: tuple[str, ...]
    doc_types: tuple[DocType, ...]


def find_matters(text: str) -> tuple[str, ...]:
    found = [f"M{m}" for m in _MATTER_RE.findall(text)]
    found += [f"M{m}" for m in _MATTER_WORD_RE.findall(text)]
    return tuple(dict.fromkeys(found))  # dedupe, keep first-mention order


def find_doc_types(text: str) -> tuple[DocType, ...]:
    hits: list[tuple[int, DocType]] = []
    for doc_type, pat in _DOC_TYPE_PATTERNS.items():
        m = pat.search(text)
        if m:
            hits.append((m.start(), doc_type))
    # "key documents"/"other documents" also contain the generic word "documents"; that's fine
    # because only the qualified forms are patterns.
    return tuple(dt for _, dt in sorted(hits))


def find_count(text: str, cap: int) -> int:
    m = _COUNT_RE.search(text)
    if not m:
        return cap
    raw = m.group(1).lower()
    n = int(raw) if raw.isdigit() else _WORDS.get(raw, cap)
    return max(1, min(n, cap))


def parse(subject: str, body: str, *, max_docs: int = 10) -> RuleResult:
    text = f"{subject}\n{body}"
    matters = find_matters(text)
    doc_types = find_doc_types(text)

    def miss(reason: str) -> RuleResult:
        return RuleResult(None, reason, matters, doc_types)

    if len(body) > 2_000:
        return miss("long_body")
    if len(matters) != 1:
        return miss("no_matter" if not matters else "multiple_matters")
    if len(doc_types) != 1:
        return miss("no_doc_type" if not doc_types else "multiple_doc_types")
    if _AMBIGUITY_RE.search(text):
        return miss("ambiguous_phrasing")
    if _SUSPICIOUS_RE.search(body):
        return miss("suspicious_content")
    if not _ASK_RE.search(text):
        return miss("no_request_phrase")
    return RuleResult(
        ParsedRequest(
            intent=Intent.DOCUMENT_REQUEST,
            matter=matters[0],
            doc_type=doc_types[0],
            max_docs=find_count(text, max_docs),
            source="rules",
            confidence=1.0,
        ),
        "ok",
        matters,
        doc_types,
    )
