"""Retry policy, queue format, sealing at rest, delivery records and reply wording (no network, no DB)."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from agent import crypto, db, queue, worker
from agent.config import Settings, get_settings
from agent.delivery.choose import Delivery
from agent.delivery.drop import DropClient, DropLink, DropUnavailable
from agent.mail import outbound
from agent.models import DocumentRef, DownloadedFile, MatterInfo, PortalUnavailable
from agent.pipeline import (
    Deps,
    Fetched,
    _failure_kind,
    _failure_message,
    _nothing_to_send,
    _order,
    _readme,
    _together,
)
from agent.providers.browser import BrowserPool
from agent.providers.uarb import UarbProvider
from agent.web import progress

UARB = UarbProvider(BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000))  # never launched


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def info(**counts: int) -> MatterInfo:
    return MatterInfo(provider="uarb", matter="M12205", title="Nova Scotia Power Inc.", counts=counts,
                      portal_url="https://uarb.example", fetched_at=datetime.now(UTC))


# ---------------------------------------------------------------- retry policy


def test_backoff_is_jittered_exponential_and_capped(settings: Settings):
    for attempt in range(1, 12):
        step = min(settings.retry_cap_s, settings.retry_base_s * 2 ** (attempt - 1))
        values = {worker.backoff_s(attempt, settings=settings) for _ in range(50)}
        assert all(0.5 * step <= v <= step for v in values)
        assert len(values) > 1  # jittered: requests that failed together don't retry together


def test_backoff_never_undercuts_a_dependencys_retry_after(settings: Settings):
    assert worker.backoff_s(1, 600, settings=settings) == 600
    assert worker.backoff_s(10, 1, settings=settings) >= 0.5 * settings.retry_cap_s


def test_worker_settings_leave_retry_decisions_to_postgres(settings: Settings):
    ws = worker.WorkerSettings
    assert ws.max_tries == worker.ARQ_MAX_TRIES >= 100
    assert ws.job_timeout == settings.job_timeout_s > settings.pipeline_timeout_s
    names = {getattr(f, "name", getattr(f, "__name__", None)) for f in ws.functions}
    assert names == {"process_request", "send_outbound"}
    assert ws.job_serializer is queue.serialize and ws.job_deserializer is queue.deserialize


def test_jobs_are_json_never_pickle():
    job = {"t": 1, "f": "process_request", "a": ["2b5c..."], "k": {}, "et": 1}
    data = queue.serialize(job)
    assert json.loads(data) == job and queue.deserialize(data) == job
    # a failed job's result is an exception: it must serialize, not break the queue
    assert "PortalUnavailable" in queue.serialize({"r": PortalUnavailable("down")}).decode()


# ---------------------------------------------------------------- database roles


def test_migrations_use_the_owner_role_when_configured(monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://owner:pw@127.0.0.1:5442/agent")
    get_settings.cache_clear()
    assert db.migration_dsn() == "postgresql://owner:pw@127.0.0.1:5442/agent"
    monkeypatch.delenv("MIGRATION_DATABASE_URL")
    get_settings.cache_clear()
    assert db.migration_dsn() == get_settings().database_url


def test_only_ragent_migrate_migrates(monkeypatch):
    import inspect

    from agent import cli

    calls = []

    async def run_migrations():
        calls.append("migrate")

    monkeypatch.setattr(db, "run_migrations", run_migrations)
    asyncio.run(cli._async("migrate"))
    assert calls == ["migrate"]
    assert "migrate(" not in inspect.getsource(worker.startup)
    assert "migrate" not in inspect.getsource(cli._async).split('if command == "migrate"')[1].split("return")[1]


# ---------------------------------------------------------------- sealing at rest


def test_sealed_values_round_trip_and_resist_tampering(settings: Settings):
    sealed = crypto.seal(b"https://drop.example/d/abc#key", aad=b"abc")
    assert b"#key" not in sealed and crypto.unseal(sealed, aad=b"abc") == b"https://drop.example/d/abc#key"
    with pytest.raises(crypto.SealError):
        crypto.unseal(sealed, aad=b"another-row")
    with pytest.raises(crypto.SealError):
        crypto.unseal(sealed[:-1] + bytes([sealed[-1] ^ 1]), aad=b"abc")


def test_without_a_configured_secret_the_key_lives_in_a_private_file(settings: Settings, tmp_path):
    crypto.seal(b"x")
    key = tmp_path / "keys" / "at-rest.key"
    assert key.stat().st_size == 32 and key.stat().st_mode & 0o077 == 0


def _link(**kw) -> DropLink:
    fields = {"url": "https://drop.example/d/abcdef123456#SECRETKEY", "id": "abcdef123456", "delete_token": "TOKEN",
              "expires_at": datetime.now(UTC) + timedelta(days=7), "size": 1000, "max_downloads": 25} | kw
    return DropLink(**fields)


def test_delivery_record_hides_the_link_and_delete_token():
    d = Delivery(kind="link", filename="M12205 Exhibits.zip", size=1000, sha256="a" * 64, file_count=2, link=_link())
    record = d.to_record("files-v1")
    flat = json.dumps(record)
    assert "SECRETKEY" not in flat and "TOKEN" not in flat and record["drop_id"] == "abcdef123456"
    again = Delivery.from_record(json.loads(flat), "files-v1")
    assert again is not None and again.link.url == d.link.url and again.link.delete_token == "TOKEN"


def test_delivery_record_is_only_reused_for_the_same_files_and_a_live_link():
    d = Delivery(kind="link", filename="p.zip", size=1, sha256="a" * 64, file_count=1, link=_link())
    assert Delivery.from_record(d.to_record("v1"), "v2") is None  # different documents packaged
    soon = Delivery(kind="link", filename="p.zip", size=1, sha256="a" * 64, file_count=1,
                    link=_link(expires_at=datetime.now(UTC) + timedelta(hours=1)))
    assert Delivery.from_record(soon.to_record("v1"), "v1") is None  # expires too soon to hand out
    assert Delivery.from_record(None, "v1") is None


# ---------------------------------------------------------------- drop Retry-After


async def test_drop_retry_after_beyond_the_upload_budget_goes_back_to_the_queue(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "900"}, text="slow down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DropClient(upload_url="http://drop.test", public_url="https://drop.test", expiry_s=3600,
                        max_downloads=5, http=http)
    f = tmp_path / "a.zip"
    f.write_bytes(b"x" * 10)
    with pytest.raises(DropUnavailable) as exc:
        await client.upload(str(f), "a.zip")
    await http.aclose()
    assert exc.value.retry_after == 900 and exc.value.dependency == "drop"


# ---------------------------------------------------------------- wording


def _files(n: int, *, size: int = 150_000) -> list[DownloadedFile]:
    refs = [DocumentRef(provider="uarb", matter="M12205", doc_type="Exhibits", external_id=f"H-{i}",
                        title=f"H-{i}", filed_on=date(2025, 4, i)) for i in range(1, n + 1)]
    return [DownloadedFile(ref=r, path="/x", sha256="0" * 64, size=size, filename=f"{r.external_id}.pdf") for r in refs]


def test_small_packages_are_sized_in_kb_and_titles_keep_one_full_stop():
    i = info(Exhibits=3)
    draft = outbound.documents_reply(
        name="Ana", subject="Request", info=i, provider=UARB, doc_type="Exhibits",
        docs=[outbound.DocLine(title="H-1", filed="2025-04-01", url=None)], requested=10, summary=None, claims=(),
        download_url=None, download_expires=None, download_size=12_345, attachment_path="/tmp/x.zip",
        track_url="https://x/r/t", order="oldest",
    )
    assert "(12 KB)" in draft.text and "0.0 MB" not in draft.text
    assert "is about Nova Scotia Power Inc.." not in draft.text and "is about Nova Scotia Power Inc. " in draft.text
    # oldest-first listing: never "the most recent"
    assert "most recent" not in draft.text and "the first of the 3 Exhibits, in the order the portal lists them" in draft.text


@pytest.mark.parametrize(("got", "total", "conf", "order", "complete", "expected"), [
    (1, 1, 0, "portal", True, "I downloaded the only Exhibit"),
    (3, 3, 0, "newest", True, "I downloaded all 3 Exhibits"),
    (1, 2, 1, "portal", True, "I downloaded the only public Exhibit (1 more is confidential)"),
    (2, 5, 3, "portal", True, "I downloaded all 2 public Exhibits (3 more are confidential)"),
    (10, 43, 0, "newest", True, "I downloaded the 10 most recent of the 43 Exhibits"),
    (1, 43, 0, "newest", True, "I downloaded the most recent of the 43 Exhibits"),
    (9, 43, 0, "newest", False, "I downloaded 9 of the 43 Exhibits"),  # one failed: not "the 9 most recent"
    (2, 43, 0, "oldest", True, "I downloaded the first 2 of the 43 Exhibits, in the order the portal lists them"),
])
def test_fetched_sentence(got, total, conf, order, complete, expected):
    assert outbound.fetched_sentence(got=got, total=total, doc_type="Exhibits", confidential=conf,
                                     order=order, complete=complete) == expected


def test_listing_order_is_only_claimed_when_every_date_says_so():
    refs = [f.ref for f in _files(3)]
    assert _order(refs) == "oldest" and _order(refs[::-1]) == "newest"
    assert _order(refs[:1]) == "portal"  # one document shows no order
    undated = [refs[0].model_copy(update={"filed_on": None}), *refs[1:]]
    assert _order(undated) == "portal"
    assert _order([refs[1], refs[0], refs[2]]) == "portal"


def test_zip_readme_orders_by_the_files_in_the_zip():
    files = _files(3)  # dated oldest first
    i = info(Exhibits=43)
    assert "(3 of 43 documents, oldest first)" in _readme(i, UARB, "Exhibits", files)
    assert "(3 of 43 documents, newest first)" in _readme(i, UARB, "Exhibits", files[::-1])
    # the listing was newest first, but the one in the middle failed to download: still newest first
    assert "(2 of 43 documents, newest first)" in _readme(i, UARB, "Exhibits", [files[2], files[0]])
    mixed = [files[1], files[0], files[2]]
    assert "(3 of 43 documents, in the order the portal lists them)" in _readme(i, UARB, "Exhibits", mixed)
    assert "(1 of 43 documents)" in _readme(i, UARB, "Exhibits", files[:1])  # one file has no order


@pytest.mark.parametrize(("filename", "content", "kind"), [
    ("Exhibit.xlsx", b"PK\x03\x04 workbook", "spreadsheet"),
    ("Exhibit.csv", b"a,b\n1,2\n", "spreadsheet"),
    ("Hearing.mp3", b"ID3", "recording"),
    ("Order.doc", b"\xd0\xcf\x11\xe0", "old_word"),
    ("Order.docx", b"not a zip at all", "damaged"),  # named Word, isn't one
    ("Filing.txt", b"plain text", "other"),
])
async def test_files_the_summary_cannot_read_say_why(tmp_path, filename, content, kind):
    from agent.pipeline import _document_text

    path = tmp_path / filename
    path.write_bytes(content)
    f = DownloadedFile(ref=_files(1)[0].ref, path=str(path), sha256="0" * 64, size=len(content), filename=filename)
    assert await _document_text(f) == ([], kind)


def _deps() -> Deps:
    class _Limits:
        r = None

    return Deps(settings=get_settings(), providers={"uarb": UARB}, limits=_Limits(), drop=None, breakers=object())


@pytest.mark.parametrize(("error", "kind", "blames_regulator"), [
    (PortalUnavailable("timeout"), "regulator", True),
    (DropUnavailable("drop down"), "drop", False),
    (RuntimeError("bug"), "internal", False),
    (OSError(28, "No space left on device"), "internal", False),
])
def test_failure_messages_blame_only_who_failed(error, kind, blames_regulator):
    assert _failure_kind(_deps(), error) == kind
    message = _failure_message(error, kind)
    assert ("regulator" in message) is blames_regulator
    if kind == "internal":
        assert "unexpected problem" in message


def test_all_confidential_tab_is_explained():
    def fetched(conf: int, total: int) -> Fetched:
        return Fetched(info=info(Exhibits=total), refs=[], files=[], failed=[], skipped=[], confidential=conf,
                       complete_listing=True)

    assert _nothing_to_send("M12205", "Exhibits", fetched(2, 2)) == (
        "All 2 Exhibits for M12205 are marked confidential, so I can't send them.")
    assert _nothing_to_send("M12205", "Exhibits", fetched(1, 1)) == (
        "The only Exhibit for M12205 is marked confidential, so I can't send it.")
    assert _nothing_to_send("M12205", "Exhibits", fetched(0, 0)) == "M12205 has no Exhibits."


def test_progress_page_never_promises_an_email_that_wasnt_sent():
    assert progress._outcome({"state": "failed", "reply_sent_at": None}, {"failure": "internal"}) == (
        "Something went wrong on our side. Please try again later.")
    assert "regulator" not in progress._outcome({"state": "failed", "reply_sent_at": 1}, {"failure": "drop"})
    assert progress._outcome({"state": "done"}, {"files": 2, "citations": 1}) == "Sent 2 documents with 1 cited key point."


# ---------------------------------------------------------------- concurrency helper


async def test_a_failed_task_cancels_its_sibling_and_is_raised_as_itself():
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def boom():
        raise DropUnavailable("drop down")

    with pytest.raises(DropUnavailable):
        await _together(slow(), boom())
    assert cancelled.is_set()
