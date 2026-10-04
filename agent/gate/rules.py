"""Deterministic request parsing: the fast path that needs no LLM.

Returns a ParsedRequest only when the email is unambiguous (exactly one matter, exactly one
document category of that matter's regulator). Anything else returns `None` with a reason and
goes to the LLM classifier.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from agent.models import Intent, ParsedRequest
from agent.providers.base import Category, all_providers, normalise_with, provider_for_matter

_NUMBER = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_GENERIC_NOUNS = ("documents", "document", "docs", "doc", "files", "file", "filings", "filing")
_WORDS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"])}

# Phrases that need judgement (negation, conditions, comparisons): defer to the LLM.
_AMBIGUITY_RE = re.compile(
    r"\b(?:not|don't|dont|do\s+not|except|instead|rather\s+than|unless|either|or\s+the|"
    r"compare|difference|which\s+one|what\s+is|what's|why|how\s+many|summar)",
    re.IGNORECASE,
)
# Request phrasing: an email that names a matter and a category but never asks for anything is
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
    doc_types: tuple[str, ...]


@dataclass(frozen=True)
class _Vocabulary:
    """Regexes for one set of categories. Group i of `mention` is alias i, naming `names[i]`."""

    mention: re.Pattern[str]
    names: tuple[str, ...]
    count: re.Pattern[str]


def _phrase(alias: str) -> str:
    return r"\s+".join(map(re.escape, alias.split()))


@lru_cache(maxsize=16)
def _vocabulary(categories: tuple[Category, ...]) -> _Vocabulary:
    # Longest first: alternation takes the first branch that matches, so "key documents" is read
    # as one phrase rather than leaving "documents" for a shorter alias.
    pairs = sorted(((a, c.name) for c in categories for a in c.aliases), key=lambda p: -len(p[0]))
    mention = re.compile(r"\b(?:" + "|".join(f"({_phrase(a)})" for a, _ in pairs) + r")\b", re.IGNORECASE)
    nouns = sorted({a for a, _ in pairs} | set(_GENERIC_NOUNS), key=len, reverse=True)
    count = re.compile(
        rf"\b(?:first|latest|last|top|up\s+to|only|just)?\s*{_NUMBER}\s+"
        r"(?:of\s+the\s+)?(?:most\s+recent\s+|latest\s+|newest\s+)?"
        rf"(?:{'|'.join(map(_phrase, nouns))})\b",
        re.IGNORECASE,
    )
    return _Vocabulary(mention, tuple(name for _, name in pairs), count)


def find_matters(text: str) -> tuple[str, ...]:
    """Canonical matter numbers of every registered regulator, in first-mention order."""
    hits: list[tuple[int, str]] = []
    for provider in all_providers():
        for m in provider.mention_pattern.finditer(text):
            matter = normalise_with(provider, m.group(0))
            if matter:
                hits.append((m.start(), matter))
    return tuple(dict.fromkeys(matter for _, matter in sorted(hits)))  # dedupe, keep first-mention order


def categories_for(matter: str | None) -> tuple[Category, ...]:
    """What a request about `matter` can ask for. Without a matter (a follow-up that inherits it
    from the thread later) any regulator's category counts; the pipeline re-checks it."""
    provider = provider_for_matter(matter) if matter else None
    if provider is not None:
        return provider.categories
    return tuple(c for p in all_providers() for c in p.categories)


def find_doc_types(text: str, categories: Sequence[Category]) -> tuple[str, ...]:
    """Names of the categories mentioned in `text`, in first-mention order."""
    vocab = _vocabulary(tuple(categories))
    names = (vocab.names[m.lastindex - 1] for m in vocab.mention.finditer(text) if m.lastindex)
    return tuple(dict.fromkeys(names))


def find_count(text: str, cap: int, categories: Sequence[Category]) -> int:
    m = _vocabulary(tuple(categories)).count.search(text)
    if not m:
        return cap
    raw = m.group(1).lower()
    n = int(raw) if raw.isdigit() else _WORDS.get(raw, cap)
    return max(1, min(n, cap))


def parse(subject: str, body: str, *, max_docs: int = 10) -> RuleResult:
    text = f"{subject}\n{body}"
    matters = find_matters(text)
    # With several matters only the first is handled, so its regulator's categories apply.
    categories = categories_for(matters[0] if matters else None)
    doc_types = find_doc_types(text, categories)

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
            max_docs=find_count(text, max_docs, categories),
            source="rules",
            confidence=1.0,
        ),
        "ok",
        matters,
        doc_types,
    )
