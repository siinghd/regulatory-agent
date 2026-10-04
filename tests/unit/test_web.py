import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.config import Settings, get_settings
from agent.models import DocType, MatterInfo
from agent.web import app as web

ORDER_PDF = Path(__file__).resolve().parents[1] / "fixtures" / "uarb_102674.pdf"
DOC_ID = uuid.UUID("0b6f3c1e-8d2a-4f7b-9c41-5e2d7a9b1c3f")
CITATION_ID = "AbCdEfGhIjKl"
XSS = '<script>alert("x")</script>'


def _citation(**overrides: object) -> web.CitationView:
    fields: dict[str, object] = {
        "id": CITATION_ID,
        "claim": "The Board approved a total project cost of $59,143,000.",
        "quote": "for a total project cost of $59,143,000, inclusive of net HST",
        "page": 2,
        "document_id": DOC_ID,
        "provider": "uarb",
        "matter": "M12205",
        "doc_type": "Other Documents",
        "external_id": "102674",
        "doc_title": "Board Order",
        "filed_on": date(2026, 7, 8),
        "page_count": 2,
        "matter_title": "Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project",
        "portal_url": "https://uarb.novascotia.ca/fmi/webd/UARB15",
    }
    return web.CitationView.model_validate(fields | overrides)


MATTER = MatterInfo(
    provider="uarb",
    matter="M12205",
    title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
    status="Open",
    type="Capital Expenditure Approvals",
    category="Water",
    date_received=date(2025, 4, 7),
    decision_date=date(2025, 10, 23),
    counts={DocType.EXHIBITS: 13, DocType.KEY_DOCUMENTS: 6, DocType.OTHER_DOCUMENTS: 43},
    portal_url="https://uarb.novascotia.ca/fmi/webd/UARB15",
    fetched_at=datetime(2026, 10, 4, tzinfo=UTC),
)


class FakeDb:
    """Stands in for the query functions; tests edit these dicts."""

    def __init__(self) -> None:
        self.citations: dict[str, web.CitationView] = {CITATION_ID: _citation()}
        self.files: dict[uuid.UUID, web.StoredFile] = {}
        self.healthy = True

    async def load_citation(self, citation_id: str) -> web.CitationView | None:
        return self.citations.get(citation_id)

    async def load_stored_file(self, document_id: uuid.UUID) -> web.StoredFile | None:
        return self.files.get(document_id)

    async def db_ok(self) -> bool:
        return self.healthy


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    fake = FakeDb()
    for name in ("load_citation", "load_stored_file", "db_ok"):
        monkeypatch.setattr(web, name, getattr(fake, name))
    return fake


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    web.app.dependency_overrides[get_settings] = lambda: Settings(data_dir=str(tmp_path))
    yield tmp_path
    web.app.dependency_overrides.clear()


@pytest.fixture
def client(fake_db: FakeDb, data_dir: Path) -> TestClient:
    return TestClient(web.app)  # not entered as a context manager: no lifespan, no real pool


def _store_blob(data_dir: Path, content: bytes) -> str:
    sha = hashlib.sha256(content).hexdigest()
    path = data_dir / "blobs" / sha[:2] / sha
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return sha


# ---------------------------------------------------------------- /c/{id}


def test_citation_page_renders_claim_quote_and_viewer(client: TestClient):
    r = client.get(f"/c/{CITATION_ID}")
    assert r.status_code == 200
    assert "for a total project cost of $59,143,000, inclusive of net HST" in r.text
    assert f'data-pdf-url="/files/{DOC_ID}.pdf"' in r.text
    assert 'data-page="2"' in r.text
    assert 'href="https://uarb.novascotia.ca/fmi/webd/UARB15"' in r.text
    assert 'integrity="sha384-' in r.text


def test_html_responses_carry_security_headers(client: TestClient):
    r = client.get(f"/c/{CITATION_ID}")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self' https://cdnjs.cloudflare.com;" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_untrusted_text_is_escaped_everywhere(client: TestClient, fake_db: FakeDb):
    fake_db.citations[CITATION_ID] = _citation(
        claim=XSS, quote=f'quote " onmouseover="alert(1) {XSS}', doc_title=XSS, matter_title=XSS
    )
    r = client.get(f"/c/{CITATION_ID}")
    assert r.status_code == 200
    assert "<script>alert" not in r.text
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in r.text
    assert 'data-quote="quote &#34; onmouseover=&#34;alert(1)' in r.text  # attribute can't be broken out of


