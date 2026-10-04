import csv
import hashlib
import io
import unicodedata
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from agent.delivery.package import (
    MANIFEST_COLUMNS,
    MAX_NAME_BYTES,
    MAX_NAME_CHARS,
    PackageError,
    build_zip,
    member_names,
    safe_filename,
)
from agent.models import DocType, DocumentRef, DownloadedFile

HOSTILE_NAMES = [
    "../../etc/passwd",
    "C:\\x",
    "/abs/olute.pdf",
    "..\\..\\windows\\system32\\evil.pdf",
    "a\x00b\nc\r\td.pdf",
    "\u202efdp.exe",  # right-to-left override: renders as "exe.pdf"
    "x" * 300 + ".pdf",
    "\u6587" * 300,  # 3 UTF-8 bytes per character
    "CON.pdf",
    "...",
    "",
    " trailing dots... ",
    'what<>:"|?*.pdf',
]


def _doc(
    blob_dir: Path, filename: str, content: bytes, *, external_id: str = "102674", **ref: object
) -> DownloadedFile:
    path = blob_dir / hashlib.sha256(content + external_id.encode()).hexdigest()
    path.write_bytes(content)
    fields: dict[str, object] = {
        "provider": "uarb",
        "matter": "M12205",
        "doc_type": DocType.OTHER_DOCUMENTS,
        "external_id": external_id,
        "title": filename,
        "filed_on": date(2024, 5, 1),
    } | ref
    return DownloadedFile(
        ref=DocumentRef(**fields),  # type: ignore[arg-type]
        path=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        filename=filename,
    )


@pytest.fixture
def blob_dir(tmp_path: Path) -> Path:
    d = tmp_path / "blobs"
    d.mkdir()
    return d


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


def _assert_portable(name: str) -> None:
    assert len(PurePosixPath(name).parts) == 1 and not PurePosixPath(name).is_absolute()
    assert len(PureWindowsPath(name).parts) == 1 and not PureWindowsPath(name).drive
    assert name not in ("", ".", "..") and not name.startswith(".")
    assert not any(unicodedata.category(ch)[0] == "C" for ch in name)
    assert not set('<>:"/\\|?*') & set(name)
    assert unicodedata.is_normalized("NFC", name)
    assert len(name) <= MAX_NAME_CHARS and len(name.encode()) <= MAX_NAME_BYTES


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_names_become_one_portable_component(name: str) -> None:
    safe = safe_filename(name, ext=".pdf")
    _assert_portable(safe)
    assert safe.endswith(".pdf")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd", "_.._etc_passwd.pdf"),
        ("C:\\x", "C__x.pdf"),
        ("a\x00b\nc.pdf", "ab c.pdf"),
        ("\u202efdp.exe", "fdp.exe.pdf"),
        ("CON.pdf", "_CON.pdf"),
        ("", "document.pdf"),
        ("Cafe\u0301 Report.PDF", "Caf\u00e9 Report.PDF"),
        ("Decision.final", "Decision.final.pdf"),
    ],
)
def test_sanitised_names(name: str, expected: str) -> None:
    assert safe_filename(name, ext=".pdf") == expected


def test_long_names_are_truncated_keeping_the_extension() -> None:
    assert safe_filename("x" * 300 + ".pdf", ext=".pdf") == "x" * 116 + ".pdf"
    cjk = safe_filename("\u6587" * 300, ext=".pdf")
    assert cjk.endswith(".pdf") and len(cjk.encode()) <= MAX_NAME_BYTES


def test_malformed_ext_is_ignored() -> None:
    assert safe_filename("report", ext="./../x") == "report"


def test_duplicates_get_numbered_case_insensitively(blob_dir: Path) -> None:
    long = "y" * 300 + ".pdf"
    files = [
        _doc(blob_dir, "Report.pdf", b"1", external_id="1"),
        _doc(blob_dir, "report.pdf", b"2", external_id="2"),
        _doc(blob_dir, "REPORT.pdf", b"3", external_id="3"),
        _doc(blob_dir, long, b"4", external_id="4"),
        _doc(blob_dir, long, b"5", external_id="5"),
        _doc(blob_dir, "readme.txt", b"6", external_id="6", file_ext=".txt"),
    ]
    names = member_names(files)
    assert names[:3] == ["Report.pdf", "report (2).pdf", "REPORT (3).pdf"]
    assert names[3] == "y" * 116 + ".pdf"
    assert names[4] == "y" * 112 + " (2).pdf" and len(names[4]) == MAX_NAME_CHARS
    assert names[5] == "readme (2).txt"  # README.txt is reserved for the package's own readme
    assert len({n.casefold() for n in names}) == len(names)


