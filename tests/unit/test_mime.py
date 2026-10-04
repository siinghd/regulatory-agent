import hashlib
from datetime import UTC, datetime

import pytest

from agent.mail.mime import (
    MAX_SUBJECT_CHARS,
    MAX_TEXT_CHARS,
    MalformedEmail,
    envelope_sender,
    parse_headers,
    parse_raw,
    received_headers,
    strip_quoted,
)

NOW = datetime(2026, 10, 4, 14, 0, tzinfo=UTC)


def raw_email(headers: str, body: str | bytes = "Please send the Other Documents for M12205.\n") -> bytes:
    """Build CRLF wire bytes from readable LF text; `bytes` bodies are appended untouched."""
    head = headers.strip("\n").replace("\n", "\r\n").encode() + b"\r\n\r\n"
    return head + (body if isinstance(body, bytes) else body.replace("\n", "\r\n").encode())


BASIC = """From: =?utf-8?q?Doe=2C_Jane?= <Jane.Doe@Example.COM>
To: Regulatory Agent <agent@hsingh.app>, "Ops, Team" <ops@example.com>
Reply-To: Someone Else <other@example.net>
Subject: =?utf-8?q?Documents_for_M12205_=F0=9F=93=84?=
Message-ID: <abc123@mail.example.com>
In-Reply-To: <req-1@hsingh.app>
References: <root@example.com>
 <req-1@hsingh.app>
X-Trace: first
X-Trace: second
Content-Type: text/plain; charset=utf-8"""


class TestParseRaw:
    def test_fields(self):
        raw = raw_email(BASIC)
        email = parse_raw(raw, NOW)

        assert email.from_addr == "jane.doe@example.com"
        assert email.from_name == "Doe, Jane"  # encoded comma did not split the address list
        assert email.to == ("agent@hsingh.app", "ops@example.com")
        assert email.reply_to == "other@example.net"
        assert email.subject == "Documents for M12205 📄"
        assert email.message_id == "<abc123@mail.example.com>"
        assert email.in_reply_to == "<req-1@hsingh.app>"
        assert email.references == ("<root@example.com>", "<req-1@hsingh.app>")
        assert email.text == "Please send the Other Documents for M12205."
        assert email.received_at == NOW
        assert email.raw_sha256 == hashlib.sha256(raw).hexdigest()

    def test_headers_are_lowercased_and_last_value_wins(self):
        headers = parse_raw(raw_email(BASIC), NOW).headers
        assert headers["x-trace"] == "second"
        assert headers["message-id"] == "<abc123@mail.example.com>"
        assert all(key == key.lower() for key in headers)

    @pytest.mark.parametrize(
        "from_headers",
        [
            "",  # missing
            "From: alice@bank.example, mallory@evil.example",
            "From: Alice <alice@bank.example>\nFrom: Mallory <mallory@evil.example>",
            "From: undisclosed-recipients:;",
            "From: <>",
            "From: just a name",
            "From: team: alice@bank.example, bob@bank.example;",
            # Display name that looks like an address: ambiguous about who is speaking.
            "From: alice@bank.example <mallory@evil.example>",
        ],
    )
    def test_from_must_name_exactly_one_address(self, from_headers):
        with pytest.raises(MalformedEmail):
            parse_raw(raw_email(f"{from_headers}\nTo: agent@hsingh.app\nSubject: hi".lstrip("\n")), NOW)

    @pytest.mark.parametrize(
        "hostile_header",
        [
            "Message-ID: <a@",  # IndexError in CPython's msg-id parser
            "Reply-To: <a@",  # IndexError in the address parser
            'To: "',
            "Content-Type: text/plain; charset*",  # IndexError inside BytesParser itself
            "Content-Disposition: inline; size*",
        ],
    )
    def test_headers_that_crash_the_stdlib_structured_parsers(self, hostile_header):
        email = parse_raw(raw_email(f"From: jane@example.com\n{hostile_header}"), NOW)
        assert email.from_addr == "jane@example.com"
        assert "M12205" in email.text

    def test_missing_message_id_is_derived_from_the_bytes(self):
        raw = raw_email("From: jane@example.com\nSubject: hi")
        first, second = parse_raw(raw, NOW), parse_raw(raw, NOW)
        assert first.message_id == second.message_id
        assert first.message_id.endswith("@no-message-id.invalid>")

    def test_subject_is_capped_and_unfolded(self):
        subject = "\n ".join(["Other Documents M12205"] * 200)
        email = parse_raw(raw_email(f"From: jane@example.com\nSubject: {subject}"), NOW)
        assert len(email.subject) == MAX_SUBJECT_CHARS
        assert "\n" not in email.subject

    def test_text_is_capped(self):
        email = parse_raw(raw_email("From: jane@example.com", "M12205 " * 10_000), NOW)
        assert len(email.text) == MAX_TEXT_CHARS

    def test_control_characters_are_removed(self):
        raw = raw_email("From: jane@example.com\nSubject: a\x00b\x07c", "M12\x0012205\n")
        email = parse_raw(raw, NOW)
        assert email.subject == "abc"
        assert email.text == "M1212205"
        assert "\x00" not in email.model_dump_json()

    def test_eight_bit_utf8_headers(self):
        raw = "From: José Núñez <jose@example.es>\r\nSubject: Café\r\n\r\nM12205\r\n".encode()
        email = parse_raw(raw, NOW)
        assert (email.from_name, email.subject) == ("José Núñez", "Café")

    def test_mime_nesting_bomb_is_malformed_not_a_crash(self):
        depth = 1200  # about 60 KB, far below the inbound size limit
        nested = "".join(
            f'Content-Type: multipart/mixed; boundary="b{i}"\n\n--b{i}\n' for i in range(1, depth)
        )
        closing = "".join(f"--b{i}--\n" for i in reversed(range(depth)))
        raw = raw_email(
            'From: jane@example.com\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="b0"',
            f"--b0\n{nested}Content-Type: text/plain\n\nhi\n{closing}",
        )
        with pytest.raises(MalformedEmail):
            parse_raw(raw, NOW)


