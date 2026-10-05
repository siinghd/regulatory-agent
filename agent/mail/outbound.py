"""Compose and send the agent's emails.

Every message we send:
- goes only to the authenticated sender of the request (never Reply-To, never an address from
  the email body or the LLM);
- is threaded (In-Reply-To / References) and carries a deterministic Message-ID derived from
  the request id, so a retry after a crash re-sends the *same* message;
- is marked Auto-Submitted: auto-replied (RFC 3834), X-Auto-Response-Suppress and X-Loop (our
  address), so other autoresponders don't answer it, plus X-Regulatory-Agent so we recognise our
  own mail if it ever comes back.
"""

import html
import mimetypes
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from uuid import UUID

import aiosmtplib

from agent.config import get_settings
from agent.models import MatterInfo
from agent.providers.base import Provider

AGENT_NAME = "Regulatory Document Agent"


@dataclass(frozen=True)
class Draft:
    kind: str  # ack | reply | notice
    subject: str
    text: str
    html: str
    attachments: Sequence[str] = field(default_factory=tuple)


def message_id(request_id: UUID, kind: str) -> str:
    domain = get_settings().agent_mail_address.split("@", 1)[1]
    return f"<{kind}.{request_id}@{domain}>"


def reply_subject(original: str) -> str:
    s = " ".join(original.split())[:200] or "Your document request"
    return s if s.lower().startswith("re:") else f"Re: {s}"


def build_message(
    draft: Draft,
    *,
    request_id: UUID,
    to_addr: str,
    in_reply_to: str,
    references: Sequence[str],
) -> EmailMessage:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr((AGENT_NAME, s.agent_mail_address))
    msg["To"] = to_addr
    msg["Subject"] = draft.subject
    domain = s.agent_mail_address.split("@", 1)[1]
    # Our domain in every Message-ID lets loop detection recognise replies to any of our mail.
    msg["Message-ID"] = (message_id(request_id, draft.kind) if draft.kind in {"ack", "reply", "notice"}
                         else make_msgid(domain=domain))
    msg["Date"] = format_datetime(datetime.now().astimezone())
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = " ".join([*references[-10:], in_reply_to]) if in_reply_to not in references else " ".join(references[-10:])
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    msg["X-Regulatory-Agent"] = "1"
    msg["X-Loop"] = s.agent_mail_address  # a responder that honours X-Loop never answers us
    msg.set_content(draft.text)
    msg.add_alternative(draft.html, subtype="html")
    for path in draft.attachments:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))
    return msg


def render(msg: EmailMessage) -> bytes:
    """The bytes the outbox stores and every (re)send transmits."""
    return msg.as_bytes(policy=policy.SMTP)


def parse(data: bytes) -> EmailMessage:
    return message_from_bytes(data, policy=policy.default)


def smtp_failure(exc: BaseException) -> tuple[bool, int | None]:
    """(permanent, SMTP code). Permanent = the server refused *this message* (5xx on a recipient
    or on DATA): resending it can't succeed. A 5xx about us (authentication, our sender address)
    is a configuration problem that gets fixed, so it is retried like any outage."""
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        codes = [r.code for r in exc.recipients]
        return bool(codes) and all(500 <= c < 600 for c in codes), max(codes, default=None)
    if isinstance(exc, (aiosmtplib.SMTPRecipientRefused, aiosmtplib.SMTPDataError)):
        return 500 <= exc.code < 600, exc.code
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return False, exc.code
    return False, None


async def send(msg: EmailMessage) -> None:
    s = get_settings()
    await aiosmtplib.send(
        msg,
        hostname=s.smtp_host,
        port=s.smtp_port,
        start_tls=True,
        username=s.agent_mail_address,
        password=s.agent_mail_password.get_secret_value(),
        timeout=60,
    )


# ------------------------------------------------------------------ templates

_CSS = (
    "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;"
    "color:#1d2733;max-width:640px"
)


def privacy_url() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/privacy"


def _wrap(inner: str, provider: Provider | None) -> str:
    """The footer names the source database when the reply is about one of `provider`'s matters,
    and links the privacy notice in every email."""
    source = (
        f' Documents come from the public <a href="{html.escape(provider.portal_url)}">'
        f"{html.escape(provider.display_name)} database</a>."
        if provider
        else ""
    )
    return f'<div style="{_CSS}">{inner}<p style="color:#6b7785;font-size:12px;margin-top:28px">' \
        f"Automated reply from the {AGENT_NAME}.{source} " \
        f'<a href="{html.escape(privacy_url())}" style="color:#6b7785">Privacy notice</a></p></div>'


