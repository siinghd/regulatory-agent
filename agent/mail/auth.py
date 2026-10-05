"""Anti-spoofing: SPF + DKIM + DMARC alignment, evaluated by us.

Our Postfix accepts mail without checking any of these, so the From header is just a claim
until this module ties it to the domain's DNS. Inputs are attacker-controlled: malformed
headers, signatures or records yield FAIL/NONE with a reason, never an exception.
"""

import asyncio
import ipaddress
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from importlib.resources import files
from typing import NamedTuple

import dkim
import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver
import idna
import spf
import structlog
from publicsuffixlist import PublicSuffixList

from agent.config import get_settings
from agent.mail.mime import envelope_sender, parse_headers, received_headers
from agent.models import AuthVerdict, InboundEmail, SenderAuth

log = structlog.get_logger()

DNS_TIMEOUT_S = 3.0
# pyspf takes either a per-lookup timeout or a budget for the whole evaluation, not both. An
# SPF check may chain 10+ lookups, so bound the total; running out yields "temperror".
SPF_BUDGET_S = 10.0
# Each signature costs a DNS lookup and an RSA verify; real mail carries one to three.
MAX_DKIM_SIGNATURES = 5
TEMPERROR = "temperror"

SpfCheck = Callable[[str, str, str], str]  # (client ip, envelope sender, helo) -> SPF result
DkimDnsFunc = Callable[..., bytes | None]  # dkimpy's dnsfunc(name: bytes, timeout=...)

_NO_RECORD = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.YXDOMAIN)
_TEMP_FAILURE = (dns.exception.Timeout, dns.resolver.NoNameservers)
# What dkimpy raises on hostile input besides DKIMException: IndexError for a message that
# starts with a continuation line or a signature whose i= equals d=, ValueError for an empty
# l=, binascii.Error (a ValueError) for bad base64.
_DKIM_ERRORS = (dkim.DKIMException, IndexError, ValueError)

# Organizational domains (RFC 7489 §3.2) come from the Public Suffix List snapshot bundled
# with the pinned publicsuffixlist release: read from the package, never downloaded. The
# private section is included, as DMARC implementations do, so tenants of shared hosting
# suffixes (github.io, herokuapp.com, ...) are separate organizations: one tenant can't align
# with another's From domain or inherit its DMARC policy. The PSL omits some shared mail-tenant
# suffixes; without these, evil.onmicrosoft.com would align with victim.onmicrosoft.com.
_SHARED_TENANT_SUFFIXES = (
    "onmicrosoft.com",  # Microsoft 365 initial tenant domains
    "mail.onmicrosoft.com",  # and their mail routing domains
    "onmicrosoft.us",  # Microsoft 365 GCC High / DoD
    "mail.onmicrosoft.us",
    "partner.onmschina.cn",  # Microsoft 365 operated by 21Vianet
)
_PSL = PublicSuffixList(
    files("publicsuffixlist").joinpath("public_suffix_list.dat").read_text("utf-8")
    + "\n"
    + "\n".join(_SHARED_TENANT_SUFFIXES)
)
# A DKIM signature that leaves these out can be replayed under another subject (the request
# we act on), so it doesn't count towards alignment.
_REQUIRED_SIGNED_HEADERS = frozenset({"from", "subject"})
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_DMARC_RECORD = re.compile(r"\s*v\s*=\s*DMARC1\s*(?:;|$)", re.IGNORECASE)
_DMARC_POLICIES = frozenset({"none", "quarantine", "reject"})
_FROM_CLAUSE = re.compile(
    r"^from\s+(?P<helo>\S+)\s+\([^()\[\]]*\[(?:IPv6:)?(?P<ip>[0-9a-f:.]+)\]\)", re.IGNORECASE
)
_LMTP_HOP = re.compile(r"\bwith\s+LMTPS?\b", re.IGNORECASE)


class DnsTempError(Exception):
    """A lookup timed out or every nameserver failed: the answer is unknown, not negative."""


