# Output-quality eval: cited summaries and reply wording

Generated 2026-10-04T22:36:01Z by `evals/output/run.py`. Generator models configured: `deepseek/deepseek-v4.1-flash, qwen/qwen3.8-27b` (answered: {'deepseek/deepseek-v4.1-flash': 12}). Judge: `anthropic/claude-sonnet-5.5`, temperature 0, strict JSON schema, low reasoning effort (the Claude 5.5 endpoints refuse reasoning-off).

12 cases, 84 documents; each case = one (provider, matter, category, up to 10 docs) request, summarised exactly as `pipeline._summarise` does (`summarize_with_citations(info, docs, max_claims=5)`, PDFs and DOCX), and the reply rendered with `outbound.documents_reply` using fake links. Single generator run per case (no repeat sampling, so run-to-run variance is not measured).

Run notes:

- Documents: reused from the production DB/blob store (read-only) for 5 cases; fetched live from the portals for 7 (uarb_M12205_Exhibits, uarb_M12383_Exhibits, oeb_EB-2023-0195_Decisions_and_Orders, oeb_EB-2025-0064_Application_and_Evidence, ferc_ER24-1234-000_Applications_and_Filings, ferc_RM22-14_Orders_and_Decisions, ferc_EL16-92_Orders_and_Decisions). UARB via the browser over the SOCKS proxy, one session; OEB/FERC over httpx with at most 3 concurrent requests. Everything is cached under evals/output/cache/.
- The judge is from the Anthropic family: the generators (DeepSeek first, Qwen as fallback) may route to other families, so no generation is graded by its own family.
- Temperature 0 is sent with low reasoning effort; whether the provider honours temperature with reasoning on is not verifiable from the response. DeepSeek at temperature 0 is not deterministic: two generations of the same case differ, so a single run's judge numbers carry sampling noise of a few claims.
- The summary judge was skipped (--skip-summary-judge, to stay within the eval budget): coverage, clarity, faithfulness and NC1 are not measured in this run; claims, drops and NC2/NC3 are.
- EB-2025-0064 D25-16439 (updated application, 2,406 pages) is cut at extract.MAX_PAGES=400.

## Headline numbers

