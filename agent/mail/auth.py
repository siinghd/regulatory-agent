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
from typing import NamedTuple

import dkim
import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver
import spf
import structlog

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

# Approximates the Public Suffix List (RFC 7489 §3.2) without a PSL dependency: the
# organizational domain is the last two labels, or three under these two-level suffixes
# (including the Canadian federal and provincial ones our users mail from).
# fmt: off
_TWO_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "govt.nz",
    "co.jp", "co.in", "co.za", "com.br", "com.mx", "com.cn",
    "gc.ca", "ab.ca", "bc.ca", "mb.ca", "nb.ca", "nl.ca", "ns.ca", "nt.ca", "nu.ca", "on.ca", "pe.ca",
    "qc.ca", "sk.ca", "yk.ca",
})
# fmt: on
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
class DkimResult:
    domains: tuple[str, ...]  # d= of every signature that verified
    temperror: bool  # some signature's key lookup failed transiently


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
) -> SenderAuth:
    """DMARC-style verdict for the From domain of `email` (parsed from `raw`).

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
        from_domain, spf_result, spf_domain, dkim_result.domains, policy, dkim_temperror=dkim_result.temperror
    )
    return auth.model_copy(update={"client_ip": client_ip})


def is_temperror(auth: SenderAuth) -> bool:
    """True when the verdict may change on retry (DNS was unavailable, not negative)."""
    return auth.verdict is AuthVerdict.NONE and auth.reason.startswith(TEMPERROR)


# ---------------------------------------------------------------- DMARC


def dmarc_alignment(
    from_domain: str,
    spf_result: str,
    spf_domain: str | None,
    dkim_domains: Sequence[str],
    policy: DmarcPolicy | None,
    *,
    dkim_temperror: bool = False,
) -> SenderAuth:
    """RFC 7489 §3.1/§4.2: PASS iff an aligned SPF pass or an aligned valid DKIM signature."""

    def result(verdict: AuthVerdict, reason: str, aligned_via: str | None = None) -> SenderAuth:
        return SenderAuth(
            verdict=verdict,
            from_domain=from_domain,
            spf=spf_result,
            spf_domain=spf_domain,
            dkim_domains=tuple(dkim_domains),
            aligned_via=aligned_via,
            reason=reason,
        )

    adkim, aspf = (policy.adkim, policy.aspf) if policy else ("r", "r")
    for domain in dkim_domains:
        if _aligned(domain, from_domain, adkim):
            return result(AuthVerdict.PASS, f"DKIM d={domain} aligned with {from_domain}", "dkim")
    if spf_result == "pass" and spf_domain and _aligned(spf_domain, from_domain, aspf):
        return result(AuthVerdict.PASS, f"SPF pass for {spf_domain} aligned with {from_domain}", "spf")

    evidence = f"spf={spf_result} for {spf_domain or '-'}, valid dkim d={','.join(dkim_domains) or '-'}"
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
    labels = domain.lower().rstrip(".").split(".")
    keep = 3 if ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


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
        return DkimResult(domains=(), temperror=False)

    count = sum(1 for name, _ in verifier.headers if name.lower() == b"dkim-signature")
    domains: list[str] = []
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
            domains.append(verifier.domain.decode("ascii", "replace").lower())
    return DkimResult(domains=tuple(dict.fromkeys(domains)), temperror=temperror)


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
    """Lowercase A-label form of a hostname, or None if it isn't one."""
    try:
        ascii_domain = domain.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return ascii_domain if _HOSTNAME.fullmatch(ascii_domain) else None


def _query_name(name: str) -> dns.name.Name | None:
    try:
        return dns.name.from_text(name)
    except dns.exception.DNSException:  # empty or oversized labels: no record can exist there
        return None