@dataclass(frozen=True)
class DmarcPolicy:
    domain: str  # where the record was found: the From domain or its organizational domain
    p: str  # none | quarantine | reject (sp= when inherited from the organizational domain)
    adkim: str = "r"  # r = relaxed (organizational domains match), s = strict (exact match)
    aspf: str = "r"


@dataclass(frozen=True)
class DkimSignature:
    domain: str  # d=, lowercase
    signed_headers: frozenset[str]  # h=, lowercase field names
    timestamp: int | None = None  # t= (signing time, Unix seconds); optional in RFC 6376
    body_length: int | None = None  # l=: only this many body bytes are signed


@dataclass(frozen=True)
class DkimResult:
    signatures: tuple[DkimSignature, ...]  # every signature that verified, topmost first
    temperror: bool  # some signature's key lookup failed transiently

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(sig.domain for sig in self.signatures))


class SmtpClient(NamedTuple):
    ip: str
    helo: str


async def verify_sender(
    raw: bytes,
    email: InboundEmail,
    *,
    trusted_mta: str,
    resolver: dns.asyncresolver.Resolver | None = None,
    spf_check: SpfCheck | None = None,
    dkim_dnsfunc: DkimDnsFunc | None = None,
    max_age: timedelta | None = None,
) -> SenderAuth:
    """DMARC-style verdict for the From domain of `email` (parsed from `raw`).

    Replay guard: mail whose Date is more than `max_age` (default: mail_max_age_days) before
    `email.received_at` fails, and an aligned DKIM signature made before then doesn't count.
    `spf_check` and `dkim_dnsfunc` default to live DNS; tests inject fakes.
    """
    from_domain = to_ascii_domain(email.from_addr.rpartition("@")[2])
    if from_domain is None:
        return SenderAuth(
            verdict=AuthVerdict.FAIL,
            from_domain=email.from_addr.rpartition("@")[2],
            spf="none",
            reason="From domain is not a valid hostname",
        )

    headers = parse_headers(raw)
    client = smtp_client(received_headers(headers), trusted_mta)
    client_ip = client.ip if client else None
    window = max_age if max_age is not None else timedelta(days=get_settings().mail_max_age_days)
    not_before = _as_utc(email.received_at) - window
    # A missing or unparseable Date doesn't fail the message: where Date isn't signed, a
    # replayer could just as easily write a fresh one, so rejecting its absence would only
    # drop mail from broken clients. The aligned signature's t= is the guard that holds.
    sent_at = _date_header(headers.get("date"))
    if sent_at is not None and sent_at < not_before:
        return SenderAuth(
            verdict=AuthVerdict.FAIL,
            from_domain=from_domain,
            spf="none",
            client_ip=client_ip,
            reason=(
                f"stale Date {sent_at:%Y-%m-%d %H:%M} UTC: more than {window.total_seconds() / 86400:g} "
                "days before we received it (possible replay)"
            ),
        )
    try:
        policy = await lookup_dmarc(from_domain, resolver or dns.asyncresolver.get_default_resolver())
    except DnsTempError as e:
        # The policy sets the alignment mode and decides FAIL vs NONE: no verdict is final without it.
        return SenderAuth(
            verdict=AuthVerdict.NONE,
            from_domain=from_domain,
            spf="none",
            client_ip=client_ip,
            reason=f"{TEMPERROR}: DMARC lookup failed: {e}",
        )

    (spf_result, spf_domain), dkim_result = await asyncio.gather(
        _evaluate_spf(client, envelope_sender(headers), spf_check or check_spf),
        asyncio.to_thread(verify_dkim, raw, dkim_dnsfunc or dkim_txt_lookup),
    )
    auth = dmarc_alignment(
        from_domain,
        spf_result,
        spf_domain,
        dkim_result.signatures,
        policy,
        dkim_temperror=dkim_result.temperror,
        not_before=not_before,
    )
    return auth.model_copy(update={"client_ip": client_ip})


def is_temperror(auth: SenderAuth) -> bool:
    """True when the verdict may change on retry (DNS was unavailable, not negative)."""
    return auth.verdict is AuthVerdict.NONE and auth.reason.startswith(TEMPERROR)


