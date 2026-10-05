"""Steps 2-5 end to end: summarise every cached case, run deterministic checks, judge, report.

    .venv/bin/python -m evals.output.fetch            # once (cached)
    .venv/bin/python -m evals.output.run [--regen]    # --regen re-runs the generator for every case

Generator and judge outputs are cached (cache/gen, cache/judge) keyed by inputs and by the source of
agent/citations/*.py + agent/llm.py, so after a fix pass a plain rerun regenerates what changed.
"""

import argparse
import asyncio
import logging
import os
import re
import statistics
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from agent.citations.claims import extract_figures, unsupported_figures
from agent.models import MatterInfo
from evals.output import checks, judge
from evals.output.common import CASES, CASES_DIR, HERE, ROOT, ensure_dirs, read_json, write_json
from evals.output.generate import PROVIDERS, generate
from evals.output.report import render

JUDGEABLE_DROPS = {"quote_not_found", "unsupported_figure", "too_few_claims", "over_limit", "duplicate",
                   "quote_too_long", "quote_too_short"}


async def evaluate_case(rec: dict, out: dict) -> dict:
    info = MatterInfo.model_validate(rec["info"])
    provider = PROVIDERS[rec["provider"]]
    gen = out["generation"]
    pages_by_doc = out["pages_by_doc"]
    titles = {r["external_id"]: r["title"] for r in rec["refs"]}
    source_text = "\n".join([out["metadata_text"], *("\n".join(p) for p in pages_by_doc.values())])
    context_text = "\n".join([out["metadata_text"], out["context"]["text"]])

    # ---- (a)-(d)
    rec_for_reply = {**rec, "_summary": gen["summary"]}
    det = {
        "quotes": checks.quote_checks(gen, pages_by_doc),
        "summary_figures": checks.figure_report(gen["summary"] or "", source_text, context_text),
        "claim_figures": [
            {"claim_id": c["id"], **checks.figure_report(c["claim"], c["quote"], c["quote"])} for c in gen["kept"]
        ],
        "docs": checks.doc_checks(gen, set(titles), out["context"]["pages"]),
        "removed_sentence_triggers": _removed_triggers(gen, out),
        "reply": checks.reply_checks(rec_for_reply, info, out["reply"], provider, len(rec["files"])),
    }

    # ---- judge
    async def judge_one(c: dict) -> dict:
        pages = pages_by_doc.get(c["doc_external_id"])
        if not pages or not 1 <= c["page"] <= len(pages):
            return {"skipped": "cited page unavailable"}
        return await judge.judge_claim(c["claim"], c["quote"], pages[c["page"] - 1],
                                       titles.get(c["doc_external_id"], ""), info.title)

    kept_j = [judge_one(c) for c in gen["kept"]]
    drop_j = [judge_one(d) if d["reason"] in JUDGEABLE_DROPS else _skip(d["reason"]) for d in gen["dropped"]]
    summary_j = (
        judge.judge_summary(gen["summary"], gen["removed_sentences"], out["metadata_text"], out["context"]["text"])
        if gen["status"] == "ok" and (gen["summary"] or gen["removed_sentences"]) else _skip("no summary")
    )
    results = await asyncio.gather(summary_j, *kept_j, *drop_j)
    sj, kj, dj = results[0], results[1 : 1 + len(kept_j)], results[1 + len(kept_j) :]

    return {
        "case": rec["case"], "provider": rec["provider"], "matter": rec["matter"], "category": rec["category"],
        "extra": rec.get("extra", False), "source": rec["source"],
        "info": {k: rec["info"][k] for k in ("title", "type", "category", "status", "outcome", "date_received",
                                              "decision_date", "counts")},
        "files": len(rec["files"]), "failed_titles": rec["failed_titles"], "confidential": rec["confidential"],
        "inventory": out["inventory"],
        "uncited": [i for i in out["inventory"] if i.get("uncited_reason")],
        "context": {k: out["context"][k] for k in ("selected", "pages", "chars")},
        "generation": {
            **{k: v for k, v in gen.items() if k not in ("kept", "dropped")},
            "kept": [{**c, "judge": j} for c, j in zip(gen["kept"], kj)],
            "dropped": [{**d, "judge": j} for d, j in zip(gen["dropped"], dj)],
        },
        "judge_summary": sj,
        "checks": det,
        "reply_text": out["reply"]["text"],
        "readme": out["reply"]["readme"],
    }


