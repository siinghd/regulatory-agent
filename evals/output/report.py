"""Step 5: render results.json as report.md."""

from evals.output.problems import PROBLEMS

WORKED_EXAMPLES = ("uarb_M12383_Other_Documents", "oeb_EB-2024-0111_Decisions_and_Orders",
                   "ferc_ER24-1234-000_Applications_and_Filings")


def _q(text: str, n: int = 400) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _jv(j: dict) -> str:
    if "verdict" not in j:
        return f"_{j.get('skipped') or j.get('error', '?')[:80]}_"
    v = j["verdict"]
    return f"**{v['verdict']}** (rel {v['decision_relevance']}, quote-alone {'y' if v['quote_alone_sufficient'] else 'n'})"


def render(r: dict) -> str:
    a = r["aggregate"]
    cases = r["cases"]
    L: list[str] = []
    w = L.append
    w("# Output-quality eval: cited summaries and reply wording\n")
    w(f"Generated {r['generated_at'][:19]}Z by `evals/output/run.py`. Generator models configured: "
      f"`{', '.join(r['generator_models_configured'])}` (answered: {a['generator_models']}). "
      f"Judge: `{r['judge_models'][0]}`"
      + (f" (fallback `{r['judge_models'][1]}`)" if len(r["judge_models"]) > 1 else "")
      + ", temperature 0, strict JSON schema, "
      "low reasoning effort (the Claude 5.5 endpoints refuse reasoning-off).\n")
    w(f"{a['cases']} cases, {a['documents']} documents; each case = one (provider, matter, category, up to 10 docs) "
      "request, summarised exactly as `pipeline._summarise` does (`summarize_with_citations(info, docs, max_claims=5)`, "
      "PDFs and DOCX), and the reply rendered with `outbound.documents_reply` using fake links. Single generator run per case "
      "(no repeat sampling, so run-to-run variance is not measured).\n")

    sources = {}
    for c in cases:
        sources.setdefault(c["source"], []).append(c["case"])
    w("Run notes:\n")
    w(f"- Documents: reused from the production DB/blob store (read-only) for {len(sources.get('prod_db', []))} cases; "
      f"fetched live from the portals for {len(sources.get('live_portal', []))} "
      f"({', '.join(sources.get('live_portal', []))}). UARB via the browser over the SOCKS proxy, one session; OEB/FERC "
      "over httpx with at most 3 concurrent requests. Everything is cached under evals/output/cache/.")
    w("- The judge is from the Anthropic family: the generators (DeepSeek first, Qwen as fallback) "
      "may route to other families, so no generation is graded by its own family.")
    w("- Temperature 0 is sent with low reasoning effort; whether the provider honours temperature with reasoning on "
      "is not verifiable from the response. DeepSeek at temperature 0 is not deterministic: two generations of the "
      "same case differ, so a single run's judge numbers carry sampling noise of a few claims.")
    if any((c.get("judge_summary") or {}).get("skipped", "").startswith("summary judge skipped") for c in cases):
        w("- The summary judge was skipped (--skip-summary-judge, to stay within the eval budget): coverage, clarity, "
          "faithfulness and NC1 are not measured in this run; claims, drops and NC2/NC3 are.")
    w("- EB-2025-0064 D25-16439 (updated application, 2,406 pages) is cut at extract.MAX_PAGES=400.\n")

    w("## Headline numbers\n")
    w("| Metric | Value |\n|---|---|")
    rows = [
        ("Cases with a summary in the reply", f"{a['cases_with_summary']} / {a['cases']}"),
        ("Cases with no summary", ", ".join(f"{k} ({v})" for k, v in a["cases_no_summary"].items()) or "none"),
        ("Documents never readable by the summariser (DOCX/XLSX/scanned)", f"{a['uncited_documents']} / {a['documents']}"),
        ("Claims proposed / kept / dropped", f"{a['claims_proposed']} / {a['claims_kept']} / {a['claims_dropped']}"),
        ("Drop reasons", ", ".join(f"{k} {v}" for k, v in a["drop_reasons"].items()) or "none"),
        ("Summary sentences removed by figure filter", a["removed_summary_sentences"]),
        ("(a) Quote == page_text[start:end] on cited page", _pct(a["det_quote_integrity_pass"])
         + f" (fuzzy matches {a['det_fuzzy_quotes']}, page-corrected {a['det_page_corrected']})"),
        ("(b) Summaries whose every figure is in the sources", _pct(a["det_summary_figures_pass"])
         + f" ({a['det_summary_figures_total']} figures; unsupported: {a['det_summary_figures_unsupported'] or 'none'})"),
        ("(b) Claims whose every figure is in their own quote", _pct(a["det_claim_figures_pass"])),
        ("(c) Claims citing a case document the model was shown", _pct(a["det_doc_membership_pass"])
         + f" (cited page itself in context: {_pct(a['det_claim_page_in_context'])})"),
        ("Judge citation precision (SUPPORTED share of kept claims)",
         f"{_pct(a['judge_citation_precision'])} of {a['judge_claims_judged']} ({a['judge_verdicts']})"),
        ("Judge: quote alone sufficient", _pct(a["judge_quote_alone_sufficient"])),
        ("Judge decision-relevance mean (1-3)", f"{a['judge_decision_relevance_mean']} {a['judge_decision_relevance_dist']}"),
        ("Judge faithfulness violations (statements) / summaries affected",
         f"{a['judge_faithfulness_violations']} / {a['judge_summaries_with_violation']}"),
        ("Judge misattributions", a["judge_misattributions"]),
        ("Judge coverage mean (1-5) / clarity mean (1-5)", f"{a['judge_coverage_mean']} / {a['judge_clarity_mean']}"),
        ("Removed summary sentences the judge says were true", a["judge_removed_sentences_supported"]
         + f" (their triggering figures were in the model's context: {a['removed_sentences_figures_in_context']})"),
        ("Dropped claims the judge says were true (by reason)", a["judge_dropped_supported_by_reason"] or "none"),
        ("Generator latency mean / max", f"{a['latency_ms_mean']} ms / {a['latency_ms_max']} ms"),
        ("Generator cost per summary (mean) / total", f"${a['cost_per_summary_mean']} / ${a['cost_total_generator']}"),
        ("Judge cost total", f"${a['cost_total_judge']}"),
        ("Negative controls caught", ", ".join(f"{k}: {'NOT RUN (judge error)' if v is None else 'YES' if v else 'NO'}"
                                               for k, v in a["negative_controls"].items())),
    ]
    L += [f"| {k} | {v} |" for k, v in rows]
    w("")
    w("Reply wording checks (pass / fail / n.a. across cases):\n")
    w("| Check | pass | fail | n/a |\n|---|---|---|---|")
    for name, c in a["det_reply_checks"].items():
        w(f"| {name} | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('n/a', 0)} |")
    w("")

    w("## Problems found and suggested fixes\n")
    for i, p in enumerate(PROBLEMS, 1):
        w(f"{i}. **{p['title']}** ({p['severity']})  \n   Evidence: {p['evidence']}  \n   Fix: {p['fix']}")
    w("")

    w("## Negative controls (judge calibration)\n")
    for n in r["negative_controls"]:
        v = (n["judge"].get("verdict") or {})
        status = "NOT RUN: " + _q(n["error"], 160) if n["caught"] is None else "YES" if n["caught"] else "NO"
        w(f"- **{n['name']}** on `{n['case']}`: caught = **{status}**")
        if "injected" in n:
            w(f"  - injected {n['injected']} in place of {n['original']}")
            w(f"  - judge flagged: {[_q(s['statement'] + ' :: ' + s['problem'], 300) for s in v.get('unsupported_statements', [])]}")
        else:
            w(f"  - {n['how']}: \"{_q(n['altered_claim'])}\" with quote \"{_q(n['quote'], 250)}\"")
            w(f"  - judge verdict {v.get('verdict')}: {_q(v.get('reasoning', ''), 400)}")
    w("")

    w("## Per case\n")
    w("| Case | docs (uncited) | gen | latency | cost | kept/dropped | removed sent. | det fails | judge prec. | rel | viol | cov | clar |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in cases:
        g = c["generation"]
        kv = [k["judge"]["verdict"] for k in g["kept"] if "verdict" in k["judge"]]
        sup = sum(v["verdict"] == "SUPPORTED" for v in kv)
        sj = c["judge_summary"].get("verdict") if c["generation"]["summary"] else None
        fails = [ch["name"] for ch in c["checks"]["reply"] if ch["ok"] is False]
        fails += ["quote"] * sum(not q["ok"] for q in c["checks"]["quotes"])
        fails += ["summary_fig"] if not c["checks"]["summary_figures"]["ok"] else []
        fails += ["doc"] * sum(not d["ok"] for d in c["checks"]["docs"])
        drops = ",".join(sorted({d["reason"] for d in g["dropped"]}))
        w(f"| {c['case']}{' (extra)' if c['extra'] else ''} | {c['files']} ({len(c['uncited'])}) | {g['status']} "
          f"| {g['llm'].get('latency_ms', '-')} | {g['llm'].get('cost', '-')} | {len(g['kept'])}/{len(g['dropped'])} "
          f"{('(' + drops + ')') if drops else ''} | {len(g['removed_sentences'])} | {', '.join(fails) or '-'} "
          f"| {f'{sup}/{len(kv)}' if kv else '-'} | {round(sum(v['decision_relevance'] for v in kv) / len(kv), 1) if kv else '-'} "
          f"| {len(sj['unsupported_statements']) if sj else '-'} | {sj['coverage'] if sj else '-'} | {sj['clarity'] if sj else '-'} |")
    w("")

    w("### Claims the judge did not rate SUPPORTED\n")
    for c in cases:
        for k in c["generation"]["kept"]:
            v = k["judge"].get("verdict") or {}
            if v and v["verdict"] != "SUPPORTED":
                w(f"- `{c['case']}` {v['verdict']}: \"{k['claim']}\" (doc {k['doc_external_id']} p{k['page']}; quote "
                  f"\"{_q(k['quote'], 200)}\"). Judge: {_q(v['reasoning'], 350)}")
    w("")
    w("### Summary statements the judge flagged as unsupported\n")
    for c in cases:
        v = c["judge_summary"].get("verdict") if c["generation"]["summary"] else None
        for u in (v or {}).get("unsupported_statements", []):
            w(f"- `{c['case']}`: \"{_q(u['statement'], 250)}\": {_q(u['problem'], 250)}")
    w("")
    w("### Sentences removed by the figure filter\n")
    for c in cases:
        jv = {x["index"]: x for x in ((c["judge_summary"].get("verdict") or {}).get("removed_sentences") or [])}
        for i, t in enumerate(c["checks"]["removed_sentence_triggers"]):
            j = jv.get(i)
            w(f"- `{c['case']}`: \"{_q(t['sentence'], 300)}\"  \n  triggered by {t['triggers']}; unsupported vs the "
              f"model's own context: {t['unsupported_vs_context'] or 'nothing'}; judge: "
              f"{'true' if j and j['supported'] else 'false' if j else '?'}{(' (' + _q(j['note'], 200) + ')') if j else ''}")
    w("")

    w("### Reply wording failures\n")
    for c in cases:
        for ch in c["checks"]["reply"]:
            if ch["ok"] is False:
                w(f"- `{c['case']}` {ch['name']}: {ch['detail']}")
    w("")
    w("### Matter sentence as rendered (first paragraph of every reply)\n")
    for c in cases:
        first = c["reply_text"].split("\n\n")[1] if "\n\n" in c["reply_text"] else ""
        w(f"- `{c['case']}`: {first}")
    w("")

    w("### Documents the summariser could not cite\n")
    w("| Case | Document | Reason |\n|---|---|---|")
    for c in cases:
        for u in c["uncited"]:
            w(f"| {c['case']} | {u['external_id']} {_q(u['title'], 70)} | {u['uncited_reason']} |")
    w("")
    w("Docs given to the summariser but not selected into context (MAX_CONTEXT_DOCS=4) are listed per case in "
      "results.json (`inventory[].in_context`).\n")

    w("## Worked examples\n")
    for cid in WORKED_EXAMPLES:
        c = next((x for x in cases if x["case"] == cid), None)
        if c is None:
            continue
        g = c["generation"]
        w(f"### {cid}\n")
        w(f"Context: {c['context']['chars']:,} chars from {c['context']['pages']}. Model `{g['llm'].get('model')}`, "
          f"{g['llm'].get('latency_ms')} ms, ${g['llm'].get('cost')}.\n")
        w(f"**Summary (as sent):** {g['summary'] or '_(empty)_'}\n")
        for s in g["removed_sentences"]:
            w(f"- removed by figure filter: \"{s}\"")
        sj = c["judge_summary"].get("verdict")
        if sj:
            w(f"\n**Judge on summary:** coverage {sj['coverage']}, clarity {sj['clarity']}, misattribution "
              f"{sj['misattribution']}. Key facts per judge: {_q(sj['key_facts_in_documents'], 500)} "
              f"Missing: {_q(sj['coverage_missing'], 300).rstrip('.') or '-'}. Unsupported statements: "
              f"{[_q(s['statement'] + ' :: ' + s['problem'], 300) for s in sj['unsupported_statements']] or 'none'}. "
              f"Removed-sentence verdicts: {sj['removed_sentences'] or '-'}\n")
        w("**Kept claims:**\n")
        for k in g["kept"]:
            jv = k["judge"].get("verdict", {})
            w(f"- {k['claim']}  \n  doc {k['doc_external_id']} p{k['page']} (score {k['score']}): \"{_q(k['quote'], 300)}\"  \n"
              f"  judge: {_jv(k['judge'])}: {_q(jv.get('reasoning', ''), 350)}")
        if g["dropped"]:
            w("\n**Dropped claims:**\n")
            for d in g["dropped"]:
                w(f"- [{d['reason']}{(' ' + d['detail']) if d['detail'] else ''}] {d['claim']}  \n  judge vs cited page: {_jv(d['judge'])}")
        w("\n<details><summary>Reply text</summary>\n\n```\n" + c["reply_text"] + "```\n</details>\n")
    return "\n".join(L) + "\n"
