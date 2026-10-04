"""Raw RFC 5322 bytes -> InboundEmail.

Every byte here is attacker-controlled. `parse_raw` either returns a usable InboundEmail or
raises MalformedEmail; hostile headers, charsets and MIME trees must not surface as any
other exception type.
"""

import codecs
import hashlib
import re
from collections.abc import Iterator
from datetime import datetime
from email import policy
from email.headerregistry import HeaderRegistry
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses

from selectolax.lexbor import LexborHTMLParser

from agent.models import InboundEmail

MAX_TEXT_CHARS = 20_000
MAX_SUBJECT_CHARS = 998  # RFC 5322 line limit; anything longer is abuse, not a subject
MAX_REFERENCES = 100

# Every header is parsed as unstructured text. CPython 3.12's structured parsers (addresses,
# Message-ID, Content-Type parameters) raise IndexError/AttributeError on hostile values, and
# BytesParser itself consults Content-Type while parsing. The Message API used below works on
# plain strings, and the few structured fields we need are parsed from raw values.
_HEADER_REGISTRY = HeaderRegistry(use_default_map=False)
_POLICY = policy.default.clone(header_factory=_HEADER_REGISTRY)

_MSG_ID = re.compile(r"<[^<>\s]+>")
# Postgres text/jsonb reject NUL, and no legitimate request needs C0 control characters.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Mislabelled bodies are the norm: "us-ascii" parts often carry UTF-8 and "iso-8859-1" parts
# often carry Windows-1252 punctuation (WHATWG decodes both labels the same way).
_CHARSET_SUPERSETS = {"ascii": "utf-8", "iso8859-1": "cp1252"}


class MalformedEmail(ValueError):
    """The message can't be attributed to exactly one sender address; it gets no reply."""


def parse_raw(raw: bytes, received_at: datetime) -> InboundEmail:
    try:
        msg = BytesParser(policy=_POLICY).parsebytes(raw)
    except RecursionError as e:
        # The stdlib parser recurses per multipart level; ~1000 levels fit in 60 KB.
        raise MalformedEmail("MIME structure nested too deeply") from e

    from_name, from_addr = _single_from(msg)
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    reply_to = _addresses(msg, "reply-to")
    in_reply_to = _msg_ids(msg.get("in-reply-to"))
    return InboundEmail(
        # Postfix adds a missing Message-ID only for local submissions; deriving one from the
        # bytes keeps ingest idempotent when the same message is delivered twice.
        message_id=next(iter(_msg_ids(msg.get("message-id"))), f"<{raw_sha256[:32]}@no-message-id.invalid>"),
        from_addr=from_addr,
        from_name=from_name,
        reply_to=reply_to[0] if reply_to else None,
        to=tuple(_addresses(msg, "to")),
        subject=_clean(" ".join(str(msg.get("subject", "")).split()))[:MAX_SUBJECT_CHARS],
        text=_clean(strip_quoted(_body_text(msg)))[:MAX_TEXT_CHARS],
        in_reply_to=in_reply_to[0] if in_reply_to else None,
        references=tuple(_msg_ids(msg.get("references"))[:MAX_REFERENCES]),
        received_at=received_at,
        headers={name.lower(): _clean(str(value)) for name, value in msg.items()},
        raw_sha256=raw_sha256,
    )


def parse_headers(raw: bytes) -> Message:
    """Header-only parse for callers that need ordered/repeated headers (auth, loop checks)."""
    return BytesParser(policy=_POLICY).parsebytes(raw, headersonly=True)


def received_headers(msg: Message) -> list[str]:
    """All Received headers, topmost (most recent hop) first, whitespace-normalised."""
    return [" ".join(str(value).split()) for value in msg.get_all("received", [])]


def envelope_sender(msg: Message) -> str | None:
    """Bare lowercase address from the topmost Return-Path, which Dovecot writes at delivery.

    "" is the null sender (bounces, DSNs); None means the header is absent. Lower Return-Path
    headers are sender-supplied and ignored.
    """
    value = msg.get("return-path")
    if value is None:
        return None
    bracketed = re.search(r"<([^<>]*)>", str(value))
    return (bracketed.group(1) if bracketed else str(value)).strip().lower()


# ---------------------------------------------------------------- addresses and ids