def _removed_triggers(gen: dict, out: dict) -> list[dict]:
    """Why the figure filter removed each sentence, and whether those figures were in what the model saw."""
    allowed = extract_figures("\n".join([out["metadata_text"], *(k["quote"] for k in gen["kept"])]))
    seen = extract_figures("\n".join([out["metadata_text"], out["context"]["text"]]))
    return [{"sentence": s, "triggers": unsupported_figures(s, allowed),
             "unsupported_vs_context": unsupported_figures(s, seen)} for s in gen["removed_sentences"]]


async def _skip(reason: str) -> dict:
    return {"skipped": reason}


# ---------------------------------------------------------------- negative controls

_AMOUNT = re.compile(r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\$\s?\d+(?:\.\d+)?\s?(?:million|billion)", re.IGNORECASE)


def _perturb_amount(raw: str) -> str:
    """A plausible but wrong amount of the same shape: $59,143,000 -> $61,846,000."""
    m = re.search(r"([\d,]+(?:\.\d+)?)", raw)
    value = Decimal(m.group(1).replace(",", ""))
    new = (value * Decimal("1.0457")).quantize(Decimal(1) if "." not in m.group(1) else Decimal("0.1"))
    if "," in m.group(1):
        new = (new // 1000) * 1000
        text = f"{int(new):,}"
    else:
        text = str(new)
    return raw.replace(m.group(1), text)


async def negative_controls(cases: list[dict], outs: dict[str, dict]) -> list[dict]:
    controls: list[dict] = []
    # NC1: a real summary with one dollar amount replaced by a plausible false one
    src = next((c for c in cases if c["generation"]["summary"] and _AMOUNT.search(c["generation"]["summary"])), None)
    if src:
        summary = src["generation"]["summary"]
        amounts = _AMOUNT.findall(summary)
        target = amounts[-1]
        fake = _perturb_amount(target)
        poisoned = summary.replace(target, fake, 1)
        out = outs[src["case"]]
        j = await judge.judge_summary(poisoned, [], out["metadata_text"], out["context"]["text"])
        digits = re.sub(r"\D", "", fake)
        v = j.get("verdict") or {}
        hit = any(digits[:4] in re.sub(r"\D", "", s["statement"] + s["problem"]) for s in v.get("unsupported_statements", []))
        controls.append({
            "name": "NC1 summary with injected false dollar amount", "case": src["case"],
            "original": target, "injected": fake, "poisoned_summary": poisoned,
            "caught": hit, "judge": j,
            "expectation": "unsupported_statements must flag the sentence carrying the injected amount",
        })
    # NC2: a real kept claim and its real quote/page, with the claim's key fact altered
    src2 = next((c for c in cases if any(_AMOUNT.search(k["claim"]) for k in c["generation"]["kept"])), None)
    if src2:
        claim = next(k for k in src2["generation"]["kept"] if _AMOUNT.search(k["claim"]))
        target = _AMOUNT.search(claim["claim"]).group(0)
        altered = claim["claim"].replace(target, _perturb_amount(target), 1)
        how = f"amount {target} -> {_perturb_amount(target)}"
    else:
        src2 = next(c for c in cases if c["generation"]["kept"])
        claim = src2["generation"]["kept"][0]
        altered = re.sub(r"\bapprov\w*", "denied", claim["claim"], count=1, flags=re.IGNORECASE)
        if altered == claim["claim"]:
            altered = "The Board denied the application in its entirety."
        how = "outcome flipped"
    pages = outs[src2["case"]]["pages_by_doc"][claim["doc_external_id"]]
    title = next(r["title"] for r in read_json(CASES_DIR / f"{src2['case']}.json")["refs"]
                 if r["external_id"] == claim["doc_external_id"])
    j = await judge.judge_claim(altered, claim["quote"], pages[claim["page"] - 1], title, src2["info"]["title"])
    verdict = (j.get("verdict") or {}).get("verdict")
    controls.append({
        "name": "NC2 claim whose quote does not support it", "case": src2["case"], "how": how,
        "original_claim": claim["claim"], "altered_claim": altered, "quote": claim["quote"],
        "caught": verdict == "NOT_SUPPORTED", "caught_loosely": verdict in {"NOT_SUPPORTED", "PARTIALLY"},
        "judge": j, "expectation": "verdict NOT_SUPPORTED",
    })
    # NC3 (extra, beyond the brief's two): a real claim paired with a real quote from another document
    pair = next(((c, a, b) for c in cases for a in c["generation"]["kept"] for b in c["generation"]["kept"]
                 if a["doc_external_id"] != b["doc_external_id"]), None)
    if pair:
        c3, a3, b3 = pair
        pages = outs[c3["case"]]["pages_by_doc"][b3["doc_external_id"]]
        title = next(r["title"] for r in read_json(CASES_DIR / f"{c3['case']}.json")["refs"]
                     if r["external_id"] == b3["doc_external_id"])
        j = await judge.judge_claim(a3["claim"], b3["quote"], pages[b3["page"] - 1], title, c3["info"]["title"])
        verdict = (j.get("verdict") or {}).get("verdict")
        controls.append({
            "name": "NC3 (extra) claim cited to another document's passage", "case": c3["case"],
            "error": j.get("error"),
            "how": f"claim from doc {a3['doc_external_id']} p{a3['page']} paired with quote from doc "
                   f"{b3['doc_external_id']} p{b3['page']}",
            "altered_claim": a3["claim"], "quote": b3["quote"],
            "caught": None if "error" in j else verdict == "NOT_SUPPORTED",
            "caught_loosely": verdict in {"NOT_SUPPORTED", "PARTIALLY"},
            "judge": j, "expectation": "verdict NOT_SUPPORTED",
        })
    return controls


# ---------------------------------------------------------------- aggregates


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def aggregate(cases: list[dict], controls: list[dict]) -> dict:
    gens = [c["generation"] for c in cases]
    ok = [c for c in cases if c["generation"]["status"] == "ok"]
    kept = [k for g in gens for k in g["kept"]]
    dropped = [d for g in gens for d in g["dropped"]]
    kv = [k["judge"]["verdict"] for k in kept if "verdict" in k["judge"]]
    dv = [(d["reason"], d["judge"]["verdict"]) for d in dropped if "verdict" in d["judge"]]
    sv = [c["judge_summary"]["verdict"] for c in ok if "verdict" in c["judge_summary"]]
    with_summary = [c for c in ok if c["generation"]["summary"]]
    sv_nonempty = [c["judge_summary"]["verdict"] for c in with_summary if "verdict" in c["judge_summary"]]
    removed_v = [r for v in sv for r in v["removed_sentences"]]

    reply_checks: dict[str, Counter] = {}
    for c in cases:
        for ch in c["checks"]["reply"]:
            reply_checks.setdefault(ch["name"], Counter())["pass" if ch["ok"] else "n/a" if ch["ok"] is None else "fail"] += 1
    q = [x for c in cases for x in c["checks"]["quotes"]]
    d = [x for c in cases for x in c["checks"]["docs"]]
    sf = [c["checks"]["summary_figures"] for c in with_summary]
    cf = [x for c in cases for x in c["checks"]["claim_figures"]]
    lat = [g["llm"].get("latency_ms") for g in gens if g["status"] == "ok"]
    cost = [g["llm"].get("cost") for g in gens if g["status"] == "ok"]
    judge_cost = sum(
        (x.get("meta") or {}).get("cost") or 0
        for c in cases for x in [c["judge_summary"], *(k["judge"] for k in c["generation"]["kept"]),
                                  *(k["judge"] for k in c["generation"]["dropped"])]
    ) + sum((n["judge"].get("meta") or {}).get("cost") or 0 for n in controls)

    def share(n, total):
        return round(n / total, 3) if total else None

    verdicts = Counter(v["verdict"] for v in kv)
    return {
        "cases": len(cases),
        "cases_with_summary": len(with_summary),
        "cases_no_summary": {c["case"]: (c["generation"]["status"] if c["generation"]["status"] != "ok"
                                         else "summary emptied by figure filter") for c in cases
                             if not c["generation"]["summary"]},
        "uncited_documents": sum(len(c["uncited"]) for c in cases),
        "documents": sum(c["files"] for c in cases),
        "generator_models": dict(Counter(g["llm"].get("model") for g in gens if g["status"] == "ok")),
        "claims_proposed": len(kept) + len(dropped),
        "claims_kept": len(kept),
        "claims_dropped": len(dropped),
        "drop_reasons": dict(Counter(x["reason"] for x in dropped)),
        "removed_summary_sentences": sum(len(g["removed_sentences"]) for g in gens),
        "det_quote_integrity_pass": share(sum(x["ok"] for x in q), len(q)),
        "det_fuzzy_quotes": sum(x["fuzzy"] for x in q),
        "det_page_corrected": sum(x["page_corrected_from"] is not None for x in q),
        "det_doc_membership_pass": share(sum(x["ok"] for x in d), len(d)),
        "det_claim_page_in_context": share(sum(x["page_in_context"] for x in d), len(d)),
        "det_summary_figures_pass": share(sum(x["ok"] for x in sf), len(sf)),
        "det_summary_figures_total": sum(len(x["figures"]) for x in sf),
        "det_summary_figures_unsupported": [u for x in sf for u in x["unsupported"]],
        "det_claim_figures_pass": share(sum(x["ok"] for x in cf), len(cf)),
        "det_reply_checks": {k: dict(v) for k, v in reply_checks.items()},
        "judge_claims_judged": len(kv),
        "judge_citation_precision": share(verdicts["SUPPORTED"], len(kv)),
        "judge_verdicts": dict(verdicts),
        "judge_quote_alone_sufficient": share(sum(v["quote_alone_sufficient"] for v in kv), len(kv)),
        "judge_decision_relevance_mean": _mean([v["decision_relevance"] for v in kv]),
        "judge_decision_relevance_dist": dict(Counter(v["decision_relevance"] for v in kv)),
        "judge_dropped_claims_judged": len(dv),
        "judge_dropped_supported_by_reason": {
            r: f"{sum(v['verdict'] == 'SUPPORTED' for rr, v in dv if rr == r)}/{sum(1 for rr, _ in dv if rr == r)}"
            for r in sorted({r for r, _ in dv})
        },
        "judge_faithfulness_violations": sum(len(v["unsupported_statements"]) for v in sv_nonempty),
        "judge_summaries_with_violation": sum(bool(v["unsupported_statements"]) for v in sv_nonempty),
        "judge_misattributions": sum(v["misattribution"] for v in sv_nonempty),
        "judge_coverage_mean": _mean([v["coverage"] for v in sv_nonempty]),
        "judge_clarity_mean": _mean([v["clarity"] for v in sv_nonempty]),
        "judge_removed_sentences_supported": f"{sum(r['supported'] for r in removed_v)}/{len(removed_v)}",
        "removed_sentences_figures_in_context": f"{sum(not t['unsupported_vs_context'] for c in cases for t in c['checks']['removed_sentence_triggers'])}"
        f"/{sum(len(c['checks']['removed_sentence_triggers']) for c in cases)}",
        "latency_ms_mean": _mean(lat), "latency_ms_max": max((x for x in lat if x), default=None),
        "cost_per_summary_mean": round(statistics.mean([x for x in cost if x is not None]), 4) if any(cost) else None,
        "cost_total_generator": round(sum(x or 0 for x in cost), 4),
        "cost_total_judge": round(judge_cost, 4),
        "negative_controls": {n["name"]: n["caught"] for n in controls},
    }


async def main(regen: bool, skip_summary_judge: bool = False) -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    ensure_dirs()
    if skip_summary_judge:  # the summary judge is ~60% of the judge's cost (whole context per case)
        async def no_summary_judge(*_args, **_kwargs) -> dict:
            return await _skip("summary judge skipped (--skip-summary-judge)")

        judge.judge_summary = no_summary_judge
    recs = [read_json(CASES_DIR / f"{c.id}.json") for c in CASES if (CASES_DIR / f"{c.id}.json").exists()]
    outs: dict[str, dict] = {}
    for rec in recs:  # sequential: latency numbers are per call, not under contention
        outs[rec["case"]] = await generate(rec["case"], rec, regen=regen)
        g = outs[rec["case"]]["generation"]
        print(f"[gen] {rec['case']}: {g['status']} kept={len(g['kept'])} dropped={len(g['dropped'])} "
              f"model={g['llm'].get('model')} {g['llm'].get('latency_ms')}ms", flush=True)
    cases = await asyncio.gather(*(evaluate_case(rec, outs[rec["case"]]) for rec in recs))
    print("[judge] cases done", flush=True)
    controls = await negative_controls(list(cases), outs)
    agg = aggregate(list(cases), controls)
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "generator_models_configured": __import__("agent.config", fromlist=["x"]).get_settings().llm_models,
        "judge_models": judge.JUDGE_MODELS,
        "aggregate": agg,
        "negative_controls": controls,
        "cases": cases,
    }
    write_json(HERE / "results.json", results)
    (HERE / "report.md").write_text(render(results))
    print(f"wrote {HERE / 'report.md'} and results.json")
    for k, v in agg.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--regen", action="store_true")
    parser.add_argument("--skip-summary-judge", action="store_true", help="judge claims only (budget)")
    args = parser.parse_args()
    os.chdir(ROOT)
    asyncio.run(main(args.regen, args.skip_summary_judge))
