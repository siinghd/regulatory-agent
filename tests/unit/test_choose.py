from datetime import UTC, date, datetime

import pytest

from agent.delivery.choose import Delivery, DeliveryDeferred, deliver
from agent.delivery.drop import DropClient, DropLink, DropRejected, DropUnavailable
from agent.delivery.package import ZipResult
from agent.models import DocumentRef, DownloadedFile

LINK = DropLink(
    url="https://drop.example/d/abc123DEF_-9#key",
    id="abc123DEF_-9",
    delete_token="token",
    expires_at=datetime(2026, 10, 11, tzinfo=UTC),
    size=4_000_000,
    max_downloads=25,
)


class StubDrop(DropClient):
    def __init__(self, outcome: DropLink | Exception):
        super().__init__(
            upload_url="http://drop.internal",
            public_url="https://drop.example",
            expiry_s=3600,
            max_downloads=25,
        )
        self.outcome = outcome
        self.uploads: list[tuple[str, str]] = []

    async def upload(self, path: str, display_name: str) -> DropLink:
        self.uploads.append((path, display_name))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _files(n: int = 2) -> list[DownloadedFile]:
    ref = DocumentRef(
        provider="uarb",
        matter="M12205",
        doc_type="Other Documents",
        external_id="1",
        title="t",
        filed_on=date(2024, 5, 1),
    )
    return [
        DownloadedFile(ref=ref, path=f"/blobs/{i}", sha256="0" * 64, size=1, filename=f"{i}.pdf")
        for i in range(n)
    ]


def _zip(size: int) -> ZipResult:
    return ZipResult(path="/tmp/req-1/package.zip", size=size, sha256="a" * 64, file_count=2)


async def test_prefers_drop_link() -> None:
    drop = StubDrop(LINK)
    delivery = await deliver(_zip(4_000_000), _files(), drop=drop, attach_max_bytes=10_000_000)

    assert delivery.kind == "link" and delivery.link == LINK and delivery.path is None
    assert delivery.filename == "M12205 Other Documents.zip"
    assert drop.uploads == [("/tmp/req-1/package.zip", "M12205 Other Documents.zip")]


@pytest.mark.parametrize("error", [DropUnavailable("busy", status=503), DropRejected("bad", status=400)])
async def test_small_zip_falls_back_to_attachment(error: Exception) -> None:
    delivery = await deliver(_zip(10_000_000), _files(), drop=StubDrop(error), attach_max_bytes=10_000_000)

    assert delivery.kind == "attachment" and delivery.path == "/tmp/req-1/package.zip"
    assert delivery.link is None and type(error).__name__ in (delivery.fallback_reason or "")


async def test_no_drop_configured_attaches() -> None:
    delivery = await deliver(_zip(1_000), _files(), drop=None, attach_max_bytes=10_000_000)
    assert delivery.kind == "attachment"


async def test_large_zip_with_drop_down_is_deferred() -> None:
    with pytest.raises(DeliveryDeferred) as exc_info:
        await deliver(
            _zip(10_000_001), _files(), drop=StubDrop(DropUnavailable("down")), attach_max_bytes=10_000_000
        )
    assert exc_info.value.retryable


async def test_large_zip_rejected_by_drop_is_not_retried() -> None:
    with pytest.raises(DropRejected):
        await deliver(
            _zip(20_000_000), _files(), drop=StubDrop(DropRejected("no")), attach_max_bytes=10_000_000
        )


async def test_mismatched_file_list_is_a_bug() -> None:
    with pytest.raises(ValueError, match="2 documents"):
        await deliver(_zip(1_000), _files(3), drop=None, attach_max_bytes=10_000_000)


def test_delivery_invariants() -> None:
    with pytest.raises(ValueError):
        Delivery(kind="link", filename="p.zip", size=1, sha256="a", file_count=1)
    with pytest.raises(ValueError):
        Delivery(kind="attachment", filename="p.zip", size=1, sha256="a", file_count=1, link=LINK, path="/x")
