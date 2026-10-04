"""Turn a freshly downloaded temp file into a verified, content-addressed DownloadedFile."""

import hashlib
import os

from agent.models import DocumentRef, DownloadedFile, PortalUnavailable, ScrapeError

_MAGIC = {".pdf": b"%PDF-"}


def finalise_download(tmp: str, ref: DocumentRef, suggested: str, dest_dir: str) -> DownloadedFile:
    """Check `tmp` is a real file of the type it claims, then move it to `<sha256><ext>`.

    `suggested` is the filename the portal served; its extension wins over the listing's.
    """
    size = os.path.getsize(tmp)
    if size == 0:
        os.remove(tmp)
        raise PortalUnavailable(f"empty download for {ref.external_id}")
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        head = f.read(8)
        h.update(head)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    ext = os.path.splitext(suggested)[1].lower() or ref.file_ext
    magic = _MAGIC.get(ext)
    if magic and not head.startswith(magic):
        os.remove(tmp)
        raise ScrapeError(f"{ref.external_id}: expected {ext} but got {head!r}")
    sha = h.hexdigest()
    final = os.path.join(dest_dir, f"{sha}{ext}")
    os.replace(tmp, final)
    return DownloadedFile(
        ref=ref, path=final, sha256=sha, size=size, filename=f"{ref.external_id}{ext}"
    )
