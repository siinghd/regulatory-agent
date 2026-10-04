"""Compose and send the agent's emails.

Every message we send:
- goes only to the authenticated sender of the request (never Reply-To, never an address from
  the email body or the LLM);
- is threaded (In-Reply-To / References) and carries a deterministic Message-ID derived from
  the request id, so a retry after a crash re-sends the *same* message;
- is marked Auto-Submitted: auto-replied (RFC 3834) and X-Auto-Response-Suppress, so other
  autoresponders don't answer it, plus X-Regulatory-Agent so we recognise our own mail if it
  ever comes back.
"""

import html
import mimetypes
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from uuid import UUID

import aiosmtplib

from agent.config import get_settings
from agent.models import DocType, MatterInfo

AGENT_NAME = "UARB Document Agent"


@dataclass(frozen=True)
class Draft:
    kind: str  # ack | reply
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
    msg["Message-ID"] = message_id(request_id, draft.kind) if draft.kind in {"ack", "reply"} else make_msgid(domain=domain)
    msg["Date"] = format_datetime(datetime.now().astimezone())
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = " ".join([*references[-10:], in_reply_to]) if in_reply_to not in references else " ".join(references[-10:])
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    msg["X-Regulatory-Agent"] = "1"
    msg.set_content(draft.text)
    msg.add_alternative(draft.html, subtype="html")
    for path in draft.attachments:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))
    return msg


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


def _wrap(inner: str) -> str:
    return f'<div style="{_CSS}">{inner}<p style="color:#6b7785;font-size:12px;margin-top:28px">' \
        f"Automated reply from the {AGENT_NAME}. Documents come from the public " \
        f'<a href="https://uarb.novascotia.ca/fmi/webd/UARB15">UARB Public Documents Database</a>.</p></div>'


def _greeting(name: str) -> str:
    first = name.split()[0] if name.strip() else ""
    return f"Hi {first}," if first and first.isalpha() else "Hi,"


def _counts_line(info: MatterInfo) -> str:
    present = [f"{n} {t.value}" for t, n in info.counts.items() if n]
    absent = [t.value for t, n in info.counts.items() if not n]
    parts = ", ".join(present) if present else "no documents"
    if absent:
        parts += f", and no {' or '.join(absent) if len(absent) <= 2 else ', '.join(absent[:-1]) + ' or ' + absent[-1]}"
    return parts


def _fmt_date(d) -> str:
    return d.strftime("%B %-d, %Y") if d else "unknown"


def matter_sentence(info: MatterInfo) -> str:
    bits = [f"{info.matter} is about {info.title}."]
    if info.type or info.category:
        bits.append(
            f"It is a {info.type} matter" + (f" in the {info.category} category." if info.category else ".")
            if info.type
            else f"It is in the {info.category} category."
        )
    if info.date_received:
        line = f"The matter was received on {_fmt_date(info.date_received)}"
        line += f" and decided on {_fmt_date(info.decision_date)}." if info.decision_date else "."
        bits.append(line)
    if info.status:
        bits.append(f"Its status is {info.status}.")
    bits.append(f"I found {_counts_line(info)}.")
    return " ".join(bits)


def ack(*, name: str, subject: str, matter: str, doc_type: DocType, track_url: str) -> Draft:
    text = (
        f"{_greeting(name)}\n\n"
        f"Got it. I'm collecting the {doc_type.value} for {matter} from the UARB database now. "
        f"You'll get a second email with the documents and a summary, usually within a couple of minutes.\n\n"
        f"Track progress: {track_url}\n"
    )
    body = (
        f"<p>{html.escape(_greeting(name))}</p>"
        f"<p>Got it. I'm collecting the <b>{html.escape(doc_type.value)}</b> for <b>{html.escape(matter)}</b> "
        f"from the UARB database now. You'll get a second email with the documents and a summary, usually "
        f"within a couple of minutes.</p>"
        f'<p><a href="{html.escape(track_url)}" style="display:inline-block;padding:9px 14px;border-radius:6px;'
        f'background:#1f5f8b;color:#fff;text-decoration:none">Track progress</a></p>'
    )
    return Draft("ack", reply_subject(subject), text, _wrap(body))


@dataclass(frozen=True)
class ClaimLine:
    text: str
    url: str


@dataclass(frozen=True)
class DocLine:
    title: str
    filed: str
    url: str | None  # viewer link


