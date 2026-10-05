# Providers

This document is written in ASD-STE100 Simplified Technical English.

A provider is the adapter for 1 regulator portal. The agent has 3 providers: Nova Scotia UARB, the Ontario Energy Board (OEB) and the US Federal Energy Regulatory Commission (FERC). The rest of the pipeline does not know which provider it uses.

NOTE: Regulator portals can change without notice. The FERC eLibrary API is an internal API without public documentation. A change on a portal can stop its provider until a developer changes the code.

## 1. Summary

| | UARB | OEB | FERC |
|---|---|---|---|
| Portal | FileMaker WebDirect (`uarb.novascotia.ca`) | HPE Content Manager WebDrawer (`rds.oeb.ca`) | eLibrary (`elibrary.ferc.gov`) |
| Access method | Playwright (Chromium) | JSON API over HTTPS (httpx) | Internal JSON API over HTTPS (httpx) |
| Egress | SOCKS tunnel to an Azure VM in Canada | Direct | Direct |
| Matter format | `M` and 5 digits, for example `M12205` | `EB-YYYY-NNNN`, for example `EB-2024-0111` | Docket, for example `ER24-1234` or `ER24-1234-000` |
| Categories | 5 | 9 | 8 |
| Concurrency for each worker | 4 browser sessions, 3 for each matter | 4 HTTP requests | 3 HTTP requests |
| Daily visit budget | 400 | 2000 | 2000 |
| Canary for "not found" | 2 independent sessions | `EB-2024-0111` | `ER24-1234-000` |

## 2. The provider interface

`agent/providers/base.py` defines the interface.

| Member | Type | Purpose |
|---|---|---|
| `name` | str | Stable id, for example `uarb`. It is also the name of the circuit breaker. |
| `display_name` | str | The name in emails, for example "Nova Scotia Utility and Review Board" |
| `portal_url` | str | The public home of the document database |
| `matter_pattern` | regex | The canonical matter number |
| `mention_pattern` | regex | A matter number as it can occur in free text |
| `matter_example` | str | An example for help texts |
| `categories` | tuple of `Category` | The document groups, in portal order. Each has a name, aliases and a 1-line description. |
| `fetch_matter(matter)` | async | Matter data and the count for each category |
| `list_matter_and_documents(matter, category, limit)` | async | Matter data and the newest documents of 1 category, in 1 portal visit |
| `download(matter, refs, dest_dir)` | async generator | Gives each file when it arrives |
| `normalise(raw)` | optional | Changes a matter number as a person writes it into the canonical form |
| `narrow(matter, following)` | optional | Narrows a matter from the text after it (FERC sub-dockets) |

All text is NFKC-normalised before the patterns apply. All patterns use `re.ASCII`. For this reason, a full-width or non-ASCII digit never becomes part of a canonical matter number. If a matter matches 2 providers, `provider_for_matter` raises an error. That is a configuration error.

## 3. Rules for all providers

### 3.1 Failure classes

| Situation | Exception | Retried |
|---|---|---|
| Timeout, connection error, HTTP 5xx or 429 | `PortalUnavailable` (with the `Retry-After` value for 429 and 503) | Yes |
| HTTP 400 or 403 | `ProviderRejected` | No |
| A redirect (never followed), an unexpected response, HTML in place of a file | `ScrapeError` | Yes |
| The matter does not exist, and the canary test passed | `MatterNotFound` | No |
| A file over 200,000,000 bytes, or a request over 600,000,000 bytes | `TooLarge` | No |
| A file type outside the allowlist | `UnsupportedFileType` | No |

A provider never raises `MatterNotFound` because of a timeout. A timeout is always "portal unavailable".

### 3.2 Checks on each downloaded file

`agent/providers/files.py` does these checks before a file goes into the store:

1. The file is not empty.
2. The file is not larger than 200,000,000 bytes (`max_file_bytes`). HTTP downloads stop at the limit.
3. The first bytes are not an HTML page.
4. The first bytes match the file type: `%PDF-` for PDF, ZIP for DOCX, XLSX and XLSM, OLE2 for DOC and XLS.
5. The file type is in `allowed_file_exts`: PDF, DOCX, DOC, XLSX, XLS, XLSM, CSV, TXT, MP3, MP4 and WAV.
6. The worker calculates the SHA-256 and moves the file to `data/blobs/<sha256[:2]>/<sha256>`.

Each failure removes the temporary file. 1 HTTP download must finish within 600 s.

## 4. Nova Scotia UARB

### 4.1 Portal and egress

The UARB Public Documents Database is a FileMaker WebDirect application at `https://uarb.novascotia.ca/fmi/webd/UARB15`. It has no stable element ids and no plain links. The document grid is virtualised. Files come from per-session URLs behind a "Download Files" dialog.

