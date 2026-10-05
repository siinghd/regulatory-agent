"""Run the gate eval: classify every case in dataset.jsonl through the real gate and LLM.

Run from the repo root (Settings reads .env relative to the working directory):

    .venv/bin/python -m evals.gate.run            # -> evals/gate/raw.jsonl
    .venv/bin/python -m evals.gate.score          # -> evals/gate/report.md, results.json

Nothing under agent/ is modified: the providers are registered the way the worker does it (without
touching a portal), and agent.llm is wrapped in-process to record what each LLM call returned and
what every OpenRouter attempt (including failed fallbacks) cost.
"""

import argparse
import asyncio
import contextvars
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import structlog

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # Settings reads .env from the working directory

from agent import llm
from agent.config import get_settings
from agent.gate import rules
from agent.gate.classify import classify
from agent.providers import base as providers_base
from agent.providers import ferc, oeb
from agent.providers.browser import BrowserPool
from agent.providers.ferc import FercProvider
from agent.providers.oeb import OebProvider
from agent.providers.uarb import UarbProvider

DATASET = HERE / "dataset.jsonl"
RAW = HERE / "raw.jsonl"
MAX_DOCS = 10  # Settings.max_docs_per_request, what the pipeline passes to classify()

# Per-run recorder: each classify() call runs in its own task, so a context variable keeps the
# LLM calls of concurrent cases apart.
_record: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("gate_eval_record", default=None)


def load_cases(path: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def register_providers(*, http: bool = True) -> list[Any]:
    """The three regulators as worker.startup registers them; constructing them touches no portal.
    Returns the HTTP clients to close (none with http=False: scoring only needs the vocabularies)."""
    uarb = UarbProvider(BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000))
    if not http:
        registered = {"uarb": uarb, "oeb": OebProvider(None), "ferc": FercProvider(None)}  # type: ignore[arg-type]
        for name, provider in registered.items():
            providers_base.register(name, lambda p=provider: p)
        return []
    oeb_client, ferc_client = oeb.make_client(), ferc.make_client()
    registered = {"uarb": uarb, "oeb": OebProvider(oeb_client), "ferc": FercProvider(ferc_client)}
    for name, provider in registered.items():
        providers_base.register(name, lambda p=provider: p)
    return [oeb_client, ferc_client]


class _RecordingClient:
    """Wraps llm's shared httpx client: one entry per OpenRouter attempt, failed fallbacks included."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def post(self, url: str, *, json: dict | None = None, **kw: Any) -> Any:
        attempt: dict[str, Any] = {"model_requested": (json or {}).get("model")}
        t0 = time.perf_counter()
        try:
            response = await self._inner.post(url, json=json, **kw)
        except Exception as e:
            attempt.update(error=f"{type(e).__name__}: {e}"[:300], latency_ms=_ms(t0))
            _attempts().append(attempt)
            raise
        attempt.update(status=response.status_code, latency_ms=_ms(t0))
        try:
            body = response.json()
            usage = body.get("usage") or {}
            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            attempt.update(
                model_served=body.get("model"),
                cost=usage.get("cost"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                finish_reason=choice.get("finish_reason"),
                content_head=content[:300],
            )
        except Exception as e:  # noqa: BLE001  (a non-JSON error page)
            attempt.update(error=f"unparseable response: {type(e).__name__}", body_head=response.text[:200])
        _attempts().append(attempt)
        return response


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def _attempts() -> list[dict[str, Any]]:
    rec = _record.get()
    return rec.setdefault("attempts", []) if rec is not None else []


def install_llm_recorder() -> None:
    real_structured = llm.structured
    real_http = llm._http
    wrapped: dict[str, _RecordingClient] = {}

    async def structured(**kw: Any):
        rec = _record.get()
        try:
            out, meta = await real_structured(**kw)
        except llm.LLMUnavailable as e:
            if rec is not None:
                rec["llm_error"] = str(e)[:800]
            raise
        if rec is not None:
            rec["llm_raw"] = out.model_dump(mode="json")
            rec["llm_meta"] = meta
        return out, meta

    def http() -> _RecordingClient:
        if "client" not in wrapped:
            wrapped["client"] = _RecordingClient(real_http())
        return wrapped["client"]

    llm.structured = structured  # classify looks it up as llm.structured at call time
    llm._http = http  # structured() looks it up as _http() at call time


async def run_one(case: dict[str, Any], run_idx: int, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        rec: dict[str, Any] = {"attempts": []}
        _record.set(rec)
        t0 = time.perf_counter()
        parsed, error = None, None
        try:
            parsed = await classify(case["subject"], case["body"], max_docs=MAX_DOCS)
        except Exception as e:  # noqa: BLE001  (a crash in the gate is a result too)
            error = f"{type(e).__name__}: {e}"[:500]
        latency_ms = _ms(t0)
    return {
        "id": case["id"],
        "run": run_idx,
        "parsed": parsed.model_dump(mode="json") if parsed else None,
        "source": parsed.source if parsed else "exception",
        "error": error,
        "latency_ms": latency_ms,
        "llm_raw": rec.get("llm_raw"),
        "llm_meta": rec.get("llm_meta"),
        "llm_error": rec.get("llm_error"),
        "attempts": rec["attempts"],
        "cost": round(sum(a.get("cost") or 0 for a in rec["attempts"]), 8),
    }


def rule_snapshot(case: dict[str, Any]) -> dict[str, Any]:
    r = rules.parse(case["subject"], case["body"], max_docs=MAX_DOCS)
    return {"reason": r.reason, "matters": list(r.matters), "doc_types": list(r.doc_types),
            "fast_path": r.parsed is not None}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeats", type=int, default=2, help="runs per LLM-path case (rules path runs once)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--only", nargs="*", help="case ids to run")
    ap.add_argument("--out", type=Path, default=RAW)
    args = ap.parse_args()
    if args.concurrency > 6:
        ap.error("concurrency is capped at 6")

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    s = get_settings()
    if not s.openrouter_api_key.get_secret_value():
        sys.exit("OPENROUTER_API_KEY missing from .env")
    clients = register_providers()
    install_llm_recorder()

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    snapshots = {c["id"]: rule_snapshot(c) for c in cases}
    jobs = [
        (c, i)
        for c in cases
        for i in range(1 if snapshots[c["id"]]["fast_path"] else args.repeats)
    ]
    sem = asyncio.Semaphore(args.concurrency)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    t0 = time.perf_counter()
    done = 0

    async def tracked(c: dict[str, Any], i: int) -> dict[str, Any]:
        nonlocal done
        result = await run_one(c, i, sem)
        done += 1
        if done % 25 == 0 or done == len(jobs):
            print(f"  {done}/{len(jobs)} runs ({time.perf_counter() - t0:.0f}s)", flush=True)
        return result

    print(f"{len(cases)} cases, {len(jobs)} runs, models {s.llm_models}", flush=True)
    results = await asyncio.gather(*(tracked(c, i) for c, i in jobs))
    for r in results:
        r["rule"] = snapshots[r["id"]]
    results.sort(key=lambda r: ([c["id"] for c in cases].index(r["id"]), r["run"]))

    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    meta = {
        "started_at": started_at,
        "wall_s": round(time.perf_counter() - t0, 1),
        "llm_models": s.llm_models,
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "cases": len(cases),
        "runs": len(results),
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {args.out} ({meta['wall_s']}s)")
    for client in clients:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
