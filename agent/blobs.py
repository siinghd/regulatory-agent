"""Content-addressed file store on local disk: {data_dir}/blobs/{sha[:2]}/{sha}.

Identical PDFs filed in several matters are stored once. Writes are atomic (rename), so a
reader never sees a partial file.
"""

import hashlib
import os
from pathlib import Path

from agent.config import get_settings


def _root(kind: str) -> Path:
    return Path(get_settings().data_dir) / kind


def blob_path(sha256: str) -> Path:
    if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
        raise ValueError("not a sha256 hex digest")
    return _root("blobs") / sha256[:2] / sha256


def has_blob(sha256: str) -> bool:
    return blob_path(sha256).is_file()


def put_file(src: str, sha256: str) -> Path:
    """Move a verified file into the store (or drop it if we already have that content)."""
    dest = blob_path(sha256)
    if dest.is_file():
        os.remove(src)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest


def put_raw(raw: bytes) -> str:
    """Store a raw inbound MIME message; returns its sha256."""
    sha = hashlib.sha256(raw).hexdigest()
    dest = _root("raw") / sha[:2] / sha
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
    return sha


def read_raw(sha256: str) -> bytes:
    return (_root("raw") / sha256[:2] / sha256).read_bytes()