The portal answers only IP addresses in North America. The worker browser uses a SOCKS proxy on `127.0.0.1:1080`. The systemd unit `deploy/uarb-egress-tunnel.service` opens this proxy as an SSH tunnel to an Azure VM in Canada. The unit restarts the tunnel 5 s after a failure.

CAUTION: The tunnel is a single point of failure for UARB. If the tunnel or the Azure VM stops, all UARB requests fail after their retries. OEB and FERC requests continue.

### 4.2 Matter format

The canonical form is `M` and 5 digits, for example `M12205`. The agent also accepts `m12205`, `M-12205`, `M 12205`, `matter 12205`, `matter no. 12205` and `matter #12205`. A longer token such as `AM123456` is not a matter.

### 4.3 Categories

| Category | Id shown on each row | Order on the portal |
|---|---|---|
| Exhibits | Exhibit number, for example `H-1`, `H-4(C)-iii` | Oldest first |
| Key Documents | Numeric id, for example `102674` | Oldest first |
| Other Documents | Numeric id | Newest first |
| Transcripts | No id. The agent makes 1, for example `TR-20220912-<hash>`. | As the portal shows |
| Recordings | No id. The agent makes 1, for example `REC-20220921-<hash>`. | As the portal shows |

The count for each category comes from the tab labels, for example "Exhibits - 13". The agent never uses fixed counts. If it cannot read all 5 counts, it raises `ScrapeError`.

The reply describes the order that the dates show: newest first, oldest first, or "as the portal lists them".

### 4.4 How the agent lists documents

1. The provider opens a fresh browser context for each session.
2. It types the matter number and presses Enter.
3. It reads the matter header. It pairs labels and values by their position on the screen.
4. It opens the tab of the category.
5. It reads the visible rows, then scrolls the grid scroller 1 screen.
6. It stops when it has `limit` public rows, or at the end of the grid.

On the Recordings tab, the provider reads all rows. It must see all rows to decide the access of rows with an empty Security cell (refer to 4.6).

If a first pass says "No Records Found", or it reads fewer rows than the count, a second pass runs in a new session. The second pass is final.

### 4.5 How the agent downloads and examines files

1. The provider divides the files into at most 3 groups and opens 1 session for each group.
2. Each session opens the matter and the tab.
3. For each file, the session finds the row and scrolls it into the grid viewport.
4. The session takes the Redis lock `uarb:download`.
5. The session clicks GO GET IT, waits for the "Download Files" dialog and clicks the file.
6. The session compares the served file name (without spaces) with the requested id.
7. If the names are different, the session discards the file and raises `ScrapeError`.
8. The session releases the lock when the correct file starts to arrive.
9. The transfer continues outside the lock. The session stops a transfer that does not grow for 60 s.
10. The session tries each file at most 3 times. Then it reports the file as failed.

The lock lives 180 s and the session waits at most 600 s for it. A session holds the lock for approximately 0.7 s to 1.8 s for each file (measured on the live portal). If the wait times out, that file fails for this try, and the retry of the request gets it.

For Transcripts and Recordings, GO GET IT first opens an "Export Field to File" dialog. The session then does these steps:

1. It clicks GO GET IT again until 2 clicks in sequence propose the same file name.
2. It makes sure that the active row of the portal is the row that it clicked.
3. It makes sure that the proposed file type is in the allowlist.
4. It types `<agent id>.<ext>` as the export name.
5. It compares the served file name with that id.

### 4.6 Access labels

The agent downloads only rows with the label "Public".

| Label | Result |
|---|---|
| Public | Downloaded |
| Confidential, Restricted, Board Only | Listed and counted, never downloaded |
| Any other label, or no label | Never downloaded (fail closed) |
| Empty Security cell on the Recordings tab | "Public" only if the agent read all rows of the tab and no row has another label. Otherwise never downloaded. |

The reply tells how many rows the agent held back.

### 4.7 Known portal problems

