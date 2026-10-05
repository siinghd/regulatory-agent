"""Citation support check: Jev (agent/citations/jev_check.py) vs the LLM check (ground.verify_support),
both measured against the judge's verdicts in evals/output/results.json.

    env -u OPENROUTER_API_KEY .venv/bin/python -m evals.output.run_jev_check run      # both checkers (cached)
    .venv/bin/python -m evals.output.run_jev_check report                              # no API calls

Items
- real: every kept claim the judge graded (SUPPORTED / PARTIALLY / NOT_SUPPORTED), every dropped claim
  the judge graded whose quote is on its page (the figure filter dropped them before any support check;
  they are still claims a model wrote), and the two negative controls NC2/NC3 (judge: NOT_SUPPORTED);
- synthetic (labelled NOT_SUPPORTED by construction, reported separately): each SUPPORTED claim paired with
  a quote and context from another matter's document ("swap"), and each SUPPORTED claim whose verb of
  decision is flipped ("approves" -> "rejects", "will" -> "will not": "polarity").
Positive class = should be dropped (PARTIALLY or NOT_SUPPORTED). The LLM check runs as production does:
one call per summary (per case and item kind); Jev runs one request per claim, concurrently.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import statistics
import time
from collections import Counter, defaultdict
from typing import Any

import structlog

from agent import typesafe
from agent.citations import jev_check
from agent.citations.ground import GroundedClaim, verify_support
from agent.config import get_settings
from agent.typesafe import ChoiceAnswer, NoulAnswer, Result
from evals.output.common import CASES_DIR, HERE, PAGES, ROOT, read_json

RESULTS = HERE / "results.json"
RAW = HERE / "raw_jev_check.json"
CACHE = HERE / "jev_check_cache.jsonl"
REPORT = HERE / "report_jev_check.md"
OUT = HERE / "results_jev_check.json"
SHOULD_DROP = {"PARTIALLY", "NOT_SUPPORTED"}

_FLIPS = [
    (r"\bapproves\b", "rejects"), (r"\bapproved\b", "rejected"), (r"\bapprove\b", "reject"),
    (r"\bgrants\b", "denies"), (r"\bgranted\b", "denied"), (r"\bgrant\b", "deny"),
    (r"\baccepts\b", "rejects"), (r"\baccepted\b", "rejected"), (r"\bwill\b", "will not"),
    (r"\bmust\b", "need not"), (r"\brequires\b", "does not require"), (r"\brequired\b", "not required"),
]


# ---------------------------------------------------------------- items


def _pages_for(rec: dict, claims: list[dict]) -> dict[str, list[str]]:
    """Each document's page texts, from the extraction whose offsets the claims were grounded against."""
    out: dict[str, list[str]] = {}
    for f in rec["files"]:
        versions = [read_json(p) for p in sorted(PAGES.glob(f"{f['sha256']}_*.json"))]
        if not versions:
            continue
        mine = [c for c in claims if c["doc_external_id"] == f["external_id"]]

        def fits(pages: list[str], mine: list[dict] = mine) -> int:
            return sum(1 <= c["page"] <= len(pages) and c["quote"] in pages[c["page"] - 1] for c in mine)

        out[f["external_id"]] = max(versions, key=fits)
    return out


def _grounded(c: dict, pages: dict[str, list[str]]) -> GroundedClaim | None:
    doc = pages.get(c["doc_external_id"])
    if not doc or not 1 <= c["page"] <= len(doc):
        return None
    text = doc[c["page"] - 1]
    start, end = c.get("char_start"), c.get("char_end")
    if start is None or text[start:end] != c["quote"]:
        start = text.find(c["quote"])
        if start < 0:
            return None
        end = start + len(c["quote"])
    return GroundedClaim(claim=c["claim"], doc_external_id=c["doc_external_id"], page=c["page"], quote=c["quote"],
                         char_start=start, char_end=end, score=100.0)


