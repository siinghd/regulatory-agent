import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import dkim
import dns.exception
import dns.resolver
import pytest
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent.mail.auth import (
    MAX_DKIM_SIGNATURES,
    DkimSignature,
    DmarcPolicy,
    DnsTempError,
    SmtpClient,
    dmarc_alignment,
    is_temperror,
    lookup_dmarc,
    organizational_domain,
    parse_dmarc,
    reply_address,
    smtp_client,
    to_ascii_domain,
    verify_dkim,
    verify_sender,
)
from agent.mail.mime import parse_raw
from agent.models import AuthVerdict, InboundEmail, SenderAuth

NOW = datetime(2026, 10, 4, 14, 0, tzinfo=UTC)
TRUSTED_MTA = "mail.hsingh.app"
SIGNED_HEADERS = [b"from", b"to", b"subject", b"date", b"message-id"]


class FakeDns:
    """One fake DNS world serving both the async DMARC resolver and dkimpy's dnsfunc."""

    def __init__(self, txt: dict[str, list[str]] | None = None, *, failing: frozenset[str] = frozenset()):
        self.txt = txt or {}
        self.failing = failing
        self.queries: list[str] = []

    async def resolve(self, qname, rdtype, *, lifetime):
        name = str(qname).rstrip(".").lower()
        self.queries.append(name)
        if name in self.failing:
            raise dns.exception.Timeout()
        if name not in self.txt:
            raise dns.resolver.NXDOMAIN()
        return [SimpleNamespace(strings=(record.encode(),)) for record in self.txt[name]]

    def dkim(self, name: bytes, timeout: float = 3.0) -> bytes | None:
        key = name.decode().rstrip(".").lower()
        self.queries.append(key)
        if key in self.failing:
            raise DnsTempError(key)
        records = self.txt.get(key)
        return records[0].encode() if records else None


class FakeSpf:
    def __init__(self, results: dict[tuple[str, str], str] | None = None):
        self.results = results or {}
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, ip: str, sender: str, helo: str) -> str:
        self.calls.append((ip, sender, helo))
        return self.results.get((ip, sender.rpartition("@")[2]), "none")


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, f"v=DKIM1; k=rsa; p={base64.b64encode(public).decode()}"


def message(
    from_addr: str = "jane@bank.example",
    body: str = "Please send the Other Documents for M12205.\r\n",
    *,
    date: str | None = "Sat, 4 Oct 2026 14:00:00 +0000",
) -> bytes:
    """`date=None` leaves the Date header out."""
    date_header = f"Date: {date}\r\n" if date is not None else ""
    return (
        f"From: Jane <{from_addr}>\r\nTo: agent@hsingh.app\r\nSubject: Documents\r\n"
        f"{date_header}Message-ID: <m1@bank.example>\r\n\r\n"
    ).encode() + body.encode()


def sign(msg: bytes, domain: str, private_key: bytes, headers: list[bytes] = SIGNED_HEADERS) -> bytes:
    return dkim.sign(msg, b"sel", domain.encode(), private_key, include_headers=headers) + msg


