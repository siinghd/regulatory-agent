# Output-quality eval: cited summaries and reply wording (fix pass D, run 1: full judge)

Run 1 of fix pass D, judged in full (claims and summaries). It predates two deterministic claim checks added after it (percentages must be in the quote; a claim may state its cited document's own date); run 2 (report.md) has the final code with claims judged only.

Generated 2026-10-04T22:29:44Z by `evals/output/run.py`. Generator models configured: `deepseek/deepseek-v4.1-flash, qwen/qwen3.8-27b` (answered: {'deepseek/deepseek-v4.1-flash': 12}). Judge: `anthropic/claude-sonnet-5.5`, temperature 0, strict JSON schema, low reasoning effort (the Claude 5.5 endpoints refuse reasoning-off).

12 cases, 84 documents; each case = one (provider, matter, category, up to 10 docs) request, summarised exactly as `pipeline._summarise` does (`summarize_with_citations(info, docs, max_claims=5)`, PDFs and DOCX), and the reply rendered with `outbound.documents_reply` using fake links. Single generator run per case (no repeat sampling, so run-to-run variance is not measured).

Run notes:

- Documents: reused from the production DB/blob store (read-only) for 5 cases; fetched live from the portals for 7 (uarb_M12205_Exhibits, uarb_M12383_Exhibits, oeb_EB-2023-0195_Decisions_and_Orders, oeb_EB-2025-0064_Application_and_Evidence, ferc_ER24-1234-000_Applications_and_Filings, ferc_RM22-14_Orders_and_Decisions, ferc_EL16-92_Orders_and_Decisions). UARB via the browser over the SOCKS proxy, one session; OEB/FERC over httpx with at most 3 concurrent requests. Everything is cached under evals/output/cache/.
- The judge is from the Anthropic family: the generators (DeepSeek first, Qwen as fallback) may route to other families, so no generation is graded by its own family.
- Temperature 0 is sent with low reasoning effort; whether the provider honours temperature with reasoning on is not verifiable from the response. DeepSeek at temperature 0 is not deterministic: two generations of the same case differ, so a single run's judge numbers carry sampling noise of a few claims.
- EB-2025-0064 D25-16439 (updated application, 2,406 pages) is cut at extract.MAX_PAGES=400.

## Headline numbers

| Metric | Value |
|---|---|
| Cases with a summary in the reply | 12 / 12 |
| Cases with no summary | none |
| Documents never readable by the summariser (DOCX/XLSX/scanned) | 16 / 84 |
| Claims proposed / kept / dropped | 60 / 53 / 7 |
| Drop reasons | unsupported_figure 4, quote_not_found 2, not_entailed 1 |
| Summary sentences removed by figure filter | 0 |
| (a) Quote == page_text[start:end] on cited page | 100% (fuzzy matches 2, page-corrected 0) |
| (b) Summaries whose every figure is in the sources | 100% (55 figures; unsupported: none) |
| (b) Claims whose every figure is in their own quote | 96% |
| (c) Claims citing a case document the model was shown | 100% (cited page itself in context: 100%) |
| Judge citation precision (SUPPORTED share of kept claims) | 96% of 53 ({'SUPPORTED': 51, 'PARTIALLY': 2}) |
| Judge: quote alone sufficient | 76% |
| Judge decision-relevance mean (1-3) | 2.74 {'3': 39, '2': 14} |
| Judge faithfulness violations (statements) / summaries affected | 3 / 3 |
| Judge misattributions | 0 |
| Judge coverage mean (1-5) / clarity mean (1-5) | 4.25 / 4.58 |
| Removed summary sentences the judge says were true | 0/0 (their triggering figures were in the model's context: 0/0) |
| Dropped claims the judge says were true (by reason) | {'quote_not_found': '1/2', 'unsupported_figure': '3/4'} |
| Generator latency mean / max | 4109.33 ms / 6414 ms |
| Generator cost per summary (mean) / total | $0.0021 / $0.0249 |
| Judge cost total | $0.816 |
| Negative controls caught | NC1 summary with injected false dollar amount: YES, NC2 claim whose quote does not support it: YES, NC3 (extra) claim cited to another document's passage: YES |

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

- **NC1 summary with injected false dollar amount** on `uarb_M12205_Other_Documents`: caught = **YES**
  - injected $61,845,000 in place of $59,143,000
  - judge flagged: ['approved ... for a total project cost of $61,845,000, inclusive of net HST :: The Order states $59,143,000, not $61,845,000. The $61,845,000 figure does not appear anywhere in the documents.']
- **NC2 claim whose quote does not support it** on `uarb_M12205_Other_Documents`: caught = **YES**
  - amount $59,143,000 -> $61,845,000: "The Board approved the application, as amended in the Decision and reflected in the updated Compliance Filing, for a total project cost of $61,845,000, inclusive of net HST." with quote "The Board approves the application, as amended in the Decision and reflected in the updated Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST, and orders that:"
  - judge verdict NOT_SUPPORTED: The claim states a total approved project cost of $61,845,000, but the quote and page both say $59,143,000. The rest of the claim matches (approval, as amended in Decision, updated Compliance Filing, inclusive of net HST), but the key figure is wrong, so the claim is not supported.
- **NC3 (extra) claim cited to another document's passage** on `uarb_M12205_Key_Documents`: caught = **YES**
  - claim from doc 102674 p2 paired with quote from doc 99761 p32: "The Board approved the application, as amended in the Decision and reflected in the updated Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST." with quote "[88] The Board approves the proposed project in principle, subject to the compliance filing to be filed by November 14, 2025."
  - judge verdict NOT_SUPPORTED: The page says the Board approves the project in principle, subject to a compliance filing due Nov 14, 2025. The claim says the Board approved the application as amended, reflected in an updated Compliance Filing, for a total cost of $59,143,000 incl. net HST. The $59,143,000 figure does not appear on the page, and the matter title lists $69,275,000. 'In principle' and pending compliance filing di…

## Per case

| Case | docs (uncited) | gen | latency | cost | kept/dropped | removed sent. | det fails | judge prec. | rel | viol | cov | clar |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| uarb_M12205_Other_Documents | 10 (2) | ok | 4305 | 0.00086324 | 5/0  | 0 | - | 5/5 | 2.6 | 0 | 4 | 4 |
| uarb_M12205_Key_Documents | 6 (1) | ok | 1979 | 0.0024957 | 4/1 (unsupported_figure) | 0 | - | 4/4 | 3.0 | 0 | 5 | 5 |
| uarb_M12205_Exhibits | 7 (0) | ok | 5659 | 0.000584025 | 5/0  | 0 | - | 5/5 | 2.8 | 0 | 4 | 5 |
| uarb_M12383_Other_Documents | 10 (2) | ok | 6414 | 0.000369175 | 5/0  | 0 | - | 5/5 | 2.8 | 0 | 5 | 4 |
| uarb_M12383_Key_Documents | 4 (0) | ok | 3964 | 0.0008652 | 5/0  | 0 | size_string_sane | 5/5 | 2.4 | 0 | 5 | 5 |
| uarb_M12383_Exhibits (extra) | 5 (0) | ok | 2344 | 0.002713704 | 5/0  | 0 | downloaded_vs_total_explained, fetched_sentence_punctuation | 5/5 | 2.6 | 0 | 4 | 5 |
| oeb_EB-2024-0111_Decisions_and_Orders | 7 (0) | ok | 2437 | 0.00579 | 3/2 (quote_not_found,unsupported_figure) | 0 | count_list_conjunction | 3/3 | 3.0 | 0 | 3 | 4 |
| oeb_EB-2023-0195_Decisions_and_Orders | 10 (7) | ok | 2090 | 0.004599204 | 3/2 (quote_not_found,unsupported_figure) | 0 | count_list_conjunction | 2/3 | 3.0 | 1 | 4 | 5 |
| oeb_EB-2025-0064_Application_and_Evidence | 10 (4) | ok | 4668 | 0.00233912 | 5/0  | 0 | count_list_conjunction | 5/5 | 2.4 | 0 | 4 | 5 |
| ferc_ER24-1234-000_Applications_and_Filings | 1 (0) | ok | 5301 | 0.00080724 | 5/0  | 0 | counts_sentence_matches, no_portal_codes, size_string_sane, title_rendering | 5/5 | 2.8 | 0 | 5 | 4 |
| ferc_RM22-14_Orders_and_Decisions | 6 (0) | ok | 5046 | 0.00165004 | 4/1 (not_entailed) | 0 | no_portal_codes, title_informative | 4/4 | 2.8 | 1 | 4 | 5 |
| ferc_EL16-92_Orders_and_Decisions | 8 (0) | ok | 5105 | 0.0017926328 | 4/1 (unsupported_figure) | 0 | no_portal_codes, title_informative, size_string_sane, count_list_conjunction | 3/4 | 3.0 | 1 | 4 | 4 |

### Claims the judge did not rate SUPPORTED

- `oeb_EB-2023-0195_Decisions_and_Orders` PARTIALLY: "The OEB approves the cost claims filed by all parties except for BOMA, and reduces BOMA's hours claimed by 42%." (doc D25-11069 p4; quote "The OEB approves the cost claims filed by all parties except for BOMA."). Judge: The quote supports the first half: OEB approves cost claims of all parties except BOMA. The claim adds that BOMA's hours claimed are reduced by 42%. The page says BOMA's claim is excessive and does not reflect the value of its participation, but this page gives no 42% figure or any statement that hours were reduced. That detail is unsupported and …
- `ferc_EL16-92_Orders_and_Decisions` PARTIALLY: "On October 7, 2020, FERC concluded that payments received under the Distribution Load Relief Programs submitted for consideration qualify for exclusion from the calculation of SCR offer floors, but payments received under the Commercial System Distribution Load Relief Programs submitted for consideration do not so qualify." (doc 20210218-3092 p3; quote "On October 7, 2020, after consideration of the initial and reply briefs filed in the paper hearing, the Commission concluded that while the payments received under the Distribution Load Relief Progra…"). Judge: The claim says the Commission concluded that payments under the Distribution Load Relief Programs qualify for exclusion, but payments under the "Commercial System Distribution Load Relief Programs" do not. The quote and page say the CSRPs (not defined on this page) do not qualify. The claim invents the expansion "Commercial System Distribution Loa…

### Summary statements the judge flagged as unsupported

- `oeb_EB-2023-0195_Decisions_and_Orders`: "BOMA, whose claim was reduced by 42%": The 42% reduction applies to BOMA's claimed hours, not the dollar claim; the summary's wording is loose, though the decision itself ties the reduction to hours.
- `ferc_RM22-14_Orders_and_Decisions`: "The matter remains in the compliance and rehearing process.": No document states the current status; the Oct 2023 order said remaining rehearing would be addressed later, and Order 2023-A then addressed rehearing, so the status claim is speculative.
- `ferc_EL16-92_Orders_and_Decisions`: "FERC set aside the October 2020 order in part and agreed that CSRP payments merit exclusion from the offer floor calculation": The CSRP holding is not stated in the order text shown (pages 1-3), though it is inferred from Clements' concurrence ('I agree that they do'). Mild inference, not an outright error.

### Sentences removed by the figure filter


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

Context: 12,605 chars from {'100181': [1, 2, 3], '100105': [1, 2], '101689': [1, 2], '101621': [1, 2]}. Model `deepseek/deepseek-v4.1-flash`, 6414 ms, $0.000369175.

**Summary (as sent):** The Town of Amherst applied to the Nova Scotia Regulatory and Appeals Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland so that the property now or formerly of Shaw Rural Housing Limited (PID No. 25038720) on the west side of Route 204 in Brookdale would become part of the Town, with the Municipality consenting and the Town providing sanitary sewer services for new customers under an Inter-Municipal Services Agreement signed in October 2024. The Board approved the application in a Decision and Order dated November 26, 2025, and an Amended Decision and Order (2025 NSRAB 132) made the change effective on a date to be ordered by the Board. After the Municipality's By-Law 25-09 was adopted January 21, 2026 and published January 23, 2026, and the Town passed its motion the same day, the Board ordered on April 21, 2026 that the boundary change is effective immediately.


**Judge on summary:** coverage 5, clarity 4, misattribution False. Key facts per judge: The Board approved the Town of Amherst's application to move PID 25038720 (Shaw Rural Housing Limited, Brookdale) into the Town on Nov 26, 2025, amended to a future effective date, and by Order dated April 21, 2026 made the change effective immediately after by-law approvals. No monetary amounts. Missing: Minor: objections received (six letters) and Board's view that land-use concerns are separate; reason for delayed effective date (by-law/policy approvals) only implicit. Unsupported statements: none. Removed-sentence verdicts: -

**Kept claims:**

- The Board ordered that the approved mutual boundary change between the Town of Amherst and the Municipality of the County of Cumberland is effective immediately.  
  doc 101689 p2 (score 100.0): "The Board orders that: 1. The change in mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland approved in the Board’s Decision and Order, 2025 NSRAB 132, is effective immediately."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote states the Board orders that the change in mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland, approved in 2025 NSRAB 132, is effective immediately. The claim matches on parties, approval status, and timing. Nothing is overstated or omitted.
- In an Amended Decision and Order issued November 26, 2025 (2025 NSRAB 132), the Board approved the application, subject to ordering a subsequent effective date.  
  doc 101689 p1 (score 100.0): "In an Amended Decision and Order issued November 26, 2025 (2025 NSRAB 132), the Board approved the application, subject to ordering a subsequent effective date:"  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote is nearly verbatim the claim's content. The page states the Amended Decision and Order was issued Nov 26, 2025 (2025 NSRAB 132), approving the application subject to ordering a subsequent effective date. The claim matches on date, citation, outcome and condition.
- Under Section 357 of the Municipal Government Act, the mutual boundary is changed so that PID No. 25038720, now or formerly owned by Shaw Rural Housing Limited, is located in the Town of Amherst, to be effective on a date to be ordered by the Board.  
  doc 101689 p1 (score 100.0): "1. Pursuant to the Section 357 of the Municipal Government Act, the mutual boundary between the Town of Amherst and the Municipality of the County of Cumberland is changed so that the property identified as PID No.25038720, now or formerly owned by Shaw Rural Housing Limited, located on the west si…"  
  judge: **SUPPORTED** (rel 3, quote-alone n): The full page text reproduces the Amended Decision and Order paragraph 1 verbatim: Section 357 MGA, mutual boundary changed so PID 25038720, now or formerly owned by Shaw Rural Housing Limited, is located in the Town of Amherst, effective on a date to be ordered by the Board. The claim matches every element. The quote is truncated at 'is located i…
- In exchange for the County land, the Town will provide sanitary sewer services for new customers under the terms of an Inter-Municipal Services Agreement signed in October 2024.  
  doc 100181 p2 (score 100.0): "In exchange for the County land, the Town will provide sanitary sewer services for new customers under the terms of an Inter-Municipal Services Agreement signed in October 2024."  
  judge: **SUPPORTED** (rel 2, quote-alone y): The quote is verbatim from the page and states the claim exactly. The page confirms the Town will provide sanitary sewer services for new customers in exchange for the County land under an Inter-Municipal Services Agreement signed October 2024. All elements match. It is context about the terms of the boundary change, not the decision itself.
- By-Law 25-09, a By-Law to Amend the Land Use By-law, was given second reading and adopted by Council on January 21, 2026, published on January 23, 2026 with no appeals received in the required 14-day period, and is now in effect.  
  doc 101621 p1 (score 100.0): "This is to advise that By-Law 25-09 - a By-Law to Amend the Land Use By-law has been given second reading and adopted by Council on January 21, 2026. It was published on January 23, 2026 and no appeals were received with the required 14- day period. This amendment is now in effect."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote states every element of the claim: By-Law 25-09 amending the Land Use By-law, second reading and adoption on January 21, 2026, publication on January 23, 2026, no appeals within the 14-day period, and now in effect. The claim restates these accurately. The quote does not name the adopting Council as the County's, but the claim just says …

<details><summary>Reply text</summary>

```
Hi Eval,

M12383 is about Municipal Boundary - Town of Amherst - 2025 Application for Mutual Boundary Change. It is a Municipal Boundaries matter in the Other category. The matter was received on July 10, 2025 and decided on November 28, 2025. Its status is Closed. I found 6 Exhibits, 4 Key Documents, 18 Other Documents, and no Transcripts or Recordings.

I downloaded the 10 most recent of the 18 Other Documents and packaged them as a ZIP (8.0 MB).

Download (encrypted link, expires October 11, 2026): https://eval.invalid/drop/fake#key

Summary
The Town of Amherst applied to the Nova Scotia Regulatory and Appeals Board on July 9, 2025, to change its mutual boundary with the Municipality of the County of Cumberland so that the property now or formerly of Shaw Rural Housing Limited (PID No. 25038720) on the west side of Route 204 in Brookdale would become part of the Town, with the Municipality consenting and the Town providing sanitary sewer services for new customers under an Inter-Municipal Services Agreement signed in October 2024. The Board approved the application in a Decision and Order dated November 26, 2025, and an Amended Decision and Order (2025 NSRAB 132) made the change effective on a date to be ordered by the Board. After the Municipality's By-Law 25-09 was adopted January 21, 2026 and published January 23, 2026, and the Town passed its motion the same day, the Board ordered on April 21, 2026 that the boundary change is effective immediately.

Key points, each linked to the exact passage:
- The Board ordered that the approved mutual boundary change between the Town of Amherst and the Municipality of the County of Cumberland is effective immediately.
  Source: https://eval.invalid/c/ufkZoK-LhxIo
- In an Amended Decision and Order issued November 26, 2025 (2025 NSRAB 132), the Board approved the application, subject to ordering a subsequent effective date.
  Source: https://eval.invalid/c/XBmmJXzmigQM
- Under Section 357 of the Municipal Government Act, the mutual boundary is changed so that PID No. 25038720, now or formerly owned by Shaw Rural Housing Limited, is located in the Town of Amherst, to be effective on a date to be ordered by the Board.
  Source: https://eval.invalid/c/ZCsQUQBFyR6v
- In exchange for the County land, the Town will provide sanitary sewer services for new customers under the terms of an Inter-Municipal Services Agreement signed in October 2024.
  Source: https://eval.invalid/c/tXInv4Zc2a4B
- By-Law 25-09, a By-Law to Amend the Land Use By-law, was given second reading and adopted by Council on January 21, 2026, published on January 23, 2026 with no appeals received in the required 14-day period, and is now in effect.
  Source: https://eval.invalid/c/OaecmFOZkvUW

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

Context: 54,526 chars from {'D25-14480': [1, 2, 3, 27, 28, 31, 36, 38], 'D24-31166': [1, 2, 9, 65, 66, 67, 68, 71, 252], 'D25-18072': [1, 2, 3, 4, 5, 6], 'D24-24180': [1, 2, 3, 4, 5, 6]}. Model `deepseek/deepseek-v4.1-flash`, 2437 ms, $0.00579.

**Summary (as sent):** Enbridge Gas Inc. applied to the Ontario Energy Board under section 36 of the Ontario Energy Board Act, 1998 for changes to its natural gas distribution, transportation and storage rates beginning January 1, 2024, plus an incentive rate-setting mechanism for 2025 to 2028, reviewed in three phases. In its November 29, 2024 decision, the OEB approved the partial settlement proposal and the draft rate order as filed, subject to clarifications, and issued an interim rate order effective January 1, 2025. In its May 29, 2025 Phase 2 Decision and Order, the OEB denied Enbridge Gas's proposal to change the Meter Reading Performance Metric, exempting it from the compliance obligation for the 2025 performance year while still requiring reporting, and denied Environmental Defence and Green Energy Coalition's revenue decoupling proposal as premature. On July 29, 2025, the OEB issued a cost awards decision ordering Enbridge Gas to pay specified amounts to 20 intervenors and to pay the OEB's costs; that decision is among these documents.


**Judge on summary:** coverage 3, clarity 4, misattribution False. Key facts per judge: Phase 2 Decision (May 29, 2025) denied the Meter Reading metric change (with 2025 exemption) and the decoupling proposal; the RNG/Lower-Carbon program was not approved as proposed (the OEB declined the financial backstop on ratepayers). Settlement approved Nov 29, 2024 with interim rates. Cost awards (July 29, 2025) paid 20 intervenors, with reductions to HRAI ($30k), FRPO ($25k), CCC, Pollution Probe and BOMA ($10k each), and ED/GEC approved as filed. Missing: The Lower-Carbon Energy Program (RNG) outcome, the cost award amounts and reductions (e.g., HRAI $30k, FRPO $25k, claims of about $1.3M), and the Aug 2024 HRAI motion decision. Unsupported statements: none. Removed-sentence verdicts: -

**Kept claims:**

- The OEB denies Enbridge Gas's proposal to change the Meter Reading Performance Metric, exempts Enbridge Gas from its compliance obligation for the 2025 performance year, and still requires Enbridge Gas to report its 2025 performance against the metric.  
  doc D25-14480 p3 (score 100.0): "The OEB denies Enbridge Gas’s proposal to change the Meter Reading Performance Metric. Enbridge Gas has made good progress toward achieving compliance and has proposed additional steps it will take. The OEB will exempt Enbridge Gas from its compliance obligation for the 2025 performance year. Enbri…"  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote reproduces the page's determination nearly verbatim: denial of the proposal to change the Meter Reading Performance Metric, exemption from the compliance obligation for 2025, and the continued requirement to report 2025 performance. Every element of the claim is stated, and the quote alone is sufficient.
- The OEB approved the settlement proposal as an acceptable basis to adjust the base rates approved in the Phase 1 proceeding each year for 2025-2028, subject to the clarifications set out in the decision.  
  doc D24-31166 p2 (score 100.0): "As a result, the OEB has determined that the settlement proposal provides an acceptable basis upon which to adjust the base rates approved in the Phase 1 proceeding, each year for the period 2025-2028, subject to the clarifications set out below."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote states the OEB determined the settlement proposal provides an acceptable basis to adjust Phase 1 base rates each year 2025-2028, subject to clarifications set out below. The claim paraphrases this accurately. It says 'approved the settlement proposal as an acceptable basis', which matches the determination. The settlement is a partial se…
- The OEB ordered Enbridge Gas to pay the OEB's costs of and incidental to the proceeding immediately upon receipt of the OEB's invoice.  
  doc D25-18072 p6 (score 100.0): "Pursuant to section 30 of the Ontario Energy Board Act, 1998, Enbridge Gas shall pay the OEB’s costs of and incidental to this proceeding immediately upon receipt of the OEB’s invoice."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote is order paragraph 2, which states that Enbridge Gas shall pay the OEB's costs of and incidental to the proceeding immediately upon receipt of the invoice. The claim matches this exactly. 'Ordered' is correct since the page opens with 'THE ONTARIO ENERGY BOARD ORDERS THAT'. The quote alone supports the claim, though it does not say the O…

**Dropped claims:**

- [quote_not_found searched pages [1, 2, 3]] The OEB approved the partial settlement proposal and issued an interim rate order effective January 1, 2025, and approved the draft rate order as filed.  
  judge vs cited page: **PARTIALLY** (rel 3, quote-alone n)
- [unsupported_figure $95,223.50, $151,718.65] The OEB ordered Enbridge Gas to immediately pay the listed cost award amounts to the intervenors, including $151,718.65 to Environmental Defence and $95,223.50 to GEC.  
  judge vs cited page: **SUPPORTED** (rel 3, quote-alone n)

<details><summary>Reply text</summary>

```
Hi Eval,

EB-2024-0111 is about Enbridge Gas Inc. – Gas rates application. It is a Gas matter in the Rates category. The matter was received on December 15, 2023 and decided on July 29, 2025. I found 7 Decisions and Orders, 11 Procedural Orders, 59 Application and Evidence, 101 Interrogatories, 58 Undertakings, 72 Submissions and Arguments, 12 Transcripts, 27 Cost Claims, 100 Correspondence.

I downloaded all 7 Decisions and Orders and packaged them as a ZIP (9.8 MB).

Download (encrypted link, expires October 11, 2026): https://eval.invalid/drop/fake#key

Summary
Enbridge Gas Inc. applied to the Ontario Energy Board under section 36 of the Ontario Energy Board Act, 1998 for changes to its natural gas distribution, transportation and storage rates beginning January 1, 2024, plus an incentive rate-setting mechanism for 2025 to 2028, reviewed in three phases. In its November 29, 2024 decision, the OEB approved the partial settlement proposal and the draft rate order as filed, subject to clarifications, and issued an interim rate order effective January 1, 2025. In its May 29, 2025 Phase 2 Decision and Order, the OEB denied Enbridge Gas's proposal to change the Meter Reading Performance Metric, exempting it from the compliance obligation for the 2025 performance year while still requiring reporting, and denied Environmental Defence and Green Energy Coalition's revenue decoupling proposal as premature. On July 29, 2025, the OEB issued a cost awards decision ordering Enbridge Gas to pay specified amounts to 20 intervenors and to pay the OEB's costs; that decision is among these documents.

Key points, each linked to the exact passage:
- The OEB denies Enbridge Gas's proposal to change the Meter Reading Performance Metric, exempts Enbridge Gas from its compliance obligation for the 2025 performance year, and still requires Enbridge Gas to report its 2025 performance against the metric.
  Source: https://eval.invalid/c/NuNc35KvLZAv
- The OEB approved the settlement proposal as an acceptable basis to adjust the base rates approved in the Phase 1 proceeding each year for 2025-2028, subject to the clarifications set out in the decision.
  Source: https://eval.invalid/c/5OASQp2rasoK
- The OEB ordered Enbridge Gas to pay the OEB's costs of and incidental to the proceeding immediately upon receipt of the OEB's invoice.
  Source: https://eval.invalid/c/MR3zRveucPAw

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

Context: 9,682 chars from {'20240212-5063': [1, 2, 3, 5, 6, 7]}. Model `deepseek/deepseek-v4.1-flash`, 5301 ms, $0.00080724.

**Summary (as sent):** NorthWestern Corporation, acting as designated filer for itself, Idaho Power Company, Avista Corporation and PacifiCorp, submitted a tariff filing with the Federal Energy Regulatory Commission under Section 205 of the Federal Power Act for a Third Amended and Restated Interconnection Agreement (Rate Schedule No. 27) covering the 230-kV Amps Line. The 2024 Agreement extends the term past the 2012 Agreement's April 20, 2025 termination date, provides for automatic 10-year extensions unless a Party gives at least two years' written notice not to extend, and reflects completion of the 46 MW upgrade and other clarifying edits. NorthWestern requested waiver of the prior notice requirement so the agreement could become effective January 12, 2024. The Commission's decision date is listed as April 8, 2024, but that decision is not among these documents.


**Judge on summary:** coverage 5, clarity 4, misattribution False. Key facts per judge: The pages hold NorthWestern's February 12, 2024 Section 205 filing of the Third Amended and Restated AMPS Interconnection Agreement (RS 27), extending the term beyond April 20, 2025 with automatic 10-year renewals, and requesting waiver for a January 12, 2024 effective date. No decision or dollar amounts appear; the metadata gives a decision date of April 8, 2024. Missing: Nothing significant; could mention the pro-rata TTC reduction and the Joint Owners clarification, which are secondary. Unsupported statements: none. Removed-sentence verdicts: -

**Kept claims:**

- NorthWestern requested waiver of the prior notice filing requirement so the 2024 Agreement could become effective on January 12, 2024.  
  doc 20240212-5063 p3 (score 100.0): "B. Proposed Effective Date & Request for Waiver Pursuant to 18 C.F.R. § 35.11 and for good cause shown, NorthWestern respectfully requests waiver of the prior notice filing requirement under 18 C.F.R. § 35.3 of the Commission’s regulations to permit the Agreement to become effective on January 12, …"  
  judge: **SUPPORTED** (rel 3, quote-alone y): The quote states NorthWestern requests waiver of the prior notice filing requirement to permit the Agreement to become effective January 12, 2024. The page identifies the Agreement as the 2024 Agreement (Third Amended and Restated). Claim matches: requested (not granted), correct date, correct purpose. 'The 2024 Agreement' is the page's own term, …
- The 2024 Agreement extends the term past the 2012 Agreement's termination date of April 20, 2025, with automatic 10-year extensions unless a Party gives written notice not to extend at least two years before expiration of the Initial Term or any Extended Term.  
  doc 20240212-5063 p2 (score 100.0): "Revisions for 2024 Agreement The 2012 Agreement is being amended to extend the term past the termination date of the 2012 Agreement of April 20, 2025."  
  judge: **SUPPORTED** (rel 3, quote-alone n): The quote supports only that the 2012 Agreement is being amended to extend the term past April 20, 2025. The claim also says the 2024 Agreement has automatic 10-year extensions unless a Party gives written notice at least two years before expiration of the Initial Term or any Extended Term. The full page states this in the next sentence, so the cl…
- The 2024 Agreement provides for automatic extensions for 10-year terms, unless any Party elects not to extend by giving written notice to the other parties not less than two years prior to the expiration of the Initial Term or any Extended Term.  
  doc 20240212-5063 p2 (score 100.0): "The 2024 Agreement provides for automatic extensions for 10-year terms, unless any Party elects to not extend by giving written notice to the other parties not less than two years prior to the expiration of the Initial Term, or any Extended Term, as those terms are defined in the 2024 Agreement."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The claim restates the quote nearly verbatim, dropping only the trailing 'as those terms are defined in the 2024 Agreement'. The page text confirms it appears in the description of the 2024 Agreement revisions. Every substantive element (automatic 10-year extensions, any Party can opt out, written notice, not less than two years before expiration …
- The Parties agreed that any transmission service offered on the 230-kV facilities under the 2024 Agreement requires a Transmission Service Agreement stating that rollover Transmission Service rights are contingent upon renewal of the Agreement and FERC approval.  
  doc 20240212-5063 p3 (score 100.0): "The Parties agreed that any transmission service offered on the 230-kV facilities in the 2024 Agreement will require a Transmission Service Agreement that states that rollover Transmission Service rights are contingent upon renewal of this Agreement and approval by FERC."  
  judge: **SUPPORTED** (rel 3, quote-alone y): The claim restates the quote almost verbatim, with 'will require' rendered as 'requires' and 'this Agreement' as 'the Agreement'. The page confirms the parties agreed to this term in the 2024 Agreement. Nothing is overstated, and the quote alone covers every element.
- NorthWestern filed the executed Third Amended and Restated Interconnection Agreement among itself, Idaho Power Company, Avista Corporation and PacifiCorp, serving as designated filer under the Joint Tariff Filing procedures in Order No. 714.  
  doc 20240212-5063 p1 (score 100.0): "NorthWestern is serving as the “designated filer” under the Joint Tariff Filing procedures outlined in Order No. 714, on behalf of the Parties."  
  judge: **SUPPORTED** (rel 2, quote-alone n): The page's opening paragraph states NorthWestern submits for filing an executed Third Amended and Restated Interconnection Agreement among itself, Idaho Power, Avista and PacifiCorp, and that it serves as designated filer under Order No. 714 Joint Tariff Filing procedures. All elements of the claim are supported by the page. The quote alone covers…

<details><summary>Reply text</summary>

```
Hi Eval,

ER24-1234-000 is about NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii): RS 27 - Third Amended and Restated AMPS Agreement submitted on 2/12/2024 12:02:46 PM…. It is a Tariff Filing matter in the DKT category. The matter was received on February 12, 2024 and decided on April 8, 2024. I found 1 Order or Decision, 1 Notice, 1 Application or Filing, and no Comments and Protests, Motions and Pleadings, Interventions, Evidence and Testimony or Correspondence.

I downloaded the only Application or Filing and packaged it as a ZIP (146 KB).

Download (encrypted link, expires October 11, 2026): https://eval.invalid/drop/fake#key

Summary
NorthWestern Corporation, acting as designated filer for itself, Idaho Power Company, Avista Corporation and PacifiCorp, submitted a tariff filing with the Federal Energy Regulatory Commission under Section 205 of the Federal Power Act for a Third Amended and Restated Interconnection Agreement (Rate Schedule No. 27) covering the 230-kV Amps Line. The 2024 Agreement extends the term past the 2012 Agreement's April 20, 2025 termination date, provides for automatic 10-year extensions unless a Party gives at least two years' written notice not to extend, and reflects completion of the 46 MW upgrade and other clarifying edits. NorthWestern requested waiver of the prior notice requirement so the agreement could become effective January 12, 2024. The Commission's decision date is listed as April 8, 2024, but that decision is not among these documents.

Key points, each linked to the exact passage:
- NorthWestern requested waiver of the prior notice filing requirement so the 2024 Agreement could become effective on January 12, 2024.
  Source: https://eval.invalid/c/qoTmCO9lZsIb
- The 2024 Agreement extends the term past the 2012 Agreement's termination date of April 20, 2025, with automatic 10-year extensions unless a Party gives written notice not to extend at least two years before expiration of the Initial Term or any Extended Term.
  Source: https://eval.invalid/c/vKFuEKNXHVyX
- The 2024 Agreement provides for automatic extensions for 10-year terms, unless any Party elects not to extend by giving written notice to the other parties not less than two years prior to the expiration of the Initial Term or any Extended Term.
  Source: https://eval.invalid/c/Ab5QLlvRof2g
- The Parties agreed that any transmission service offered on the 230-kV facilities under the 2024 Agreement requires a Transmission Service Agreement stating that rollover Transmission Service rights are contingent upon renewal of the Agreement and FERC approval.
  Source: https://eval.invalid/c/FYMVPxzoZ9x5
- NorthWestern filed the executed Third Amended and Restated Interconnection Agreement among itself, Idaho Power Company, Avista Corporation and PacifiCorp, serving as designated filer under the Joint Tariff Filing procedures in Order No. 714.
  Source: https://eval.invalid/c/0AZzUNe5rv16

Documents:
- NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii: RS 27 - Third Amended and Restated AMPS Agreement to be effective 1/12/2024 under ER24-1234. Filing Type : 10 (2024-02-12)
  View: https://eval.invalid/files/20240212-5063.pdf

Request details: https://eval.invalid/r/fake

--
Automated reply from the Regulatory Document Agent. Privacy notice: https://uarb.hsingh.app/privacy
```
</details>