def documents_reply(
    *,
    name: str,
    subject: str,
    info: MatterInfo,
    doc_type: DocType,
    docs: Sequence[DocLine],
    requested: int,
    summary: str | None,
    claims: Sequence[ClaimLine],
    download_url: str | None,
    download_expires: datetime | None,
    download_size: int,
    attachment_path: str | None,
    track_url: str,
    extra_doc_types: Sequence[DocType] = (),
    failed_titles: Sequence[str] = (),
) -> Draft:
    total = info.counts.get(doc_type, 0)
    got = len(docs)
    if got == total:
        fetched = f"I downloaded all {got} {doc_type.value}"
    else:
        fetched = f"I downloaded the {got} most recent of the {total} {doc_type.value}"
    where = "attached them as a ZIP" if attachment_path else "packaged them as a ZIP"
    size_mb = f"{download_size / 1_000_000:.1f} MB"

    t = [_greeting(name), "", matter_sentence(info), "", f"{fetched} and {where} ({size_mb})."]
    if download_url:
        t += ["", f"Download (encrypted link, expires {_fmt_date(download_expires)}): {download_url}"]
    if failed_titles:
        t += ["", "I couldn't download: " + "; ".join(failed_titles) + ". The rest are complete."]
    if summary:
        t += ["", "Summary", summary]
    if claims:
        t += ["", "Key points, each linked to the exact passage:"]
        t += [f"- {c.text}\n  Source: {c.url}" for c in claims]
    t += ["", "Documents:"] + [f"- {d.title} ({d.filed})" + (f"\n  View: {d.url}" if d.url else "") for d in docs]
    if extra_doc_types:
        t += ["", "You also mentioned " + ", ".join(x.value for x in extra_doc_types)
              + ". Reply to this email with the type you want next and I'll send it."]
    t += ["", f"Request details: {track_url}"]

    h = [f"<p>{html.escape(_greeting(name))}</p>", f"<p>{html.escape(matter_sentence(info))}</p>",
         f"<p>{html.escape(fetched)} and {where} ({size_mb}).</p>"]
    if download_url:
        h.append(
            f'<p><a href="{html.escape(download_url)}" style="display:inline-block;padding:10px 16px;border-radius:6px;'
            f'background:#1f5f8b;color:#fff;text-decoration:none">Download ZIP</a> '
            f'<span style="color:#6b7785;font-size:13px">End-to-end encrypted link, expires '
            f"{html.escape(_fmt_date(download_expires))}.</span></p>"
        )
    if failed_titles:
        h.append(f"<p>I couldn't download: {html.escape('; '.join(failed_titles))}. The rest are complete.</p>")
    if summary:
        h.append(f'<h3 style="font-size:15px;margin:22px 0 6px">Summary</h3><p>{html.escape(summary)}</p>')
    if claims:
        h.append('<h3 style="font-size:15px;margin:22px 0 6px">Key points</h3><ul style="padding-left:18px">')
        h += [f'<li>{html.escape(c.text)} <a href="{html.escape(c.url)}">view source</a></li>' for c in claims]
        h.append("</ul>")
    h.append(f'<h3 style="font-size:15px;margin:22px 0 6px">{html.escape(doc_type.value)}</h3><ol style="padding-left:18px">')
    for d in docs:
        title = html.escape(d.title)
        link = f' <a href="{html.escape(d.url)}">view</a>' if d.url else ""
        h.append(f'<li>{title} <span style="color:#6b7785">({html.escape(d.filed)})</span>{link}</li>')
    h.append("</ol>")
    if extra_doc_types:
        h.append("<p>You also mentioned " + html.escape(", ".join(x.value for x in extra_doc_types))
                 + ". Reply to this email with the type you want next and I'll send it.</p>")
    h.append(f'<p style="font-size:13px"><a href="{html.escape(track_url)}">Request details</a></p>')
    return Draft("reply", reply_subject(subject), "\n".join(t) + "\n", _wrap("".join(h)),
                 attachments=(attachment_path,) if attachment_path else ())


def simple_reply(*, name: str, subject: str, paragraphs: Sequence[str], track_url: str | None = None) -> Draft:
    """Clarifications, not-found, declines, failures: short plain answers."""
    t = [_greeting(name), "", *[p + "\n" for p in paragraphs]]
    h = [f"<p>{html.escape(_greeting(name))}</p>", *[f"<p>{html.escape(p)}</p>" for p in paragraphs]]
    if track_url:
        t.append(f"Request details: {track_url}")
        h.append(f'<p style="font-size:13px"><a href="{html.escape(track_url)}">Request details</a></p>')
    return Draft("reply", reply_subject(subject), "\n".join(t).rstrip() + "\n", _wrap("".join(h)))
