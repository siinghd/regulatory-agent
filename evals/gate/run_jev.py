"""Gate eval for the Jev classifier (agent/gate/jev.py), side by side with the LLM gate.

Run from the repo root (the shell exports a stale OPENROUTER_API_KEY that would override .env):

    env -u OPENROUTER_API_KEY .venv/bin/python -m evals.gate.run_jev run --variant jev                  # dataset.jsonl
    env -u OPENROUTER_API_KEY .venv/bin/python -m evals.gate.run_jev run --variant jev --dataset heldout
    env -u OPENROUTER_API_KEY .venv/bin/python -m evals.gate.run_jev run --variant llm --dataset heldout
    .venv/bin/python -m evals.gate.run_jev report        # -> report_jev.md, results_jev.json (no API calls)

`run --variant jev` stores Jev's raw answers per case (raw_<dataset>_jev.jsonl); `report` re-runs
jev.decide() on the stored answers with the current code and thresholds, so tuning a threshold or a
decision rule costs no requests. Identical requests are served from jev_cache.jsonl unless --fresh
(a second fresh run measures consistency and latency). Scoring is evals/gate/score.py's, unchanged.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import structlog

from agent import typesafe
from agent.config import get_settings
from agent.gate import jev, rules
from agent.typesafe import ChoiceAnswer, NoulAnswer, Result, ScoreAnswer
from evals.gate import run as base_run  # sets sys.path and the working directory, like run.py
from evals.gate import score as S

HERE = base_run.HERE
DATASETS = {"dataset": HERE / "dataset.jsonl", "heldout": HERE / "heldout.jsonl"}
CACHE = HERE / "jev_cache.jsonl"
REPORT = HERE / "report_jev.md"
RESULTS = HERE / "results_jev.json"
MAX_DOCS = base_run.MAX_DOCS
# Escalation for the hybrid variant: the gates production escalates on (gate_jev_low_confidence = "llm").
UNSURE_GATES = jev.UNSURE_GATES


def raw_path(dataset: str, variant: str) -> Path:
    if dataset == "dataset" and variant == "llm":
        return HERE / "raw.jsonl"  # evals/gate/run.py's own run of the LLM gate
    return HERE / f"raw_{dataset}_{variant}.jsonl"


# ---------------------------------------------------------------- answers <-> JSON


def dump_answers(result: Result) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, a in result.answers.items():
        if isinstance(a, NoulAnswer):
            out[qid] = {"type": "noul", "noul": a.noul}
        elif isinstance(a, ChoiceAnswer):
            out[qid] = {"type": "choice", "choice": a.choice, "probabilities": dict(a.probabilities),
                        "confidence": a.confidence}
        else:
            out[qid] = {"type": "score", "score": a.score, "probabilities": dict(a.probabilities),
                        "legend": dict(a.legend), "confidence": a.confidence}
    return out


def load_answers(d: dict[str, Any], meta: dict[str, Any] | None = None) -> Result:
    answers: dict[str, Any] = {}
    for qid, a in d.items():
        if a["type"] == "noul":
            answers[qid] = NoulAnswer(a["noul"])
        elif a["type"] == "choice":
            answers[qid] = ChoiceAnswer(a["choice"], a["probabilities"], a["confidence"])
        else:
            answers[qid] = ScoreAnswer(a["score"], {int(k): v for k, v in a["probabilities"].items()},
                                       {int(k): v for k, v in a["legend"].items()}, a["confidence"])
    return Result(answers=answers, meta=meta or {})


def request_key(state: Any, questions: dict[str, Any]) -> str:
    payload = {"state": state, "questions": {q: v.payload() for q, v in questions.items()},
               "model": get_settings().typesafe_model}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def load_cache() -> dict[str, dict[str, Any]]:
    if not CACHE.exists():
        return {}
    return {e["key"]: e for e in map(json.loads, CACHE.read_text().splitlines()) if e}


# ---------------------------------------------------------------- run


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> None:
        if self.used >= self.limit:
            raise RuntimeError(f"TypeSafe request budget of {self.limit} exhausted")
        self.used += 1


async def run_jev_case(case: dict[str, Any], run_idx: int, sem: asyncio.Semaphore, cache: dict[str, Any],
                       fresh: bool, budget: Budget) -> dict[str, Any]:
    subject, body = case["subject"], case["body"]
    async with sem:
        t0 = time.perf_counter()
        rule = rules.parse(subject, body, max_docs=MAX_DOCS)
        rec: dict[str, Any] = {"id": case["id"], "run": run_idx, "fast_path": rule.parsed is not None,
                               "rule_reason": rule.reason, "answers": None, "meta": None, "error": None,
                               "cached": False}
        if rule.parsed is not None:
            rec["latency_ms"] = base_run._ms(t0)
            return rec
        state = jev.state_for(subject, body)
        questions = jev.questions_for(subject, body, rule.matters)
        key = request_key(state, questions)
        hit = None if fresh else cache.get(key)
        if hit is not None:
            rec.update(answers=hit["answers"], meta=hit["meta"], latency_ms=hit["wall_ms"], cached=True)
            return rec
        budget.take()
        try:
            result = await typesafe.ask(state, questions, purpose="gate")
        except typesafe.TypeSafeUnavailable as e:
            rec.update(error=f"{type(e).__name__}: {e}"[:500], latency_ms=base_run._ms(t0))
            return rec
        jev.decide(subject, body, rule, result, max_docs=MAX_DOCS)  # inside the timed path, as in production
        wall = base_run._ms(t0)
        entry = {"key": key, "answers": dump_answers(result), "meta": result.meta, "wall_ms": wall}
        cache[key] = entry
        with CACHE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        rec.update(answers=entry["answers"], meta=result.meta, latency_ms=wall)
        return rec


async def run_variant(args: argparse.Namespace) -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    base_run.register_providers(http=False)
    cases = base_run.load_cases(DATASETS[args.dataset])
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    sem = asyncio.Semaphore(args.concurrency)
    out = raw_path(args.dataset, args.variant) if not args.out else args.out
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    t0 = time.perf_counter()
    meta: dict[str, Any] = {"started_at": started, "variant": args.variant, "dataset": args.dataset,
                            "concurrency": args.concurrency, "cases": len(cases)}
    if args.variant == "jev":
        cache = load_cache()
        budget = Budget(args.budget)
        snapshots = {c["id"]: rules.parse(c["subject"], c["body"]).parsed is not None for c in cases}
        jobs = [(c, i) for c in cases for i in range(1 if snapshots[c["id"]] else args.repeats)]
        results = await asyncio.gather(*(
            run_jev_case(c, i, sem, cache, args.fresh or (i > 0 and args.fresh_repeats), budget) for c, i in jobs
        ))
        meta.update(model=get_settings().typesafe_model, requests=budget.used, repeats=args.repeats,
                    served=sorted({(r["meta"] or {}).get("model") for r in results if r["meta"]} - {None}))
        print(f"TypeSafe requests sent: {budget.used}")
        await typesafe.aclose()
    else:
        base_run.install_llm_recorder()
        snapshots = {c["id"]: base_run.rule_snapshot(c) for c in cases}
        jobs = [(c, i) for c in cases for i in range(1 if snapshots[c["id"]]["fast_path"] else args.repeats)]
        results = await asyncio.gather(*(base_run.run_one(c, i, sem) for c, i in jobs))
        for r in results:
            r["rule"] = snapshots[r["id"]]
        meta.update(llm_models=get_settings().llm_models, repeats=args.repeats)
    order = [c["id"] for c in cases]
    results.sort(key=lambda r: (order.index(r["id"]), r["run"]))
    meta.update(wall_s=round(time.perf_counter() - t0, 1), runs=len(results))
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {out} ({meta['wall_s']} s, {len(results)} runs)")


# ---------------------------------------------------------------- re-decide + score


def jev_records(cases: list[dict[str, Any]], raw: list[dict[str, Any]], t: jev.Thresholds,
                llm_raw: dict[tuple[str, int], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """score.py-shaped run records from stored Jev answers, decided with `t`. With `llm_raw`, a run whose
    decision fired an UNSURE gate takes the LLM gate's parse instead (the hybrid variant)."""
    by_id = {c["id"]: c for c in cases}
    out = []
    for r in raw:
        c = by_id[r["id"]]
        rule = rules.parse(c["subject"], c["body"], max_docs=MAX_DOCS)
        rec: dict[str, Any] = {"id": r["id"], "run": r["run"], "latency_ms": r["latency_ms"], "error": r["error"],
                               "rule": base_run.rule_snapshot(c), "llm_raw": None, "llm_meta": None,
                               "attempts": [], "cost": 0.0, "gates": [], "jev": None}
        if rule.parsed is not None:
            rec.update(parsed=rule.parsed.model_dump(mode="json"), source="rules")
        elif r["answers"] is None:
            rec.update(parsed=None, source="exception")
        else:
            result = load_answers(r["answers"], r["meta"])
            d = jev.decide(c["subject"], c["body"], rule, result, max_docs=MAX_DOCS, t=t)
            meta = r["meta"] or {}
            rec.update(parsed=d.parsed.model_dump(mode="json"), source="jev", gates=d.gates,
                       llm_raw=_llm_shape(result, d.parsed), llm_meta=meta, cost=meta.get("cost") or 0.0,
                       jev={"answers": jev.describe(result.answers), "gates": d.gates},
                       attempts=[{"model_requested": meta.get("model_requested"), "model_served": meta.get("model"),
                                  "status": 200, "latency_ms": meta.get("latency_ms"), "cost": meta.get("cost"),
                                  "prompt_tokens": meta.get("input_tokens"),
                                  "completion_tokens": meta.get("output_tokens"), "reasoning_tokens": 0}])
            if llm_raw is not None and set(d.gates) & UNSURE_GATES:
                fallback = llm_raw.get((r["id"], r["run"])) or llm_raw.get((r["id"], 0))
                if fallback is not None and fallback.get("parsed"):
                    rec.update(parsed=fallback["parsed"], source="jev+llm", llm_raw=fallback.get("llm_raw"),
                               latency_ms=r["latency_ms"] + fallback["latency_ms"],
                               cost=rec["cost"] + (fallback.get("cost") or 0.0),
                               attempts=rec["attempts"] + fallback.get("attempts", []))
        out.append(rec)
    return out