def sign_at(when: datetime, msg: bytes, domain: str, private_key: bytes) -> bytes:
    """Sign with t= set to `when` (dkimpy stamps t= from time.time())."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dkim.time, "time", lambda: when.timestamp())
        return sign(msg, domain, private_key)


def signature(
    domain: str, signed: tuple[str, ...] = ("from", "subject"), t: int | None = None
) -> DkimSignature:
    return DkimSignature(domain=domain, signed_headers=frozenset(signed), timestamp=t)


def delivered(
    msg: bytes,
    *,
    envelope: str = "<bounce@bank.example>",
    client: str = "mx.bank.example (mx.bank.example [192.0.2.10])",
) -> bytes:
    """Prepend the trace headers Postfix and Dovecot add on delivery (topmost = last hop)."""
    return (
        f"Return-Path: {envelope}\r\n"
        "Received: from mail.hsingh.app\r\n\tby ubuntu-16gb-hel1-1 with LMTP id k0Bn\r\n"
        "\tfor <agent@hsingh.app>; Sat, 4 Oct 2026 14:00:01 +0000\r\n"
        f"Received: from {client}\r\n\tby mail.hsingh.app (Postfix) with ESMTPS id 4XQ2\r\n"
        "\tfor <agent@hsingh.app>; Sat, 4 Oct 2026 14:00:01 +0000\r\n"
    ).encode() + msg


async def verify(raw: bytes, world: FakeDns, spf: FakeSpf | None = None):
    return await verify_sender(
        raw,
        parse_raw(raw, NOW),
        trusted_mta=TRUSTED_MTA,
        resolver=world,
        spf_check=spf or FakeSpf(),
        dkim_dnsfunc=world.dkim,
    )


# ---------------------------------------------------------------- domains


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("bank.example", "bank.example"),
        ("mail.eu.bank.example", "bank.example"),
        ("Mail.Bank.Example.", "bank.example"),
        ("a.b.utility.co.uk", "utility.co.uk"),
        ("utility.co.uk", "utility.co.uk"),
        ("gov.ns.ca", "gov.ns.ca"),
        ("uarb.novascotia.ca", "novascotia.ca"),
        ("mail.nrcan.gc.ca", "nrcan.gc.ca"),
        ("mail.example.com.tr", "example.com.tr"),
        ("a.b.example.co.kr", "example.co.kr"),
        # The PSL lists nhs.uk itself (ICANN section): each NHS trust is its own organization,
        # and nhs.uk, being a public suffix, is its own organizational domain.
        ("mail.trust.nhs.uk", "trust.nhs.uk"),
        ("nhs.uk", "nhs.uk"),
        ("co.uk", "co.uk"),
        # Private section: a github.io tenant is not github.io.
        ("mail.jane.github.io", "jane.github.io"),
        # Not in the PSL; added on top of it so tenants stay apart.
        ("contoso.onmicrosoft.com", "contoso.onmicrosoft.com"),
        ("eu.contoso.onmicrosoft.com", "contoso.onmicrosoft.com"),
        ("contoso.mail.onmicrosoft.com", "contoso.mail.onmicrosoft.com"),
        ("mail.example.xn--p1ai", "example.xn--p1ai"),  # IDN TLD in A-label form
        ("localhost", "localhost"),  # a lone (unknown) TLD is its own
        ("a..b.example", "a..b.example"),  # unplaceable: returned as is, never an exception
        ("", ""),
    ],
)
def test_organizational_domain(domain, expected):
    assert organizational_domain(domain) == expected


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("Bank.Example", "bank.example"),
        ("bébé.fr", "xn--bb-bjab.fr"),
        ("bad..example", None),
        ("-bad.example", None),
        ("localhost", None),
        ("[192.0.2.1]", None),
        ("a" * 64 + ".example", None),
        ("", None),
        ("bücher.example", "xn--bcher-kva.example"),
        ("BÜCHER.Example", "xn--bcher-kva.example"),  # UTS #46 maps case before encoding
        ("ｂａｎｋ.example", "bank.example"),  # fullwidth letters map to ASCII
        ("Ｂücher．example", "xn--bcher-kva.example"),  # fullwidth full stop is a label separator
        ("faß.de", "xn--fa-hia.de"),  # IDNA2008 (nontransitional): ß is kept, not mapped to ss
        ("XN--BCHER-KVA.example", "xn--bcher-kva.example"),
        ("xn--zz.example", None),  # invalid A-label
        ("example.com.", None),  # trailing dot rejected, not stripped
        ("example.com。", None),  # ideographic full stop maps to a trailing dot
        (".example.com", None),
        ("  ", None),
        ("a_b.example", None),
        ("a." * 200 + "example", None),  # over 253 octets
    ],
)
def test_to_ascii_domain(domain, expected):
    assert to_ascii_domain(domain) == expected


# ---------------------------------------------------------------- DMARC records


class TestDmarcRecords:
    def test_parse(self):
        record = "v=DMARC1; p=Reject; adkim=s; aspf=r; rua=mailto:dmarc@bank.example"
        assert parse_dmarc(record, found_at="bank.example", inherited=False) == DmarcPolicy(
            domain="bank.example", p="reject", adkim="s", aspf="r"
        )

    def test_subdomain_policy_applies_only_when_inherited(self):
        record = "v=DMARC1; p=reject; sp=none"
        assert parse_dmarc(record, found_at="bank.example", inherited=False).p == "reject"
        assert parse_dmarc(record, found_at="bank.example", inherited=True).p == "none"

    def test_invalid_policy_still_counts_as_published(self):
        assert parse_dmarc("v=DMARC1; p=bogus", found_at="bank.example", inherited=False).p == "none"

    async def test_lookup_falls_back_to_organizational_domain(self):
        world = FakeDns({"_dmarc.bank.example": ["v=spf1 -all", "v=DMARC1; p=reject; sp=quarantine"]})
        policy = await lookup_dmarc("mail.bank.example", world)
        assert policy == DmarcPolicy(domain="bank.example", p="quarantine")
        assert world.queries == ["_dmarc.mail.bank.example", "_dmarc.bank.example"]

    async def test_lookup_falls_back_to_the_psl_organizational_domain(self):
        world = FakeDns({"_dmarc.example.com.tr": ["v=DMARC1; p=reject"]})
        policy = await lookup_dmarc("mail.example.com.tr", world)
        assert policy == DmarcPolicy(domain="example.com.tr", p="reject")
        assert world.queries == ["_dmarc.mail.example.com.tr", "_dmarc.example.com.tr"]

    async def test_lookup_never_climbs_to_a_shared_tenant_suffix(self):
        world = FakeDns({"_dmarc.onmicrosoft.com": ["v=DMARC1; p=none"]})
        assert await lookup_dmarc("contoso.onmicrosoft.com", world) is None
        assert world.queries == ["_dmarc.contoso.onmicrosoft.com"]

    async def test_lookup_prefers_the_exact_domain(self):
        world = FakeDns(
            {"_dmarc.mail.bank.example": ["v=DMARC1; p=none"], "_dmarc.bank.example": ["v=DMARC1; p=reject"]}
        )
        assert (await lookup_dmarc("mail.bank.example", world)).p == "none"

    async def test_multiple_records_mean_no_policy(self):
        world = FakeDns({"_dmarc.bank.example": ["v=DMARC1; p=reject", "v=DMARC1; p=none"]})
        assert await lookup_dmarc("bank.example", world) is None

    async def test_timeout_is_a_temporary_error(self):
        with pytest.raises(DnsTempError):
            await lookup_dmarc("bank.example", FakeDns(failing=frozenset({"_dmarc.bank.example"})))


# ---------------------------------------------------------------- alignment (pure)

REJECT = DmarcPolicy(domain="bank.example", p="reject")
STRICT = DmarcPolicy(domain="bank.example", p="reject", adkim="s", aspf="s")


@pytest.mark.parametrize(
    ("spf", "spf_domain", "dkim_domains", "policy", "dkim_temperror", "verdict", "via"),
    [
        ("none", None, ["bank.example"], REJECT, False, AuthVerdict.PASS, "dkim"),
        ("none", None, ["mail.bank.example"], REJECT, False, AuthVerdict.PASS, "dkim"),
        ("none", None, ["mail.bank.example"], STRICT, False, AuthVerdict.FAIL, None),
        ("fail", "bank.example", ["bank.example"], REJECT, False, AuthVerdict.PASS, "dkim"),
        ("pass", "bounce.bank.example", [], REJECT, False, AuthVerdict.PASS, "spf"),
        ("pass", "bounce.bank.example", [], STRICT, False, AuthVerdict.FAIL, None),
        ("pass", "esp.example", [], REJECT, False, AuthVerdict.FAIL, None),
        ("pass", "esp.example", ["evil.example"], REJECT, False, AuthVerdict.FAIL, None),
        ("softfail", "esp.example", ["evil.example"], None, False, AuthVerdict.NONE, None),
        ("fail", "bank.example", [], None, False, AuthVerdict.FAIL, None),
        ("temperror", "bank.example", [], REJECT, False, AuthVerdict.NONE, None),
        ("none", None, [], REJECT, True, AuthVerdict.NONE, None),
        ("pass", "bank.example", [], REJECT, True, AuthVerdict.PASS, "spf"),
    ],
)
def test_dmarc_alignment(spf, spf_domain, dkim_domains, policy, dkim_temperror, verdict, via):
    signatures = [signature(domain) for domain in dkim_domains]
    auth = dmarc_alignment("bank.example", spf, spf_domain, signatures, policy, dkim_temperror=dkim_temperror)
    assert (auth.verdict, auth.aligned_via) == (verdict, via)
    assert (auth.spf, auth.spf_domain, auth.dkim_domains) == (spf, spf_domain, tuple(dkim_domains))
    assert auth.reason


def test_temperror_is_flagged_for_retry():
    retry = dmarc_alignment("bank.example", "temperror", "bank.example", [], REJECT)
    final = dmarc_alignment("bank.example", "fail", "bank.example", [], REJECT)
    assert is_temperror(retry)
    assert not is_temperror(final)


@pytest.mark.parametrize(
    ("from_domain", "signer"),
    [
        ("bank.co.uk", "other.co.uk"),
        ("victim.onmicrosoft.com", "evil.onmicrosoft.com"),
        ("fabrikam.onmicrosoft.com", "contoso.onmicrosoft.com"),
        ("fabrikam.mail.onmicrosoft.com", "contoso.mail.onmicrosoft.com"),
        ("trust-a.nhs.uk", "trust-b.nhs.uk"),
        ("victim.github.io", "evil.github.io"),
        ("example.com.tr", "other.com.tr"),
    ],
)
def test_registrable_domains_under_public_suffixes_do_not_align(from_domain, signer):
    for dkim_signer, spf_domain in ((signer, None), ("unrelated.example", signer)):
        auth = dmarc_alignment(
            from_domain, "pass" if spf_domain else "none", spf_domain, [signature(dkim_signer)], None
        )
        assert auth.verdict is not AuthVerdict.PASS


@pytest.mark.parametrize(
    ("from_domain", "signer"),
    [
        ("contoso.onmicrosoft.com", "eu.contoso.onmicrosoft.com"),
        ("mail.example.com.tr", "example.com.tr"),
        ("trust.nhs.uk", "mail.trust.nhs.uk"),
    ],
)
def test_subdomains_of_one_organization_align(from_domain, signer):
    assert dmarc_alignment(from_domain, "none", None, [signature(signer)], REJECT).verdict is AuthVerdict.PASS


class TestDkimSignatureRequirements:
    FRESH = int(NOW.timestamp()) - 3600

    @pytest.mark.parametrize("signed", [("from",), ("subject",), ("from", "to", "date"), ()])
    def test_aligned_signature_must_sign_from_and_subject(self, signed):
        auth = dmarc_alignment("bank.example", "none", None, [signature("bank.example", signed)], REJECT)
        assert auth.verdict is AuthVerdict.FAIL
        assert auth.dkim_domains == ("bank.example",)  # still reported as a valid signature
        assert "does not sign" in auth.reason

    def test_aligned_spf_still_passes_without_a_usable_signature(self):
        auth = dmarc_alignment(
            "bank.example", "pass", "bank.example", [signature("bank.example", ("from",))], REJECT
        )
        assert (auth.verdict, auth.aligned_via) == (AuthVerdict.PASS, "spf")

    def test_another_signature_can_still_align(self):
        signatures = [signature("bank.example", ("from",)), signature("mail.bank.example")]
        auth = dmarc_alignment("bank.example", "none", None, signatures, REJECT)
        assert (auth.verdict, auth.reason) == (
            AuthVerdict.PASS,
            "DKIM d=mail.bank.example aligned with bank.example",
        )

    def test_stale_signature_does_not_count(self):
        stale = int((NOW - timedelta(days=4)).timestamp())
        not_before = NOW - timedelta(days=3)
        auth = dmarc_alignment(
            "bank.example", "none", None, [signature("bank.example", t=stale)], REJECT, not_before=not_before
        )
        assert auth.verdict is AuthVerdict.FAIL
        assert "stale" in auth.reason

    @pytest.mark.parametrize("t", [FRESH, None])
    def test_fresh_or_untimed_signature_counts(self, t):
        auth = dmarc_alignment(
            "bank.example",
            "none",
            None,
            [signature("bank.example", t=t)],
            REJECT,
            not_before=NOW - timedelta(days=3),
        )
        assert auth.verdict is AuthVerdict.PASS


# ---------------------------------------------------------------- Received parsing

POSTFIX_TLS = (
    "from mx.bank.example (mx.bank.example [192.0.2.10]) (using TLSv1.3 with cipher TLS_AES_256_GCM_SHA384 "
    "(256/256 bits) key-exchange X25519 server-signature RSA-PSS (2048 bits) server-digest SHA256) "
    "(No client certificate requested) by mail.hsingh.app (Postfix) with ESMTPS id 4XQ2 "
    "for <agent@hsingh.app>; Sat, 4 Oct 2026 14:00:01 +0000"
)
FORGED = "from mail-sor-f41.google.com (mail-sor-f41.google.com [209.85.220.41]) by mail.hsingh.app (Postfix)"
LMTP = "from mail.hsingh.app by mail.hsingh.app with LMTP id k0Bn (envelope-from <x@bank.example>)"


class TestSmtpClient:
    def test_postfix_header(self):
        assert smtp_client([POSTFIX_TLS], TRUSTED_MTA) == SmtpClient(ip="192.0.2.10", helo="mx.bank.example")

    def test_ipv6(self):
        header = (
            "from mx6.bank.example (unknown [IPv6:2001:DB8::25]) by mail.hsingh.app (Postfix) with ESMTPS"
        )
        assert smtp_client([header], TRUSTED_MTA) == SmtpClient(ip="2001:db8::25", helo="mx6.bank.example")

    def test_only_the_topmost_trusted_header_counts(self):
        assert smtp_client([LMTP, POSTFIX_TLS, FORGED], TRUSTED_MTA).ip == "192.0.2.10"

    def test_trusted_header_without_client_never_falls_through_to_forged_ones(self):
        local_pickup = (
            "by mail.hsingh.app (Postfix, from userid 1000) id 4XQ3; Sat, 4 Oct 2026 14:00:01 +0000"
        )
        assert smtp_client([local_pickup, FORGED], TRUSTED_MTA) is None

    def test_no_header_from_our_mta(self):
        assert smtp_client([FORGED.replace("mail.hsingh.app", "mx.evil.example")], TRUSTED_MTA) is None

    def test_lookalike_hostname_is_not_our_mta(self):
        assert (
            smtp_client([FORGED.replace("mail.hsingh.app", "mail.hsingh.app.evil.example")], TRUSTED_MTA)
            is None
        )

    def test_helo_cannot_inject_an_address(self):
        header = "from x([8.8.8.8]) (unknown [203.0.113.9]) by mail.hsingh.app (Postfix) with ESMTP"
        assert smtp_client([header], TRUSTED_MTA) == SmtpClient(ip="203.0.113.9", helo="x([8.8.8.8])")

    def test_invalid_address(self):
        assert smtp_client(["from x (unknown [999.1.1.1]) by mail.hsingh.app"], TRUSTED_MTA) is None


# ---------------------------------------------------------------- DKIM


class TestVerifyDkim:
    def test_valid_signature(self, keypair):
        private, public = keypair
        world = FakeDns({"sel._domainkey.bank.example": [public]})
        result = verify_dkim(sign(message(), "bank.example", private), world.dkim)
        assert (result.domains, result.temperror) == (("bank.example",), False)

    def test_reports_signed_headers_and_timestamp(self, keypair):
        private, public = keypair
        world = FakeDns({"sel._domainkey.bank.example": [public]})
        signed = sign_at(NOW, message(), "bank.example", private)
        assert verify_dkim(signed, world.dkim).signatures == (
            DkimSignature(
                domain="bank.example",
                signed_headers=frozenset({"from", "to", "subject", "date", "message-id"}),
                timestamp=int(NOW.timestamp()),
            ),
        )

    def test_tampered_body_fails(self, keypair):
        private, public = keypair
        signed = sign(message(), "bank.example", private)
        tampered = signed.replace(b"M12205", b"M99999")
        assert verify_dkim(tampered, FakeDns({"sel._domainkey.bank.example": [public]}).dkim).domains == ()

    def test_tampered_signed_header_fails(self, keypair):
        private, public = keypair
        tampered = sign(message(), "bank.example", private).replace(
            b"Subject: Documents", b"Subject: Invoice"
        )
        assert verify_dkim(tampered, FakeDns({"sel._domainkey.bank.example": [public]}).dkim).domains == ()

    def test_missing_key(self, keypair):
        private, _ = keypair
        assert verify_dkim(sign(message(), "bank.example", private), FakeDns().dkim).domains == ()

    def test_every_signature_is_checked(self, keypair):
        private, public = keypair
        world = FakeDns({"sel._domainkey.bank.example": [public], "sel._domainkey.esp.example": [public]})
        twice = sign(sign(message(), "bank.example", private), "esp.example", private)
        assert verify_dkim(twice, world.dkim).domains == ("esp.example", "bank.example")

    def test_key_lookup_timeout_is_temporary(self, keypair):
        private, _ = keypair
        world = FakeDns(failing=frozenset({"sel._domainkey.bank.example"}))
        result = verify_dkim(sign(message(), "bank.example", private), world.dkim)
        assert (result.domains, result.temperror) == ((), True)

    @pytest.mark.parametrize(
        "raw",
        [
            b"DKIM-Signature: garbage\r\n" + message(),
            b"DKIM-Signature: v=1; a=rsa-sha256; d=bank.example; i=bank.example; s=sel; h=from; bh=AAAA; b=AAAA\r\n"
            + message(),  # i= equal to d= makes dkimpy index out of range
            b"DKIM-Signature: v=1; a=rsa-sha256; d=bank.example; s=sel; l=; h=from; bh=AAAA; b=AAAA\r\n"
            + message(),
            b" continuation line before any header\r\n" + message(),
        ],
    )
    def test_hostile_signatures_are_invalid_not_errors(self, raw):
        world = FakeDns({"sel._domainkey.bank.example": ["v=DKIM1; p=AAAA"]})
        assert verify_dkim(raw, world.dkim).domains == ()

    def test_signature_count_is_capped(self):
        bogus = b"DKIM-Signature: v=1; a=rsa-sha256; d=bank.example; s=sel; h=from; bh=AAAA; b=AAAA\r\n"
        world = FakeDns()
        verify_dkim(bogus * (MAX_DKIM_SIGNATURES + 10) + message(), world.dkim)
        assert len(world.queries) == MAX_DKIM_SIGNATURES


# ---------------------------------------------------------------- verify_sender (end to end, fake DNS)

BANK_DMARC = {"_dmarc.bank.example": ["v=DMARC1; p=reject"]}


class TestVerifySender:
    async def test_aligned_dkim_passes(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        auth = await verify(delivered(sign(message(), "bank.example", private)), world)
        assert (auth.verdict, auth.aligned_via, auth.dkim_domains) == (
            AuthVerdict.PASS,
            "dkim",
            ("bank.example",),
        )
        assert auth.client_ip == "192.0.2.10"

    async def test_aligned_spf_passes_using_our_mtas_view_of_the_client(self):
        spf = FakeSpf({("192.0.2.10", "bank.example"): "pass"})
        auth = await verify(delivered(message()), FakeDns(BANK_DMARC), spf)
        assert (auth.verdict, auth.aligned_via, auth.spf_domain) == (AuthVerdict.PASS, "spf", "bank.example")
        assert spf.calls == [("192.0.2.10", "bounce@bank.example", "mx.bank.example")]

    async def test_signature_valid_for_another_domain_is_not_aligned(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.evil.example": [public]})
        spf = FakeSpf({("192.0.2.66", "evil.example"): "pass"})
        raw = delivered(
            sign(message(), "evil.example", private),
            envelope="<bounce@evil.example>",
            client="mx.evil.example (mx.evil.example [192.0.2.66])",
        )
        auth = await verify(raw, world, spf)
        assert auth.verdict is AuthVerdict.FAIL
        assert (auth.spf, auth.spf_domain, auth.dkim_domains) == ("pass", "evil.example", ("evil.example",))

    async def test_tampered_signed_message_does_not_pass(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        raw = delivered(sign(message(), "bank.example", private)).replace(b"M12205", b"M99999")
        assert (await verify(raw, world)).verdict is AuthVerdict.FAIL

    async def test_no_dmarc_and_nothing_aligned_is_none(self):
        auth = await verify(
            delivered(message()), FakeDns(), FakeSpf({("192.0.2.10", "bank.example"): "neutral"})
        )
        assert auth.verdict is AuthVerdict.NONE
        assert not is_temperror(auth)

    async def test_dmarc_timeout_is_retryable(self):
        auth = await verify(delivered(message()), FakeDns(failing=frozenset({"_dmarc.bank.example"})))
        assert auth.verdict is AuthVerdict.NONE
        assert is_temperror(auth)

    async def test_spf_temperror_is_retryable(self):
        spf = FakeSpf({("192.0.2.10", "bank.example"): "temperror"})
        assert is_temperror(await verify(delivered(message()), FakeDns(BANK_DMARC), spf))

    async def test_null_sender_checks_the_helo_identity(self):
        spf = FakeSpf({("192.0.2.10", "mx.bank.example"): "pass"})
        auth = await verify(delivered(message(), envelope="<>"), FakeDns(BANK_DMARC), spf)
        assert spf.calls == [("192.0.2.10", "postmaster@mx.bank.example", "mx.bank.example")]
        assert (auth.verdict, auth.aligned_via) == (AuthVerdict.PASS, "spf")

    async def test_sender_written_trace_headers_are_ignored(self):
        forged = (
            b"Return-Path: <ceo@bank.example>\r\n"
            b"Received: from mail-sor-f41.google.com (mail-sor-f41.google.com [209.85.220.41])\r\n"
            b"\tby mail.hsingh.app (Postfix) with ESMTPS id FORGED\r\n"
        )
        spf = FakeSpf({("209.85.220.41", "bank.example"): "pass", ("185.220.101.47", "evil.example"): "fail"})
        raw = delivered(
            forged + message(),
            envelope="<x@evil.example>",
            client="bank.example (unknown [185.220.101.47])",
        )
        auth = await verify(raw, FakeDns(BANK_DMARC), spf)
        assert spf.calls == [("185.220.101.47", "x@evil.example", "bank.example")]
        assert (auth.verdict, auth.client_ip) == (AuthVerdict.FAIL, "185.220.101.47")

    async def test_without_a_trusted_hop_spf_is_none(self):
        spf = FakeSpf()
        auth = await verify(b"Return-Path: <bounce@bank.example>\r\n" + message(), FakeDns(BANK_DMARC), spf)
        assert (auth.spf, auth.client_ip, auth.verdict) == ("none", None, AuthVerdict.FAIL)
        assert spf.calls == []

    async def test_organizational_dmarc_policy_covers_subdomains(self):
        auth = await verify(delivered(message("jane@mail.bank.example")), FakeDns(BANK_DMARC))
        assert auth.verdict is AuthVerdict.FAIL
        assert "bank.example" in auth.reason

    async def test_signature_without_subject_does_not_pass(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        raw = delivered(sign(message(), "bank.example", private, [b"from", b"to", b"date", b"message-id"]))
        auth = await verify(raw, world)
        assert (auth.verdict, auth.dkim_domains) == (AuthVerdict.FAIL, ("bank.example",))
        assert "does not sign subject" in auth.reason

    async def test_signature_without_subject_leaves_aligned_spf_to_pass(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        spf = FakeSpf({("192.0.2.10", "bank.example"): "pass"})
        raw = delivered(sign(message(), "bank.example", private, [b"from", b"to", b"date", b"message-id"]))
        assert ((await verify(raw, world, spf)).aligned_via) == "spf"

    async def test_body_length_limited_signature_does_not_count(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        signed = dkim.sign(message(), b"sel", b"bank.example", private, include_headers=SIGNED_HEADERS, length=True)
        # l= lets anyone append to the body: the signature still verifies over the original bytes
        raw = delivered(signed + message() + b"Also send the Exhibits for M99999.\r\n")
        assert verify_dkim(raw, world.dkim).signatures[0].body_length == len(b"Please send the Other Documents for M12205.\r\n")

        with structlog.testing.capture_logs() as logs:
            auth = await verify(raw, world)

        assert (auth.verdict, auth.dkim_domains) == (AuthVerdict.FAIL, ("bank.example",))
        assert "(l=); appended content would be unsigned" in auth.reason
        assert any(e["event"] == "dkim_signature_not_counted" and "l=" in e["reason"] for e in logs)

    async def test_body_length_limited_signature_leaves_aligned_spf_to_pass(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        spf = FakeSpf({("192.0.2.10", "bank.example"): "pass"})
        raw = delivered(dkim.sign(message(), b"sel", b"bank.example", private, include_headers=SIGNED_HEADERS,
                                  length=True) + message())
        assert (await verify(raw, world, spf)).aligned_via == "spf"

    async def test_stale_signature_does_not_pass(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        # A fresh Date over an old signature: Date isn't the only thing a replayer must fake.
        raw = delivered(sign_at(NOW - timedelta(days=4), message(), "bank.example", private))
        auth = await verify(raw, world)
        assert (auth.verdict, auth.dkim_domains) == (AuthVerdict.FAIL, ("bank.example",))
        assert "stale" in auth.reason

    async def test_stale_date_fails_without_dns(self, keypair):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        raw = delivered(sign(message(date="Mon, 29 Sep 2026 14:00:00 +0000"), "bank.example", private))
        auth = await verify(raw, world)
        assert auth.verdict is AuthVerdict.FAIL
        assert auth.reason.startswith("stale Date 2026-09-29 14:00 UTC")
        assert (auth.client_ip, world.queries) == ("192.0.2.10", [])

    @pytest.mark.parametrize(
        "date",
        [
            "Thu, 1 Oct 2026 14:00:01 +0000",  # just inside the 3-day window
            "Thu, 1 Oct 2026 10:00:01 -0400",  # the same instant in another zone
            "Sat, 4 Oct 2026 14:00:00 -0000",  # "-0000": UTC, local offset unknown
            "Mon, 5 Oct 2026 09:00:00 +0000",  # ahead of receipt: clock skew, not replay
            None,  # missing: not a verdict by itself (see verify_sender)
            "Fri, Oct 3, 2026 at 5:40 PM",  # unparseable: same
            "Sat, 31 Feb 2026 14:00:00 +0000",
            "Sat, 4 Oct 2026 14:00:00 +9999",
        ],
    )
    async def test_fresh_missing_or_unparseable_date_passes(self, keypair, date):
        private, public = keypair
        world = FakeDns({**BANK_DMARC, "sel._domainkey.bank.example": [public]})
        auth = await verify(delivered(sign(message(date=date), "bank.example", private)), world)
        assert (auth.verdict, auth.aligned_via) == (AuthVerdict.PASS, "dkim"), auth.reason

    async def test_max_age_can_be_overridden(self):
        raw = delivered(message(date="Fri, 3 Oct 2026 13:00:00 +0000"))
        spf = FakeSpf({("192.0.2.10", "bank.example"): "pass"})
        email = parse_raw(raw, NOW)
        kwargs = {"trusted_mta": TRUSTED_MTA, "resolver": FakeDns(BANK_DMARC), "spf_check": spf}
        assert (await verify_sender(raw, email, **kwargs)).verdict is AuthVerdict.PASS
        short = await verify_sender(raw, email, max_age=timedelta(hours=12), **kwargs)
        assert short.verdict is AuthVerdict.FAIL
        assert "more than 0.5 days" in short.reason

    async def test_reply_goes_to_the_authenticated_a_label(self):
        spf = FakeSpf({("192.0.2.10", "xn--bcher-kva.example"): "pass"})
        world = FakeDns({"_dmarc.xn--bcher-kva.example": ["v=DMARC1; p=reject"]})
        raw = delivered(message("Jane@BÜCHER.example"), envelope="<bounce@xn--bcher-kva.example>")
        email = parse_raw(raw, NOW)
        auth = await verify(raw, world, spf)
        assert (email.from_addr, auth.verdict, auth.from_domain) == (
            "jane@bücher.example",
            AuthVerdict.PASS,
            "xn--bcher-kva.example",
        )
        assert reply_address(email, auth) == "jane@xn--bcher-kva.example"

    async def test_malformed_from_domain_fails_without_dns(self):
        world = FakeDns()
        email = InboundEmail(message_id="<m@x>", from_addr="jane@bad..example", received_at=NOW)
        auth = await verify_sender(
            message(),
            email,
            trusted_mta=TRUSTED_MTA,
            resolver=world,
            spf_check=FakeSpf(),
            dkim_dnsfunc=world.dkim,
        )
        assert auth.verdict is AuthVerdict.FAIL
        assert world.queries == []


def test_reply_address_uses_the_authenticated_domain():
    email = InboundEmail(message_id="<m@x>", from_addr="jane.doe@ｂｕｃｈｅｒ.example", received_at=NOW)
    auth = SenderAuth(verdict=AuthVerdict.PASS, from_domain="bucher.example", spf="pass")
    assert reply_address(email, auth) == "jane.doe@bucher.example"
