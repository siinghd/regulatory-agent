"""Domain types shared across ingest, gate, providers, delivery and the viewer.

Frozen pydantic models: values flow between stages and get persisted as JSON, so nothing
should mutate them in place.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------- inbound mail


class AuthVerdict(StrEnum):
    PASS = "pass"  # DMARC-aligned SPF or DKIM pass
    FAIL = "fail"  # spoofed or failed alignment
    NONE = "none"  # no usable authentication at all


class SenderAuth(Frozen):
    verdict: AuthVerdict
    from_domain: str
    spf: str  # pass|fail|softfail|neutral|none|temperror|permerror
    spf_domain: str | None = None
    dkim_domains: tuple[str, ...] = ()  # d= of every valid signature
    aligned_via: str | None = None  # "spf" | "dkim"
    client_ip: str | None = None
    reason: str = ""


class InboundEmail(Frozen):
    message_id: str
    from_addr: str  # bare lowercase address from the From header
    from_name: str = ""
    reply_to: str | None = None  # recorded, never used as the recipient
    to: tuple[str, ...] = ()
    subject: str = ""
    text: str = ""  # best-effort plain text, quoted history stripped
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    received_at: datetime
    headers: dict[str, str] = Field(default_factory=dict)  # lowercased single-value view
    raw_sha256: str = ""


# ---------------------------------------------------------------- request parsing


class DocType(StrEnum):
    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"


class Intent(StrEnum):
    DOCUMENT_REQUEST = "document_request"
    QUESTION = "question"  # about a matter, no download wanted
    UNRELATED = "unrelated"
    SPAM = "spam"
    INJECTION = "injection_attempt"


class ParsedRequest(Frozen):
    intent: Intent
    matter: str | None = None  # normalised "M12205"
    doc_type: DocType | None = None
    max_docs: int = 10
    source: str = "rules"  # rules | llm
    confidence: float = 1.0
    needs_clarification: str | None = None  # user-facing question when ambiguous
    extra_matters: tuple[str, ...] = ()  # additional matters mentioned (handled one per reply)


# ---------------------------------------------------------------- provider data


class DocumentRef(Frozen):
    provider: str
    matter: str
    doc_type: DocType
    external_id: str  # UARB file id, e.g. "102674"
    title: str
    filed_on: date | None = None
    access: str = "Public"
    file_ext: str = ".pdf"
    row_index: int = 0


class MatterInfo(Frozen):
    provider: str
    matter: str
    title: str
    status: str | None = None
    type: str | None = None
    category: str | None = None
    date_received: date | None = None
    decision_date: date | None = None
    outcome: str | None = None
    counts: dict[DocType, int]
    portal_url: str
    fetched_at: datetime


class DownloadedFile(Frozen):
    ref: DocumentRef
    path: str  # content-addressed path on disk
    sha256: str
    size: int
    filename: str  # safe display name


# ---------------------------------------------------------------- citations


class Citation(Frozen):
    id: str
    doc_external_id: str
    page: int  # 1-based
    quote: str
    claim: str


# ---------------------------------------------------------------- errors


class AgentError(Exception):
    """Base. `retryable` drives the queue: retry with backoff vs fail fast and tell the user."""

    retryable = False
    user_message = "Something went wrong while handling your request."


class MatterNotFound(AgentError):
    retryable = False

    def __init__(self, matter: str):
        super().__init__(matter)
        self.user_message = (
            f"I couldn't find matter {matter} in the public database. Matter numbers look like M12205."
        )


class PortalUnavailable(AgentError):
    retryable = True
    user_message = "The regulator's website is slow or unavailable right now. I'll keep retrying."


class ScrapeError(AgentError):
    retryable = True
    user_message = "I hit an unexpected problem reading the regulator's website. I'll retry shortly."