def build_items() -> tuple[list[dict], dict[str, dict[str, list[str]]]]:
    res = read_json(RESULTS)
    items: list[dict] = []
    pages_by_case: dict[str, dict[str, list[str]]] = {}
    for case in res["cases"]:
        rec = read_json(CASES_DIR / f"{case['case']}.json")
        gen = case["generation"]
        pages = _pages_for(rec, gen["kept"] + gen["dropped"])
        pages_by_case[case["case"]] = pages
        for kind, claims in (("kept", gen["kept"]), ("dropped", gen["dropped"])):
            for c in claims:
                label = ((c.get("judge") or {}).get("verdict") or {}).get("verdict")
                g = _grounded(c, pages) if label else None
                if g is None:
                    continue
                items.append({"case": case["case"], "kind": f"real_{kind}", "label": label,
                              "drop_reason": c.get("reason"), "claim": g.model_dump()})
    # NC2 / NC3: claims the judge graded NOT_SUPPORTED
    by_case = {c["case"]: c for c in res["cases"]}
    for nc in res["negative_controls"]:
        if not nc["name"].startswith(("NC2", "NC3")) or nc.get("error"):
            continue
        label = ((nc.get("judge") or {}).get("verdict") or {}).get("verdict")
        case = by_case[nc["case"]]
        pages = pages_by_case[nc["case"]]
        hit = next(((d, p) for d, ps in pages.items() for p, t in enumerate(ps, 1) if nc["quote"] in t), None)
        if hit is None:  # NC3's quote comes from another case's document
            hit = next(((d, p, cs) for cs, pg in pages_by_case.items() for d, ps in pg.items()
                        for p, t in enumerate(ps, 1) if nc["quote"] in t), None)
            if hit is None:
                continue
            pages_by_case[nc["case"]] = {**pages, hit[0]: pages_by_case[hit[2]][hit[0]]}
            pages = pages_by_case[nc["case"]]
        g = _grounded({"claim": nc["altered_claim"], "doc_external_id": hit[0], "page": hit[1],
                       "quote": nc["quote"]}, pages)
        if g is not None:
            items.append({"case": case["case"], "kind": "negative_control", "label": label,
                          "name": nc["name"], "claim": g.model_dump()})
    items += _synthetic(items, pages_by_case)
    for i, it in enumerate(items):
        it["n"] = i
    return items, pages_by_case


def _synthetic(items: list[dict], pages_by_case: dict[str, dict[str, list[str]]]) -> list[dict]:
    supported = [it for it in items if it["kind"] == "real_kept" and it["label"] == "SUPPORTED"]
    out = []
    cases = sorted({it["case"] for it in supported})
    for i, it in enumerate(supported):
        c = it["claim"]
        # swap: the same claim with a quote (and its context) from another matter's document
        other_case = next(cs for cs in cases[cases.index(it["case"]) + 1:] + cases if
                          cs.split("_")[1] != it["case"].split("_")[1])
        donors = [d for d in supported if d["case"] == other_case]
        donor = donors[i % len(donors)]["claim"]
        pages = {**pages_by_case[it["case"]], donor["doc_external_id"]: pages_by_case[other_case][donor["doc_external_id"]]}
        pages_by_case[it["case"]] = pages
        out.append({"case": it["case"], "kind": "synthetic_swap", "label": "NOT_SUPPORTED",
                    "claim": {**donor, "claim": c["claim"], "id": c["id"] + "-swap"}})
        # polarity: flip the claim's first verb of decision
        for pattern, repl in _FLIPS:
            if re.search(pattern, c["claim"]):
                flipped = re.sub(pattern, repl, c["claim"], count=1)
                out.append({"case": it["case"], "kind": "synthetic_polarity", "label": "NOT_SUPPORTED",
                            "flip": f"{pattern} -> {repl}", "claim": {**c, "claim": flipped, "id": c["id"] + "-flip"}})
                break
    return out


# ---------------------------------------------------------------- run


def _key(state: dict) -> str:
    payload = {"state": state, "questions": {q: v.payload() for q, v in jev_check.QUESTIONS.items()},
               "model": get_settings().typesafe_model}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def _dump(result: Result) -> dict:
    out = {}
    for q, a in result.answers.items():
        out[q] = ({"type": "noul", "noul": a.noul} if isinstance(a, NoulAnswer) else
                  {"type": "choice", "choice": a.choice, "probabilities": dict(a.probabilities),
                   "confidence": a.confidence})
    return out


def _load(d: dict) -> Result:
    return Result(answers={q: NoulAnswer(a["noul"]) if a["type"] == "noul" else
                           ChoiceAnswer(a["choice"], a["probabilities"], a["confidence"]) for q, a in d.items()})