| Problem found on the live portal | Effect without a fix | How the agent handles it |
|---|---|---|
| A click on Search immediately after the typed number searches for an empty value | "No Records Found" for a real matter (2 of 2 runs) | The agent presses Enter. Enter commits the field and submits (2 of 2 correct). A "not found" result must occur again in a fresh session. |
| GO GET IT acts on the active record. The portal shares the prepared file across guest sessions from 1 client IP. | Session A got the files that sessions B and C had requested, under the correct names | The agent compares each served file name with the requested id. A Redis lock for each egress IP serialises the click-to-file step. |
| The grid is virtualised. The grid uses its row elements again. A row can be in the DOM but outside the grid scroller. | Clicks fail or hit the wrong row | The agent scrolls the grid scroller, not the window. It reads rows by their screen position. |
| Exhibit ids have stray spaces or parentheses, for example `A -5` and `H-4(C)-iii` | The row is not found, or the id does not match | The agent removes spaces from ids. Row search allows spaces around a hyphen and escapes the pattern. |
| Transcripts and Recordings rows have no file id. 2 rows can be identical. | No id to compare with the served file | The agent makes an id from the tab, the date and the text. A second identical row gets `_2`. The export dialog and the active row give the proof. |
| A prefilled export name with a comma, for example `...September 12, 2022.pdf` | Chromium refuses the download | The agent exports the file under its own id. |
| A "Continue or Cancel Script" prompt follows a cancelled export | The next click does nothing | The agent cancels the prompt before each click. |
| The portal sometimes ignores a GO GET IT click | The dialog does not open | The agent clicks up to 3 times, with a longer wait each time. |
| "No Records Found" in a download session for a matter that the agent listed seconds before | A false "not found" | The agent treats it as `ScrapeError` (retry), never as `MatterNotFound`. |
| Metadata values render before their labels | Wrong field values | The agent pairs labels and values by their position on the screen. |

## 5. Ontario Energy Board (OEB)

### 5.1 Portal

The OEB Regulatory Document Search is an HPE Content Manager WebDrawer. Its JSON API is at `https://rds.oeb.ca/CMWebDrawer/`. The provider uses plain HTTP. It never follows a redirect.

### 5.2 Matter format

The canonical form is `EB-YYYY-NNNN`, for example `EB-2024-0111`. The agent also accepts `eb 2024 0111`, `EB2024-0111` and other dash characters from PDFs, for example `EB–2024–0111`.

### 5.3 Categories

The portal gives each record 1 or more document types (`SIDocumentType`). The provider puts these types into 9 categories. A record with more than 1 type goes to the first category that claims 1 of its types.

| Category | Document types (examples) |
|---|---|
| Decisions and Orders | Decisions, decisions and orders, rate orders |
| Procedural Orders | Procedural orders, notices, letters of direction, acknowledgement letters |
| Application and Evidence | Application and evidence, intervenor evidence, exhibits, exhibit lists |
| Interrogatories | Interrogatories to the applicant or intervenors, and the responses |
| Undertakings | Undertaking responses and lists, declarations and undertakings |
| Submissions and Arguments | Submissions, argument in chief, reply argument, comments, settlement proposals, motions |
| Transcripts | Transcripts, cross-examination material |
| Cost Claims | Cost claims, objections and replies |
| Correspondence | All other types, also types that the portal adds later |

### 5.4 How the agent lists and downloads documents

1. The provider searches `CaseNumber=<matter>`, newest registered first, 700 records for each page.
2. It reads at most 5000 records.
3. It sorts the records by date, newest first.
4. It derives the matter data from all records: title, energy type, application type and dates.
5. It downloads each file with `GET Record/<id>/File/document`, with at most 4 requests at the same time.
6. It compares the size from the search result with the size limit before the download.
7. It applies the file checks of section 3.2.

The date of a record is `DateIssued`, else `fDateReceived`, else the registration time converted to the Toronto date. The decision date of a matter is the date of the latest final decision. Cost-award decisions and draft rate orders do not count.

### 5.5 Known portal problems

| Problem | How the agent handles it |
|---|---|
| A search that the server does not understand also returns 0 records | Before "not found", the provider searches the canary case `EB-2024-0111`. If the canary returns 0, the result is `ScrapeError`. The canary result stays valid for 900 s. |
| An unknown record returns HTTP 200 with an HTML error page | The content check rejects HTML. The result is `ScrapeError`. |
| The property `RecordContainer` is access-denied for the public and makes the full search fail | The provider never asks for it. |
| The portal stores record times in UTC | The provider uses the Toronto date, so a document filed in the evening gets the correct day. |
| Record titles are often file names | The summary ranks documents by the portal document type, not by the title. |

## 6. US FERC eLibrary

### 6.1 Portal

eLibrary is a single-page web application. Its JSON API is at `https://elibrary.ferc.gov/eLibraryWebAPI/api/`. This API has no public documentation. The provider uses plain HTTP, with at most 3 requests at the same time.

### 6.2 Matter format

A matter is a docket. It has a docket prefix, a 2-digit fiscal year, a hyphen and 1 to 6 digits, for example `ER24-1234`. An optional sub-docket of 3 digits can follow, for example `ER24-1234-000`.

