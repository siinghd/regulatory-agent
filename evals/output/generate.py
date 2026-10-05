"""Step 2: run the real summariser on a cached case, exactly as pipeline._summarise does, and render
the reply with outbound.documents_reply (fake links). Results are cached by input + code version."""

import hashlib
import os
import time
from datetime import UTC, datetime, timedelta

from agent.citations import claims as claims_mod
from agent.citations.claims import build_context, select_documents, summarize_with_citations
from agent.citations.extract import extract_pages, is_docx, needs_ocr
from agent.config import get_settings
from agent.llm import LLMUnavailable
from agent.mail import outbound
from agent.models import DocumentRef, DownloadedFile, MatterInfo
from agent.pipeline import _order, _readme
from agent.providers.ferc import FercProvider
from agent.providers.oeb import OebProvider
from agent.providers.uarb import UarbProvider
from evals.output.common import GEN, PAGES, ROOT, read_json, stable_key, write_json

# documents_reply/matter_sentence only read class attributes (categories, display_name, portal_url).
PROVIDERS = {"uarb": UarbProvider, "oeb": OebProvider, "ferc": FercProvider}
FAKE_BASE = "https://eval.invalid"
_CODE = ("agent/citations/claims.py", "agent/citations/ground.py", "agent/citations/extract.py", "agent/llm.py")


def code_version(paths=_CODE) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update((ROOT / p).read_bytes())
    return h.hexdigest()[:12]


def load_case(rec: dict) -> tuple[MatterInfo, list[DownloadedFile]]:
    info = MatterInfo.model_validate(rec["info"])
    refs = {r["external_id"]: DocumentRef.model_validate(r) for r in rec["refs"]}
    files = [
        DownloadedFile(
            ref=DocumentRef.model_validate(f["ref"]) if "ref" in f else refs[f["external_id"]],
            path=str(ROOT / f["path"]), sha256=f["sha256"], size=f["size"], filename=f["filename"],
        )
        for f in rec["files"]
    ]
    return info, files


def pages_for(f: DownloadedFile) -> list[str]:
    """agent.citations.extract, cached per (content hash, extract.py version)."""
    path = PAGES / f"{f.sha256}_{code_version(('agent/citations/extract.py',))}.json"
    if path.exists():
        return read_json(path)
    pages = extract_pages(f.path)
    write_json(path, pages)
    return pages


def doc_inventory(files: list[DownloadedFile]) -> tuple[list, list[dict]]:
    """(docs as the pipeline builds them, per-file inventory incl. why a file is uncited)."""
    docs, inventory = [], []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        item = {"external_id": f.ref.external_id, "title": f.ref.title, "filed_on": str(f.ref.filed_on or ""),
                "filename": f.filename, "ext": ext, "size": f.size}
        # pipeline._summarise reads PDFs and (with the DOCX extractor wired in) Word files
        if not (f.filename.lower().endswith(".pdf") or is_docx(f.path)):
            item["uncited_reason"] = f"not a PDF or DOCX ({ext or 'no extension'})"
        else:
            pages = pages_for(f)
            item.update(pages=len(pages), chars=sum(map(len, pages)), needs_ocr=needs_ocr(pages))
            if not pages:
                item["uncited_reason"] = "unreadable/encrypted (no pages)"
            else:
                docs.append((f.ref, pages))
                if needs_ocr(pages):
                    item["uncited_reason"] = "scanned PDF (no text layer; skipped by select_documents)"
        inventory.append(item)
    return docs, inventory


async def generate(case_id: str, rec: dict, *, regen: bool = False) -> dict:
    info, files = load_case(rec)
    provider = PROVIDERS[rec["provider"]]
    docs, inventory = doc_inventory(files)
    selected = select_documents(docs)
    context = build_context(selected)
    metadata = claims_mod._metadata_text(info, [ref for ref, _ in docs])
    for item in inventory:
        item["in_context"] = item["external_id"] in context.pages
        item["context_pages"] = context.pages.get(item["external_id"], [])

    key = stable_key(code_version(), get_settings().llm_models, [f.sha256 for f in files], rec["info"])
    path = GEN / f"{case_id}__{key}.json"
    if path.exists() and not regen:
        gen = read_json(path)
    else:
        gen = await _run_summary(info, docs, provider)
        write_json(path, gen)

    reply = render_reply(rec, info, files, provider, gen)
    return {
        "inventory": inventory,
        "context": {"text": context.text, "pages": context.pages, "chars": len(context.text),
                    "selected": [ref.external_id for ref, _ in selected]},
        "metadata_text": metadata,
        "pages_by_doc": {ref.external_id: list(pages) for ref, pages in docs},
        "generation": gen,
        "reply": reply,
    }


async def _run_summary(info: MatterInfo, docs: list, provider) -> dict:
    if not docs:
        return {"status": "no_pdf_docs", "summary": None, "kept": [], "dropped": [], "removed_sentences": [],
                "llm": {}, "wall_ms": 0}
    t0 = time.perf_counter()
    try:
        result = await summarize_with_citations(info, docs, max_claims=5, regulator=provider.display_name)
    except LLMUnavailable as e:
        return {"status": "llm_unavailable", "error": str(e)[:1000], "summary": None, "kept": [], "dropped": [],
                "removed_sentences": [], "llm": {}, "wall_ms": round((time.perf_counter() - t0) * 1000)}
    return {
        "status": "ok",
        "summary": result.summary,
        "kept": [c.model_dump() for c in result.claims],
        "dropped": [
            {"reason": d.reason.value, "detail": d.detail, **d.draft.model_dump()} for d in result.dropped
        ],
        "removed_sentences": list(result.removed_sentences),
        "context_docs": list(result.context_docs),
        "unreadable_docs": list(result.unreadable_docs),
        "unread_docs": list(result.unread_docs),
        "llm": result.llm,
        "support_check": result.support_check,
        "wall_ms": round((time.perf_counter() - t0) * 1000),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def render_reply(rec: dict, info: MatterInfo, files: list[DownloadedFile], provider, gen: dict) -> dict:
    """outbound.documents_reply as pipeline._fulfil calls it, with a fake drop link and viewer URLs."""
    claims = [outbound.ClaimLine(text=c["claim"], url=f"{FAKE_BASE}/c/{c['id']}") for c in gen["kept"]]
    draft = outbound.documents_reply(
        name="Eval Tester", subject=f"{rec['matter']} {rec['category']}", info=info, provider=provider,
        doc_type=rec["category"],
        docs=[outbound.DocLine(
            title=f.ref.title, filed=f.ref.filed_on.isoformat() if f.ref.filed_on else "undated",
            url=f"{FAKE_BASE}/files/{f.ref.external_id}.pdf" if f.filename.lower().endswith(".pdf") else None,
        ) for f in files],
        requested=10, summary=gen["summary"] or None, claims=claims,
        download_url=f"{FAKE_BASE}/drop/fake#key", download_expires=datetime.now(UTC) + timedelta(days=7),
        download_size=sum(f.size for f in files),  # ~ZIP size: PDFs barely compress
        attachment_path=None, track_url=f"{FAKE_BASE}/r/fake",
        failed_titles=rec["failed_titles"], order=_order([f.ref for f in files]), confidential=rec["confidential"],
    )
    return {
        "text": draft.text,
        "matter_sentence": outbound.matter_sentence(info, provider),
        "newest_first_flag": _order([f.ref for f in files]) == "newest",
        "readme": _readme(info, provider, rec["category"], files),
        "download_size": sum(f.size for f in files),
    }