class TestBodyText:
    def test_prefers_plain_over_html(self):
        body = """--b
Content-Type: text/plain; charset=utf-8

plain M12205
--b
Content-Type: text/html; charset=utf-8

<p>html M99999</p>
--b--
"""
        email = parse_raw(
            raw_email('From: j@example.com\nContent-Type: multipart/alternative; boundary="b"', body), NOW
        )
        assert email.text == "plain M12205"

    def test_html_only_keeps_blocks_and_drops_scripts_styles_and_quotes(self):
        html = (
            "<html><head><style>.x{color:red}</style><title>t</title></head><body>"
            "<div>Hi,</div><div>Please send the <b>Other   Documents</b>\n for M12205.</div>"
            "<p>Thanks,<br>Jane &amp; co</p><ul><li>one</li><li>two</li></ul>"
            "<table><tr><td>a</td><td>b</td></tr></table>"
            "<script>steal()</script><blockquote>old M99999</blockquote></body></html>"
        )
        email = parse_raw(raw_email("From: j@example.com\nContent-Type: text/html; charset=utf-8", html), NOW)
        assert (
            email.text
            == "Hi,\nPlease send the Other Documents for M12205.\nThanks,\nJane & co\none\ntwo\na b"
        )

    def test_html_br_lines_survive_block_merging(self):
        html = '<div dir="ltr">Hi,<div><br></div><div>Other Documents for M12205?</div><div>Jane</div></div>'
        email = parse_raw(raw_email("From: j@example.com\nContent-Type: text/html", html), NOW)
        assert email.text == "Hi,\n\nOther Documents for M12205?\nJane"

    def test_inline_text_parts_are_joined_and_attachments_skipped(self):
        body = """--m
Content-Type: text/plain

first part M12205
--m
Content-Type: image/png
Content-Disposition: inline; filename="logo.png"

iVBORw0KGgo=
--m
Content-Type: text/plain

second part
--m
Content-Type: text/plain
Content-Disposition: attachment; filename="notes.txt"

attached M99999
--m
Content-Type: text/plain; charset=utf-8
Content-Disposition: inline; filename="inline.txt"

named inline M88888
--m
Content-Type: message/rfc822

From: someone@example.org
Subject: forwarded as attachment

attached message M77777
--m--
"""
        email = parse_raw(
            raw_email('From: j@example.com\nContent-Type: multipart/mixed; boundary="m"', body), NOW
        )
        assert email.text == "first part M12205\nsecond part"

    @pytest.mark.parametrize(
        ("charset", "payload", "expected"),
        [
            ("iso-8859-1", "caf\xe9 \x93quoted\x94".encode("latin-1"), "café “quoted”"),  # really cp1252
            ("us-ascii", "naïve".encode(), "naïve"),  # really UTF-8
            ("windows-1252", b"\x80 5", "€ 5"),
            ("x-unknown-charset", "M12205 ✓".encode(), "M12205 ✓"),
            ("base64", b"M12205", "M12205"),  # a codec, but not a text encoding
            ("idna", b"M12205", "M12205"),  # supports only errors="strict"
            ("utf-8", b"bad \xff byte", "bad � byte"),
        ],
    )
    def test_charsets(self, charset, payload, expected):
        raw = raw_email(f'From: j@example.com\nContent-Type: text/plain; charset="{charset}"', payload)
        assert parse_raw(raw, NOW).text == expected

    def test_base64_and_quoted_printable(self):
        b64 = raw_email(
            "From: j@example.com\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64",
            "Rm9yIE0xMjIwNSwgcsOpZ2llLg==\n",
        )
        qp = raw_email(
            "From: j@example.com\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: quoted-printable",
            "For M12205, r=C3=A9g=\nie.\n",
        )
        assert parse_raw(b64, NOW).text == "For M12205, régie."
        assert parse_raw(qp, NOW).text == "For M12205, régie."


