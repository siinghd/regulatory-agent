"""Step 1: materialise each case (MatterInfo, refs, downloaded files) under cache/cases/.

Source order per case:
1. cache/cases/<id>.json already there -> nothing to do;
2. the production DB already holds the listing and every blob (read-only SELECTs; blobs are
   hardlinked into cache/files/) -> same refs the pipeline's cache path would build;
3. otherwise the real provider: UARB through the browser over the SOCKS proxy (sequential),
   OEB/FERC over httpx with at most 3 concurrent requests.

    .venv/bin/python -m evals.output.fetch [case_id ...]
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, date, datetime

from agent.config import get_settings
from agent.models import DocumentRef, MatterInfo
from agent.pipeline import _ref_from_row
from evals.output.common import (
    CACHE,
    CASES,
    CASES_DIR,
    MAX_DOCS,
    ROOT,
    Case,
    cached_file,
    ensure_dirs,
    write_json,
)

POLITE_CONCURRENCY = 3


# ---------------------------------------------------------------- production DB (read-only)


def _psql_json(sql: str):
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "agent", "-At", "-c", sql],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return json.loads(out) if out else None


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def from_prod(case: Case) -> dict | None:
    row = _psql_json(
        "SELECT json_build_object('info', info, 'listings', listings) FROM matters "
        f"WHERE provider = {_sql_str(case.provider)} AND matter = {_sql_str(case.matter)}"
    )
    if not row:
        return None
    info = MatterInfo.model_validate(row["info"])
    listing = (row["listings"] or {}).get(case.category)
    if listing is None or len(listing) < min(MAX_DOCS, info.counts.get(case.category, 0)):
        return None  # the pipeline would re-list from the portal too
    confidential = int((row["listings"] or {}).get(f"{case.category}#confidential") or 0)
    ids = ",".join(_sql_str(x) for x in listing[:MAX_DOCS]) or "''"
    docs = _psql_json(
        "SELECT coalesce(json_agg(d), '[]') FROM (SELECT provider, matter, doc_type, external_id, title, "
        f"filed_on, sha256, size_bytes, filename FROM documents WHERE provider = {_sql_str(case.provider)} "
        f"AND external_id IN ({ids})) d"
    ) or []
    known = {d["external_id"]: d for d in docs}
    refs: list[DocumentRef] = []
    files: list[dict] = []
    for i, x in enumerate(listing[:MAX_DOCS]):
        d = known.get(x)
        if d is None:
            return None
        d = {**d, "filed_on": date.fromisoformat(d["filed_on"]) if d["filed_on"] else None}
        blob = ROOT / "data" / "blobs" / (d["sha256"] or "xx")[:2] / (d["sha256"] or "")
        if not d["sha256"] or not blob.is_file():
            return None
        ref = _ref_from_row(d, i)
        refs.append(ref)
        ext = os.path.splitext(d["filename"] or ".pdf")[1].lower()
        dest = cached_file(d["sha256"], ext)
        if not dest.exists():
            try:
                os.link(blob, dest)
            except OSError:
                shutil.copyfile(blob, dest)
        files.append({
            "external_id": ref.external_id, "sha256": d["sha256"], "size": d["size_bytes"],
            "filename": d["filename"] or f"{ref.external_id}{ext}", "path": str(dest.relative_to(ROOT)),
        })
    return _case_record(case, info, refs, files, [], confidential, "prod_db")


# ---------------------------------------------------------------- live providers


async def from_portal(case: Case, provider) -> dict:
    t0 = time.perf_counter()
    info, listed = await provider.list_matter_and_documents(case.matter, case.category, MAX_DOCS)
    refs = [r for r in listed if r.access == "Public"][:MAX_DOCS]
    confidential = sum(1 for r in listed if r.access != "Public")
    files: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="eval-dl-", dir=CACHE) as tmp:
        if refs:
            async for f in provider.download(case.matter, refs, tmp):
                ext = os.path.splitext(f.path)[1].lower()
                dest = cached_file(f.sha256, ext)
                if dest.exists():
                    os.remove(f.path)
                else:
                    os.replace(f.path, dest)
                files[f.ref.external_id] = {
                    "external_id": f.ref.external_id, "sha256": f.sha256, "size": f.size,
                    "filename": f.filename, "path": str(dest.relative_to(ROOT)),
                    "ref": json.loads(f.ref.model_dump_json()),  # FERC rewrites file_ext on download
                }
    ordered = [files[r.external_id] for r in refs if r.external_id in files]
    failed = [r.title for r in refs if r.external_id not in files]
    rec = _case_record(case, info, refs, ordered, failed, confidential, "live_portal")
    rec["fetch_seconds"] = round(time.perf_counter() - t0, 1)
    return rec


def _case_record(case, info, refs, files, failed, confidential, source) -> dict:
    return {
        "case": case.id, "provider": case.provider, "matter": case.matter, "category": case.category,
        "extra": case.extra, "source": source, "fetched_at": datetime.now(UTC).isoformat(),
        "info": json.loads(info.model_dump_json()),
        "refs": [json.loads(r.model_dump_json()) for r in refs],
        "files": files, "failed_titles": failed, "confidential": confidential,
    }


def _make_provider(name: str, stack: list):
    s = get_settings()
    if name == "uarb":
        from agent.providers.browser import BrowserPool
        from agent.providers.uarb import UarbProvider

        pool = BrowserPool(proxy=s.uarb_proxy, max_sessions=1, nav_timeout_ms=s.browser_nav_timeout_ms)
        stack.append(pool.close)
        return UarbProvider(pool, sessions_per_matter=1)  # sequential: one session at a time
    if name == "oeb":
        from agent.providers.oeb import OebProvider, make_client

        client = make_client(s.oeb_proxy)
        stack.append(client.aclose)
        return OebProvider(client, max_concurrency=POLITE_CONCURRENCY)
    if name == "ferc":
        from agent.providers.ferc import FercProvider
        from agent.providers.ferc import make_client as make_ferc_client

        client = make_ferc_client(s.ferc_proxy)
        stack.append(client.aclose)
        return FercProvider(client, max_concurrency=POLITE_CONCURRENCY)
    raise ValueError(name)


async def main(only: list[str]) -> None:
    ensure_dirs()
    providers: dict[str, object] = {}
    closers: list = []
    try:
        for case in CASES:  # sequential across cases: one portal visit at a time
            if only and case.id not in only:
                continue
            path = CASES_DIR / f"{case.id}.json"
            if path.exists():
                print(f"[cached] {case.id}")
                continue
            rec = from_prod(case)
            if rec is None:
                if case.provider not in providers:
                    providers[case.provider] = _make_provider(case.provider, closers)
                print(f"[portal] {case.id} ...", flush=True)
                rec = await from_portal(case, providers[case.provider])
            write_json(path, rec)
            exts = sorted({os.path.splitext(f["filename"])[1].lower() for f in rec["files"]})
            print(f"[{rec['source']}] {case.id}: {len(rec['files'])} files {exts}, "
                  f"failed={len(rec['failed_titles'])}, confidential={rec['confidential']}")
    finally:
        for close in closers:
            await close()


if __name__ == "__main__":
    os.chdir(ROOT)
    asyncio.run(main(sys.argv[1:]))
