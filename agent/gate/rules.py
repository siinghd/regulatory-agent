"""Deterministic request parsing: the fast path that needs no LLM.

Returns a ParsedRequest only when the email is unambiguous (exactly one matter, exactly one
document category of that matter's regulator, a request phrase, no negation). Anything else
returns `None` with a reason and goes to the LLM classifier. The one exception is a bare
acknowledgement ("Thanks, that's all I needed!"), which is answered here as unrelated so that a
thread's earlier request is never fetched again.

All matching runs on NFKC-normalised text (full-width "Ｍ１２２０５" is M12205), with email
addresses, URLs and law-firm matter references blanked out first: "m12205@gmail.com" or "Client
Matter: 30127-0042" never name a regulator's matter.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from agent.models import Intent, ParsedRequest
from agent.providers.base import Category, all_providers, normalise_with, provider_for_matter

_NUMBER = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_GENERIC_NOUNS = ("documents", "document", "docs", "doc", "files", "file", "filings", "filing")
_WORDS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"])}
_FEW = {"couple": 2, "few": 3}

# Negation, exclusion and alternatives: which category is wanted needs judgement. The LLM decides,
# and its category is never overridden by the one category the text names (it may be the excluded one).
_NEGATION_RE = re.compile(
    r"\b(?:not|don't|dont|do\s+not|doesn't|no\s+need|except|excluding|exclude|instead|rather\s+than|"
    r"unless|either|or\s+the|anything\s+but|everything\s+but|all\s+but|everything\s+except|other\s+than|"
    r"apart\s+from|aside\s+from|besides|skip|without|already\s+have|already\s+got|what\s+else)\b"
    r"|\bno\s+(?=[a-z])",  # "no comments please" (not "matter no 12205")
    re.IGNORECASE,
)
# Questions and comparisons: also for the LLM, but they don't make the named category doubtful.
_QUESTION_RE = re.compile(
    r"\b(?:compare|difference|which\s+one|what\s+is|what's|why|how\s+many|summar\w*)\b", re.IGNORECASE
)
# Request phrasing: an email that names a matter and a category but never asks for anything is
# probably a forward or a signature block, not a request.
_ASK_RE = re.compile(
    r"\b(?:send|give|get|fetch|share|forward|provide|email|need|want|pull|download|grab|"
    r"can\s+you|could\s+you|would\s+you|please|request|looking\s+for|"
    r"i['’]?d\s+like|i\s+would\s+like|(?:may|can|could)\s+i\s+(?:have|get|see))\b",
    re.IGNORECASE,
)
# One specific document ("exhibit H-1", "Exhibit KT2.2", "accession 20240212-5063"; a bare
# accession-like number may be the sender's own reference): we can only
# fetch whole categories, so the request is not "the Exhibits tab" and needs a question.
_SPECIFIC_DOC_RE = re.compile(
    r"\bexhibit\s+(?:no\.?\s*|number\s*|#\s*)?[A-Z]{0,3}[-.]?\d+(?:[.-]\d+)*(?:\s?\([A-Za-z0-9]{1,3}\))?(?![\w-])"
    r"|\b(?:accession|document|doc|filing|submittal|issuance)\s*(?:no\.?|number|#)?\s*:?\s*\d{8}-\d{4}(?![\w-])",
    re.IGNORECASE,
)


# Anything that tries to steer the agent, or names another mailbox, gets a closer look from the
# classifier. (It is harmless either way: replies only ever go to the authenticated sender.)
_SUSPICIOUS_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.-]+|\bignore\b.{0,40}\binstructions?\b|system\s+prompt|"
    r"\b(?:admin|developer|debug|maintenance)\s+mode\b|\bact\s+as\b|\byou\s+are\s+now\b|\bjailbreak|"
    r"\b(?:bcc|cc)\b|https?://|\b(?:system|assistant)\s*:|<!--|<<<|>>>|\{\s*\"|`|\boverride\b|"
    "[\\u200b-\\u200f\\u2060\\ufeff]",  # zero-width characters hide text from a human reader
    re.IGNORECASE,
)
# A reply that only thanks or acknowledges: it asks for nothing, whatever the thread was about.
# ("OK" and "yes" are not here: they may answer the question our last reply asked.)
_ACKNOWLEDGEMENT_RE = re.compile(
    r"(?:(?:many\s+|much\s+)?thanks?(?:\s+(?:you|so\s+much|a\s+lot))?|thx|ty|cheers|much\s+appreciated|"
    r"appreciated?|got\s+(?:it|them)|received|perfect|great|awesome|excellent|all\s+set|"
    r"that'?s\s+(?:all|everything|great|perfect)|no\s+further\s+(?:questions|requests))\b[^?]*",
    re.IGNORECASE,
)
_ACK_MAX_CHARS = 200
_IN_ADVANCE_RE = re.compile(r"\bin\s+advance\b", re.IGNORECASE)  # thanks for what is still to come
_REPLY_SUBJECT_RE = re.compile(r"\s*(?:re|aw|sv|antw?)\s*:", re.IGNORECASE)

# Text that may look like a matter number but never is one: an email address (m12205@gmail.com),
# a URL, or a law firm's own file reference ("Client Matter: 30127-0042", "Matter No. 48213-0007").
_NOT_A_MATTER_RE = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|(?:https?://|www\.)\S+"
    r"|\b(?:client|firm|billing)\s+matter\s*(?:no\.?|number|#|id)?\s*:?\s*[\w-]+"
    r"|\bmatter\s*(?:no\.?|number|#)?\s*:?\s*\d{5}-\d+",
    re.IGNORECASE,
)


_SCOPE_CHARS = 60  # how far after a matter number a provider's `narrow` looks


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
    singular: re.Pattern[str]
    few: re.Pattern[str]


def _phrase(alias: str) -> str:
    return r"\s+".join(map(re.escape, alias.split()))


def _alternation(phrases) -> str:
    return "|".join(map(_phrase, sorted(set(phrases), key=len, reverse=True)))


@lru_cache(maxsize=16)
def _vocabulary(categories: tuple[Category, ...]) -> _Vocabulary:
    # Longest first: alternation takes the first branch that matches, so "key documents" is read
    # as one phrase rather than leaving "documents" for a shorter alias.
    pairs = sorted(((a, c.name) for c in categories for a in c.aliases), key=lambda p: -len(p[0]))
    mention = re.compile(r"\b(?:" + "|".join(f"({_phrase(a)})" for a, _ in pairs) + r")\b", re.IGNORECASE)
    nouns = _alternation({a for a, _ in pairs} | set(_GENERIC_NOUNS))
    # A singular alias is one whose plural (+s) names the same category: "exhibit", "decision", not
    # "evidence" or "correspondence" (which say nothing about how many).
    singular_aliases = {a for c in categories for a in c.aliases if f"{a}s" in c.aliases}
    singular_aliases |= {"document", "doc", "file", "filing"}
    recent = r"(?:most\s+recent|latest|newest|last|first|top)"
    # A count is only read where it is asked for: after a request verb ("send me 3 exhibits",
    # "send 3 of the undertakings") or a selector ("the latest 3", "up to 5", "just one"), or
    # before one ("the two most recent decisions"). "I have 2 files", "day 2 transcripts",
    # "our 3 analysts" and years are not counts.
    count = re.compile(
        rf"(?:\b(?:send|give|get|fetch|share|forward|provide|email|pull|download|grab|want|need|like)"
        rf"\s+(?:me\s+|us\s+|over\s+)?(?:the\s+)?(?:{recent}\s+)?"
        rf"|\b(?:{recent}|up\s+to|only|just)\s+)"
        rf"{_NUMBER}\s+(?:of\s+the\s+)?(?:{recent}\s+)?(?:{nouns})\b"
        rf"|\b{_NUMBER}\s+(?:of\s+the\s+)?{recent}\s+(?:{nouns})\b",
        re.IGNORECASE,
    )
    singular = re.compile(rf"\bthe\s+(?:very\s+)?{recent}\s+(?:{_alternation(singular_aliases)})\b(?!\s+(?:and|&))",
                          re.IGNORECASE)
    few = re.compile(rf"\ba\s+(couple|few)\s+(?:of\s+)?(?:the\s+)?(?:{recent}\s+)?(?:{nouns})\b", re.IGNORECASE)
    return _Vocabulary(mention, tuple(name for _, name in pairs), count, singular, few)


def normalise_text(text: str) -> str:
    """NFKC (full-width letters and digits become ASCII) with non-matter look-alikes blanked out,
    same length so match positions keep their order."""
    text = unicodedata.normalize("NFKC", text)
    return _NOT_A_MATTER_RE.sub(lambda m: " " * len(m.group(0)), text)


def _mentions(text: str) -> list[tuple[int, int, str]]:
    """(start, end, canonical matter) of every matter mention in `text` (already normalised), in order."""
    hits: list[tuple[int, int, str]] = []
    for provider in all_providers():
        narrow = getattr(provider, "narrow", None)  # "ER24-1234 (the -000 sub-docket only)"
        for m in provider.mention_pattern.finditer(text):
            matter = normalise_with(provider, m.group(0))
            if matter and narrow is not None:
                matter = narrow(matter, text[m.end() : m.end() + _SCOPE_CHARS])
            if matter:
                hits.append((m.start(), m.end(), matter))
    return sorted(hits)


def find_matters(text: str) -> tuple[str, ...]:
    """Canonical matter numbers of every registered regulator, in first-mention order."""
    return tuple(dict.fromkeys(matter for _, _, matter in _mentions(normalise_text(text))))


def category_for(text: str, matter: str, shared: str | None) -> tuple[str, int | None] | None:
    """The one category (and count, if stated) that `text` asks for `matter`, when it names several
    matters; None when that takes judgement.

    Each matter owns the words between it and its neighbours: the words since the previous matter
    when the email names a category before its first matter ("the decisions in EB-2024-0111 and
    the Other Documents for M12205"), else the words up to the next matter ("M12205: exhibits;
    M12383: key documents"). Failing that, the one category of the matter's regulator the whole
    email names, if it is `shared` with the first matter ("the Exhibits for M12205 and M12383").
    Negation, or two categories in the matter's words, is None.
    """
    provider = provider_for_matter(matter)
    text = normalise_text(text)
    if provider is None or _NEGATION_RE.search(text):
        return None
    mentions = [(start, end) for start, end, m in _mentions(text) if m == matter]
    if not mentions:
        return None
    start, end = mentions[0]
    others = [(s, e) for s, e, m in _mentions(text) if m != matter]
    if find_doc_types(text[: min(start, *(s for s, _ in others))], categories_for(None)):
        words = text[max((e for _, e in others if e <= start), default=0) : end]  # category, then matter
    else:
        words = text[end : min((s for s, _ in others if s >= end), default=len(text))]  # matter, then category
    names = find_doc_types(words, provider.categories)
    if len(names) == 1:
        return names[0], explicit_count(words, provider.categories)
    if names:
        return None
    names = find_doc_types(text, provider.categories)
    if len(names) == 1 and names[0] == shared:
        return names[0], None
    return None


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
    names = (vocab.names[m.lastindex - 1] for m in vocab.mention.finditer(unicodedata.normalize("NFKC", text))
             if m.lastindex)
    return tuple(dict.fromkeys(names))


def explicit_count(text: str, categories: Sequence[Category]) -> int | None:
    """The number of documents the text asks for, or None if it names none (uncapped, >= 1)."""
    vocab = _vocabulary(tuple(categories))
    text = unicodedata.normalize("NFKC", text)
    if m := vocab.count.search(text):
        raw = (m.group(1) or m.group(2)).lower()
        return max(1, int(raw) if raw.isdigit() else _WORDS[raw])
    if m := vocab.few.search(text):
        return _FEW[m.group(1).lower()]
    if vocab.singular.search(text):
        return 1
    return None


def find_count(text: str, cap: int, categories: Sequence[Category]) -> int:
    n = explicit_count(text, categories)
    return cap if n is None else min(n, cap)


def negated(text: str) -> bool:
    """Negation, exclusion or alternatives ("not the transcripts", "anything but", "either")."""
    return bool(_NEGATION_RE.search(unicodedata.normalize("NFKC", text)))


def specific_document(text: str) -> str | None:
    """The one document the text asks for by its number ("exhibit H-1"), if any."""
    for m in _SPECIFIC_DOC_RE.finditer(unicodedata.normalize("NFKC", text)):
        if not find_matters(m.group(0)):  # "exhibit M12205" names a matter, not a document
            return m.group(0)
    return None


def acknowledgement(subject: str, body: str) -> bool:
    """A short reply that only thanks or acknowledges and asks for nothing (the subject may still
    name the thread's matter: "Re: Transcripts for M12383")."""
    text = " ".join(unicodedata.normalize("NFKC", body).split())
    if not text or len(text) > _ACK_MAX_CHARS or "?" in text or _ASK_RE.search(text) or _IN_ADVANCE_RE.search(text):
        return False
    if _ASK_RE.search(subject) and not _REPLY_SUBJECT_RE.match(subject):
        return False  # "Exhibits for M12205 please" / "Thanks!": the request is in the subject
    if find_matters(text) or find_doc_types(text, categories_for(None)):
        return False
    return bool(_ACKNOWLEDGEMENT_RE.match(text))


def parse(subject: str, body: str, *, max_docs: int = 10) -> RuleResult:
    text = unicodedata.normalize("NFKC", f"{subject}\n{body}")
    matters = find_matters(text)
    # With several matters only the first is handled, so its regulator's categories apply.
    categories = categories_for(matters[0] if matters else None)
    doc_types = find_doc_types(text, categories)

    def miss(reason: str) -> RuleResult:
        return RuleResult(None, reason, matters, doc_types)

    if acknowledgement(subject, body):
        unrelated = ParsedRequest(intent=Intent.UNRELATED, source="rules", confidence=0.9)
        return RuleResult(unrelated, "acknowledgement", matters, doc_types)
    if len(body) > 2_000:
        return miss("long_body")
    if len(matters) != 1:
        return miss("no_matter" if not matters else "multiple_matters")
    if len(doc_types) != 1:
        return miss("no_doc_type" if not doc_types else "multiple_doc_types")
    if _NEGATION_RE.search(text) or _QUESTION_RE.search(text):
        return miss("ambiguous_phrasing")
    if _SUSPICIOUS_RE.search(text):
        return miss("suspicious_content")
    if specific_document(text):
        return miss("specific_document")
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