class TestTraceHeaders:
    RAW = raw_email(
        """Return-Path: <bounce@bank.example>
Received: from mail.hsingh.app
\tby ubuntu-16gb-hel1-1 with LMTP id x
\tfor <agent@hsingh.app>; Sat, 4 Oct 2026 14:00:00 +0000
Received: from mx.bank.example (mx.bank.example [192.0.2.10])
\tby mail.hsingh.app (Postfix) with ESMTPS id ABC
\tfor <agent@hsingh.app>; Sat, 4 Oct 2026 14:00:00 +0000
Received: from forged.example (forged.example [8.8.8.8]) by mail.hsingh.app
Return-Path: <forged@evil.example>
From: jane@bank.example"""
    )

    def test_received_headers_are_topmost_first_and_unfolded(self):
        received = received_headers(parse_headers(self.RAW))
        assert len(received) == 3
        assert received[1] == (
            "from mx.bank.example (mx.bank.example [192.0.2.10]) by mail.hsingh.app (Postfix) "
            "with ESMTPS id ABC for <agent@hsingh.app>; Sat, 4 Oct 2026 14:00:00 +0000"
        )

    def test_envelope_sender_is_the_topmost_return_path(self):
        assert envelope_sender(parse_headers(self.RAW)) == "bounce@bank.example"

    @pytest.mark.parametrize(
        ("header", "expected"),
        [("Return-Path: <>", ""), ("Return-Path: <A@B.example>", "a@b.example"), ("", None)],
    )
    def test_envelope_sender_forms(self, header, expected):
        assert (
            envelope_sender(parse_headers(raw_email(f"{header}\nFrom: j@example.com".lstrip("\n"))))
            == expected
        )