def _with_footer(text: str) -> str:
    """The plain-text body's footer: the privacy notice, in every email."""
    return f"{text.rstrip()}\n\n--\nAutomated reply from the {AGENT_NAME}. Privacy notice: {privacy_url()}\n"


def _greeting(name: str) -> str:
    first = name.split()[0] if name.strip() else ""
    return f"Hi {first}," if first and first.isalpha() else "Hi,"


def singular(name: str) -> str:
    """A category name for exactly one document: "Exhibits" -> "Exhibit", "Decisions and Orders"
    -> "Decision or Order", a mass noun such as "Correspondence" -> "Correspondence document"."""
    parts = name.split(" and ")
    out = [_singular_phrase(p) for p in parts]
    if out == parts:
        return f"{name} document"
    return " or ".join(out)


def _singular_phrase(phrase: str) -> str:
    head, _, last = phrase.rpartition(" ")
    if last.endswith("ies") and len(last) > 4:
        last = last[:-3] + "y"
    elif last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return f"{head} {last}" if head else last


def count_of(n: int, name: str) -> str:
    """ "1 Exhibit", "2 Exhibits" (`name` is the plural category name)."""
    return f"{n} {singular(name) if n == 1 else name}"


def human_size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(n // 1000, 1)} KB"


def _series(items: Sequence[str], conjunction: str) -> str:
    """ "a", "a and b", "a, b, and c" (with "or": "a, b or c", no serial comma)."""
    if len(items) <= 2:
        return f" {conjunction} ".join(items)
    comma = "," if conjunction == "and" else ""
    return f"{', '.join(items[:-1])}{comma} {conjunction} {items[-1]}"


def _counts_line(info: MatterInfo, provider: Provider) -> str:
    """ "2 Exhibits, 3 Other Documents, and no Key Documents or Transcripts": "and" before the last item."""
    # portal order, whatever the source dict order (cached counts come back from JSONB re-ordered)
    counts = [(c.name, info.counts.get(c.name, 0)) for c in provider.categories]
    present = [count_of(n, name) for name, n in counts if n]
    absent = [name for name, n in counts if not n]
    if not present:
        return "no documents"
    return _series(present + ([f"no {_series(absent, 'or')}"] if absent else []), "and")


def _a(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _fmt_date(d) -> str:
    return d.strftime("%B %-d, %Y") if d else "unknown"


def matter_sentence(info: MatterInfo, provider: Provider) -> str:
    title = info.title.strip()
    bits = [f"{info.matter} is about {title}" + ("" if title.endswith((".", "!", "?")) else ".")]
    if info.type or info.category:
        bits.append(
            f"It is {_a(info.type)} {info.type} matter"
            + (f" in the {info.category} category." if info.category else ".")
            if info.type
            else f"It is in the {info.category} category."
        )
    if info.date_received:
        line = f"The matter was received on {_fmt_date(info.date_received)}"
        line += f" and decided on {_fmt_date(info.decision_date)}." if info.decision_date else "."
        bits.append(line)
    if info.status:
        bits.append(f"Its status is {info.status}.")
    bits.append(f"I found {_counts_line(info, provider)}.")
    return " ".join(bits)


def ack(*, name: str, subject: str, matter: str, doc_type: str, provider: Provider, track_url: str) -> Draft:
    text = (
        f"{_greeting(name)}\n\n"
        f"Got it. I'm collecting the {doc_type} for {matter} from the {provider.display_name} database now. "
        f"You'll get a second email with the documents and a summary, usually within a couple of minutes.\n\n"
        f"Track progress: {track_url}\n"
    )
    body = (
        f"<p>{html.escape(_greeting(name))}</p>"
        f"<p>Got it. I'm collecting the <b>{html.escape(doc_type)}</b> for <b>{html.escape(matter)}</b> "
        f"from the {html.escape(provider.display_name)} database now. You'll get a second email with the "
        f"documents and a summary, usually within a couple of minutes.</p>"
        f'<p><a href="{html.escape(track_url)}" style="display:inline-block;padding:9px 14px;border-radius:6px;'
        f'background:#1f5f8b;color:#fff;text-decoration:none">Track progress</a></p>'
    )
    return Draft("ack", reply_subject(subject), _with_footer(text), _wrap(body, provider))


@dataclass(frozen=True)
class ClaimLine:
    text: str
    url: str


@dataclass(frozen=True)
class DocLine:
    title: str
    filed: str
    url: str | None  # viewer link


# Why a document couldn't be read, as (one, several): "1 couldn't be read (a spreadsheet)".
UNREADABLE_KINDS = {
    "scanned": ("scanned", "scanned"),
    "spreadsheet": ("a spreadsheet", "spreadsheets"),
    "recording": ("a recording", "recordings"),
    "old_word": ("an old Word file", "old Word files"),
    "damaged": ("damaged or protected", "damaged or protected"),
    "other": ("another file type", "other file types"),
}


@dataclass(frozen=True)
class SummaryBasis:
    """What a summary was written from, for its "based on N of M documents" line."""

    used: int  # documents the summary read
    total: int  # documents sent
    unreadable: int = 0  # no usable text (scans, spreadsheets, recordings, damaged files)
    unread: int = 0  # readable, but ranked below the ones the summary had room for
    kinds: tuple[str, ...] = ()  # keys of UNREADABLE_KINDS, for the unreadable ones

    def sentence(self) -> str | None:
        """None when the summary read every document (or nothing is known about what it read)."""
        if self.used >= self.total or self.used < 0:
            return None
        words = [UNREADABLE_KINDS[k][self.unreadable > 1] for k in dict.fromkeys(self.kinds) if k in UNREADABLE_KINDS]
        why = f" ({_series(words, 'or')})" if words else ""
        if self.used == 0:  # written from the matter's details alone
            if not self.unreadable:
                return None
            none = "The only document couldn't" if self.total == 1 else f"None of the {self.total} documents could"
            return f"{none} be read{why}, so the summary is based on the matter's details only."
        if self.unread:
            most = "the most decision-relevant" if self.used == 1 else f"the {self.used} most decision-relevant"
            line = f"The summary is based on {most} of the {self.total} documents"
        else:
            line = f"The summary is based on {self.used} of {self.total} documents"
        if self.unreadable:
            line += f"; {self.unreadable} couldn't be read{why}"
        return line + "."


def fetched_sentence(
    *, got: int, total: int, doc_type: str, confidential: int = 0, order: str = "portal", complete: bool = True
) -> str:
    """What was downloaded, claiming "most recent" only when the portal's listing showed it.

    `order` is "newest" or "oldest" when every listed document is dated and the dates run that
    way, else "portal"; `complete` is False when some listed documents couldn't be included.
    """
    one = singular(doc_type)
    if got == total:
        return f"I downloaded the only {one}" if got == 1 else f"I downloaded all {got} {doc_type}"
    if confidential and got + confidential == total:
        more = f"{confidential} more {'is' if confidential == 1 else 'are'} confidential"
        return (f"I downloaded the only public {one} ({more})" if got == 1
                else f"I downloaded all {got} public {doc_type} ({more})")
    of_total = f"of the {total} {doc_type}"
    if complete and order == "newest":
        return f"I downloaded the most recent {of_total}" if got == 1 else f"I downloaded the {got} most recent {of_total}"
    if complete:
        first = "the first" if got == 1 else f"the first {got}"
        return f"I downloaded {first} {of_total}, in the order the portal lists them"
    return f"I downloaded {got} {of_total}"


def documents_reply(
    *,
    name: str,
    subject: str,
    info: MatterInfo,
    provider: Provider,
    doc_type: str,
    docs: Sequence[DocLine],
    requested: int,
    summary: str | None,
    claims: Sequence[ClaimLine],
    download_url: str | None,
    download_expires: datetime | None,
    download_size: int,
    attachment_path: str | None,
    track_url: str,
    extra_doc_types: Sequence[str] = (),
    extra_matters: Sequence[str] = (),
    failed_titles: Sequence[str] = (),
    newest_first: bool = True,
    confidential: int = 0,
    order: str | None = None,
    skipped_titles: Sequence[str] = (),
    size_budget: int | None = None,
    summary_basis: SummaryBasis | None = None,
    daily_allowance: bool = False,  # size_budget is the sender's daily allowance, not the per-request cap
) -> Draft:
    total = info.counts.get(doc_type, 0)
    got = len(docs)
    fetched = fetched_sentence(
        got=got, total=total, doc_type=doc_type, confidential=confidential,
        order=order or ("newest" if newest_first else "portal"),
        complete=not failed_titles and not skipped_titles,
    )
    them = "it" if got == 1 else "them"
    where = f"attached {them} as a ZIP" if attachment_path else f"packaged {them} as a ZIP"
    size = human_size(download_size)
    hidden = None
    if confidential and got + confidential != total:
        hidden = (f"1 of the {doc_type} listed is marked confidential, so I didn't include it." if confidential == 1
                  else f"{confidential} of the {doc_type} listed are marked confidential, so I didn't include them.")
    skipped = None
    if skipped_titles:
        limit = f" ({human_size(size_budget)})" if size_budget else ""
        skipped = ("I left out " + "; ".join(skipped_titles)
                   + (f" because sending them would go over today's download allowance{limit}. "
                      "Ask again tomorrow for the rest." if daily_allowance else
                      f" because together the documents would be over what I can send in one request{limit}."))

    t = [_greeting(name), "", matter_sentence(info, provider), "", f"{fetched} and {where} ({size})."]
    if download_url:
        t += ["", f"Download (encrypted link, expires {_fmt_date(download_expires)}): {download_url}"]
    if failed_titles:
        t += ["", "I couldn't download: " + "; ".join(failed_titles) + ". The rest are complete."]
    if skipped:
        t += ["", skipped]
    if hidden:
        t += ["", hidden]
    basis = summary_basis.sentence() if summary and summary_basis else None
    if summary:
        t += ["", "Summary", summary]
    if basis:
        t.append(basis)
    if claims:
        t += ["", "Key points, each linked to the exact passage:"]
        t += [f"- {c.text}\n  Source: {c.url}" for c in claims]
    t += ["", "Documents:"] + [f"- {d.title} ({d.filed})" + (f"\n  View: {d.url}" if d.url else "") for d in docs]
    if extra_doc_types:
        t += ["", "You also mentioned " + ", ".join(extra_doc_types)
              + ". Reply to this email with the type you want next and I'll send it."]
    if extra_matters:
        t += ["", "You also mentioned " + ", ".join(extra_matters)
              + ". I handle one matter per email, so please send a separate request for it."]
    t += ["", f"Request details: {track_url}"]

    h = [f"<p>{html.escape(_greeting(name))}</p>", f"<p>{html.escape(matter_sentence(info, provider))}</p>",
         f"<p>{html.escape(fetched)} and {where} ({size}).</p>"]
    if download_url:
        h.append(
            f'<p><a href="{html.escape(download_url)}" style="display:inline-block;padding:10px 16px;border-radius:6px;'
            f'background:#1f5f8b;color:#fff;text-decoration:none">Download ZIP</a> '
            f'<span style="color:#6b7785;font-size:13px">End-to-end encrypted link, expires '
            f"{html.escape(_fmt_date(download_expires))}.</span></p>"
        )
    if failed_titles:
        h.append(f"<p>I couldn't download: {html.escape('; '.join(failed_titles))}. The rest are complete.</p>")
    if skipped:
        h.append(f"<p>{html.escape(skipped)}</p>")
    if hidden:
        h.append(f"<p>{html.escape(hidden)}</p>")
    if summary:
        h.append(f'<h3 style="font-size:15px;margin:22px 0 6px">Summary</h3><p>{html.escape(summary)}</p>')
    if basis:
        h.append(f'<p style="color:#6b7785;font-size:13px">{html.escape(basis)}</p>')
    if claims:
        h.append('<h3 style="font-size:15px;margin:22px 0 6px">Key points</h3><ul style="padding-left:18px">')
        h += [f'<li>{html.escape(c.text)} <a href="{html.escape(c.url)}">view source</a></li>' for c in claims]
        h.append("</ul>")
    h.append(f'<h3 style="font-size:15px;margin:22px 0 6px">{html.escape(doc_type)}</h3><ol style="padding-left:18px">')
    for d in docs:
        title = html.escape(d.title)
        link = f' <a href="{html.escape(d.url)}">view</a>' if d.url else ""
        h.append(f'<li>{title} <span style="color:#6b7785">({html.escape(d.filed)})</span>{link}</li>')
    h.append("</ol>")
    if extra_doc_types:
        h.append("<p>You also mentioned " + html.escape(", ".join(extra_doc_types))
                 + ". Reply to this email with the type you want next and I'll send it.</p>")
    if extra_matters:
        h.append("<p>You also mentioned " + html.escape(", ".join(extra_matters))
                 + ". I handle one matter per email, so please send a separate request for it.</p>")
    h.append(f'<p style="font-size:13px"><a href="{html.escape(track_url)}">Request details</a></p>')
    return Draft("reply", reply_subject(subject), _with_footer("\n".join(t)), _wrap("".join(h), provider),
                 attachments=(attachment_path,) if attachment_path else ())


def simple_reply(
    *,
    name: str,
    subject: str,
    paragraphs: Sequence[str],
    track_url: str | None = None,
    provider: Provider | None = None,
) -> Draft:
    """Clarifications, not-found, declines, failures: short plain answers."""
    t = [_greeting(name), "", *[p + "\n" for p in paragraphs]]
    h = [f"<p>{html.escape(_greeting(name))}</p>", *[f"<p>{html.escape(p)}</p>" for p in paragraphs]]
    if track_url:
        t.append(f"Request details: {track_url}")
        h.append(f'<p style="font-size:13px"><a href="{html.escape(track_url)}">Request details</a></p>')
    return Draft("reply", reply_subject(subject), _with_footer("\n".join(t)), _wrap("".join(h), provider))