def _raw_values(msg: Message, name: str) -> list[str]:
    """Undecoded header values, so RFC 2047 words can't smuggle commas or angle brackets."""
    return [
        # BytesParser keeps 8-bit header bytes as surrogate escapes; RFC 6532 says they are UTF-8.
        value.encode("ascii", "surrogateescape")
        .decode("utf-8", "replace")
        .replace("\r", "")
        .replace("\n", "")
        for key, value in msg.raw_items()
        if key.lower() == name
    ]


def _single_from(msg: Message) -> tuple[str, str]:
    addresses = getaddresses(_raw_values(msg, "from"))
    if len(addresses) != 1:
        raise MalformedEmail(f"expected exactly one From address, found {len(addresses)}")
    display_name, address = addresses[0]
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        raise MalformedEmail(f"From address {address!r} is not local@domain")
    return _clean(str(_HEADER_REGISTRY("x-display-name", display_name))), address.lower()


def _addresses(msg: Message, name: str) -> list[str]:
    return [address.lower() for _, address in getaddresses(_raw_values(msg, name)) if "@" in address]


def _msg_ids(value: object) -> list[str]:
    return _MSG_ID.findall(str(value)) if value is not None else []


def _clean(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


# ---------------------------------------------------------------- body text


def _body_text(msg: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in _inline_text_parts(msg):
        (plain if part.get_content_subtype() == "plain" else html).append(_decode_payload(part))
    if plain:
        return "\n".join(plain)
    return "\n".join(_html_to_text(doc) for doc in html)


def _inline_text_parts(msg: Message) -> Iterator[Message]:
    """text/plain and text/html leaves in document order, skipping attachments.

    Attached messages (message/rfc822, DSN status parts) are not descended into. Iterative,
    because the MIME tree depth is attacker-chosen.
    """
    stack = [msg]
    while stack:
        part = stack.pop()
        maintype = part.get_content_maintype()
        if maintype == "multipart" and part.is_multipart():
            stack.extend(reversed(part.get_payload()))
        elif (
            maintype == "text"
            and part.get_content_subtype() in ("plain", "html")
            and part.get_content_disposition() != "attachment"
            and part.get_param("filename", header="content-disposition") is None
        ):
            yield part


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        codec = codecs.lookup(charset).name
        return payload.decode(_CHARSET_SUPERSETS.get(codec, codec), errors="replace")
    except (LookupError, ValueError):
        # Unknown, malformed, non-text ("base64") or strict-only ("idna") codec named by the
        # sender; UnicodeError is a ValueError.
        return payload.decode("utf-8", errors="replace")


_DROP_TAGS = "script, style, head, title, noscript, template, blockquote"
_BLOCK_TAGS = (
    "p, div, li, tr, table, ul, ol, dl, dt, dd, pre, hr, section, article, header, footer, "
    "h1, h2, h3, h4, h5, h6"
)
# Private-use sentinels survive the whitespace collapsing below. Adjacent block boundaries
# merge into one line break (<div>a</div><div>b</div> is two lines, not three); every <br>
# counts, so Gmail's <div><br></div> still renders as a blank line.
_BLOCK_BREAK = "\ue000"
_LINE_BREAK = "\ue001"


def _html_to_text(html: str) -> str:
    """Visible text with block structure kept as line breaks.

    <blockquote> is dropped because Gmail, Apple Mail and Thunderbird put quoted history there.
    """
    tree = LexborHTMLParser(html)
    for node in tree.css(_DROP_TAGS):
        node.decompose()
    for node in tree.css("br"):
        node.replace_with(_LINE_BREAK)
    for node in tree.css(_BLOCK_TAGS):
        node.insert_before(_BLOCK_BREAK)
        node.insert_after(_BLOCK_BREAK)
    for node in tree.css("td, th"):
        node.insert_after(" ")
    root = tree.body or tree.root
    if root is None:
        return ""
    # Source whitespace (including newlines) is insignificant in HTML; only the sentinels break lines.
    flat = re.sub(r"\s+", " ", root.text(separator=""))
    flat = re.sub(f"(?: ?{_BLOCK_BREAK})+ ?", _BLOCK_BREAK, flat)
    return "\n".join(line.strip() for line in re.split(f"[{_BLOCK_BREAK}{_LINE_BREAK}]", flat))


# ---------------------------------------------------------------- quoted history

_LEAD_WORDS = ("on ", "le ", "am ", "el ", "op ", "em ", "il ")
_ATTRIBUTION = re.compile(
    r"^\s*(?:"
    r"On\s.{1,250}?\s(?:wrote|said)\s*"  # Gmail, Apple Mail, Thunderbird
    r"|Le\s.{1,250}?\sa\s+écrit\s*"  # French
    r"|Am\s.{1,250}?\sschrieb\s[^:]{0,250}"  # German: "Am <date> schrieb <name>:"
    r"|El\s.{1,250}?\sescribió\s*"  # Spanish
    r"|Op\s.{1,250}?\sschreef\s[^:]{0,250}"  # Dutch
    r"|Em\s.{1,250}?\sescreveu\s*"  # Portuguese
    r"|Il\s.{1,250}?\sha\s+scritto\s*"  # Italian
    r"):\s*$",
    re.IGNORECASE,
)
_ORIGINAL_MESSAGE = re.compile(
    r"^\s*-{2,}\s*(?:Original Message|Message d'origine|Ursprüngliche Nachricht|Mensaje original"
    r"|Messaggio originale|Oorspronkelijk bericht)\s*-{2,}\s*$",
    re.IGNORECASE,
)
# Outlook's reply header block: "From: ..." followed within a few lines by "Sent:"/"Date:".
_HEADER_FROM = re.compile(r"^\s*\*?(?:From|De|Von|Van|Da)\s*:\*?\s*\S", re.IGNORECASE)
_HEADER_SENT = re.compile(
    r"^\s*\*?(?:Sent|Date|Envoyé|Gesendet|Datum|Verzonden|Inviato|Enviado)\s*:\*?\s*\S", re.IGNORECASE
)
# A forward carries the message the user wants acted on, so nothing after it is stripped
# (its own "From:/Date:" block would otherwise look like a reply header).
_FORWARD_MARKER = re.compile(
    r"^\s*(?:-{2,}\s*(?:Forwarded message|Message transféré|Weitergeleitete Nachricht)\s*-{2,}"
    r"|Begin forwarded message:)\s*$",
    re.IGNORECASE,
)
_RULE = re.compile(r"[\s_\-=*]*")  # blank or a horizontal rule such as Outlook's "_____"
_MAX_SEPARATOR_CHARS = 300  # real separators are short; bounds regex work on hostile lines


def strip_quoted(text: str) -> str:
    """Drop quoted history: '>' lines and everything from the first reply separator down.

    A separator only cuts when the user wrote something above it (top-posting). When it heads
    the message (bottom-posting), the separator line is dropped and the reply below is kept.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    wrote_something = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FORWARD_MARKER.match(line):
            kept.extend(rest for rest in lines[i:] if not _is_quoted(rest))
            break
        separator_lines = _separator_length(lines, i)
        if separator_lines:
            if wrote_something:
                break
            i += separator_lines
            continue
        if not _is_quoted(line):
            kept.append(line)
            wrote_something = wrote_something or not _RULE.fullmatch(line)
        i += 1
    return _tidy(kept)


def _is_quoted(line: str) -> bool:
    return line.lstrip().startswith(">")


def _separator_length(lines: list[str], i: int) -> int:
    """Number of lines (0, 1 or 2) forming a reply separator that starts at lines[i]."""
    line = lines[i]
    if len(line) > _MAX_SEPARATOR_CHARS:
        return 0
    if _ORIGINAL_MESSAGE.match(line) or _ATTRIBUTION.match(line):
        return 1
    # Gmail wraps long attributions: "On Mon, ... Jane Doe <\njane@example.com> wrote:"
    wrapped = line.lstrip().lower().startswith(_LEAD_WORDS) and i + 1 < len(lines)
    if wrapped and _ATTRIBUTION.match(f"{line} {lines[i + 1]}"):
        return 2
    if _HEADER_FROM.match(line) and any(_HEADER_SENT.match(nxt) for nxt in lines[i + 1 : i + 5]):
        return 1
    return 0


def _tidy(lines: list[str]) -> str:
    """Trim trailing whitespace, collapse blank runs, drop leading/trailing blanks and rules."""
    out: list[str] = []
    for line in (raw.rstrip() for raw in lines):
        if line or (out and out[-1]):
            out.append(line)
    while out and _RULE.fullmatch(out[-1]):
        out.pop()
    return "\n".join(out)