class TestStripQuoted:
    def test_gmail_top_post_with_wrapped_attribution(self):
        text = (
            "Please send the Key Documents for M12205.\n\nJane\n\n"
            "On Sat, Oct 4, 2026 at 10:02 AM Regulatory Agent <\nagent@hsingh.app> wrote:\n\n"
            "> Here are the Other Documents for M99999.\n"
        )
        assert strip_quoted(text) == "Please send the Key Documents for M12205.\n\nJane"

    def test_outlook_header_block(self):
        text = (
            "Exhibits for M12205 please.\n\n________________________________\n"
            "From: Regulatory Agent <agent@hsingh.app>\nSent: Friday, October 3, 2026 4:15 PM\n"
            "To: Bob\nSubject: Re: Documents\n\nHere are the documents for M99999.\n"
        )
        assert strip_quoted(text) == "Exhibits for M12205 please."

    def test_outlook_header_block_converted_from_html(self):
        text = "Exhibits for M12205.\n\n*From:* Agent <agent@hsingh.app>\n*Date:* Friday\n*To:* Bob\n\nM99999"
        assert strip_quoted(text) == "Exhibits for M12205."

    @pytest.mark.parametrize(
        "separator",
        [
            "-----Original Message-----",
            "----- Original Message -----",
            "-----Message d'origine-----",
            "-----Ursprüngliche Nachricht-----",
            "On Oct 4, 2026, at 09:12, Regulatory Agent <agent@hsingh.app> wrote:",
            "Le sam. 4 oct. 2026 à 10:02, Regulatory Agent <agent@hsingh.app> a écrit :",
            "Am 04.10.2026 um 10:02 schrieb Regulatory Agent <agent@hsingh.app>:",
            "El sáb, 4 oct 2026 a las 10:02, Regulatory Agent (<agent@hsingh.app>) escribió:",
            "Op za 4 okt. 2026 om 10:02 schreef Regulatory Agent <agent@hsingh.app>:",
        ],
    )
    def test_reply_separators(self, separator):
        text = f"Transcripts for M12205, merci.\n\n{separator}\nOld request for M99999.\n"
        assert strip_quoted(text) == "Transcripts for M12205, merci."

    def test_quoted_lines_are_dropped_anywhere(self):
        text = "> what matter?\nM12205\n  > > nested quote\nOther Documents"
        assert strip_quoted(text) == "M12205\nOther Documents"

    def test_bottom_posted_reply_is_kept(self):
        text = "On Fri, Oct 3, 2026 at 4:15 PM, Agent wrote:\n> Here is M99999.\n>\n\nNow M12205 please.\n"
        assert strip_quoted(text) == "Now M12205 please."

    def test_forwarded_message_is_kept(self):
        text = (
            "FYI, can you handle this?\n\n---------- Forwarded message ---------\n"
            "From: Sam <sam@example.com>\nDate: Fri, Oct 3, 2026\nSubject: docs\n\n"
            "Key Documents for M12205 please.\n> old quote\n"
        )
        assert strip_quoted(text) == (
            "FYI, can you handle this?\n\n---------- Forwarded message ---------\n"
            "From: Sam <sam@example.com>\nDate: Fri, Oct 3, 2026\nSubject: docs\n\n"
            "Key Documents for M12205 please."
        )

    @pytest.mark.parametrize(
        "text",
        [
            "On second thought, please send the Exhibits for M12205 instead.",
            "Am I able to get the Key Documents for M12205?",
            "From: our records, M12205 is open.\nThanks",
        ],
    )
    def test_ordinary_sentences_are_not_separators(self, text):
        assert strip_quoted(text) == text

    def test_tidies_whitespace(self):
        assert strip_quoted("\r\n\r\nline one  \r\n\r\n\r\n\r\nline two\r\n\r\n") == "line one\n\nline two"
