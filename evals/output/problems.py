"""Concrete quality problems found by this eval, with suggested fixes (rendered into report.md).

Curated by hand from results.json of the 2026-10-04 run; each item names the evidence (case ids) and
the code to change (file:function). Re-check the counts here after a rerun."""

PROBLEMS: list[dict] = [
    {
        "title": "FERC orders and decisions never get a summary or citations",
        "severity": "high",
        "evidence": "ferc_RM22-14 and ferc_EL16-92 Orders and Decisions: all 14 documents are .docx (FERC issues its "
        "own orders as Word files), so `_summarise` skips every one and the reply has no Summary and no Key points "
        "for the category an analyst most wants.",
        "fix": "agent/pipeline.py:_summarise + agent/citations/extract.py: read DOCX. The simplest route that keeps "
        "page-numbered citations and the PDF viewer is a headless LibreOffice DOCX->PDF conversion before "
        "extract_pages. Alternatively, agent/providers/ferc.py:primary_file could fetch a FERC-generated PDF "
        "rendition if eLibrary has one (not checked here).",
    },
    {
        "title": "The summary figure filter deletes true sentences, sometimes the opening one",
        "severity": "high",
        "evidence": "All 3 removed sentences were true according to the judge. Each was removed for a date that was in "
        "the context the model saw but not in the metadata or a kept quote (2025-07-09; 2029-12-31; 2024-08-16 and "
        "2024-11-12). uarb_M12383_Other_Documents now opens with \"The Municipality consented, and in exchange...\" "
        "and names no applicant or property (judge clarity 3). oeb_EB-2023-0195 lost the settlement approval and the "
        "Final Rate Order, which is the main outcome (the judge's coverage note says \"removed by filter\").",
        "fix": "agent/citations/claims.py:summarize_with_citations: build `allowed` from the metadata, the kept quotes "
        "and `context.text` (everything the model was shown). Keep the strict quote-only rule for claims "
        "(_check_claim_figures). If the first sentence is still removed, regenerate once, or drop only the clause "
        "with the bad figure, so the summary never starts in the middle of the story.",
    },
    {
        "title": "Prompt vocabulary (\"metadata\", \"provided pages\") appears in user-facing summaries",
        "severity": "medium",
        "evidence": "3 of 10 summaries say \"The metadata lists a decision date of ..., but the provided pages ...\": "
        "uarb_M12205_Exhibits, oeb_EB-2025-0064, ferc_ER24-1234-000. Related: uarb_M12383_Exhibits leaves out the "
        "outcome (Allowed/Approved on November 28, 2025, in the metadata) because the Exhibits tab doesn't contain "
        "the decision (judge coverage 3).",
        "fix": "agent/citations/claims.py:_SYSTEM: tell the model the reader never sees \"metadata\" or \"pages\". When "
        "the metadata has an outcome or decision date and the pages don't include the decision, have it say so "
        "plainly (\"The Board approved the application on November 28, 2025; that decision is not among these "
        "Exhibits.\"). Add a post-filter that drops sentences containing \"metadata\".",
    },
    {
        "title": "The highlighted quote often doesn't support the claim on its own",
        "severity": "medium",
        "evidence": "The judge rated the quote alone sufficient for only 61% of kept claims (30 of 49); the rest need the "
        "surrounding page. Examples: a quote cut off before \"until project completion\" (M12205); \"PID "
        "No.25038720\" cut off before \"is located in the Town of Amherst\" (M12383); a bare table row \"TOTAL "
        "$5,755,338 $6,000,000 ...\" (M12205 Exhibits); \"• Environmental Defence $151,718.65 • FRPO $98,640.21\" "
        "with no actor (EB-2024-0111). A reader who clicks \"view source\" sees a fragment. This also means "
        "turning on check_entailment as it stands (it asks whether the quote alone states the claim) would drop "
        "about 39% of claims.",
        "fix": "agent/citations/ground.py:_ground: once the quote is found, widen [char_start, char_end) to the "
        "sentence boundaries on the page (up to MAX_QUOTE_CHARS), so the stored and highlighted quote is the full "
        "sentence. In agent/citations/claims.py:_SYSTEM, ask for the complete sentence including its subject. Then "
        "re-measure with check_entailment=True.",
    },
    {
        "title": "Claims drop conditions and qualifiers",
        "severity": "medium",
        "evidence": "The judge rated 6 of 49 kept claims PARTIALLY. Most drop a condition: \"subject to the "
        "clarifications\" (EB-2024-0111), \"for new customers\" (M12383), and the assumptions behind a bill-impact "
        "estimate (M12205 Exhibits). One misdescribes a figure: $204-$456 is the increase in a rebate, not the "
        "rebate (EB-2025-0064). One names an expert who isn't on the cited page (EB-2025-0064).",
        "fix": "agent/citations/claims.py:_SYSTEM: \"keep every condition, qualifier or assumption attached to the "
        "fact (subject to ..., for new customers, assuming ...)\". Optionally run check_support (after the quote "
        "widening above), with a prompt that treats a dropped condition as unsupported.",
    },
    {
        "title": "OEB document ranking ignores titles, so context goes to the newest documents rather than the most important",
        "severity": "medium",
        "evidence": "Every OEB title scores 0 in _doc_rank. The titles are file names (\"dec_order_EGI Rates_Ph 2\", "
        "\"EGI_Updated_APPL_...\", \"ED-GEC_IntrvEVD_cvrltr_...\"), `\\border\\b` doesn't match across underscores, "
        "and abbreviations like dec, APPL, DRO, cvrltr and Exh aren't recognised. In EB-2024-0111 the cost-awards "
        "decision leads the context and the summary's last sentence is about cost awards. In EB-2025-0064 a cover "
        "letter ranks first and the updated application (D25-16439) is never shown to the model.",
        "fix": "agent/citations/claims.py:_doc_rank: replace \"_\" with spaces before matching, recognise OEB "
        "abbreviations (dec/dec_order -> decision, APPL -> application, DRO -> rate order), treat cvrltr and cost "
        "awards as ancillary. Better: carry the provider's document type (OEB SIDocumentType, FERC documentClass) "
        "on DocumentRef and rank on that instead of the title.",
    },
    {
        "title": "The FERC matter sentence is garbled in every FERC reply",
        "severity": "high",
        "evidence": "\"It is a Tariff Filing matter in the DKT category.\" (category is eLibrary's docket status code "
        "\"DKT\" in 3 of 3 FERC cases). \"RM22-14 is about NOPR.\" and \"EL16-92 is about Formal Complaint.\" (the "
        "docket description is a document-type word). \"It is a Motion/Notice of Intervention matter\" for a "
        "rulemaking (type comes from the earliest submittal, here an intervention). The ER24-1234-000 title ends "
        "with \"submitted on 2/12/2024 12:02:46 PM….\"",
        "fix": "agent/providers/ferc.py:FercProvider._matter_info: don't put the docket status into category (map "
        "known codes to words or leave it empty). Take type from the docket prefix (ER, RM, EL, ...) rather than "
        "the earliest submittal. When the description is generic (under 3 words, or a document-type word), build "
        "the title from the opening filing's description. trim_title: strip the \"submitted on <date time>\" "
        "suffix. agent/mail/outbound.py:matter_sentence: skip code-like values.",
    },
    {
        "title": "Grammar slips in the reply's count sentences",
        "severity": "low",
        "evidence": "\"I downloaded all 1 Applications and Filings\" and \"1 Notices\" (ferc_ER24-1234-000). When every "
        "category has documents there is no \"and\" before the last item (3 OEB cases and EL16-92: \"..., 2 Evidence "
        "and Testimony, 2 Correspondence.\"). \"in the order the portal lists them and packaged them as a ZIP\" "
        "lacks a comma and reads as if the portal packaged them (uarb_M12383_Exhibits).",
        "fix": "agent/mail/outbound.py:documents_reply: handle got == total == 1 (\"I downloaded the only document in "
        "Applications and Filings\") and add the comma in the \"first N of M\" branch. agent/mail/outbound.py:"
        "_counts_line: add \", and\" before the last present item when nothing is absent.",
    },
    {
        "title": "The ZIP README says \"newest first\" for tabs listed oldest first",
        "severity": "low",
        "evidence": "UARB Exhibits and Key Documents are listed oldest first (4 cases), but the README says \"newest "
        "first\". The reply's own ordering wording was right in all 5 cases where it made an ordering claim.",
        "fix": "agent/pipeline.py:_readme: word the order from _newest_first(files), as documents_reply does.",
    },
    {
        "title": "\"First 5 of the 6 Exhibits\" when nothing was truncated",
        "severity": "low",
        "evidence": "uarb_M12383_Exhibits: the portal counts 6 exhibits, the live listing returned 5 (A-1 to A-4 and "
        "A-6; there is no A-5), none confidential, limit 10. The reply suggests we picked a subset, which we "
        "didn't.",
        "fix": "agent/providers/uarb.py:_collect_rows: investigate whether a row is missed in the virtualised grid "
        "or a withdrawn exhibit has no access label. agent/mail/outbound.py:documents_reply: when got < total and "
        "got < requested, say \"the portal lists N of the M it counts\" rather than \"the first N\".",
    },
    {
        "title": "Scanned PDFs are never cited, and the reply doesn't say what the summary is based on",
        "severity": "low",
        "evidence": "5 UARB PDFs have no text layer (needs_ocr): 102197 and 102202 (8-page compliance filings), 99712 "
        "(26-page Applicant's Submissions), 97354, 99373. 11 OEB spreadsheets (.xlsx/.xlsm) can't be summarised, so "
        "in oeb_EB-2023-0195 only 3 of 10 documents were readable. 30 of 84 documents were uncited overall.",
        "fix": "agent/citations/extract.py: OCR (ocrmypdf or tesseract) when needs_ocr(pages), keeping per-page text. "
        "agent/mail/outbound.py:documents_reply: add \"Summary based on N of M documents\" when some were not read.",
    },
    {
        "title": "Minor faithfulness slips: regulator name and an overgeneralisation",
        "severity": "low",
        "evidence": "uarb_M12383_Exhibits (the only summary with judge-flagged statements) uses \"Nova Scotia Utility "
        "and Review Board\" (injected from provider.display_name into _SYSTEM), while the documents say Regulatory "
        "and Appeals Board / Energy and Regulatory Boards Tribunal. It also says objections \"were filed by the "
        "September 15, 2025 deadline\" when one was late. No misattributions and no invented amounts were found "
        "anywhere.",
        "fix": "agent/citations/claims.py:_SYSTEM: \"name bodies and parties as the documents do; don't "
        "generalise from one document to all\".",
    },
]
