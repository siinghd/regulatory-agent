"""Nova Scotia Utility and Review Board: Public Documents Database (FileMaker WebDirect).

The portal is a Vaadin/FileMaker app: no stable ids, no plain links, inputs are contenteditable
divs, the document grid is virtualised, and files come from per-session connector URLs behind a
"Download Files" dialog. Everything here is driven by what the page renders, with explicit waits
on observable state and no fixed sleeps.

Failure classification matters more than anything else: a slow portal must surface as
PortalUnavailable (retry), never as MatterNotFound (which we tell the user).
"""

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from agent.models import (
    DocType,
    DocumentRef,
    DownloadedFile,
    MatterInfo,
    MatterNotFound,
    PortalUnavailable,
    ScrapeError,
)
from agent.providers.browser import BrowserPool

log = structlog.get_logger()

PORTAL_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
MATTER_RE = re.compile(r"M\d{5}")
_TAB_COUNT_RE = re.compile(r"(Exhibits|Key Documents|Other Documents|Transcripts|Recordings)\s*-\s*(\d+)")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# Exhibit numbers seen live: H-1, H-4(C), H-4(C)-iii, H-5(c)-ii, H-5-iii. No spaces, short.
_EXHIBIT_ID_RE = re.compile(r"[A-Za-z]{1,4}\d{0,2}-\d{1,4}[-()A-Za-z0-9.]{0,14}")

# Header labels as the portal renders them. Some cells stack two labels ("Matter No / Status",
# "Type / Category") over two stacked values, so values are matched to labels by geometry.
_HEADER_JS = r"""
() => {
  const LABELS = ["Matter No","Status","Title - Description","Type","Category",
                  "Date Received","Decision Date","Outcome"];
  const vis = r => r.width > 0 && r.height > 0;
  const labelLines = [];
  for (const p of document.querySelectorAll(".iwps_text_box .fm-text-paragraph")) {
    const r = p.getBoundingClientRect();
    if (!vis(r)) continue;
    // stacked labels ("Matter No\nStatus") live in one paragraph: one label per line
    const lines = p.innerText.split("\n").map(s => s.trim()).filter(Boolean);
    lines.forEach((t, i) => {
      if (LABELS.includes(t)) labelLines.push({t, x0:r.left, x1:r.right, y:r.top + i * r.height / lines.length});
    });
  }
  if (!labelLines.length) return null;
  const labelBottom = Math.max(...labelLines.map(l => l.y)) + 8;
  const tabs = [...document.querySelectorAll("button")].find(b => /^Exhibits\s*-\s*\d+/.test(b.innerText.trim()));
  const tabsTop = tabs ? tabs.getBoundingClientRect().top : labelBottom + 200;
  // value boxes: read-only fields and layout cells between the label row and the tab bar
  const vals = [];
  for (const el of document.querySelectorAll(".fm-textarea, .iwp-css-layout-common-style, .iwps_text_box")) {
    const r = el.getBoundingClientRect();
    if (!vis(r) || r.top < labelBottom || r.top > tabsTop) continue;
    if (el.querySelector(".fm-textarea, .iwps_text_box")) continue;  // keep leaves only
    vals.push({t: el.innerText.trim(), x0:r.left, x1:r.right, y:r.top});
  }
  // group label lines into columns by x-overlap, then zip each column's labels with the
  // values under it in vertical order
  const cols = [];
  for (const l of labelLines.sort((a,b) => a.y - b.y)) {
    let c = cols.find(c => Math.abs(c.x0 - l.x0) < 12);
    if (!c) { c = {x0:l.x0, x1:l.x1, labels:[]}; cols.push(c); }
    c.labels.push(l.t);
  }
  const out = {};
  for (const c of cols) {
    const mine = vals.filter(v => {
      const cx = (v.x0 + v.x1) / 2;
      return cx >= c.x0 - 20 && (v.x0 <= c.x0 + 30) && cx <= c.x0 + 520;
    }).sort((a,b) => a.y - b.y);
    // de-duplicate nested wrappers that report the same row
    const rows = [];
    for (const v of mine) if (!rows.some(r => Math.abs(r.y - v.y) < 6)) rows.push(v);
    c.labels.forEach((name, i) => { out[name] = rows[i] ? rows[i].t : ""; });
  }
  return out;
}
"""

_ROWS_JS = r"""
() => [...document.querySelectorAll("tr.v-grid-row-has-data")].map((tr, i) => {
  const texts = [...tr.querySelectorAll(".fm-textarea .text, .v-label, .fm-text-character")]
    .map(e => e.innerText.trim()).filter(Boolean);
  return {texts, top: tr.getBoundingClientRect().top};
})
"""