def test_non_https_portal_url_is_not_linked(client: TestClient, fake_db: FakeDb):
    fake_db.citations[CITATION_ID] = _citation(portal_url="javascript:alert(1)")
    r = client.get(f"/c/{CITATION_ID}")
    assert r.status_code == 200
    assert "javascript:" not in r.text


@pytest.mark.parametrize("citation_id", ["ZZZZZZZZZZZZ", "short", "has.dot.inside", "a" * 40, "%3Cscript%3E"])
def test_unknown_or_malformed_citation_is_a_plain_404(client: TestClient, citation_id: str):
    r = client.get(f"/c/{citation_id}")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "content-security-policy" in r.headers
    assert citation_id not in r.text and "detail" not in r.text


def test_head_is_allowed_for_link_scanners(client: TestClient):
    assert client.head(f"/c/{CITATION_ID}").status_code == 200


# ---------------------------------------------------------------- /files/{id}.pdf


def test_pdf_is_streamed_inline_with_safe_headers(client: TestClient, fake_db: FakeDb, data_dir: Path):
    content = ORDER_PDF.read_bytes()
    sha = _store_blob(data_dir, content)
    fake_db.files[DOC_ID] = web.StoredFile(external_id="102674", sha256=sha, filename='Board "Order".pdf')
    r = client.get(f"/files/{DOC_ID}.pdf")
    assert r.status_code == 200
    assert r.content == content
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline;")
    assert '"Order"' not in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in r.headers["cache-control"]


def test_pdf_supports_range_requests(client: TestClient, fake_db: FakeDb, data_dir: Path):
    sha = _store_blob(data_dir, ORDER_PDF.read_bytes())
    fake_db.files[DOC_ID] = web.StoredFile(external_id="102674", sha256=sha)
    r = client.get(f"/files/{DOC_ID}.pdf", headers={"Range": "bytes=0-4"})
    assert r.status_code == 206
    assert r.content == b"%PDF-"


@pytest.mark.parametrize(
    "path",
    [
        "/files/../../etc/passwd",
        "/files/..%2F..%2Fetc%2Fpasswd.pdf",
        "/files/%2e%2e%2f%2e%2e%2fetc%2fpasswd.pdf",
        "/files/not-a-uuid.pdf",
        f"/files/{DOC_ID}.pdf/../../../etc/passwd",
        f"/files/{uuid.uuid4()}.pdf",  # well-formed but not in the documents table
    ],
)
def test_path_traversal_and_unknown_documents_are_404(client: TestClient, path: str):
    r = client.get(path)
    assert r.status_code in (404, 422)
    assert "root:" not in r.text


def test_stored_hash_is_never_trusted_as_a_path(client: TestClient, fake_db: FakeDb):
    fake_db.files[DOC_ID] = web.StoredFile(external_id="102674", sha256="../../../../etc/passwd")
    assert client.get(f"/files/{DOC_ID}.pdf").status_code == 404


def test_non_pdf_blob_is_not_served_as_pdf(client: TestClient, fake_db: FakeDb, data_dir: Path):
    sha = _store_blob(data_dir, b"PK\x03\x04 an xlsx exhibit")
    fake_db.files[DOC_ID] = web.StoredFile(external_id="102674", sha256=sha)
    assert client.get(f"/files/{DOC_ID}.pdf").status_code == 404


def test_missing_blob_is_404(client: TestClient, fake_db: FakeDb):
    fake_db.files[DOC_ID] = web.StoredFile(external_id="102674", sha256="ab" * 32)
    assert client.get(f"/files/{DOC_ID}.pdf").status_code == 404


# ---------------------------------------------------------------- /health


@pytest.mark.parametrize("path", ["/m/uarb/M12205", "/m/uarb/M00000"])
def test_matter_pages_are_not_exposed(client: TestClient, path: str):
    # Removed on purpose: a public page per matter would reveal which matters people asked about.
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("healthy", [True, False])
def test_health_reports_database(client: TestClient, fake_db: FakeDb, healthy: bool):
    fake_db.healthy = healthy
    assert client.get("/health").json() == {"ok": True, "db": healthy}


def test_api_docs_are_not_exposed(client: TestClient):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