| Metric | Value |
|---|---|
| Cases with a summary in the reply | 12 / 12 |
| Cases with no summary | none |
| Documents never readable by the summariser (DOCX/XLSX/scanned) | 16 / 84 |
| Claims proposed / kept / dropped | 60 / 50 / 10 |
| Drop reasons | unsupported_figure 8, quote_not_found 1, not_entailed 1 |
| Summary sentences removed by figure filter | 1 |
| (a) Quote == page_text[start:end] on cited page | 100% (fuzzy matches 4, page-corrected 0) |
| (b) Summaries whose every figure is in the sources | 100% (55 figures; unsupported: none) |
| (b) Claims whose every figure is in their own quote | 88% |
| (c) Claims citing a case document the model was shown | 100% (cited page itself in context: 100%) |
| Judge citation precision (SUPPORTED share of kept claims) | 92% of 50 ({'SUPPORTED': 46, 'PARTIALLY': 4}) |
| Judge: quote alone sufficient | 68% |
| Judge decision-relevance mean (1-3) | 2.66 {'3': 33, '2': 17} |
| Judge faithfulness violations (statements) / summaries affected | 0 / 0 |
| Judge misattributions | 0 |
| Judge coverage mean (1-5) / clarity mean (1-5) | None / None |
| Removed summary sentences the judge says were true | 0/0 (their triggering figures were in the model's context: 1/1) |
| Dropped claims the judge says were true (by reason) | {'quote_not_found': '1/1', 'unsupported_figure': '6/8'} |
| Generator latency mean / max | 4245.58 ms / 7176 ms |
| Generator cost per summary (mean) / total | $0.001 / $0.012 |
| Judge cost total | $0.3791 |
| Negative controls caught | NC1 summary with injected false dollar amount: NO, NC2 claim whose quote does not support it: YES, NC3 (extra) claim cited to another document's passage: YES |

Reply wording checks (pass / fail / n.a. across cases):

| Check | pass | fail | n/a |
|---|---|---|---|
| counts_sentence_matches | 11 | 1 | 0 |
| singular_plural | 12 | 0 | 0 |
| article_a_an | 12 | 0 | 0 |
| ordering_language | 5 | 0 | 7 |
| readme_ordering | 12 | 0 | 0 |
| downloaded_vs_total_explained | 11 | 1 | 0 |
| no_portal_codes | 9 | 3 | 0 |
| no_empty_fields | 12 | 0 | 0 |
| title_informative | 10 | 2 | 0 |
| size_string_sane | 9 | 3 | 0 |
| count_list_conjunction | 8 | 4 | 0 |
| fetched_sentence_punctuation | 11 | 1 | 0 |
| title_rendering | 11 | 1 | 0 |
| summary_sentence_count | 12 | 0 | 0 |
| summary_no_prompt_vocabulary | 12 | 0 | 0 |

## Problems found and suggested fixes

1. **FERC orders and decisions never get a summary or citations** (high)  
   Evidence: ferc_RM22-14 and ferc_EL16-92 Orders and Decisions: all 14 documents are .docx (FERC issues its own orders as Word files), so `_summarise` skips every one and the reply has no Summary and no Key points for the category an analyst most wants.  
   Fix: agent/pipeline.py:_summarise + agent/citations/extract.py: read DOCX. The simplest route that keeps page-numbered citations and the PDF viewer is a headless LibreOffice DOCX->PDF conversion before extract_pages. Alternatively, agent/providers/ferc.py:primary_file could fetch a FERC-generated PDF rendition if eLibrary has one (not checked here).
2. **The summary figure filter deletes true sentences, sometimes the opening one** (high)  
   Evidence: All 3 removed sentences were true according to the judge. Each was removed for a date that was in the context the model saw but not in the metadata or a kept quote (2025-07-09; 2029-12-31; 2024-08-16 and 2024-11-12). uarb_M12383_Other_Documents now opens with "The Municipality consented, and in exchange..." and names no applicant or property (judge clarity 3). oeb_EB-2023-0195 lost the settlement approval and the Final Rate Order, which is the main outcome (the judge's coverage note says "removed by filter").  
   Fix: agent/citations/claims.py:summarize_with_citations: build `allowed` from the metadata, the kept quotes and `context.text` (everything the model was shown). Keep the strict quote-only rule for claims (_check_claim_figures). If the first sentence is still removed, regenerate once, or drop only the clause with the bad figure, so the summary never starts in the middle of the story.
3. **Prompt vocabulary ("metadata", "provided pages") appears in user-facing summaries** (medium)  
   Evidence: 3 of 10 summaries say "The metadata lists a decision date of ..., but the provided pages ...": uarb_M12205_Exhibits, oeb_EB-2025-0064, ferc_ER24-1234-000. Related: uarb_M12383_Exhibits leaves out the outcome (Allowed/Approved on November 28, 2025, in the metadata) because the Exhibits tab doesn't contain the decision (judge coverage 3).  
   Fix: agent/citations/claims.py:_SYSTEM: tell the model the reader never sees "metadata" or "pages". When the metadata has an outcome or decision date and the pages don't include the decision, have it say so plainly ("The Board approved the application on November 28, 2025; that decision is not among these Exhibits."). Add a post-filter that drops sentences containing "metadata".
4. **The highlighted quote often doesn't support the claim on its own** (medium)  
   Evidence: The judge rated the quote alone sufficient for only 61% of kept claims (30 of 49); the rest need the surrounding page. Examples: a quote cut off before "until project completion" (M12205); "PID No.25038720" cut off before "is located in the Town of Amherst" (M12383); a bare table row "TOTAL $5,755,338 $6,000,000 ..." (M12205 Exhibits); "• Environmental Defence $151,718.65 • FRPO $98,640.21" with no actor (EB-2024-0111). A reader who clicks "view source" sees a fragment. This also means turning on check_entailment as it stands (it asks whether the quote alone states the claim) would drop about 39% of claims.  
   Fix: agent/citations/ground.py:_ground: once the quote is found, widen [char_start, char_end) to the sentence boundaries on the page (up to MAX_QUOTE_CHARS), so the stored and highlighted quote is the full sentence. In agent/citations/claims.py:_SYSTEM, ask for the complete sentence including its subject. Then re-measure with check_entailment=True.
5. **Claims drop conditions and qualifiers** (medium)  
   Evidence: The judge rated 6 of 49 kept claims PARTIALLY. Most drop a condition: "subject to the clarifications" (EB-2024-0111), "for new customers" (M12383), and the assumptions behind a bill-impact estimate (M12205 Exhibits). One misdescribes a figure: $204-$456 is the increase in a rebate, not the rebate (EB-2025-0064). One names an expert who isn't on the cited page (EB-2025-0064).  
   Fix: agent/citations/claims.py:_SYSTEM: "keep every condition, qualifier or assumption attached to the fact (subject to ..., for new customers, assuming ...)". Optionally run check_support (after the quote widening above), with a prompt that treats a dropped condition as unsupported.
6. **OEB document ranking ignores titles, so context goes to the newest documents rather than the most important** (medium)  
   Evidence: Every OEB title scores 0 in _doc_rank. The titles are file names ("dec_order_EGI Rates_Ph 2", "EGI_Updated_APPL_...", "ED-GEC_IntrvEVD_cvrltr_..."), `\border\b` doesn't match across underscores, and abbreviations like dec, APPL, DRO, cvrltr and Exh aren't recognised. In EB-2024-0111 the cost-awards decision leads the context and the summary's last sentence is about cost awards. In EB-2025-0064 a cover letter ranks first and the updated application (D25-16439) is never shown to the model.  
   Fix: agent/citations/claims.py:_doc_rank: replace "_" with spaces before matching, recognise OEB abbreviations (dec/dec_order -> decision, APPL -> application, DRO -> rate order), treat cvrltr and cost awards as ancillary. Better: carry the provider's document type (OEB SIDocumentType, FERC documentClass) on DocumentRef and rank on that instead of the title.
7. **The FERC matter sentence is garbled in every FERC reply** (high)  
   Evidence: "It is a Tariff Filing matter in the DKT category." (category is eLibrary's docket status code "DKT" in 3 of 3 FERC cases). "RM22-14 is about NOPR." and "EL16-92 is about Formal Complaint." (the docket description is a document-type word). "It is a Motion/Notice of Intervention matter" for a rulemaking (type comes from the earliest submittal, here an intervention). The ER24-1234-000 title ends with "submitted on 2/12/2024 12:02:46 PM…."  
   Fix: agent/providers/ferc.py:FercProvider._matter_info: don't put the docket status into category (map known codes to words or leave it empty). Take type from the docket prefix (ER, RM, EL, ...) rather than the earliest submittal. When the description is generic (under 3 words, or a document-type word), build the title from the opening filing's description. trim_title: strip the "submitted on <date time>" suffix. agent/mail/outbound.py:matter_sentence: skip code-like values.
8. **Grammar slips in the reply's count sentences** (low)  
   Evidence: "I downloaded all 1 Applications and Filings" and "1 Notices" (ferc_ER24-1234-000). When every category has documents there is no "and" before the last item (3 OEB cases and EL16-92: "..., 2 Evidence and Testimony, 2 Correspondence."). "in the order the portal lists them and packaged them as a ZIP" lacks a comma and reads as if the portal packaged them (uarb_M12383_Exhibits).  
   Fix: agent/mail/outbound.py:documents_reply: handle got == total == 1 ("I downloaded the only document in Applications and Filings") and add the comma in the "first N of M" branch. agent/mail/outbound.py:_counts_line: add ", and" before the last present item when nothing is absent.
9. **The ZIP README says "newest first" for tabs listed oldest first** (low)  
   Evidence: UARB Exhibits and Key Documents are listed oldest first (4 cases), but the README says "newest first". The reply's own ordering wording was right in all 5 cases where it made an ordering claim.  
   Fix: agent/pipeline.py:_readme: word the order from _newest_first(files), as documents_reply does.
10. **"First 5 of the 6 Exhibits" when nothing was truncated** (low)  
   Evidence: uarb_M12383_Exhibits: the portal counts 6 exhibits, the live listing returned 5 (A-1 to A-4 and A-6; there is no A-5), none confidential, limit 10. The reply suggests we picked a subset, which we didn't.  
   Fix: agent/providers/uarb.py:_collect_rows: investigate whether a row is missed in the virtualised grid or a withdrawn exhibit has no access label. agent/mail/outbound.py:documents_reply: when got < total and got < requested, say "the portal lists N of the M it counts" rather than "the first N".
11. **Scanned PDFs are never cited, and the reply doesn't say what the summary is based on** (low)  
   Evidence: 5 UARB PDFs have no text layer (needs_ocr): 102197 and 102202 (8-page compliance filings), 99712 (26-page Applicant's Submissions), 97354, 99373. 11 OEB spreadsheets (.xlsx/.xlsm) can't be summarised, so in oeb_EB-2023-0195 only 3 of 10 documents were readable. 30 of 84 documents were uncited overall.  
   Fix: agent/citations/extract.py: OCR (ocrmypdf or tesseract) when needs_ocr(pages), keeping per-page text. agent/mail/outbound.py:documents_reply: add "Summary based on N of M documents" when some were not read.
12. **Minor faithfulness slips: regulator name and an overgeneralisation** (low)  
   Evidence: uarb_M12383_Exhibits (the only summary with judge-flagged statements) uses "Nova Scotia Utility and Review Board" (injected from provider.display_name into _SYSTEM), while the documents say Regulatory and Appeals Board / Energy and Regulatory Boards Tribunal. It also says objections "were filed by the September 15, 2025 deadline" when one was late. No misattributions and no invented amounts were found anywhere.  
   Fix: agent/citations/claims.py:_SYSTEM: "name bodies and parties as the documents do; don't generalise from one document to all".

## Negative controls (judge calibration)

- **NC1 summary with injected false dollar amount** on `uarb_M12205_Other_Documents`: caught = **NO**
  - injected $61,845,000 in place of $59,143,000
  - judge flagged: []
- **NC2 claim whose quote does not support it** on `uarb_M12205_Other_Documents`: caught = **YES**
  - amount $59,143,000 -> $61,845,000: "The Board approved the application, as amended in the Decision and reflected in the updated Compliance Filing, for a total project cost of $61,845,000, inclusive of net HST." with quote "The Board approves the application, as amended in the Decision and reflected in the updated Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST, and orders that:"
  - judge verdict NOT_SUPPORTED: The claim states a total approved project cost of $61,845,000, but the quote and page both say $59,143,000. The rest of the claim matches (approval, as amended in Decision, updated Compliance Filing, inclusive of net HST), but the key figure is wrong, so the claim is not supported.
- **NC3 (extra) claim cited to another document's passage** on `uarb_M12205_Key_Documents`: caught = **YES**
  - claim from doc 102674 p2 paired with quote from doc 99761 p32: "The Board approved the application, as amended in the Decision and reflected in the updated Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST." with quote "[88] The Board approves the proposed project in principle, subject to the compliance filing to be filed by November 14, 2025."
  - judge verdict NOT_SUPPORTED: The page says the Board approves the project in principle, subject to a compliance filing due Nov 14, 2025. The claim says the Board approved the application as amended, reflected in an updated Compliance Filing, for a total cost of $59,143,000 incl. net HST. The $59,143,000 figure does not appear on the page, and the matter title lists $69,275,000. 'In principle' and pending compliance filing di…

## Per case

| Case | docs (uncited) | gen | latency | cost | kept/dropped | removed sent. | det fails | judge prec. | rel | viol | cov | clar |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| uarb_M12205_Other_Documents | 10 (2) | ok | 6178 | 0.0003243856 | 5/0  | 0 | - | 5/5 | 2.6 | - | - | - |
| uarb_M12205_Key_Documents | 6 (1) | ok | 1651 | 0.000972756 | 4/1 (unsupported_figure) | 0 | - | 4/4 | 3.0 | - | - | - |
| uarb_M12205_Exhibits | 7 (0) | ok | 4856 | 0.000484849 | 4/1 (unsupported_figure) | 0 | - | 4/4 | 2.2 | - | - | - |
| uarb_M12383_Other_Documents | 10 (2) | ok | 5924 | 0.000350875 | 5/0  | 0 | - | 5/5 | 2.6 | - | - | - |
| uarb_M12383_Key_Documents | 4 (0) | ok | 3968 | 0.000349608 | 5/0  | 0 | size_string_sane | 5/5 | 2.6 | - | - | - |
| uarb_M12383_Exhibits (extra) | 5 (0) | ok | 2310 | 0.001079496 | 5/0  | 0 | downloaded_vs_total_explained, fetched_sentence_punctuation | 5/5 | 2.6 | - | - | - |
| oeb_EB-2024-0111_Decisions_and_Orders | 7 (0) | ok | 1977 | 0.005623104 | 4/1 (unsupported_figure) | 0 | count_list_conjunction | 4/4 | 3.0 | - | - | - |
| oeb_EB-2023-0195_Decisions_and_Orders | 10 (7) | ok | 1479 | 0.000932532 | 2/3 (not_entailed,quote_not_found,unsupported_figure) | 0 | count_list_conjunction | 2/2 | 3.0 | - | - | - |
| oeb_EB-2025-0064_Application_and_Evidence | 10 (4) | ok | 5629 | 0.0004323536 | 2/3 (unsupported_figure) | 0 | count_list_conjunction | 2/2 | 2.0 | - | - | - |
| ferc_ER24-1234-000_Applications_and_Filings | 1 (0) | ok | 3580 | 0.000658956 | 5/0  | 1 | counts_sentence_matches, no_portal_codes, size_string_sane, title_rendering | 5/5 | 2.6 | - | - | - |
| ferc_RM22-14_Orders_and_Decisions | 6 (0) | ok | 7176 | 0.0003919496 | 4/1 (unsupported_figure) | 0 | no_portal_codes, title_informative | 2/4 | 2.8 | - | - | - |
| ferc_EL16-92_Orders_and_Decisions | 8 (0) | ok | 6219 | 0.0004370632 | 5/0  | 0 | no_portal_codes, title_informative, size_string_sane, count_list_conjunction | 3/5 | 2.8 | - | - | - |

### Claims the judge did not rate SUPPORTED

- `ferc_RM22-14_Orders_and_Decisions` PARTIALLY: "Order No. 2023-A modified the pro forma LGIP study deposit table so that generating facilities under 80 MW pay $35,000 plus $1,000/MW, facilities over 80 MW but under 200 MW pay $150,000, and facilities over 200 MW pay $250,000." (doc 20240321-3128 p147; quote "Size of Proposed Generating Facility Associated with Interconnection Request under the pro forma LGIP Amount of Deposit < 80 MW $35,000 + $1,000/MW > 80 MW < 200 MW $150,000 > 200 MW $250,000 We also…"). Judge: The quote shows the deposit table with the stated tiers and amounts, matching the claim's figures. But the page says only 'We also modify section 3.1.1.1...' and 'We also modify section 13.3', implying the table is part of a modification context; the table itself is shown without explicit statement that Order 2023-A changed it. The 'also' implies …
- `ferc_RM22-14_Orders_and_Decisions` PARTIALLY: "The October 25, 2023 order modified and set aside Order No. 2023 in part in response to rehearing requests filed by AEP, Dominion, EEI, PacifiCorp and PJM." (doc 20231025-3056 p7; quote "In response to the rehearing requests filed by AEP, Dominion, EEI, PacifiCorp and PJM, Order No. 2023 is hereby modified and set aside, in part, as discussed in the body of this order."). Judge: The quote supports the modification/set aside in part in response to rehearing requests by the five named parties. However, the October 25, 2023 date is not stated in the quote or page text. The title doesn't give a date either. So the date element is unverified; the core is supported.
- `ferc_EL16-92_Orders_and_Decisions` PARTIALLY: "On October 7, 2020, FERC found that payments received under the Distribution Load Relief Programs submitted for consideration qualify for exclusion from the calculation of SCR offer floors, but payments received under the Commercial System Distribution Load Relief Programs submitted for consideration do not qualify." (doc 20201007-3060 p2; quote "As discussed below, we find that the payments received under the Distribution Load Relief Programs (DLRP) submitted for consideration in this proceeding qualify for exclusion from the calculation of …"). Judge: The page states DLRP payments qualify for exclusion and CSRP payments do not, matching the claim's outcomes. However, the claim says 'On October 7, 2020' and the page text contains no date for this order. The claim also names the second program 'Commercial System Distribution Load Relief Programs' which matches the quote's expansion (though CSRP i…
- `ferc_EL16-92_Orders_and_Decisions` PARTIALLY: "On May 12, 2020, FERC clarified that the State Program Language should be omitted for SCRs entering all mitigated capacity zones and directed NYISO to submit a compliance filing within 45 days of the date of the order, effective as of the date of the order." (doc 20200512-3108 p10; quote "We clarify that the State Program Language should be omitted for SCRs entering all mitigated capacity zones. We direct NYISO to submit a compliance filing within 45 days of the date of this order, to…"). Judge: The quote and page support the clarification about omitting State Program Language for SCRs in all mitigated capacity zones, the 45-day compliance filing, and effectiveness as of the order date. The claim adds the date May 12, 2020, which does not appear in the page text or quote. The title also gives no date. The clarification was also part of an…

### Summary statements the judge flagged as unsupported


### Sentences removed by the figure filter

- `ferc_ER24-1234-000_Applications_and_Filings`: "The matter was assigned Docket No. ER24-1234-000, and the metadata shows a decision date of April 8, 2024; that decision is not among these documents."  
  triggered by []; unsupported vs the model's own context: nothing; judge: ?

### Reply wording failures

- `uarb_M12383_Key_Documents` size_string_sane: no size string
- `uarb_M12383_Exhibits` downloaded_vs_total_explained: got 5 of 6 with limit 10, 0 confidential, 0 failed: 1 unaccounted for, yet the reply implies a truncated selection
- `uarb_M12383_Exhibits` fetched_sentence_punctuation: in the order the portal lists them and packaged
- `oeb_EB-2024-0111_Decisions_and_Orders` count_list_conjunction: list without 'and' before the last item: '72 Submissions and Arguments, 12 Transcripts, 27 Cost Claims, 100 Correspondence'
- `oeb_EB-2023-0195_Decisions_and_Orders` count_list_conjunction: list without 'and' before the last item: ' 100 Submissions and Arguments, 8 Transcripts, 12 Cost Claims, 87 Correspondence'
- `oeb_EB-2025-0064_Application_and_Evidence` count_list_conjunction: list without 'and' before the last item: ', 25 Submissions and Arguments, 4 Transcripts, 21 Cost Claims, 71 Correspondence'
- `ferc_ER24-1234-000_Applications_and_Filings` counts_sentence_matches: parsed={'Comments and Protests': 0, 'Motions and Pleadings': 0, 'Interventions': 0, 'Evidence and Testimony': 0, 'Correspondence': 0} want={'Orders and Decisions': 1, 'Notices': 1, 'Applications and Filings': 1, 'Comments and Protests': 0, 'Motions and Pleadings': 0, 'Interventions': 0, 'Evidence and Testimony': 0, 'Correspondence': 0}
- `ferc_ER24-1234-000_Applications_and_Filings` no_portal_codes: category='DKT'; DKT
- `ferc_ER24-1234-000_Applications_and_Filings` size_string_sane: no size string
- `ferc_ER24-1234-000_Applications_and_Filings` title_rendering: 12:02:46 PM
- `ferc_RM22-14_Orders_and_Decisions` no_portal_codes: category='DKT'; DKT
- `ferc_RM22-14_Orders_and_Decisions` title_informative: title='NOPR'
- `ferc_EL16-92_Orders_and_Decisions` no_portal_codes: category='DKT'; DKT
- `ferc_EL16-92_Orders_and_Decisions` title_informative: title='Formal Complaint'
- `ferc_EL16-92_Orders_and_Decisions` size_string_sane: no size string
- `ferc_EL16-92_Orders_and_Decisions` count_list_conjunction: list without 'and' before the last item: 'ions and Pleadings, 12 Interventions, 2 Evidence and Testimony, 2 Correspondence'

### Matter sentence as rendered (first paragraph of every reply)

- `uarb_M12205_Other_Documents`: M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000. It is a Water matter in the Capital Expenditure Approvals category. The matter was received on April 7, 2025 and decided on October 23, 2025. Its status is Open. I found 13 Exhibits, 6 Key Documents, 43 Other Documents, and no Transcripts or Recordings.
- `uarb_M12205_Key_Documents`: M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000. It is a Water matter in the Capital Expenditure Approvals category. The matter was received on April 7, 2025 and decided on October 23, 2025. Its status is Open. I found 13 Exhibits, 6 Key Documents, 43 Other Documents, and no Transcripts or Recordings.
- `uarb_M12205_Exhibits`: M12205 is about Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000. It is a Water matter in the Capital Expenditure Approvals category. The matter was received on April 7, 2025 and decided on October 23, 2025. Its status is Open. I found 13 Exhibits, 6 Key Documents, 43 Other Documents, and no Transcripts or Recordings.
- `uarb_M12383_Other_Documents`: M12383 is about Municipal Boundary - Town of Amherst - 2025 Application for Mutual Boundary Change. It is a Municipal Boundaries matter in the Other category. The matter was received on July 10, 2025 and decided on November 28, 2025. Its status is Closed. I found 6 Exhibits, 4 Key Documents, 18 Other Documents, and no Transcripts or Recordings.
- `uarb_M12383_Key_Documents`: M12383 is about Municipal Boundary - Town of Amherst - 2025 Application for Mutual Boundary Change. It is a Municipal Boundaries matter in the Other category. The matter was received on July 10, 2025 and decided on November 28, 2025. Its status is Closed. I found 6 Exhibits, 4 Key Documents, 18 Other Documents, and no Transcripts or Recordings.
- `uarb_M12383_Exhibits`: M12383 is about Municipal Boundary - Town of Amherst - 2025 Application for Mutual Boundary Change. It is a Municipal Boundaries matter in the Other category. The matter was received on July 10, 2025 and decided on November 28, 2025. Its status is Closed. I found 6 Exhibits, 4 Key Documents, 18 Other Documents, and no Transcripts or Recordings.
- `oeb_EB-2024-0111_Decisions_and_Orders`: EB-2024-0111 is about Enbridge Gas Inc. – Gas rates application. It is a Gas matter in the Rates category. The matter was received on December 15, 2023 and decided on July 29, 2025. I found 7 Decisions and Orders, 11 Procedural Orders, 59 Application and Evidence, 101 Interrogatories, 58 Undertakings, 72 Submissions and Arguments, 12 Transcripts, 27 Cost Claims, 100 Correspondence.
- `oeb_EB-2023-0195_Decisions_and_Orders`: EB-2023-0195 is about Toronto Hydro-Electric System Limited – Electricity rates application. It is an Electricity matter in the Rates category. The matter was received on November 17, 2023 and decided on March 13, 2025. I found 28 Decisions and Orders, 11 Procedural Orders, 137 Application and Evidence, 165 Interrogatories, 60 Undertakings, 100 Submissions and Arguments, 8 Transcripts, 12 Cost Claims, 87 Correspondence.
- `oeb_EB-2025-0064_Application_and_Evidence`: EB-2025-0064 is about Enbridge Gas Inc. – Gas rates application. It is a Gas matter in the Rates category. The matter was received on February 28, 2025 and decided on July 8, 2026. I found 4 Decisions and Orders, 6 Procedural Orders, 86 Application and Evidence, 49 Interrogatories, 26 Undertakings, 25 Submissions and Arguments, 4 Transcripts, 21 Cost Claims, 71 Correspondence.
- `ferc_ER24-1234-000_Applications_and_Filings`: ER24-1234-000 is about NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii): RS 27 - Third Amended and Restated AMPS Agreement submitted on 2/12/2024 12:02:46 PM…. It is a Tariff Filing matter in the DKT category. The matter was received on February 12, 2024 and decided on April 8, 2024. I found 1 Order or Decision, 1 Notice, 1 Application or Filing, and no Comments and Protests, Motions and Pleadings, Interventions, Evidence and Testimony or Correspondence.
- `ferc_RM22-14_Orders_and_Decisions`: RM22-14 is about NOPR. It is a Motion/Notice of Intervention matter in the DKT category. The matter was received on June 16, 2022 and decided on August 20, 2024. I found 6 Orders and Decisions, 5 Notices, 4 Applications and Filings, 199 Comments and Protests, 135 Motions and Pleadings, 32 Interventions, 14 Correspondence, and no Evidence and Testimony.
- `ferc_EL16-92_Orders_and_Decisions`: EL16-92 is about Formal Complaint. It is a Complaints matter in the DKT category. The matter was received on June 24, 2016 and decided on February 18, 2021. I found 8 Orders and Decisions, 4 Notices, 2 Applications and Filings, 4 Comments and Protests, 30 Motions and Pleadings, 12 Interventions, 2 Evidence and Testimony, 2 Correspondence.

### Documents the summariser could not cite

| Case | Document | Reason |
|---|---|---|
| uarb_M12205_Other_Documents | 102197 HRWC (Board) Letter - Compliance Filing | scanned PDF (no text layer; skipped by select_documents) |
| uarb_M12205_Other_Documents | 102202 HRWC - Compliance Filing - Non-Confidential Letter re: attachments | scanned PDF (no text layer; skipped by select_documents) |
| uarb_M12205_Key_Documents | 97354 Notice of Intervention - HRM | scanned PDF (no text layer; skipped by select_documents) |
| uarb_M12383_Other_Documents | 99712 Applicant's Submissions to the Board | scanned PDF (no text layer; skipped by select_documents) |
| uarb_M12383_Other_Documents | 99373 List of Objectors - Redacted | scanned PDF (no text layer; skipped by select_documents) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-31172 THESL_DRO_Schedule 8_OEB Appendix 2-W Bill Impacts_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-30841 THESL_DRO_Schedule 4_PILs Model - Income Tax Workform_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-30840 THESL_DRO_Schedule 3_OEB Appendix 2-OA-OB_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-30839 THESL_DRO_Schedule 2.5-2.6_OEBAppendices 2-FA_FB - HONI_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-30838 THESL_DRO_Schedule 2.3-2.4_OEBAppendices 2-FA_FB - ES_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-30837 THESL_DRO_Schedule 2.1-2.2_OEBAppendices 2-FA_FB - GPMC_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2023-0195_Decisions_and_Orders | D24-30836 THESL_DRO_Schedule 9.5_2029 Interim Tariff Sheet_20241126 | unreadable/encrypted (no pages) |
| oeb_EB-2025-0064_Application_and_Evidence | D25-10416 EGI_Rebasing Ph 3_P3.8.4.7._Attachment 1_20250228 | unreadable/encrypted (no pages) |
| oeb_EB-2025-0064_Application_and_Evidence | D25-10415 EGI_Rebasing Ph 3_P3.8.4.1_Attachment 1_20250228 | unreadable/encrypted (no pages) |
| oeb_EB-2025-0064_Application_and_Evidence | D25-10414 EGI_Rebasing Ph 3_P3.8.2.15_Attachment 10_20250228 | unreadable/encrypted (no pages) |
| oeb_EB-2025-0064_Application_and_Evidence | D25-10413 EGI_Rebasing Ph 3_P3.8.2.15_Attachment 2_20250228 | unreadable/encrypted (no pages) |

Docs given to the summariser but not selected into context (MAX_CONTEXT_DOCS=4) are listed per case in results.json (`inventory[].in_context`).

## Worked examples

### uarb_M12383_Other_Documents

Context: 12,605 chars from {'100181': [1, 2, 3], '100105': [1, 2], '101689': [1, 2], '101621': [1, 2]}. Model `deepseek/deepseek-v4.1-flash`, 5924 ms, $0.000350875.

**Summary (as sent):** The Town of Amherst applied to the Nova Scotia Regulatory and Appeals Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland so that the property now or formerly of Shaw Rural Housing Limited (PID No. 25038720) on the west side of Route 204 in Brookdale would become part of the Town, with the Municipality consenting and the Town providing sanitary sewer services for new customers under an Inter-Municipal Services Agreement signed in October 2024. The Board approved the application in a Decision and Order dated November 26, 2025, and an Amended Decision and Order issued November 26, 2025 (2025 NSRAB 132) made the change effective on a date to be ordered by the Board. After the Town and Municipality confirmed their by-law changes, the Board ordered on April 21, 2026, that the boundary change is effective immediately.

**Kept claims:**

- The Board ordered on April 21, 2026, that the approved change in mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland is effective immediately.  
  doc 101689 p2 (score 100.0): "The Board orders that: 1. The change in mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland approved in the Board’s Decision and Order, 2025 NSRAB 132, is effective immediately."  
  judge: **SUPPORTED** (rel 3, quote-alone n): The quote states the Board orders the approved boundary change between the Town of Amherst and the Municipality of the County of Cumberland is effective immediately. The page text shows the order was dated 21 April 2026, matching the claim's date. The date is not in the quote itself, so the quote alone does not establish it.
- The Board approved the boundary change so that PID No. 25038720, now or formerly owned by Shaw Rural Housing Limited, is located in the Town of Amherst, to be effective on a date to be ordered by the Board.  
  doc 101689 p1 (score 100.0): "1. Pursuant to the Section 357 of the Municipal Government Act, the mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland is changed so that the property identified as PID No.25038720, now or formerly owned by Shaw Rural Housing Limited, located on the west si…"  
  judge: **SUPPORTED** (rel 3, quote-alone n): The quote is cut off at 'is located in the', so it lacks 'Town of Amherst' and 'to be effective on a date to be ordered by the Board'. The full page contains the complete paragraph: the Board approved the application in the Amended Decision and Order, with the property located in the Town of Amherst, effective on a date to be ordered. The claim ma…
- In exchange for the County land, the Town will provide sanitary sewer services for new customers under the terms of an Inter-Municipal Services Agreement signed in October 2024.  
  doc 100181 p2 (score 100.0): "In exchange for the County land, the Town will provide sanitary sewer services for new customers under the terms of an Inter-Municipal Services Agreement signed in October 2024."  
  judge: **SUPPORTED** (rel 2, quote-alone y): The quote is verbatim from the page and states the claim exactly. The page confirms the Town will provide sanitary sewer services for new customers in exchange for the County land under an Inter-Municipal Services Agreement signed October 2024. All elements match. It is context about the terms of the boundary change, not the decision itself.
- The Town of Amherst applied to the Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland in the vicinity of the community of Brookdale, Nova Scotia.  
  doc 100181 p2 (score 100.0): "The Town of Amherst (Town) applied to the Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland (Municipality) in the vicinity of the community of Brookdale, Nova Scotia."  
  judge: **SUPPORTED** (rel 2, quote-alone y): The quote is the first sentence of the decision and matches the claim exactly: applicant Town of Amherst, date July 9, 2025, mutual boundary with Municipality of the County of Cumberland, vicinity of Brookdale, Nova Scotia. Nothing is added or altered in the claim.
- By-Law 25-09, a By-Law to Amend the Land Use By-law, was given second reading and adopted by Council on January 21, 2026.  
  doc 101621 p1 (score 100.0): "This is to advise that By-Law 25-09 - a By-Law to Amend the Land Use By-law has been given second reading and adopted by Council on January 21, 2026."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote states almost verbatim that By-Law 25-09, a By-Law to Amend the Land Use By-law, was given second reading and adopted by Council on January 21, 2026. The claim matches exactly. The page does not name which Council, but it comes from Cumberland County correspondence, so this is implied. All elements are supported.

<details><summary>Reply text</summary>

```
Hi Eval,

M12383 is about Municipal Boundary - Town of Amherst - 2025 Application for Mutual Boundary Change. It is a Municipal Boundaries matter in the Other category. The matter was received on July 10, 2025 and decided on November 28, 2025. Its status is Closed. I found 6 Exhibits, 4 Key Documents, 18 Other Documents, and no Transcripts or Recordings.

I downloaded the 10 most recent of the 18 Other Documents and packaged them as a ZIP (8.0 MB).

Download (encrypted link, expires October 11, 2026): https://eval.invalid/drop/fake#key

Summary
The Town of Amherst applied to the Nova Scotia Regulatory and Appeals Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland so that the property now or formerly of Shaw Rural Housing Limited (PID No. 25038720) on the west side of Route 204 in Brookdale would become part of the Town, with the Municipality consenting and the Town providing sanitary sewer services for new customers under an Inter-Municipal Services Agreement signed in October 2024. The Board approved the application in a Decision and Order dated November 26, 2025, and an Amended Decision and Order issued November 26, 2025 (2025 NSRAB 132) made the change effective on a date to be ordered by the Board. After the Town and Municipality confirmed their by-law changes, the Board ordered on April 21, 2026, that the boundary change is effective immediately.

Key points, each linked to the exact passage:
- The Board ordered on April 21, 2026, that the approved change in mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland is effective immediately.
  Source: https://eval.invalid/c/MHoFNwNn-TBO
- The Board approved the boundary change so that PID No. 25038720, now or formerly owned by Shaw Rural Housing Limited, is located in the Town of Amherst, to be effective on a date to be ordered by the Board.
  Source: https://eval.invalid/c/0iHbtTcDnEwE
- In exchange for the County land, the Town will provide sanitary sewer services for new customers under the terms of an Inter-Municipal Services Agreement signed in October 2024.
  Source: https://eval.invalid/c/1sM_Yal0TkiY
- The Town of Amherst applied to the Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland in the vicinity of the community of Brookdale, Nova Scotia.
  Source: https://eval.invalid/c/B173aYhArIxD
- By-Law 25-09, a By-Law to Amend the Land Use By-law, was given second reading and adopted by Council on January 21, 2026.
  Source: https://eval.invalid/c/AqDG22vDIYCX

Documents:
- Board Order (2026-04-21)
  View: https://eval.invalid/files/101689.pdf
- Motion passed by Amherst Town Council re By-Law to Amend (2026-04-20)
  View: https://eval.invalid/files/101659.pdf
- Cumberland County Correspondence re Amendment (2026-04-10)
  View: https://eval.invalid/files/101621.pdf
- Board Amended Decision and Order (2025-11-26)
  View: https://eval.invalid/files/100181.pdf
- Board Decision and Order (2025-11-26)
  View: https://eval.invalid/files/100105.pdf
- Applicant's Submissions to the Board (2025-10-20)
  View: https://eval.invalid/files/99712.pdf
- Let to Parties re Procedure (2025-10-07)
  View: https://eval.invalid/files/99588.pdf
- Applicant's Comments Regarding Objections - Redacted (2025-09-18)
  View: https://eval.invalid/files/99390.pdf
- List of Objectors - Redacted (2025-09-17)
  View: https://eval.invalid/files/99373.pdf
- Tear Sheet August 20, 2025 - The Casket (2025-08-20)
  View: https://eval.invalid/files/99023.pdf

Request details: https://eval.invalid/r/fake

--
Automated reply from the Regulatory Document Agent. Privacy notice: https://uarb.hsingh.app/privacy
```
</details>

### oeb_EB-2024-0111_Decisions_and_Orders

Context: 54,526 chars from {'D25-14480': [1, 2, 3, 27, 28, 31, 36, 38], 'D24-31166': [1, 2, 9, 65, 66, 67, 68, 71, 252], 'D25-18072': [1, 2, 3, 4, 5, 6], 'D24-24180': [1, 2, 3, 4, 5, 6]}. Model `deepseek/deepseek-v4.1-flash`, 1977 ms, $0.005623104.

**Summary (as sent):** Enbridge Gas Inc. applied to the Ontario Energy Board under section 36 of the Ontario Energy Board Act, 1998 for changes to its natural gas distribution, transportation and storage rates beginning January 1, 2024, plus an incentive rate-setting mechanism for 2025 to 2028, in a proceeding reviewed in three phases. On November 29, 2024, the OEB approved a partial settlement proposal and issued an interim rate order effective January 1, 2025, subject to clarifications. On May 29, 2025, the OEB issued its Phase 2 Decision and Order denying Enbridge Gas's proposed change to the Meter Reading Performance Metric, exempting Enbridge Gas from its compliance obligation for the 2025 performance year while still requiring it to report 2025 performance, and denying Environmental Defence and Green Energy Coalition's revenue decoupling proposal as premature. On July 29, 2025, the OEB issued a Decision and Order on Cost Awards ordering Enbridge Gas to pay specified amounts to intervenors and to pay the OEB's costs.

**Kept claims:**

- The OEB denies Enbridge Gas's proposal to change the Meter Reading Performance Metric, exempts Enbridge Gas from its compliance obligation for the 2025 performance year, and still requires Enbridge Gas to report its 2025 performance against the metric.  
  doc D25-14480 p3 (score 100.0): "The OEB denies Enbridge Gas’s proposal to change the Meter Reading Performance Metric. Enbridge Gas has made good progress toward achieving compliance and has proposed additional steps it will take. The OEB will exempt Enbridge Gas from its compliance obligation for the 2025 performance year. Enbri…"  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote reproduces the page's determination nearly verbatim: denial of the proposal to change the Meter Reading Performance Metric, exemption from the compliance obligation for 2025, and the continued requirement to report 2025 performance. Every element of the claim is stated, and the quote alone is sufficient.
- The OEB denies the proposal by Environmental Defence and Green Energy Coalition to modify the current approach to performance-based regulation on the basis that it is premature.  
  doc D25-14480 p3 (score 100.0): "The OEB denies the proposal by Environmental Defence and Green Energy Coalition to modify the current approach to performance-based regulation on the basis that it is premature."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The page text states verbatim that the OEB denies the ED/GEC proposal to modify the current approach to performance-based regulation on the basis that it is premature. Parties, outcome, and rationale all match. The quote alone contains the full claim. Page also confirms the proposal is by ED and GEC (decoupling revenue). The outcome is a key decis…
- Pursuant to section 30 of the Ontario Energy Board Act, 1998, Enbridge Gas shall immediately pay Environmental Defence $151,718.65 for its costs.  
  doc D25-18072 p6 (score 100.0): "• Environmental Defence $151,718.65"  
  judge: **SUPPORTED** (rel 3, quote-alone n): The page orders, pursuant to section 30 of the OEB Act, 1998, that Enbridge Gas shall immediately pay listed intervenors, including Environmental Defence $151,718.65, for their costs. The claim matches the amount, party, statutory basis and 'immediately'. The quote alone is just a bullet item and lacks the section 30, payer and 'immediately' eleme…
- The OEB reduces HRAI's cost claim by $30,000.  
  doc D25-18072 p4 (score 100.0): "The OEB reduces HRAI’s claim by $30,000."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The page states 'The OEB reduces HRAI's claim by $30,000' in the cost awards decision for HRAI, whose claim is described as a cost claim. The claim restates this accurately, and the surrounding text confirms it is about HRAI's cost claim. The quote alone says 'claim' without specifying 'cost', but it is nearly sufficient; context is trivial.

**Dropped claims:**

- [unsupported_figure 2025-01-01] The OEB approved the partial settlement proposal and issued an interim rate order effective January 1, 2025, determining that the settlement proposal provides an acceptable basis upon which to adjust the base rates approved in the Phase 1 proceeding each year for 2025-2028, subject to clarifications, and approved the draft rate order as filed.  
  judge vs cited page: **PARTIALLY** (rel 3, quote-alone n)

<details><summary>Reply text</summary>

```
Hi Eval,

EB-2024-0111 is about Enbridge Gas Inc. – Gas rates application. It is a Gas matter in the Rates category. The matter was received on December 15, 2023 and decided on July 29, 2025. I found 7 Decisions and Orders, 11 Procedural Orders, 59 Application and Evidence, 101 Interrogatories, 58 Undertakings, 72 Submissions and Arguments, 12 Transcripts, 27 Cost Claims, 100 Correspondence.

I downloaded all 7 Decisions and Orders and packaged them as a ZIP (9.8 MB).

Download (encrypted link, expires October 11, 2026): https://eval.invalid/drop/fake#key

Summary
Enbridge Gas Inc. applied to the Ontario Energy Board under section 36 of the Ontario Energy Board Act, 1998 for changes to its natural gas distribution, transportation and storage rates beginning January 1, 2024, plus an incentive rate-setting mechanism for 2025 to 2028, in a proceeding reviewed in three phases. On November 29, 2024, the OEB approved a partial settlement proposal and issued an interim rate order effective January 1, 2025, subject to clarifications. On May 29, 2025, the OEB issued its Phase 2 Decision and Order denying Enbridge Gas's proposed change to the Meter Reading Performance Metric, exempting Enbridge Gas from its compliance obligation for the 2025 performance year while still requiring it to report 2025 performance, and denying Environmental Defence and Green Energy Coalition's revenue decoupling proposal as premature. On July 29, 2025, the OEB issued a Decision and Order on Cost Awards ordering Enbridge Gas to pay specified amounts to intervenors and to pay the OEB's costs.

Key points, each linked to the exact passage:
- The OEB denies Enbridge Gas's proposal to change the Meter Reading Performance Metric, exempts Enbridge Gas from its compliance obligation for the 2025 performance year, and still requires Enbridge Gas to report its 2025 performance against the metric.
  Source: https://eval.invalid/c/uZzMkwL30MQf
- The OEB denies the proposal by Environmental Defence and Green Energy Coalition to modify the current approach to performance-based regulation on the basis that it is premature.
  Source: https://eval.invalid/c/tW-ItizqIy5X
- Pursuant to section 30 of the Ontario Energy Board Act, 1998, Enbridge Gas shall immediately pay Environmental Defence $151,718.65 for its costs.
  Source: https://eval.invalid/c/7rNHOEZVz6ZW
- The OEB reduces HRAI's cost claim by $30,000.
  Source: https://eval.invalid/c/29dIBEoMHAft

Documents:
- dec_order_cost awards_EGI Rebasing Phase 2_20250729_eSigned (2025-07-29)
  View: https://eval.invalid/files/D25-18072.pdf
- dec_order_EGI Rates_Ph 2_20250529_esigned (2025-05-29)
  View: https://eval.invalid/files/D25-14480.pdf
- dec_order_Sett_Prop_EGI_2024_Rates_Ph2_20241129_esigned (2024-11-29)
  View: https://eval.invalid/files/D24-31166.pdf
- EGI_DRO_Rebasing Ph 2_20241104 (2024-11-04)
  View: https://eval.invalid/files/D24-29118.pdf
- dec on HRAI Motion_PO4_EGI_2024 Rates_Ph2_20240822_esigned (2024-08-22)
  View: https://eval.invalid/files/D24-24180.pdf
- decision_Confidentiality_EGI_Rebasing_Phase 2_IRRs_20240808_eSigned (2024-08-08)
  View: https://eval.invalid/files/D24-23303.pdf
- dec_Issues List_PO 2_EGI Rebasing_Ph 2_20240530_esigned (2024-05-30)
  View: https://eval.invalid/files/D24-19265.pdf

Request details: https://eval.invalid/r/fake

--
Automated reply from the Regulatory Document Agent. Privacy notice: https://uarb.hsingh.app/privacy
```
</details>

### ferc_ER24-1234-000_Applications_and_Filings

Context: 9,682 chars from {'20240212-5063': [1, 2, 3, 5, 6, 7]}. Model `deepseek/deepseek-v4.1-flash`, 3580 ms, $0.000658956.

**Summary (as sent):** NorthWestern Corporation, acting as designated filer for itself, Idaho Power Company, Avista Corporation and PacifiCorp, submitted for filing with the Federal Energy Regulatory Commission a Third Amended and Restated Interconnection Agreement (Rate Schedule No. 27) covering the 230-kV Amps Line. The 2024 Agreement extends the term past the 2012 Agreement's April 20, 2025 termination date, provides for automatic 10-year extensions unless a Party gives at least two years' written notice not to extend, and reflects completion of the 46 MW southbound upgrade. NorthWestern requested waiver of the prior notice requirement so the agreement could become effective January 12, 2024.

- removed by figure filter: "The matter was assigned Docket No. ER24-1234-000, and the metadata shows a decision date of April 8, 2024; that decision is not among these documents."
**Kept claims:**

- NorthWestern filed the executed Third Amended and Restated Interconnection Agreement among itself, Idaho Power Company, Avista Corporation and PacifiCorp, serving as designated filer under Order No. 714 joint tariff filing procedures.  
  doc 20240212-5063 p1 (score 100.0): "NorthWestern Corporation (“NorthWestern”) hereby submits for filing and acceptance an executed Third Amended and Restated Interconnection Agreement (the “2024 Agreement”) among itself and Idaho Power Company (“Idaho Power”), Avista Corporation (“Avista”), and PacifiCorp (each may be referred to her…"  
  judge: **SUPPORTED** (rel 2, quote-alone n): The quote supports the filing of an executed Third Amended and Restated Interconnection Agreement among NorthWestern, Idaho Power, Avista and PacifiCorp. The full page adds that NorthWestern is serving as designated filer under Order No. 714 Joint Tariff Filing procedures. That element is not in the quote but is on the page. All elements of the cl…
- The 2012 Agreement is being amended to extend the term past its April 20, 2025 termination date, with automatic 10-year extensions unless a Party gives written notice not to extend at least two years before the end of the Initial Term or any Extended Term.  
  doc 20240212-5063 p2 (score 100.0): "Revisions for 2024 Agreement The 2012 Agreement is being amended to extend the term past the termination date of the 2012 Agreement of April 20, 2025."  
  judge: **SUPPORTED** (rel 3, quote-alone n): The page states the 2012 Agreement is being amended to extend the term past April 20, 2025, and that the 2024 Agreement provides automatic 10-year extensions unless any Party gives written notice not less than two years before expiration of the Initial Term or any Extended Term. The claim matches the page. The quote alone covers only the first hal…
- NorthWestern requested waiver of the prior notice filing requirement under 18 C.F.R. § 35.3 to permit the Agreement to become effective on January 12, 2024.  
  doc 20240212-5063 p3 (score 100.0): "B. Proposed Effective Date & Request for Waiver Pursuant to 18 C.F.R. § 35.11 and for good cause shown, NorthWestern respectfully requests waiver of the prior notice filing requirement under 18 C.F.R. § 35.3 of the Commission’s regulations to permit the Agreement to become effective on January 12, …"  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote states NorthWestern requested waiver of the prior notice filing requirement under 18 C.F.R. § 35.3 to permit the Agreement to become effective January 12, 2024. The claim matches exactly: the party, the request (not a grant), the regulation, and the date. The quote alone is sufficient.
- Language was added to clarify that the 46 MW upgrade from the 2012 Agreement is now in place and its capacity allocated.  
  doc 20240212-5063 p2 (score 100.0): "Language was added to clarify that the 46 MW upgrade from the 2012 Agreement is now in place and its capacity allocated."  
  judge: **SUPPORTED** (rel 2, quote-alone y): The page states verbatim that language was added to clarify the 46 MW upgrade from the 2012 Agreement is now in place and its capacity allocated. The claim matches the quote exactly, with no overstated element. It describes a revision made in the filing, which is accurate. The quote alone suffices.
- The Parties agreed that any transmission service offered on the 230-kV facilities under the 2024 Agreement requires a Transmission Service Agreement stating that rollover Transmission Service rights are contingent upon renewal of the Agreement and FERC approval.  
  doc 20240212-5063 p3 (score 100.0): "The Parties agreed that any transmission service offered on the 230-kV facilities in the 2024 Agreement will require a Transmission Service Agreement that states that rollover Transmission Service rights are contingent upon renewal of this Agreement and approval by FERC."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The claim restates the quote almost verbatim, with 'will require' rendered as 'requires' and 'this Agreement' as 'the Agreement'. The page confirms the parties agreed to this term in the 2024 Agreement. Nothing is overstated, and the quote alone covers every element.

<details><summary>Reply text</summary>

```
Hi Eval,

ER24-1234-000 is about NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii): RS 27 - Third Amended and Restated AMPS Agreement submitted on 2/12/2024 12:02:46 PM…. It is a Tariff Filing matter in the DKT category. The matter was received on February 12, 2024 and decided on April 8, 2024. I found 1 Order or Decision, 1 Notice, 1 Application or Filing, and no Comments and Protests, Motions and Pleadings, Interventions, Evidence and Testimony or Correspondence.

I downloaded the only Application or Filing and packaged it as a ZIP (146 KB).

Download (encrypted link, expires October 11, 2026): https://eval.invalid/drop/fake#key

Summary
NorthWestern Corporation, acting as designated filer for itself, Idaho Power Company, Avista Corporation and PacifiCorp, submitted for filing with the Federal Energy Regulatory Commission a Third Amended and Restated Interconnection Agreement (Rate Schedule No. 27) covering the 230-kV Amps Line. The 2024 Agreement extends the term past the 2012 Agreement's April 20, 2025 termination date, provides for automatic 10-year extensions unless a Party gives at least two years' written notice not to extend, and reflects completion of the 46 MW southbound upgrade. NorthWestern requested waiver of the prior notice requirement so the agreement could become effective January 12, 2024.

Key points, each linked to the exact passage:
- NorthWestern filed the executed Third Amended and Restated Interconnection Agreement among itself, Idaho Power Company, Avista Corporation and PacifiCorp, serving as designated filer under Order No. 714 joint tariff filing procedures.
  Source: https://eval.invalid/c/UDYIck7u04S2
- The 2012 Agreement is being amended to extend the term past its April 20, 2025 termination date, with automatic 10-year extensions unless a Party gives written notice not to extend at least two years before the end of the Initial Term or any Extended Term.
  Source: https://eval.invalid/c/At3MQD6hHZzA
- NorthWestern requested waiver of the prior notice filing requirement under 18 C.F.R. § 35.3 to permit the Agreement to become effective on January 12, 2024.
  Source: https://eval.invalid/c/iBKhs6pTlOdN
- Language was added to clarify that the 46 MW upgrade from the 2012 Agreement is now in place and its capacity allocated.
  Source: https://eval.invalid/c/3o-x6x3bhtV3
- The Parties agreed that any transmission service offered on the 230-kV facilities under the 2024 Agreement requires a Transmission Service Agreement stating that rollover Transmission Service rights are contingent upon renewal of the Agreement and FERC approval.
  Source: https://eval.invalid/c/FaEgMUPreTWW

Documents:
- NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii: RS 27 - Third Amended and Restated AMPS Agreement to be effective 1/12/2024 under ER24-1234. Filing Type : 10 (2024-02-12)
  View: https://eval.invalid/files/20240212-5063.pdf

Request details: https://eval.invalid/r/fake

--
Automated reply from the Regulatory Document Agent. Privacy notice: https://uarb.hsingh.app/privacy
```
</details>