async def run(budget: int, concurrency: int) -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    items, pages_by_case = build_items()
    cache = {e["key"]: e for e in map(json.loads, CACHE.read_text().splitlines())} if CACHE.exists() else {}
    sem = asyncio.Semaphore(concurrency)
    used = 0

    async def jev_one(it: dict) -> None:
        nonlocal used
        claim = GroundedClaim.model_validate(it["claim"])
        state = jev_check.state_for(claim, pages_by_case[it["case"]])
        key = _key(state)
        if key in cache:
            it["jev"] = cache[key]["jev"]
            return
        if used >= budget:
            raise RuntimeError(f"TypeSafe budget of {budget} requests exhausted")
        used += 1
        async with sem:
            t0 = time.perf_counter()
            result = await typesafe.ask(state, jev_check.QUESTIONS, purpose="support_check")
            wall = round((time.perf_counter() - t0) * 1000, 1)
        it["jev"] = {"answers": _dump(result), "meta": result.meta, "wall_ms": wall}
        cache[key] = {"key": key, "jev": it["jev"]}
        with CACHE.open("a") as f:
            f.write(json.dumps(cache[key]) + "\n")

    await asyncio.gather(*(jev_one(it) for it in items))
    print(f"TypeSafe requests sent: {used}")

    # LLM check: one call per (case, kind), as production checks one summary's claims in one call
    previous = {r["group"]: r for r in read_json(RAW)["llm_calls"]} if RAW.exists() else {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[f"{it['case']}|{it['kind'].split('_')[0] if it['kind'].startswith('real') else it['kind']}"].append(it)
    calls = []

    async def llm_group(group: str, its: list[dict]) -> None:
        ids = [it["claim"]["id"] for it in its]
        prev = previous.get(group)
        if prev and prev["ids"] == ids:
            calls.append(prev)
            return
        claims = [GroundedClaim.model_validate(it["claim"]) for it in its]
        async with sem:
            t0 = time.perf_counter()
            kept, dropped, meta = await verify_support(claims, pages_by_case[its[0]["case"]])
            wall = round((time.perf_counter() - t0) * 1000, 1)
        calls.append({"group": group, "ids": ids, "kept": [c.id for c in kept], "meta": meta, "wall_ms": wall,
                      "failed": any(d.reason.value == "support_check_failed" for d in dropped)})

    await asyncio.gather(*(llm_group(g, its) for g, its in groups.items()))
    await typesafe.aclose()
    kept_ids = {i for c in calls for i in c["kept"]}
    for it in items:
        it["llm_supported"] = it["claim"]["id"] in kept_ids
    RAW.write_text(json.dumps({"items": items, "llm_calls": calls}, indent=1, ensure_ascii=False))
    print(f"wrote {RAW.relative_to(ROOT)}: {len(items)} items, {len(calls)} LLM calls")


# ---------------------------------------------------------------- report


def confusion(items: list[dict], says_drop) -> dict[str, Any]:
    tp = sum(1 for it in items if it["label"] in SHOULD_DROP and says_drop(it))
    fn = sum(1 for it in items if it["label"] in SHOULD_DROP and not says_drop(it))
    fp = sum(1 for it in items if it["label"] not in SHOULD_DROP and says_drop(it))
    tn = sum(1 for it in items if it["label"] not in SHOULD_DROP and not says_drop(it))
    n = tp + fn + fp + tn
    return {"n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
            "agreement": (tp + tn) / n if n else None,
            "missed": [it["claim"]["id"] for it in items if it["label"] in SHOULD_DROP and not says_drop(it)],
            "false_drops": [it["claim"]["id"] for it in items if it["label"] not in SHOULD_DROP and says_drop(it)]}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def report() -> None:
    raw = read_json(RAW)
    items, calls = raw["items"], raw["llm_calls"]
    t = jev_check.THRESHOLDS

    def jev_drop(it: dict, th: jev_check.SupportThresholds = t) -> bool:
        return not jev_check.verdict(_load(it["jev"]["answers"]), th)

    def llm_drop(it: dict) -> bool:
        return not it["llm_supported"]

    real = [it for it in items if not it["kind"].startswith("synthetic")]
    synth = [it for it in items if it["kind"].startswith("synthetic")]
    subsets = {"real (judge labels)": real, "synthetic (NOT_SUPPORTED by construction)": synth, "all": items}
    L: list[str] = []
    w = L.append
    out: dict[str, Any] = {"thresholds": dataclasses.asdict(t), "subsets": {}}
    labels = Counter(it["label"] for it in real)
    w("# Citation support check: Jev vs the LLM check\n")
    w("Generated by `evals/output/run_jev_check.py report` (no API calls). Positive class = the claim should be "
      "dropped (judge PARTIALLY or NOT_SUPPORTED). `llm` = agent/citations/ground.py:verify_support "
      "(deepseek/deepseek-v4.1-flash, one call per summary); `jev` = agent/citations/jev_check.py (one TypeSafe "
      "request per claim, concurrently), kept iff the Choice says supported with P >= "
      f"{t.supported}.\n")
    w(f"Real items: {len(real)} ({', '.join(f'{k} {v}' for k, v in labels.most_common())}; kinds "
      f"{dict(Counter(it['kind'] for it in real))}). Synthetic: {len(synth)} "
      f"({dict(Counter(it['kind'] for it in synth))}).\n")
    for name, subset in subsets.items():
        rows = {"llm": confusion(subset, llm_drop), "jev": confusion(subset, jev_drop)}
        out["subsets"][name] = rows
        w(f"## {name}\n")
        w("| checker | n | caught (TP) | missed (FN) | false drops (FP) | precision | recall | agreement |")
        w("|---|---|---|---|---|---|---|---|")
        for k, r in rows.items():
            w(f"| {k} | {r['n']} | {r['tp']} | {r['fn']} | {r['fp']} | {_pct(r['precision'])} | {_pct(r['recall'])} "
              f"| {_pct(r['agreement'])} |")
        w("")
        for k, r in rows.items():
            if r["missed"] or r["false_drops"]:
                w(f"- {k}: missed {', '.join(r['missed']) or 'none'}; false drops {', '.join(r['false_drops']) or 'none'}")
        w("")

    # per real item detail
    w("## Real items, one by one\n")
    w("| claim id | kind | judge | llm keeps | jev choice | P(supported) | P(partially) | P(not) | figure | condition |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for it in sorted(real, key=lambda it: (it["label"] == "SUPPORTED", it["claim"]["id"])):
        a = it["jev"]["answers"]
        p = a["support"]["probabilities"]
        w(f"| {it['claim']['id']} | {it['kind']} | {it['label']} | {'yes' if it['llm_supported'] else 'no'} | "
          f"{a['support']['choice']} | {p.get('supported', 0):.2f} | {p.get('partially', 0):.2f} | "
          f"{p.get('not_supported', 0):.2f} | {a['figure']['noul']:.2f} | {a['condition']['noul']:.2f} |")
    w("")

    # sweeps
    w("## Jev thresholds (real items / synthetic items)\n")
    w("| P(supported) >= | figure Noul < | condition Noul < | real precision | real recall | real FP | synthetic recall |")
    w("|---|---|---|---|---|---|---|")
    sweeps = []
    for sup in (0.3, 0.5, 0.7, 0.8, 0.9, 0.95):
        for fig, cond in ((1.01, 1.01), (0.5, 1.01), (1.01, 0.5), (1.01, 0.7), (0.5, 0.5)):
            th = jev_check.SupportThresholds(supported=sup, figure=fig, condition=cond)
            r = confusion(real, lambda it, th=th: jev_drop(it, th))
            s = confusion(synth, lambda it, th=th: jev_drop(it, th))
            sweeps.append({"supported": sup, "figure": fig, "condition": cond, "real": r, "synthetic": s})
            mark = " *" if th == t else ""
            w(f"| {sup}{mark} | {fig} | {cond} | {_pct(r['precision'])} | {_pct(r['recall'])} | {r['fp']} | "
              f"{_pct(s['recall'])} |")
    out["sweeps"] = [{k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items()
                                                              if kk not in ("missed", "false_drops")})
                      for k, v in s.items()} for s in sweeps]
    w("")

    # latency and cost
    jev_lat = [it["jev"]["wall_ms"] for it in items]
    jev_tok = [it["jev"]["meta"]["input_tokens"] for it in items]
    jev_cost = [it["jev"]["meta"]["cost"] for it in items]
    by_group: dict[str, list[float]] = defaultdict(list)
    for it in items:
        by_group[f"{it['case']}|{it['kind']}"].append(it["jev"]["wall_ms"])
    llm_lat = [c["wall_ms"] for c in calls]
    llm_claims = [len(c["ids"]) for c in calls]
    llm_cost = [c["meta"].get("cost_total") or c["meta"].get("cost") or 0 for c in calls]
    llm_tok = [c["meta"].get("prompt_tokens") or 0 for c in calls]
    perf = {
        "jev_requests": len(items),
        "jev_latency_p50_ms": _pctl(jev_lat, 0.5), "jev_latency_p95_ms": _pctl(jev_lat, 0.95),
        "jev_summary_wall_p50_ms": _pctl([max(v) for v in by_group.values()], 0.5),
        "jev_summary_wall_p95_ms": _pctl([max(v) for v in by_group.values()], 0.95),
        "jev_mean_input_tokens_per_claim": statistics.mean(jev_tok),
        "jev_cost_per_claim_usd": statistics.mean(jev_cost),
        "llm_calls": len(calls), "llm_mean_claims_per_call": statistics.mean(llm_claims),
        "llm_latency_p50_ms": _pctl(llm_lat, 0.5), "llm_latency_p95_ms": _pctl(llm_lat, 0.95),
        "llm_mean_prompt_tokens_per_call": statistics.mean(llm_tok),
        "llm_cost_per_claim_usd": sum(llm_cost) / sum(llm_claims),
        "llm_failed_calls": sum(c["failed"] for c in calls),
        "llm_models": dict(Counter(c["meta"].get("model") for c in calls)),
    }
    out["performance"] = perf
    w("## Latency and cost\n")
    w("| | LLM check (one call per summary) | Jev (one request per claim, concurrent) |")
    w("|---|---|---|")
    w(f"| calls | {perf['llm_calls']} (mean {perf['llm_mean_claims_per_call']:.1f} claims each) | {perf['jev_requests']} |")
    w(f"| latency p50 / p95 per call | {perf['llm_latency_p50_ms'] / 1000:.2f} s / {perf['llm_latency_p95_ms'] / 1000:.2f} s "
      f"| {perf['jev_latency_p50_ms']:.0f} ms / {perf['jev_latency_p95_ms']:.0f} ms |")
    w(f"| wall per summary p50 / p95 | same as per call | {perf['jev_summary_wall_p50_ms']:.0f} ms / "
      f"{perf['jev_summary_wall_p95_ms']:.0f} ms |")
    w(f"| input tokens | {perf['llm_mean_prompt_tokens_per_call']:.0f} per call | "
      f"{perf['jev_mean_input_tokens_per_claim']:.0f} per claim |")
    w(f"| cost per claim | ${perf['llm_cost_per_claim_usd']:.6f} | ${perf['jev_cost_per_claim_usd']:.6f} |")
    w(f"| cost per 1,000 summaries (5 claims each) | ${5000 * perf['llm_cost_per_claim_usd']:.3f} | "
      f"${5000 * perf['jev_cost_per_claim_usd']:.3f} |")
    w(f"| failed calls | {perf['llm_failed_calls']} | 0 |")
    w(f"\nLLM models that answered: {perf['llm_models']}. Jev cost is input tokens x $0.042/Mtok "
      "(docs.typesafe.ai/models; output tokens are free); the API returns tokens, not prices.\n")
    real_pos = sum(it["label"] in SHOULD_DROP for it in real)
    w("## Notes\n")
    w(f"- Small sample: {real_pos} real claims should be dropped (judge PARTIALLY/NOT_SUPPORTED) out of {len(real)}; "
      "one claim moves recall by 12 points. The judge (one model, `anthropic/claude-sonnet-5.5`, full page) is "
      "the reference, not ground truth.")
    w(f"- Jev's threshold (P(supported) >= {t.supported}) was set before the run and not moved; the sweep shows "
      "0.3-0.5 is a plateau and higher values only add false drops. The figure and condition Nouls add nothing "
      "here (the deterministic figure filter in claims.py already catches what the figure Noul sees; the "
      "condition Noul false-alarms), so they stay off.")
    w("- Synthetic items (swapped quotes, flipped verbs) are easy for both checkers: they show neither misses gross "
      "misuse, not how either does on subtle overstatement, which is where the real items differ.")
    w("- In production the LLM check only runs on claims whose quote was fuzzy or widened (claims.py "
      "`_check_entailment`); this eval checks every claim with both.")
    w("")
    REPORT.write_text("\n".join(L))
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print(f"-> {REPORT.relative_to(ROOT)}, {OUT.relative_to(ROOT)}")
    for name, rows in out["subsets"].items():
        for k, r in rows.items():
            print(f"{name[:9]:9} {k:4} n={r['n']} tp={r['tp']} fn={r['fn']} fp={r['fp']} "
                  f"precision={_pct(r['precision'])} recall={_pct(r['recall'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--budget", type=int, default=160, help="max TypeSafe requests")
    r.add_argument("--concurrency", type=int, default=6)
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "run":
        asyncio.run(run(args.budget, args.concurrency))
    report()


if __name__ == "__main__":
    main()
