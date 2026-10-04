"""Package downloaded documents into one ZIP for delivery.

Guarantees:
- every member name is a single, portable path component (no traversal, drive letters, control or
  bidi characters, reserved device names), unique case-insensitively, and short enough to extract
  on Windows, macOS and Linux;
- documents are streamed from disk in blocks and re-hashed on the way in, so the manifest's sha256
  values describe the bytes actually shipped;
- the ZIP is written to a temp file next to `dest_path` and atomically renamed: nothing is written
  outside that directory and a crash never leaves a truncated ZIP under the final name.
"""

import csv
import hashlib
import io
import os
import re
import tempfile
import unicodedata
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from agent.models import AgentError, DownloadedFile

README_NAME = "README.txt"
MANIFEST_NAME = "MANIFEST.csv"
MANIFEST_COLUMNS = ("file", "document_id", "matter", "doc_type", "title", "filed_on", "size_bytes", "sha256")

MAX_NAME_CHARS = 120
# ext4 and APFS cap a path component at 255 bytes; 120 CJK/emoji characters can exceed that.
MAX_NAME_BYTES = 240

_BLOCK = 1024 * 1024
_FILE_MODE = 0o644 << 16  # unix permission bits live in the high half of external_attr
_ZIP_EPOCH = date(1980, 1, 1)  # earliest timestamp the ZIP format can store

# Already-compressed formats (PDF streams are Flate-encoded) are STORED: deflating them again
# burns CPU for a ~1-3% gain and makes the ZIP size track the input size predictably.
_STORED_EXTS = frozenset({".pdf", ".zip", ".mp3", ".mp4", ".m4a", ".jpg", ".jpeg", ".png"})

_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*]')
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}")
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class PackageError(AgentError):
    retryable = False
    user_message = "I couldn't package the documents safely, so I didn't send a partial set."


@dataclass(frozen=True, slots=True)
class ZipResult:
    path: str
    size: int
    sha256: str
    file_count: int  # documents only; README.txt and MANIFEST.csv are not counted


def build_zip(files: Sequence[DownloadedFile], dest_path: str, *, readme_text: str) -> ZipResult:
    """Write `files` (in the given order) plus README.txt and MANIFEST.csv to `dest_path`."""
    if not files:
        raise ValueError("build_zip needs at least one file")
    dest = Path(dest_path)
    arcnames = member_names(files)
    now = datetime.now(UTC)

    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w") as zf:
            _write_bytes(zf, README_NAME, readme_text.encode("utf-8"), now)
            _write_bytes(zf, MANIFEST_NAME, _manifest_csv(files, arcnames), now)
            for f, arcname in zip(files, arcnames, strict=True):
                _write_document(zf, f, arcname, fallback_time=now)
        size, digest = _digest(tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ZipResult(path=str(dest), size=size, sha256=digest, file_count=len(files))


def member_names(files: Sequence[DownloadedFile]) -> list[str]:
    """Safe, unique member names in input order; never collide with README/MANIFEST."""
    used = {README_NAME.casefold(), MANIFEST_NAME.casefold()}
    return [_dedupe(safe_filename(f.filename, ext=f.ref.file_ext), used) for f in files]


def safe_filename(name: str, *, ext: str = "") -> str:
    """Reduce `name` to one portable path component, ending in `ext` when `ext` is well formed."""
    text = "".join(" " if ch.isspace() else ch for ch in name)
    # Category C = control, format (incl. bidi overrides that can disguise "fdp.exe" as
    # "exe.pdf"), surrogate, private-use and unassigned code points.
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = unicodedata.normalize("NFC", text)  # after stripping, which can expose new compositions
    text = _RESERVED_CHARS.sub("_", " ".join(text.split()))
    text = text.strip(" .")  # leading dots: hidden files / ".."; trailing: Windows drops them

    stem, suffix = _split_ext(text)
    wanted = ext if _EXT.fullmatch(ext) else ""
    if wanted and suffix.lower() != wanted.lower():
        stem, suffix = text, wanted
    if stem.split(".")[0].strip().upper() in _WINDOWS_DEVICES:
        stem = f"_{stem}"
    return _fit(stem, suffix)


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, tail = name.rpartition(".")
    if dot and stem and _EXT.fullmatch(f".{tail}"):
        return stem, f".{tail}"
    return name, ""


def _fit(stem: str, tail: str) -> str:
    """Trim the stem, never the tail (extension or dedupe suffix), until both limits hold."""
    stem = stem[: max(0, MAX_NAME_CHARS - len(tail))]
    while stem and len((stem + tail).encode("utf-8")) > MAX_NAME_BYTES:
        stem = stem[:-1]
    return (stem.rstrip(" .") or "document") + tail


def _dedupe(name: str, used: set[str]) -> str:
    # casefold: Windows and macOS filesystems are case-insensitive, so "A.pdf" and "a.pdf" collide.
    stem, ext = _split_ext(name)
    candidate, n = name, 1
    while candidate.casefold() in used:
        n += 1
        candidate = _fit(stem, f" ({n}){ext}")
    used.add(candidate.casefold())
    return candidate


def _manifest_csv(files: Sequence[DownloadedFile], arcnames: Sequence[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(MANIFEST_COLUMNS)
    for f, arcname in zip(files, arcnames, strict=True):
        ref = f.ref
        writer.writerow(
            [
                _csv_cell(arcname),
                _csv_cell(ref.external_id),
                _csv_cell(ref.matter),
                _csv_cell(ref.doc_type),
                _csv_cell(ref.title),
                ref.filed_on.isoformat() if ref.filed_on else "",
                f.size,
                f.sha256.lower(),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel reads UTF-8 titles correctly


def _csv_cell(value: str) -> str:
    # Titles come from the regulator's site: a leading "=" would run as a formula in Excel.
    return f"'{value}" if value.startswith(_CSV_FORMULA_PREFIXES) else value


def _zip_info(arcname: str, when: datetime | date, compress_type: int) -> zipfile.ZipInfo:
    hms = (when.hour, when.minute, when.second) if isinstance(when, datetime) else (0, 0, 0)
    info = zipfile.ZipInfo(arcname, date_time=(when.year, when.month, when.day, *hms))
    info.compress_type = compress_type
    info.external_attr = _FILE_MODE
    return info


def _write_bytes(zf: zipfile.ZipFile, arcname: str, data: bytes, when: datetime) -> None:
    zf.writestr(_zip_info(arcname, when, zipfile.ZIP_DEFLATED), data)


def _write_document(zf: zipfile.ZipFile, f: DownloadedFile, arcname: str, *, fallback_time: datetime) -> None:
    # The filed date as mtime makes extracted files sort chronologically in a file browser.
    filed = f.ref.filed_on
    when = filed if filed and filed >= _ZIP_EPOCH else fallback_time
    stored = _split_ext(arcname)[1].lower() in _STORED_EXTS
    info = _zip_info(arcname, when, zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
    info.file_size = f.size  # lets zipfile decide on ZIP64 before streaming

    digest = hashlib.sha256()
    written = 0
    with open(f.path, "rb") as src, zf.open(info, "w") as dst:
        while block := src.read(_BLOCK):
            digest.update(block)
            dst.write(block)
            written += len(block)
    if written != f.size or digest.hexdigest() != f.sha256.lower():
        raise PackageError(
            f"document {f.ref.external_id} at {f.path} does not match its recorded size/sha256"
        )


def _digest(path: Path) -> tuple[int, str]:
    with open(path, "rb") as fh:
        digest = hashlib.file_digest(fh, "sha256")
        return os.fstat(fh.fileno()).st_size, digest.hexdigest()