_ROW_SIGNATURE_JS = (
    "() => [...document.querySelectorAll('tr.v-grid-row-has-data')]"
    ".map(r => r.innerText.slice(0, 12)).join('|')"
)


def _parse_date(text: str | None) -> date | None:
    if text and _DATE_RE.match(text.strip()):
        return datetime.strptime(text.strip(), "%m/%d/%Y").replace(tzinfo=UTC).date()
    return None


def parse_row(texts: list[str]) -> dict | None:
    """Turn the cell texts of one grid row into fields. Order-independent on purpose."""
    uniq = list(dict.fromkeys(t for t in texts if t not in {"Preview", "GO GET IT"}))
    ext = next((t for t in uniq if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", t)), "")
    # Most tabs identify files by a numeric id ("102674"); Exhibits use exhibit numbers
    # ("H-1", "H-4(C)-i"). Either way it is also the name the portal serves the file under.
    file_id = next((t for t in uniq if re.fullmatch(r"\d{3,9}", t)), None) or next(
        (t for t in uniq if _EXHIBIT_ID_RE.fullmatch(t)), None
    )
    filed = next((t for t in uniq if _DATE_RE.match(t)), None)
    access = next((t for t in uniq if t in {"Public", "Confidential", "Restricted"}), "Public")
    rest = [t for t in uniq if t not in {ext, file_id, filed, access}]
    if not file_id or not rest:
        return None
    return {
        "external_id": file_id,
        "title": max(rest, key=len),
        "filed_on": _parse_date(filed),
        "access": access,
        "file_ext": ext.lower() or ".pdf",
    }


def parse_counts(body_text: str) -> dict[DocType, int]:
    found = {DocType(name): int(n) for name, n in _TAB_COUNT_RE.findall(body_text)}
    if len(found) != len(DocType):
        raise ScrapeError(f"expected 5 tab counts, found {sorted(found)}")
    return found


class UarbProvider:
    name = "uarb"
    display_name = "Nova Scotia Utility and Review Board"
    matter_pattern = MATTER_RE
    doc_types = tuple(DocType)

    def __init__(
        self,
        pool: BrowserPool,
        *,
        sessions_per_matter: int = 3,
        download_lock: Callable[[], AbstractAsyncContextManager] | None = None,
    ):
        self._pool = pool
        self._sessions_per_matter = sessions_per_matter
        # The portal prepares "GO GET IT" files in state shared across concurrent guest sessions
        # from one client: measured on the live portal, session A asking for 102674 was served
        # the files sessions B and C had just requested. Navigation stays parallel; the short
        # click -> download section is serialised. In production this is a Redis lock so it
        # also holds across worker processes (see agent.limits.redis_lock).
        local = asyncio.Lock()
        self._download_lock = download_lock or (lambda: local)

    # ------------------------------------------------------------------ navigation

    async def _open_matter(self, page: Page, matter: str) -> None:
        try:
            await page.goto(PORTAL_URL, wait_until="domcontentloaded")
            field = page.locator('.fm-textarea:has(.placeholder:text-is("eg M01234"))')
            await field.wait_for(state="visible")
            box = await field.bounding_box()
            await self._type_matter(page, field, matter)
            # Enter commits the field *and* submits. Clicking Search straight after typing races
            # the field commit and searches for an empty value ("No Records Found" for a real
            # matter): measured 2/2 wrong on the live portal, Enter 2/2 right.
            await page.keyboard.press("Enter")
            try:
                outcome = await self._wait_matter_outcome(page, timeout=20_000)
            except PlaywrightTimeout:
                await self._click_search_next_to(page, box)
                outcome = await self._wait_matter_outcome(page)
        except PlaywrightTimeout as e:
            raise PortalUnavailable(f"timeout opening {matter}: {e}") from e
        except PlaywrightError as e:
            # proxy/network failures (ERR_PROXY_CONNECTION_FAILED, ERR_TIMED_OUT, ...)
            raise PortalUnavailable(f"browser error opening {matter}: {e}") from e
        if outcome == "not_found":
            raise MatterNotFound(matter)

    async def _type_matter(self, page: Page, field, matter: str) -> None:
        # The editor activates asynchronously (round-trip to the FileMaker server); keystrokes
        # sent before it has focus are silently dropped, and the field starts with a newline.
        # So: focus, verify focus, replace content atomically, verify the value, retry.
        text = field.locator(".text")
        for _ in range(4):
            await text.click(force=True)
            await page.wait_for_function(
                "document.activeElement && document.activeElement.isContentEditable", timeout=10_000
            )
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(matter)
            try:
                await page.wait_for_function(
                    "([el, v]) => el.innerText.trim() === v",
                    arg=[await text.element_handle(), matter],
                    timeout=3_000,
                )
                return
            except PlaywrightTimeout:
                continue
        raise ScrapeError(f"could not enter matter number {matter}")

    async def _click_search_next_to(self, page: Page, field_box: dict | None) -> None:
        buttons = page.locator("button").filter(has_text=re.compile(r"^\s*Search\s*$"))
        for i in range(await buttons.count()):
            b = await buttons.nth(i).bounding_box()
            if b and field_box and abs((b["y"] + b["height"] / 2) - (field_box["y"] + field_box["height"] / 2)) < 20:
                await buttons.nth(i).click(force=True)
                return
        raise ScrapeError("matter Search button not found next to the matter field")

    async def _wait_matter_outcome(self, page: Page, timeout: float | None = None) -> str:
        handle = await page.wait_for_function(
            """() => {
                const t = document.body.innerText;
                if (/Exhibits\\s*-\\s*\\d+/.test(t)) return "found";
                if (t.includes("No Records Found") || t.includes("No records matched")) return "not_found";
                return false;
            }""",
            polling=250,
            timeout=timeout,
        )
        return await handle.json_value()

    # ------------------------------------------------------------------ metadata

    async def fetch_matter(self, matter: str) -> MatterInfo:
        async with self._pool.session() as page:
            await self._open_matter(page, matter)
            return await self._read_matter(page, matter)

    async def _read_matter(self, page: Page, matter: str) -> MatterInfo:
        body = await page.evaluate("document.body.innerText")
        counts = parse_counts(body)
        header = await page.evaluate(_HEADER_JS) or {}
        shown = (header.get("Matter No") or "").strip()
        if shown and shown != matter:
            raise ScrapeError(f"portal showed {shown!r} for {matter}")
        title = header.get("Title - Description") or ""
        if not title:
            raise ScrapeError(f"no title parsed for {matter}: {header}")
        return MatterInfo(
            provider=self.name,
            matter=matter,
            title=" ".join(title.split()),
            status=header.get("Status") or None,
            type=header.get("Type") or None,
            category=header.get("Category") or None,
            date_received=_parse_date(header.get("Date Received")),
            decision_date=_parse_date(header.get("Decision Date")),
            outcome=header.get("Outcome") or None,
            counts=counts,
            portal_url=PORTAL_URL,
            fetched_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ documents

    async def _open_tab(self, page: Page, doc_type: DocType) -> None:
        tab = page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(doc_type.value)}\s*-\s*\d+\s*$"))
        await tab.first.click(force=True)
        try:
            await page.locator("tr.v-grid-row-has-data").first.wait_for(state="attached")
        except PlaywrightTimeout as e:
            raise PortalUnavailable(f"{doc_type} grid did not load") from e

    async def _scroll_grid(self, page: Page) -> bool:
        """Scroll the virtualised grid one viewport. Returns False at the bottom."""
        return await page.evaluate(
            """() => {
                const s = document.querySelector('.v-grid-scroller-vertical');
                if (!s) return false;
                const before = s.scrollTop;
                s.scrollTop = Math.min(s.scrollTop + s.clientHeight * 0.8, s.scrollHeight);
                s.dispatchEvent(new Event('scroll'));
                return s.scrollTop > before;
            }"""
        )

    async def _collect_rows(self, page: Page, matter: str, doc_type: DocType, limit: int) -> list[DocumentRef]:
        refs: dict[str, DocumentRef] = {}
        stale_rounds = 0
        # Collect until `limit` *public* rows: confidential rows are listed (so the reply can say
        # they exist) but never downloaded or sent.
        def public() -> int:
            return sum(1 for r in refs.values() if r.access == "Public")

        while public() < limit and stale_rounds < 3:
            before = len(refs)
            for row in await page.evaluate(_ROWS_JS):
                parsed = parse_row(row["texts"])
                if parsed and parsed["external_id"] not in refs and public() < limit:
                    refs[parsed["external_id"]] = DocumentRef(
                        provider=self.name, matter=matter, doc_type=doc_type, row_index=len(refs), **parsed
                    )
            if public() >= limit:
                break
            signature = await page.evaluate(_ROW_SIGNATURE_JS)
            if not await self._scroll_grid(page):
                break
            # wait until the grid has repainted the newly exposed rows
            try:
                await page.wait_for_function(f"(sig) => ({_ROW_SIGNATURE_JS})() !== sig", arg=signature, timeout=4_000)
            except PlaywrightTimeout:
                pass  # no repaint: counted as a stale round below
            stale_rounds = stale_rounds + 1 if len(refs) == before else 0
        return list(refs.values())

    async def list_documents(self, matter: str, doc_type: DocType, limit: int) -> list[DocumentRef]:
        async with self._pool.session() as page:
            await self._open_matter(page, matter)
            info = await self._read_matter(page, matter)
            if info.counts.get(doc_type, 0) == 0:
                return []
            await self._open_tab(page, doc_type)
            return await self._collect_rows(page, matter, doc_type, min(limit, info.counts[doc_type]))

    async def list_matter_and_documents(
        self, matter: str, doc_type: DocType, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        """One session for both: the common path for a request (saves a full portal round)."""
        try:
            return await self._list_once(matter, doc_type, limit)
        except MatterNotFound:
            # Telling a user their matter doesn't exist is the costliest mistake this agent can
            # make, so a negative answer must reproduce in an independent session.
            log.info("uarb.not_found_recheck", matter=matter)
            return await self._list_once(matter, doc_type, limit)

    async def _list_once(
        self, matter: str, doc_type: DocType, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        async with self._pool.session() as page:
            await self._open_matter(page, matter)
            info = await self._read_matter(page, matter)
            if info.counts.get(doc_type, 0) == 0 or limit <= 0:
                return info, []
            await self._open_tab(page, doc_type)
            refs = await self._collect_rows(page, matter, doc_type, min(limit, info.counts[doc_type]))
            return info, refs

    async def _row_for(self, page: Page, external_id: str):
        """Locate a row by file id, scrolling the virtualised grid if needed."""
        row = page.locator("tr.v-grid-row-has-data").filter(
            has=page.locator(".text", has_text=re.compile(rf"^\s*{re.escape(external_id)}\s*$"))
        )
        if not await row.count():
            # rows are newest-first: scan from the top so we never scroll past the target
            await page.evaluate(
                "() => { const s = document.querySelector('.v-grid-scroller-vertical');"
                " if (s) { s.scrollTop = 0; s.dispatchEvent(new Event('scroll')); } }"
            )
            await page.wait_for_timeout(250)
        for _ in range(12):
            if await row.count():
                return row.first
            if not await self._scroll_grid(page):
                break
            await page.wait_for_timeout(250)  # virtual scroll repaint; bounded by the loop
        raise ScrapeError(f"row {external_id} not found in grid")

    async def _ensure_visible(self, page: Page, row) -> None:
        """Rows can be rendered but outside the grid's own scroll viewport, where clicks fail.
        Scroll the grid's scroller (not the window) until the row sits inside it."""
        for _ in range(6):
            delta = await row.evaluate(
                """(tr) => {
                    const body = tr.closest('.v-grid-tablewrapper') || tr.closest('.v-grid');
                    const s = document.querySelector('.v-grid-scroller-vertical');
                    if (!body || !s) return 0;
                    const b = body.getBoundingClientRect(), r = tr.getBoundingClientRect();
                    if (r.top >= b.top + 40 && r.bottom <= b.bottom - 4) return 0;
                    const d = Math.round(r.top - (b.top + b.height / 2));
                    s.scrollTop += d; s.dispatchEvent(new Event('scroll'));
                    return d;
                }"""
            )
            if delta == 0:
                return
            await page.wait_for_timeout(200)  # repaint; bounded by the loop

    async def _download_one(self, page: Page, ref: DocumentRef, dest_dir: str) -> DownloadedFile:
        row = await self._row_for(page, ref.external_id)
        await self._ensure_visible(page, row)
        go =row.locator("button").filter(has_text=re.compile("go get it", re.IGNORECASE)).first
        dialog = page.locator(".v-window").filter(has_text="Download Files").last
        button = dialog.locator(".fm-download-button").first
        async with self._download_lock():
            return await self._click_and_save(page, ref, go, button, dest_dir)

    async def _click_and_save(self, page: Page, ref: DocumentRef, go, button, dest_dir: str) -> DownloadedFile:
        try:
            for attempt in range(3):
                await go.click(force=True)
                try:
                    await button.wait_for(state="visible", timeout=6_000 + attempt * 6_000)
                    break
                except PlaywrightTimeout:
                    continue
            else:
                raise PortalUnavailable(f"download dialog never opened for {ref.external_id}")
            caption = (await button.inner_text()).strip()
            async with page.expect_download(timeout=120_000) as dl_info:
                await button.click()
            download = await dl_info.value
            failure = await download.failure()
            if failure:
                raise PortalUnavailable(f"download of {ref.external_id} failed: {failure}")
            # Integrity: the portal names each file after its id. GO GET IT acts on FileMaker's
            # active record, which in a fresh session can still be row 0 rather than the row we
            # clicked; without this check the user would get the wrong document under this title.
            # (Clicking the id cell to select the row navigates away, so it isn't an option.)
            # The click does make our row active, so the caller's retry gets the right file.
            served = os.path.splitext(download.suggested_filename or caption)[0]
            if served != ref.external_id:
                await download.delete()
                raise ScrapeError(f"asked for {ref.external_id}, portal served {served!r}")
            tmp = os.path.join(dest_dir, f".{ref.external_id}.part")
            await download.save_as(tmp)
        finally:
            await self._dismiss_modals(page)
        return _finalise_file(tmp, ref, download.suggested_filename, dest_dir)

    async def _download_worker(
        self, matter: str, refs: list[DocumentRef], dest_dir: str, out: asyncio.Queue
    ) -> None:
        async with self._pool.session() as page:
            await self._open_matter(page, matter)
            await self._open_tab(page, refs[0].doc_type)
            for ref in refs:
                for attempt in range(3):
                    try:
                        await out.put(await self._download_one(page, ref, dest_dir))
                        break
                    except (PlaywrightTimeout, PortalUnavailable, ScrapeError) as e:
                        log.warning("uarb.download_retry", file=ref.external_id, attempt=attempt, error=str(e)[:200])
                        if attempt == 2:
                            await out.put(e)

    async def _dismiss_modals(self, page: Page) -> None:
        """Close every open dialog so the modal curtain can't swallow the next click."""
        for _ in range(3):
            closes = await page.locator(".v-window .v-button").filter(has_text="Close").all()
            if not closes:
                return
            for close in closes:
                try:
                    await close.click(timeout=2_000)
                except PlaywrightError:
                    pass  # already detached by the previous close
            try:
                await page.locator(".v-window").first.wait_for(state="detached", timeout=3_000)
            except PlaywrightTimeout:
                continue

    async def download(self, matter: str, refs: list[DocumentRef], dest_dir: str) -> AsyncIterator[DownloadedFile]:
        """Split the refs across a few independent portal sessions and yield files as they land.

        Download URLs are bound to the FileMaker session, so parallelism means parallel sessions.
        """
        if not refs:
            return
        n = max(1, min(self._sessions_per_matter, len(refs)))
        shards = [refs[i::n] for i in range(n)]
        out: asyncio.Queue = asyncio.Queue()
        tasks = [asyncio.create_task(self._download_worker(matter, s, dest_dir, out)) for s in shards]
        remaining = len(refs)
        errors: list[BaseException] = []
        try:
            while remaining:
                getter = asyncio.create_task(out.get())
                done, _ = await asyncio.wait({getter, *tasks}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    item = getter.result()
                    remaining -= 1
                    if isinstance(item, BaseException):
                        errors.append(item)
                    else:
                        yield item
                    continue
                getter.cancel()
                # a session task finished or crashed: surface crashes, account for its lost refs
                for t in done:
                    tasks.remove(t)
                    if t.exception():
                        errors.append(t.exception())
                if not tasks and out.empty():
                    break
        finally:
            for t in tasks:
                t.cancel()
        if errors:
            log.warning("uarb.download_errors", count=len(errors), first=str(errors[0])[:200])
            if remaining == len(refs):  # nothing at all came through
                raise errors[0] if isinstance(errors[0], Exception) else ScrapeError(str(errors[0]))


_MAGIC = {".pdf": b"%PDF-"}


def _finalise_file(tmp: str, ref: DocumentRef, suggested: str, dest_dir: str) -> DownloadedFile:
    size = os.path.getsize(tmp)
    if size == 0:
        os.remove(tmp)
        raise PortalUnavailable(f"empty download for {ref.external_id}")
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        head = f.read(8)
        h.update(head)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    ext = os.path.splitext(suggested)[1].lower() or ref.file_ext
    magic = _MAGIC.get(ext)
    if magic and not head.startswith(magic):
        os.remove(tmp)
        raise ScrapeError(f"{ref.external_id}: expected {ext} but got {head!r}")
    sha = h.hexdigest()
    final = os.path.join(dest_dir, f"{sha}{ext}")
    os.replace(tmp, final)
    return DownloadedFile(
        ref=ref, path=final, sha256=sha, size=size, filename=f"{ref.external_id}{ext}"
    )