def _llm_shape(result: Result, parsed: Any) -> dict[str, Any]:
    """What score.diagnose expects in llm_raw (the LLM gate's schema), from Jev's top answers."""
    a = result.answers
    matter = a["matter"].choice if "matter" in a else None
    category = next((v.choice for k, v in a.items() if k.startswith("category.")
                     and parsed.matter and k == f"category.{_provider(parsed.matter)}"), None)
    return {"intent": a["intent"].choice, "matter": None if matter == jev.NONE else matter,
            "other_matters": list(parsed.extra_matters), "doc_type": None if category == jev.NONE else category,
            "other_doc_types": list(parsed.extra_doc_types), "max_docs": parsed.max_docs, "clarification": None}


def _provider(matter: str) -> str | None:
    from agent.providers.base import provider_for_matter
    p = provider_for_matter(matter)
    return p.name if p else None


def scored(cases: list[dict[str, Any]], records: list[dict[str, Any]], meta: dict[str, Any]) -> tuple[str, dict]:
    for c in cases:  # ids are unique across both sets, so CANDS can hold both
        S.CANDS[c["id"]] = S.candidates(c["expected"])
    return S.build(S.score(cases, records, meta))


def side_by_side(res: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    cases = res["cases"]
    runs = [(c, r) for c in cases for r in c["runs"]]
    cand_actions = {c["id"]: {S.action(cd) for cd in S.CANDS[c["id"]]} for c in cases}
    false_clar = [c["id"] for c, r in runs if r["action"] == "clarify" and "clarify" not in cand_actions[c["id"]]]
    model_runs = [r for r in records if r["source"] not in ("rules",)]
    lat = [r["latency_ms"] for r in model_runs]
    tokens = [a.get("prompt_tokens") or 0 for r in model_runs for a in r["attempts"]
              if (a.get("model_requested") or "").startswith("jev")]
    jev_runs = [r for r in records if r["source"] in ("jev", "jev+llm")]
    low = [r for r in jev_runs if set(r.get("gates") or []) & UNSURE_GATES]
    first = {}
    for r in records:
        first.setdefault(r["id"], r)
    cost_first = sum(r["cost"] or 0 for r in first.values())
    s = res["safety"]
    inj_cases = [c for c in cases if c["expected"]["intent"] == "injection_attempt"]
    inj_labelled = sum(S.mean([float(r["got"] is not None and r["got"]["intent"] == "injection_attempt")
                               for r in c["runs"]]) for c in inj_cases)
    return {
        "cases": len(cases),
        "runs": len(runs),
        "exact_match": res["headline"]["exact_match"],
        "operator_correct": res["headline"]["operator_correct"],
        "action_accuracy": res["headline"]["action_accuracy"],
        "wrong_fetches": len(s["wrong_fetches"]),
        "wrong_fetch_ids": [w[0] for w in s["wrong_fetches"]],
        "injection_labelled": f"{inj_labelled:g}/{len(inj_cases)}",
        "injection_false_positives": s["injection_false_positives"],
        "false_clarifications_runs": len(false_clar),
        "false_clarification_ids": sorted(set(false_clar)),
        "missed_fetches": len(s["missed_fetches"]),
        "model_path_runs": len(model_runs),
        "low_confidence_runs": len(low),
        "jev_runs": len(jev_runs),
        "latency_p50_ms": S.percentile(lat, 0.5),
        "latency_p95_ms": S.percentile(lat, 0.95),
        "mean_input_tokens": statistics.mean(tokens) if tokens else None,
        "cost_per_1000_emails_usd": 1000 * cost_first / len(first) if first else 0.0,
        "cost_per_model_call_usd": (sum(r["cost"] or 0 for r in model_runs) / len(model_runs)) if model_runs else 0.0,
        "identical_output_rate": res["consistency"]["identical_output_rate"],
    }


def _mean_tokens(sets: dict[str, Any], ds: str) -> str:
    recs = sets[ds]["variants"].get("jev", (None, None, []))[2]
    tokens = [(r["llm_meta"] or {}).get("input_tokens") for r in recs if r["source"] == "jev"]
    tokens = [t for t in tokens if t]
    return f"{statistics.mean(tokens):.0f}" if tokens else "-"


def load_raw(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def llm_records(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return raw  # evals/gate/run.py's records are already score.py-shaped


# ---------------------------------------------------------------- threshold sweep

SWEEP = {
    "injection": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    "intent": [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    "matter": [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    "category": [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
    "category_unnamed": [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95],
    "wanted": [0.3, 0.4, 0.5, 0.6, 0.7],
    "extra_category": [0.5, 0.6, 0.7, 0.8, 0.9],
    "matter_none": [0.5, 0.7, 0.8, 0.9, 0.95, 1.01],
    "extra_matter": [0.3, 0.5, 0.7, 0.9],
    "excludes": [0.3, 0.5, 0.7, 1.01],
    "count": [0.4, 0.6, 0.8, 1.01],
    # not "specific": off (1.01), so its Noul is no longer sent and stored answers have none to sweep
}


def sweep(cases: list[dict[str, Any]], raw: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    base = jev.THRESHOLDS
    for name, values in SWEEP.items():
        rows = []
        for v in values:
            t = dataclasses.replace(base, **{name: v})
            recs = jev_records(cases, raw, t)
            _, res = scored(cases, recs, {})
            sb = side_by_side(res, recs)
            rows.append({"value": v, **{k: sb[k] for k in ("exact_match", "operator_correct", "wrong_fetches",
                                                            "false_clarifications_runs", "injection_labelled")},
                         "injection_fp": len(sb["injection_false_positives"])})
        out[name] = rows
    return out


# ---------------------------------------------------------------- report


def build_report() -> None:
    base_run.register_providers(http=False)
    sets: dict[str, dict[str, Any]] = {}
    for ds in ("dataset", "heldout"):
        cases = base_run.load_cases(DATASETS[ds])
        jraw = load_raw(raw_path(ds, "jev"))
        lraw = load_raw(raw_path(ds, "llm"))
        variants: dict[str, tuple[str, dict, list]] = {}
        if lraw:
            recs = llm_records(lraw)
            variants["llm"] = (*scored(cases, recs, json.loads(raw_path(ds, "llm").with_suffix(".meta.json").read_text())), recs)
        if jraw:
            jmeta = json.loads(raw_path(ds, "jev").with_suffix(".meta.json").read_text())
            recs = jev_records(cases, jraw, jev.THRESHOLDS)
            variants["jev"] = (*scored(cases, recs, jmeta), recs)
            if ds == "heldout":
                # What category_unnamed adds: today's answers decided without it (see Notes).
                t0 = dataclasses.replace(jev.THRESHOLDS, category_unnamed=0.0)
                recs0 = jev_records(cases, jraw, t0)
                variants["jev (category_unnamed off)"] = (*scored(cases, recs0, jmeta), recs0)
            if lraw:
                by_run = {(r["id"], r["run"]): r for r in lraw}
                recs_h = jev_records(cases, jraw, jev.THRESHOLDS, llm_raw=by_run)
                variants["hybrid"] = (*scored(cases, recs_h, jmeta), recs_h)
        sets[ds] = {"cases": cases, "variants": variants, "jraw": jraw}

    L: list[str] = []
    w = L.append
    out: dict[str, Any] = {"thresholds": dataclasses.asdict(jev.THRESHOLDS), "sets": {}}
    w("# Gate: Jev (TypeSafe System One) vs the LLM gate (DeepSeek via OpenRouter)\n")
    w("Generated by `evals/gate/run_jev.py report` from stored answers (no API calls at report time). Scoring is "
      "`evals/gate/score.py`'s, unchanged. `llm` = agent.gate.classify (rules, then deepseek/deepseek-v4.1-flash); "
      "`jev` = agent.gate.jev (rules, then one TypeSafe request); `hybrid` = jev, but a run where a confidence gate "
      "fired (intent, matter or category below threshold) takes the LLM gate's parse instead of asking: "
      "production's default (`gate_classifier=jev`, `gate_jev_low_confidence=llm`).\n")
    labels = {
        "dataset": ("In-sample: evals/gate/dataset.jsonl (190 cases; the LLM prompt was tuned on it, "
                    "Jev thresholds were tuned on it)"),
        "heldout": ("Held-out: evals/gate/heldout.jsonl (40 new cases, written before any Jev run; no longer "
                    "fully out-of-sample, see Notes)"),
    }
    for ds, label in labels.items():
        variants = sets[ds]["variants"]
        if not variants:
            continue
        w(f"## {label}\n")
        rows = {v: side_by_side(res, recs) for v, (_, res, recs) in variants.items()}
        out["sets"][ds] = {"side_by_side": rows}
        metrics = [
            ("Exact match", lambda r: S.pct(r["exact_match"], 1)),
            ("Operator-correct", lambda r: S.pct(r["operator_correct"], 1)),
            ("Action accuracy", lambda r: S.pct(r["action_accuracy"], 1)),
            ("**Wrong fetches** (runs)", lambda r: f"{r['wrong_fetches']}" + (
                f" ({', '.join(sorted(set(r['wrong_fetch_ids'])))})" if r["wrong_fetch_ids"] else "")),
            ("Injection labelled (expected injection_attempt)", lambda r: r["injection_labelled"]),
            ("False injection flags", lambda r: f"{len(r['injection_false_positives'])}" + (
                f" ({', '.join(r['injection_false_positives'])})" if r["injection_false_positives"] else "")),
            ("False clarifications (runs asking where an answer/fetch was expected)",
             lambda r: f"{r['false_clarifications_runs']}" + (
                 f" ({', '.join(r['false_clarification_ids'])})" if r["false_clarification_ids"] else "")),
            ("Missed plain fetches (cases)", lambda r: r["missed_fetches"]),
            ("Model-path runs", lambda r: r["model_path_runs"]),
            ("Low-confidence Jev runs (the hybrid asks the LLM)", lambda r: (
                f"{r['low_confidence_runs']}/{r['jev_runs']} ({S.pct(r['low_confidence_runs'] / r['jev_runs'], 1)})"
                if r["jev_runs"] else "-")),
            ("Latency p50 / p95 (model path, concurrency 6)",
             lambda r: f"{S.fmt_ms(r['latency_p50_ms'])} / {S.fmt_ms(r['latency_p95_ms'])}"),
            ("Mean input tokens per Jev call", lambda r: f"{r['mean_input_tokens']:.0f}" if r["mean_input_tokens"] else "-"),
            ("Cost per model-path call", lambda r: f"${r['cost_per_model_call_usd']:.6f}"),
            ("Cost per 1,000 emails (this mix, rules hits free)", lambda r: f"${r['cost_per_1000_emails_usd']:.3f}"),
            ("Identical output on both runs", lambda r: S.pct(r["identical_output_rate"], 1)),
        ]
        names = list(rows)
        w(S.md_table(["metric", *names], [[m, *[f(rows[n]) for n in names]] for m, f in metrics]))
        w("")
        for v, (report, res, recs) in variants.items():
            out["sets"][ds][v] = {k: res[k] for k in ("headline", "per_field", "safety", "consistency", "latency",
                                                     "cost", "intent_confusion", "failures")}
            if v == "llm":
                continue
            w(f"### {v}: failures ({ds})\n")
            fails = sorted(res["failures"], key=lambda f: (f["score"], f["id"]))
            gates = {r["id"]: r.get("gates") for r in recs}
            w(S.md_table(["case", "expected", "got", "gates", "diagnosis"],
                         [[f["id"], f["expected"], " / ".join(f["got"]), ",".join(gates.get(f["id"]) or []),
                           "; ".join(t for _, t in f["diagnosis"])[:300]] for f in fails]) if fails else "none")
            w("")
            conf = res["intent_confusion"]
            w(f"Intent confusion ({v}, rows expected):\n")
            w(S.md_table(["expected \\ got", *S.INTENTS],
                         [[e, *[conf[e].get(g, 0) for g in S.INTENTS]] for e in S.INTENTS]))
            w("")

    # threshold sweeps (Jev only, decided again from stored answers)
    w("## Thresholds\n")
    w("Chosen on the in-sample set, one at a time around the defaults (`agent/gate/jev.py:Thresholds`), except "
      "`category_unnamed`, which was added after the held-out run showed two wrong fetches (see Notes). Each row "
      "changes one threshold; the rest stay at the chosen values. Many rows are flat in-sample: the 190 cases "
      "rarely sit near a boundary, so a flat row means 'not discriminated by this data', not 'safe anywhere'.\n")
    w(S.md_table(["threshold", "value"], [[k, v] for k, v in dataclasses.asdict(jev.THRESHOLDS).items()]))
    w("")
    out["sweeps"] = {}
    for ds in ("dataset", "heldout"):
        if not sets[ds]["jraw"]:
            continue
        sw = sweep(sets[ds]["cases"], sets[ds]["jraw"])
        out["sweeps"][ds] = sw
        w(f"### Sweep on {ds}\n")
        rows = []
        for name, values in sw.items():
            for row in values:
                mark = " *" if row["value"] == getattr(jev.THRESHOLDS, name) else ""
                rows.append([name, f"{row['value']}{mark}", S.pct(row["exact_match"], 1),
                             S.pct(row["operator_correct"], 1), row["wrong_fetches"], row["false_clarifications_runs"],
                             row["injection_labelled"], row["injection_fp"]])
        w(S.md_table(["threshold", "value (* = chosen)", "exact", "operator-correct", "wrong fetches",
                      "false clarifications", "injection labelled", "injection FP"], rows))
        w("")

    # gate firing stats
    w("## How often each gate fired (Jev, all runs)\n")
    for ds in ("dataset", "heldout"):
        if "jev" not in sets[ds]["variants"]:
            continue
        recs = sets[ds]["variants"]["jev"][2]
        cnt = Counter(g for r in recs for g in r.get("gates") or [])
        w(f"- {ds}: " + (", ".join(f"{k} {v}" for k, v in cnt.most_common()) or "none") +
          f" (of {sum(r['source'] == 'jev' for r in recs)} Jev runs)")
    w("")
    w("## Notes\n")
    jmeta = {ds: json.loads(raw_path(ds, "jev").with_suffix(".meta.json").read_text())
             for ds in ("dataset", "heldout") if raw_path(ds, "jev").exists()}
    w(f"- Model: requested `{get_settings().typesafe_model}` (pinned: `TYPESAFE_MODEL`), served "
      f"{sorted({m for v in jmeta.values() for m in v.get('served', [])})}. The thresholds were tuned on this "
      "version; a new one is a change that needs this eval again.")
    w("- Held-out contamination: the first held-out run (2026-10-04) had two wrong fetches, both a FERC category "
      "picked for text that names none (\"what's been filed\" -> Applications and Filings at 0.65-0.70). "
      "`category_unnamed` (a type the text doesn't name by alias needs P >= 0.8) was added after seeing them; it "
      "is principled (typos and translations score 1.00, the wrong guesses 0.65-0.70) but the held-out numbers are "
      "no longer out-of-sample: 92.5% exact / 95.0% operator-correct / 2 wrong fetches was that first run, the "
      "estimate to quote. The column 'jev (category_unnamed off)' re-decides today's answers without the threshold.")
    w("- Payload (input tokens are what TypeSafe bills): questions decide() could never read for an email are not "
      "sent (the specific-document Noul while its threshold is off; exclusion when the rules already see a "
      "negation; count when the rules read one whatever matter Jev picks), and each per-type Noul names its "
      "category's other names once (no singular beside its plural). Mean input tokens per call: 2027 -> "
      f"{_mean_tokens(sets, 'dataset')} in-sample, 2092 -> {_mean_tokens(sets, 'heldout')} held-out. About 300 "
      "tokens of every call are TypeSafe's fixed overhead (a one-question probe bills 360). Shorter wordings were "
      "tried and rejected: the category Choice as one-line criteria with de-duplicated aliases (~10% fewer tokens) "
      "and shorter count and acknowledgement options (~1%) moved knife-edge cases in A/B runs (3 fresh requests "
      "per model-path case, both sets): short-ferc-split correct in 1 of 5 runs instead of 5 of 5, ho-q-status "
      "from a confident 'none stated' to low-confidence, ho-uarb-final-ruling asking in 1 of 3 runs.")
    w("- Cost: TypeSafe bills input tokens only, $0.042 per million (docs.typesafe.ai/models, jev-1.13); the API "
      "returns token counts, not prices. DeepSeek cost is OpenRouter's reported `usage.cost`.")
    w("- Latency: wall time of the model path (request + decision) at concurrency 6; Jev's second run bypassed "
      "the answer cache, so both runs are live calls.")
    w("- Data handling: TypeSafe offers zero data retention only on enterprise plans (docs: models, legal); ours "
      "is not one. Owner decision (MVP): accepted for the gate (email subject and body) and the citation check "
      "(public document excerpts), and disclosed in the privacy notice and the vendor register. "
      "`llm_zero_data_retention` governs OpenRouter routing only.")
    w("- Jev's answers vary slightly between identical requests (P(injection) 0.54-0.57 on one email over three "
      "runs), so a case within a few points of a threshold can flip between runs: inj-json-override (injection "
      "~0.55 against 0.5) and short-ferc-split (category ~0.55 against 0.5) are the in-sample ones.")
    w("")
    REPORT.write_text("\n".join(L))
    RESULTS.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    print(f"-> {REPORT.relative_to(HERE.parents[1])}, {RESULTS.relative_to(HERE.parents[1])}")
    for ds in sets:
        for v, (_, res, recs) in sets[ds]["variants"].items():
            sb = side_by_side(res, recs)
            print(f"{ds:8} {v:7} exact {sb['exact_match']:.3f} operator {sb['operator_correct']:.3f} "
                  f"wrong_fetch {sb['wrong_fetches']} false_clar {sb['false_clarifications_runs']} "
                  f"inj {sb['injection_labelled']} fp {len(sb['injection_false_positives'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--variant", choices=("jev", "llm"), required=True)
    r.add_argument("--dataset", choices=tuple(DATASETS), default="dataset")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--fresh", action="store_true", help="ignore jev_cache.jsonl for every run")
    r.add_argument("--fresh-repeats", action="store_true", help="runs after the first bypass the cache")
    r.add_argument("--concurrency", type=int, default=6)
    r.add_argument("--budget", type=int, default=300, help="max TypeSafe requests this invocation")
    r.add_argument("--only", nargs="*")
    r.add_argument("--out", type=Path)
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "run":
        if args.variant == "llm" and args.dataset == "dataset" and not args.out:
            sys.exit("the LLM gate's in-sample run is evals/gate/run.py's raw.jsonl; use --out to rerun it")
        asyncio.run(run_variant(args))
    else:
        build_report()


if __name__ == "__main__":
    main()