def reply_address(email: InboundEmail, auth: SenderAuth) -> str:
    """Where to answer `email`: its From local part at the domain `verify_sender` checked.

    The From header may spell the domain as a U-label, in fullwidth or in mixed case; the reply
    goes to the A-label that SPF/DKIM/DMARC were evaluated for, not to the sender's spelling.
    """
    return f"{email.from_addr.rpartition('@')[0]}@{auth.from_domain}"


# ---------------------------------------------------------------- DMARC


def dmarc_alignment(
    from_domain: str,
    spf_result: str,
    spf_domain: str | None,
    dkim_signatures: Sequence[DkimSignature],
    policy: DmarcPolicy | None,
    *,
    dkim_temperror: bool = False,
    not_before: datetime | None = None,
) -> SenderAuth:
    """RFC 7489 §3.1/§4.2: PASS iff an aligned SPF pass or an aligned valid DKIM signature.

    An aligned signature counts only if it signed From and Subject, signed the whole body (no l=)
    and, when it carries t=, was made at or after `not_before`. Others are still reported in
    `dkim_domains`, and why they didn't count is logged and in the reason.
    """
    dkim_domains = tuple(dict.fromkeys(sig.domain for sig in dkim_signatures))

    def result(verdict: AuthVerdict, reason: str, aligned_via: str | None = None) -> SenderAuth:
        return SenderAuth(
            verdict=verdict,
            from_domain=from_domain,
            spf=spf_result,
            spf_domain=spf_domain,
            dkim_domains=dkim_domains,
            aligned_via=aligned_via,
            reason=reason,
        )

    adkim, aspf = (policy.adkim, policy.aspf) if policy else ("r", "r")
    not_counted: list[str] = []
    for sig in dkim_signatures:
        if not _aligned(sig.domain, from_domain, adkim):
            continue
        problem = _signature_problem(sig, not_before)
        if problem is None:
            return result(AuthVerdict.PASS, f"DKIM d={sig.domain} aligned with {from_domain}", "dkim")
        log.info("dkim_signature_not_counted", domain=sig.domain, from_domain=from_domain, reason=problem)
        not_counted.append(f"d={sig.domain} {problem}")
    if spf_result == "pass" and spf_domain and _aligned(spf_domain, from_domain, aspf):
        return result(AuthVerdict.PASS, f"SPF pass for {spf_domain} aligned with {from_domain}", "spf")

    evidence = f"spf={spf_result} for {spf_domain or '-'}, valid dkim d={','.join(dkim_domains) or '-'}"
    if not_counted:
        evidence += f" (aligned but not counted: {'; '.join(not_counted)})"
    if spf_result == TEMPERROR or dkim_temperror:
        return result(
            AuthVerdict.NONE, f"{TEMPERROR}: DNS failed before alignment was established; {evidence}"
        )
    if policy is not None:
        return result(AuthVerdict.FAIL, f"DMARC p={policy.p} at {policy.domain}, nothing aligned; {evidence}")
    if spf_result == "fail":
        return result(AuthVerdict.FAIL, f"SPF hard fail and no aligned DKIM; {evidence}")
    return result(AuthVerdict.NONE, f"no DMARC record for {from_domain} and nothing aligned; {evidence}")


def organizational_domain(domain: str) -> str:
    """Public suffix plus one label; a public suffix (or a name the PSL can't place) is its own."""
    name = domain.lower().rstrip(".")
    return _PSL.privatesuffix(name) or name


def _signature_problem(sig: DkimSignature, not_before: datetime | None) -> str | None:
    """Why an aligned, cryptographically valid signature still doesn't authenticate the message."""
    unsigned = _REQUIRED_SIGNED_HEADERS - sig.signed_headers
    if unsigned:
        return f"does not sign {', '.join(sorted(unsigned))}"
    if sig.body_length is not None:
        # Anyone can append to a body signed with l= (a request, a link) and the signature still
        # verifies: it vouches for the first l= bytes, not for the message we act on.
        return f"signs only the first {sig.body_length} body bytes (l=); appended content would be unsigned"
    if not_before is not None and sig.timestamp is not None and sig.timestamp < not_before.timestamp():
        return f"signed at t={sig.timestamp}, before {not_before.astimezone(UTC):%Y-%m-%d %H:%M} UTC (stale)"
    return None