- A whole docket, for example `RM22-14`, covers all its sub-dockets.
- Text such as "ER24-1234 (the -000 sub-docket only)" narrows the matter to `ER24-1234-000`.
- Only prefixes from a fixed list form a docket. For this reason, `x86-64` or `FY24-25` in an email is never a matter.
- Hydro project numbers, for example `P-2114`, have no fiscal year. The agent does not support them.

### 6.3 Categories

| Category | eLibrary document classes (examples) |
|---|---|
| Orders and Decisions | Order/Opinion, ALJ Issuance |
| Notices | Notice |
| Applications and Filings | Application/Petition/Request, agreements, reports and forms, tariff filings, other submittals |
| Comments and Protests | Comments/Protest, FERC Comment |
| Motions and Pleadings | Pleading/Motion, briefs, court documents |
| Interventions | Intervention |
| Evidence and Testimony | Testimony, Exhibit, Transcript, data requests and responses |
| Correspondence | Correspondence, memos, status reports, staff reports and studies, and all other classes |

1 time for each worker, the provider asks eLibrary for its list of classes. It writes a log line for each class that no category names. Such a class goes to Correspondence.

### 6.4 How the agent lists and downloads documents

1. The provider gets the docket description and the first search page at the same time.
2. It gets the other pages, 100 hits for each page, at most 5000 hits.
3. For each document (accession number), it selects 1 primary file. This is the first PDF, else the first DOCX, else the first file.
4. It lists documents that are not public (`availCode` is not `P`) as "Non-public". It never downloads them.
5. It downloads each primary file with `POST File/DownloadP8File`.
6. It rejects a response with an HTML or JSON content type.
7. It finds the file type from the bytes, because eLibrary serves all files as `application/octet-stream`.
8. It names the file `<accession>_<original name>`.

FERC issues many of its orders as DOCX files. The summary reads DOCX files. The agent delivers TXT and DOC files but does not cite them.

### 6.5 Known portal problems

| Problem | How the agent handles it |
|---|---|
| The API has no public documentation and can change | The provider validates each response against a schema. An unexpected form is `ScrapeError` (retry), never "not found". |
| An unknown docket gets the description "Applicant not Found." | With 0 hits, the provider searches the canary `ER24-1234-000` first. If the canary fails, the result is `ScrapeError`. |
| A search without a date range returns 0 hits | The provider always sends a date range. The canary test finds a similar change. |
| 1 accession can have many files (for example, 46 parts of approximately 165 MB each) | The provider fetches only the primary file. The README in the ZIP links the docket, where the other files are. |
| eLibrary is behind Cloudflare | No challenge has occurred. A challenge page is HTML, so the content check rejects it. |

## 7. Procedure: add a provider

Do this procedure to add a regulator. Read section 2 and section 3 first.

1. Create the file `agent/providers/<name>.py`.
2. Define the categories as a tuple of `Category`, in the order that the portal uses.
3. Write `matter_pattern` with `re.ASCII`.
4. Make sure that `matter_pattern` does not match a matter of another provider.
5. Write `mention_pattern` and, if necessary, a `normalise()` function.
6. Write `fetch_matter()` and `list_matter_and_documents()`. Use 1 portal visit for the list.
7. Raise `MatterNotFound` only after a canary matter still returns documents.
8. Write `download()` as an async generator. Give each file when it arrives.
9. Pass each temporary file to `finalise_download()` in `agent/providers/files.py`.
10. For an HTTP portal, use `make_client()`, `raise_for_status()` and `download_to()` from `agent/providers/http.py`.
11. Set `access` to `Public` only for rows that the portal marks as public.
12. Add the proxy and concurrency parameters to `agent/config.py`.
13. Add the provider name and a daily visit limit to `portal_daily_visits`.
14. Add the provider to the `providers` dictionary in `startup()` in `agent/worker.py`.
15. Add the env key pattern of the provider (for example `NEWREG_*`) to `[worker]` in `deploy/env/services.toml`.
16. Save real portal responses in `tests/fixtures/`. Write unit tests against them.
17. Write a live test in `tests/live/` with the marker `live`.
18. Add cases with the new matter format to `evals/gate/dataset.jsonl`.
19. Run the unit tests.

    ```bash
    .venv/bin/pytest -q
    ```

    Expected result: all tests pass.

20. Run the gate eval and compare the report with the last report ([Quality and evals](quality-and-evals.md)).
21. Run the live test of the provider.

    ```bash
    .venv/bin/pytest -m live -q tests/live/test_<name>_live.py
    ```

    Expected result: the test lists and downloads real documents.

22. Tell the owner of `docs/policies/vendor-register.md` about the new regulator and its egress.

Expected result: the gate, the cache, the package, the summary, the breakers, the metrics and the viewer use the new provider. No other module changes.