def test_zip_layout_integrity_and_result(blob_dir: Path, out_dir: Path) -> None:
    contents = [b"%PDF-1.7 first", b"%PDF-1.7 second" * 1000, b"plain text"]
    files = [
        _doc(blob_dir, "B second.pdf", contents[0], external_id="2"),
        _doc(blob_dir, "A first.pdf", contents[1], external_id="1"),
        _doc(blob_dir, "notes", contents[2], external_id="3", file_ext=".txt", filed_on=None),
    ]
    dest = out_dir / "package.zip"
    result = build_zip(files, str(dest), readme_text="Hello \u2014 M12205\n")

    assert result.path == str(dest) and result.file_count == 3
    raw = dest.read_bytes()
    assert result.size == len(raw) and result.sha256 == hashlib.sha256(raw).hexdigest()
    assert sorted(p.name for p in out_dir.iterdir()) == ["package.zip"]  # no temp files left behind

    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["README.txt", "MANIFEST.csv", "B second.pdf", "A first.pdf", "notes.txt"]
        assert zf.read("README.txt").decode() == "Hello \u2014 M12205\n"
        for name, data in zip(zf.namelist()[2:], contents, strict=True):
            assert zf.read(name) == data
        assert zf.getinfo("A first.pdf").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("notes.txt").compress_type == zipfile.ZIP_DEFLATED
        assert zf.getinfo("A first.pdf").date_time == (2024, 5, 1, 0, 0, 0)


def test_manifest_lists_every_document(blob_dir: Path, out_dir: Path) -> None:
    files = [
        _doc(blob_dir, "../evil.pdf", b"one", external_id="102674", title="Evidence of NSPI"),
        _doc(blob_dir, "two.pdf", b"two!", external_id="102675", title='=HYPERLINK("http://x")'),
    ]
    dest = out_dir / "p.zip"
    build_zip(files, str(dest), readme_text="r")

    with zipfile.ZipFile(dest) as zf:
        manifest = zf.read("MANIFEST.csv")
    assert manifest.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(manifest.decode("utf-8-sig"))))
    assert tuple(rows[0]) == MANIFEST_COLUMNS
    assert rows[1] == [
        "_evil.pdf",
        "102674",
        "M12205",
        "Other Documents",
        "Evidence of NSPI",
        "2024-05-01",
        "3",
        hashlib.sha256(b"one").hexdigest(),
    ]
    assert rows[2][4] == '\'=HYPERLINK("http://x")'  # CSV formula injection neutralised
    assert rows[2][6:] == ["4", hashlib.sha256(b"two!").hexdigest()]


def test_hostile_names_extract_inside_target(blob_dir: Path, out_dir: Path, tmp_path: Path) -> None:
    files = [
        _doc(blob_dir, name, f"doc {i}".encode(), external_id=str(i)) for i, name in enumerate(HOSTILE_NAMES)
    ]
    dest = out_dir / "p.zip"
    build_zip(files, str(dest), readme_text="r")

    target = tmp_path / "extract"
    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            _assert_portable(name)
        zf.extractall(target)
    extracted = sorted(p for p in target.rglob("*") if p.is_file())
    assert len(extracted) == len(HOSTILE_NAMES) + 2
    assert all(p.parent == target for p in extracted)


def test_sha_mismatch_aborts_without_leaving_files(blob_dir: Path, out_dir: Path) -> None:
    good = _doc(blob_dir, "good.pdf", b"good", external_id="1")
    bad = _doc(blob_dir, "bad.pdf", b"bad", external_id="2")
    Path(bad.path).write_bytes(b"tampered")
    dest = out_dir / "p.zip"

    with pytest.raises(PackageError, match="sha256"):
        build_zip([good, bad], str(dest), readme_text="r")
    assert list(out_dir.iterdir()) == []


def test_refuses_empty_package(out_dir: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_zip([], str(out_dir / "p.zip"), readme_text="r")