def _aligned(domain: str, from_domain: str, mode: str) -> bool:
    if mode == "s":
        return domain.lower() == from_domain.lower()
    return organizational_domain(domain) == organizational_domain(from_domain)


async def lookup_dmarc(domain: str, resolver: dns.asyncresolver.Resolver) -> DmarcPolicy | None:
    """RFC 7489 §6.6.3 policy discovery: the From domain first, then its organizational domain."""
    org_domain = organizational_domain(domain)
    for candidate in (domain, org_domain) if org_domain != domain else (domain,):
        records = [r for r in await _txt_records(resolver, f"_dmarc.{candidate}") if _DMARC_RECORD.match(r)]
        if len(records) > 1:
            return None  # several records: discovery ends with no policy
        if records:
            return parse_dmarc(records[0], found_at=candidate, inherited=candidate != domain)
    return None


def parse_dmarc(record: str, *, found_at: str, inherited: bool) -> DmarcPolicy:
    tags: dict[str, str] = {}
    for item in record.split(";"):
        key, sep, value = item.partition("=")
        if sep:
            tags[key.strip().lower()] = value.strip().lower()
    p = tags.get("p", "")
    if inherited and tags.get("sp") in _DMARC_POLICIES:
        p = tags["sp"]
    return DmarcPolicy(
        domain=found_at,
        # An invalid p= still means the owner published DMARC; treat it as monitoring only.
        p=p if p in _DMARC_POLICIES else "none",
        adkim="s" if tags.get("adkim") == "s" else "r",
        aspf="s" if tags.get("aspf") == "s" else "r",
    )


async def _txt_records(resolver: dns.asyncresolver.Resolver, name: str) -> list[str]:
    qname = _query_name(name)
    if qname is None:
        return []
    try:
        answer = await resolver.resolve(qname, "TXT", lifetime=DNS_TIMEOUT_S)
    except _NO_RECORD:
        return []
    except _TEMP_FAILURE as e:
        raise DnsTempError(f"TXT {name}: {e}") from e
    return [b"".join(rdata.strings).decode("utf-8", "replace") for rdata in answer]


# ---------------------------------------------------------------- SPF


def smtp_client(received: Sequence[str], trusted_mta: str) -> SmtpClient | None:
    """Connecting client of our MTA, from the topmost Received header it wrote.

    Headers below that one were written by the sender and can claim anything. Dovecot's LMTP
    hop sits above it and carries no client IP, so it is skipped.
    """
    by_trusted_mta = re.compile(rf"\bby\s+{re.escape(trusted_mta)}(?![\w.-])", re.IGNORECASE)
    for header in received:
        if not by_trusted_mta.search(header) or _LMTP_HOP.search(header):
            continue
        match = _FROM_CLAUSE.match(header)
        if match is None:
            return None
        try:
            ip = ipaddress.ip_address(match["ip"])
        except ValueError:
            return None
        return SmtpClient(ip=str(ip), helo=match["helo"].lower())
    return None


def check_spf(ip: str, sender: str, helo: str) -> str:
    """pyspf with live DNS; blocking, so callers run it in a thread."""
    result, _explanation = spf.check2(i=ip, s=sender, h=helo, querytime=SPF_BUDGET_S)
    return result


async def _evaluate_spf(
    client: SmtpClient | None, envelope: str | None, spf_check: SpfCheck
) -> tuple[str, str | None]:
    """(SPF result, domain it was evaluated for)."""
    if client is None or envelope is None:
        return "none", None
    # RFC 7208 §2.4: for the null sender (bounces) the HELO identity is checked instead.
    local, _, domain = (envelope or f"postmaster@{client.helo}").rpartition("@")
    ascii_domain = to_ascii_domain(domain)
    if not local or ascii_domain is None:
        return "none", None
    return await asyncio.to_thread(spf_check, client.ip, f"{local}@{ascii_domain}", client.helo), ascii_domain


# ---------------------------------------------------------------- DKIM


def verify_dkim(raw: bytes, dnsfunc: DkimDnsFunc) -> DkimResult:
    """Verify every DKIM-Signature (up to MAX_DKIM_SIGNATURES). Blocking: run in a thread."""
    try:
        verifier = dkim.DKIM(raw, timeout=DNS_TIMEOUT_S)
    except _DKIM_ERRORS as e:
        log.info("dkim_unparseable_message", error=f"{type(e).__name__}: {e}")
        return DkimResult(signatures=(), temperror=False)

    count = sum(1 for name, _ in verifier.headers if name.lower() == b"dkim-signature")
    signatures: list[DkimSignature] = []
    temperror = False
    for idx in range(min(count, MAX_DKIM_SIGNATURES)):
        try:
            valid = verifier.verify(idx=idx, dnsfunc=dnsfunc)
        except DnsTempError as e:
            log.info("dkim_key_lookup_failed", index=idx, error=str(e))
            temperror = True
            continue
        except _DKIM_ERRORS as e:
            log.info("dkim_signature_invalid", index=idx, error=f"{type(e).__name__}: {e}")
            continue
        if valid:
            signatures.append(_verified_signature(verifier))
    return DkimResult(signatures=tuple(signatures), temperror=temperror)


def _verified_signature(verifier: dkim.DKIM) -> DkimSignature:
    """d=, h=, t= and l= of the signature `verifier` just verified.

    dkimpy has validated their syntax: t= and l= are decimal digits it already converted with int().
    """
    timestamp = verifier.signature_fields.get(b"t")
    length = verifier.signature_fields.get(b"l")
    return DkimSignature(
        domain=verifier.domain.decode("ascii", "replace").lower(),
        signed_headers=frozenset(
            name.decode("ascii", "replace").strip().lower() for name in verifier.include_headers
        ),
        timestamp=int(timestamp) if timestamp is not None else None,
        body_length=int(length) if length is not None else None,
    )


def dkim_txt_lookup(name: bytes, timeout: float = DNS_TIMEOUT_S) -> bytes | None:
    """dkimpy dnsfunc: the selector's key record, or None if it doesn't exist. Blocking."""
    qname = _query_name(name.decode("utf-8", "replace"))
    if qname is None:
        return None
    try:
        answer = dns.resolver.resolve(qname, "TXT", lifetime=timeout)
    except _NO_RECORD:
        return None
    except _TEMP_FAILURE as e:
        raise DnsTempError(f"TXT {qname}: {e}") from e
    return b"".join(next(iter(answer)).strings)


# ---------------------------------------------------------------- names


def to_ascii_domain(domain: str) -> str | None:
    """Lowercase A-label form of a hostname (UTS #46 mapping, IDNA2008), or None if it isn't one.

    A trailing dot (also a mapped one, like the ideographic full stop) is rejected rather than
    stripped: a mail domain is never written as an absolute name.
    """
    domain = domain.strip()
    # A valid name is at most 253 octets; the cap also bounds idna's work on hostile input.
    if not domain or len(domain) > 253:
        return None
    try:
        ascii_domain = idna.encode(domain, uts46=True).decode("ascii").lower()
    except (UnicodeError, ValueError):  # idna.IDNAError is a UnicodeError
        return None
    return ascii_domain if _HOSTNAME.fullmatch(ascii_domain) else None


def _date_header(value: object) -> datetime | None:
    """The Date header as an aware UTC datetime, or None if absent or unparseable."""
    if value is None:
        return None
    try:
        return _as_utc(parsedate_to_datetime(str(value)))
    except (ValueError, TypeError, IndexError, OverflowError):
        return None


def _as_utc(moment: datetime) -> datetime:
    # RFC 5322's "-0000" zone (parsed as naive) means UTC with no local offset known.
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _query_name(name: str) -> dns.name.Name | None:
    try:
        return dns.name.from_text(name)
    except dns.exception.DNSException:  # empty or oversized labels: no record can exist there
        return None
