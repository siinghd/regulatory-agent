"""Nova Scotia Utility and Review Board: Public Documents Database (FileMaker WebDirect).

The portal is a Vaadin/FileMaker app: no stable ids, no plain links, inputs are contenteditable
divs, the document grid is virtualised, and files come from per-session connector URLs behind a
"Download Files" dialog. Everything here is driven by what the page renders, with explicit waits
on observable state and no fixed sleeps.

Failure classification matters more than anything else: a slow portal must surface as
PortalUnavailable (retry), never as MatterNotFound (which we tell the user).
"""

import asyncio
import contextlib
import hashlib
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime

import structlog
from playwright.async_api import Download, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from agent.limits import LockTimeout
from agent.models import (
    AgentError,
    DocumentRef,
    DownloadedFile,
    MatterInfo,
    MatterNotFound,
    PortalUnavailable,
    ScrapeError,
    TooLarge,
)
from agent.providers.base import Category, nfkc
from agent.providers.browser import BrowserPool
from agent.providers.files import FilePolicy, error_to_raise, finalise_download

log = structlog.get_logger()

PORTAL_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
# re.ASCII: \d is 0-9 only, so full-width or other non-ASCII digits never form a matter number.
MATTER_RE = re.compile(r"M\d{5}", re.ASCII)
# "M12205", "m12205", "M-12205", "M 12205" (not inside longer tokens like "AM123456"), and
# "matter 12205", "matter no. 12205", "matter #12205"
MENTION_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])M[-\s]?|\bmatter\s*(?:no\.?|number|#)?\s*:?\s*)(\d{5})(?!\d)",
    re.IGNORECASE | re.ASCII,
)
PUBLIC = "Public"
# Access labels seen on the portal: "Public", "Confidential", "Board Only" (Exhibits of M10431).
# A row with any other label, or none, is treated as not public: fail closed, never downloaded.
_ACCESS_LABELS = frozenset({PUBLIC, "Confidential", "Restricted", "Board Only"})
UNKNOWN_ACCESS = "Unknown"
# A Recordings row whose Security cell is empty (most of M10431's): see _Rows.refs for when that
# counts as public. Never leaves this module: an unresolved one becomes UNKNOWN_ACCESS.
UNLABELLED = "Unlabelled"
DOWNLOAD_TIMEOUT_S = 600.0  # one file, click to saved
_PROGRESS_POLL_S = 0.5
_MAX_SCREENS = 60  # grid screens scanned for one row (about 8 rows each)
_CANCEL = re.compile(r"^\s*Cancel\s*$")
_SCRIPT_PROMPT = "Continue or Cancel Script"  # the window's title

# The portal's tabs, in the order it shows them.
CATEGORIES = (
    Category(
        "Exhibits",
        aliases=("exhibits", "exhibit"),
        description="Evidence entered into the hearing record, numbered like H-1",
    ),
    Category(
        "Key Documents",
        aliases=(
            "key documents", "key document", "key docs", "key doc",
            "key files", "key file", "key filings", "key filing",
        ),
        description="The application, the Board's decisions and orders, and other principal filings",
    ),
    Category(
        "Other Documents",
        aliases=(
            "other documents", "other document", "other docs", "other doc",
            "other files", "other file", "other filings", "other filing",
        ),
        description="Correspondence, information requests, submissions and all other filings",
    ),
    Category(
        "Transcripts",
        aliases=("transcripts", "transcript", "hearing transcripts", "hearing transcript"),
        description="Hearing transcripts",
    ),
    Category(
        "Recordings",
        aliases=("recordings", "recording", "audio", "video"),
        description="Audio or video recordings of hearings",
    ),
)

_TAB_COUNT_RE = re.compile(rf"({'|'.join(re.escape(c.name) for c in CATEGORIES)})\s*-\s*(\d+)")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# Exhibit numbers seen live: H-1, H-4(C), H-4(C)-iii, H-5(c)-ii, H-5-iii, and once "A -5" (M12383:
# a stray space, kept in the served name "A -5.pdf"; the id is "A-5", see _compact). Short.
_EXHIBIT_ID_RE = re.compile(r"[A-Za-z]{1,4}\d{0,2} ?- ?\d{1,4}[-()A-Za-z0-9.]{0,14}")
_BUTTONS = frozenset({"Preview", "GO GET IT"})
# Tabs whose rows show no file id: Transcripts (date, description, file type as a word, Security)
# and Recordings (date, title, Security, often empty). Their rows get an id derived from what they
# show (_derived_id), and their files are verified differently (see _export).
_UNNUMBERED = {"Transcripts": "TR", "Recordings": "REC"}  # tab -> prefix of the derived ids
# Transcripts give the file type as a word ("Pdf", "PDF"), not an extension.
_TYPE_WORD_RE = re.compile(r"(?i)pdf|docx?|xlsx?|mp3|mp4|wav")

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

# Per rendered row: its cell texts; `top` on screen; `y`, its offset in the whole list (the grid
# places rows with translate3d(0, y, 0), so y names a row whatever the scroll); `dom`, its index
# among the rendered rows; `active`, whether FileMaker marks it as the active record.
_ROWS_JS = r"""
() => {
  const s = document.querySelector('.v-grid-scroller-vertical');
  const scrolled = s ? s.scrollTop : 0;
  return [...document.querySelectorAll("tr.v-grid-row-has-data")].map((tr, i) => {
    const texts = [...tr.querySelectorAll(".fm-textarea .text, .v-label, .fm-text-character")]
      .map(e => e.innerText.trim()).filter(Boolean);
    const top = tr.getBoundingClientRect().top;
    const m = /translate3d\([^,]*,\s*(-?[\d.]+)px/.exec(tr.style.transform || "");
    return {texts, top, y: Math.round(m ? parseFloat(m[1]) : top + scrolled), dom: i,
            active: !!tr.querySelector(".iwps_body_active")};
  });
}
"""


_ROW_SIGNATURE_JS = (
    "() => [...document.querySelectorAll('tr.v-grid-row-has-data')]"
    ".map(r => r.innerText.slice(0, 12)).join('|')"
)


def _parse_date(text: str | None) -> date | None:
    if text and _DATE_RE.match(text.strip()):
        return datetime.strptime(text.strip(), "%m/%d/%Y").replace(tzinfo=UTC).date()
    return None


def _compact(file_id: str) -> str:
    """A file id without whitespace: the portal shows (and serves) exhibit "A -5" for A-5."""
    return "".join(file_id.split())


def _derived_id(doc_type: str, filed: str | None, parts: list[str]) -> str:
    """A stable id for a row that shows none: tab, date and description, hashed. Like
    "TR-20220912-6c1f0e2a9b". Safe as a filename and as a citation id ([A-Za-z0-9_.-])."""
    day = _parse_date(filed)
    key = "\n".join([doc_type, filed or "", *(" ".join(p.split()) for p in parts)])
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return f"{_UNNUMBERED[doc_type]}-{day.strftime('%Y%m%d') if day else 'undated'}-{digest}"


def _base_id(external_id: str) -> str:
    """A derived id without the "_2" that tells identical rows apart (see _Rows)."""
    return external_id.partition("_")[0]


def parse_row(texts: list[str], doc_type: str | None = None) -> dict | None:
    """Turn the cell texts of one grid row of tab `doc_type` into fields. Order-independent on
    purpose, except for the title of a row without a file id (see _parse_unnumbered)."""
    uniq = list(dict.fromkeys(t for t in texts if t not in _BUTTONS))
    filed = next((t for t in uniq if _DATE_RE.match(t)), None)
    access = next((t for t in uniq if t in _ACCESS_LABELS), UNKNOWN_ACCESS)
    if doc_type in _UNNUMBERED:
        return _parse_unnumbered(uniq, doc_type, filed, access)
    ext = next((t for t in uniq if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", t)), "")
    # Most tabs identify files by a numeric id ("102674"); Exhibits use exhibit numbers
    # ("H-1", "H-4(C)-i"). Either way it is also the name the portal serves the file under.
    file_id = next((t for t in uniq if re.fullmatch(r"\d{3,9}", t)), None) or next(
        (t for t in uniq if _EXHIBIT_ID_RE.fullmatch(t)), None
    )
    rest = [t for t in uniq if t not in {ext, file_id, filed, access}]
    if not file_id or not rest:
        return None
    return {
        "external_id": _compact(file_id),
        "title": max(rest, key=len),
        "filed_on": _parse_date(filed),
        "access": access,
        "file_ext": ext.lower() or ".pdf",
    }


def _parse_unnumbered(uniq: list[str], doc_type: str, filed: str | None, access: str) -> dict | None:
    """A Transcripts or Recordings row: no file id, so it is identified by what it shows.

    Transcripts: "09/12/2022", "September 12, 2022", ["Evening Session"], "Pdf", "Public" (the
    description is a repeating field, its parts in screen order). Recordings: "09/12/2022",
    "M10431 – NS Power 2022 GRA - Monday", and a Security cell that is often empty. The file type
    of a recording only shows once asked for (".mp3", ".wav", ".MP3" seen): file_ext is "" here.
    """
    word = next((t for t in uniq if _TYPE_WORD_RE.fullmatch(t)), "")
    parts = [t for t in uniq if t not in {word, filed, access}]
    if not parts:
        return None
    if access == UNKNOWN_ACCESS and doc_type == "Recordings" and len(parts) == 1:
        # Date and title only: the Security cell is empty. Any other extra cell might be a
        # label we don't know, which stays UNKNOWN_ACCESS (and marks the tab, see _Rows.refs).
        access = UNLABELLED
    return {
        "external_id": _derived_id(doc_type, filed, parts),
        "title": " - ".join(" ".join(p.split()) for p in parts),
        "filed_on": _parse_date(filed),
        "access": access,
        "file_ext": f".{word.lower()}" if word else "",
    }


class _Rows:
    """One tab's rows as read so far, top to bottom, by external id.

    Identical rows without a file id are told apart by order: M10431 lists the recording
    "Tuesday (2(1of2))" of 09/21/2022 twice, stored as two different files; the lower one gets
    "_2". That needs the rows above, so a tab is always read from its top (the grid opens there).
    """

    def __init__(self, doc_type: str):
        self.doc_type = doc_type
        self.parsed: dict[str, dict] = {}  # in screen order
        self.position: dict[str, int] = {}  # id -> y, the row's offset in the whole list
        self._twins: dict[str, list[int]] = {}  # derived id -> positions of rows showing it

    def add(self, row: dict) -> str | None:
        """Parse one row of _ROWS_JS; its external id, or None for a row that isn't a file."""
        parsed = parse_row(row["texts"], self.doc_type)
        if parsed is None:
            return None
        y = row.get("y")
        if self.doc_type in _UNNUMBERED and y is not None:
            seen = self._twins.setdefault(parsed["external_id"], [])
            if y not in seen:
                seen.append(y)
                seen.sort()
            if rank := seen.index(y):
                parsed["external_id"] += f"_{rank + 1}"
        self.parsed.setdefault(parsed["external_id"], parsed)
        self.position.setdefault(parsed["external_id"], y)
        return parsed["external_id"]

    def public(self) -> int:
        return sum(1 for r in self.parsed.values() if r["access"] == PUBLIC)

    def refs(self, provider: str, matter: str, limit: int, *, complete: bool) -> list[DocumentRef]:
        """The rows in screen order up to the `limit`-th public one.

        An empty Security cell on a Recordings row counts as Public when every row of the tab has
        been read (`complete`) and none of them carries any other label. Reasoning: this is the
        Board's public documents database, served to any anonymous visitor; recordings are of
        public hearings; and where the portal restricts a file it says so on the row
        ("Confidential", "Board Only"). The Recordings tab does have a Security column, filled in
        on some rows ("Public" on 3 of M10431's 23, on all 4 of M09548's) and blank on the rest:
        blank there is the absence of a restriction, not a hidden one. A tab that marks any row
        otherwise, or that we didn't read in full, gets no such benefit of the doubt: its blank
        rows stay Unknown and are never downloaded (fail closed).
        """
        rows = list(self.parsed.values())
        marked = any(r["access"] not in {PUBLIC, UNLABELLED} for r in rows)
        unlabelled = PUBLIC if complete and not marked else UNKNOWN_ACCESS
        out: list[DocumentRef] = []
        public = 0
        for r in rows:
            if public >= limit:
                break
            access = unlabelled if r["access"] == UNLABELLED else r["access"]
            out.append(DocumentRef(provider=provider, matter=matter, doc_type=self.doc_type,
                                   row_index=len(out), **{**r, "access": access}))
            public += access == PUBLIC
        return out


def parse_counts(body_text: str) -> dict[str, int]:
    found = {name: int(n) for name, n in _TAB_COUNT_RE.findall(body_text)}
    if len(found) != len(CATEGORIES):
        raise ScrapeError(f"expected {len(CATEGORIES)} tab counts, found {sorted(found)}")
    return {c.name: found[c.name] for c in CATEGORIES}


class _ShortListing(ScrapeError):
    """Fewer rows read than the portal's count implies, and not because rows are non-public."""


def check_listing(refs: list[DocumentRef], *, wanted: int, total: int, final: bool, what: str) -> None:
    """A listing must hold `wanted` public rows, or every one of the tab's `total` rows (the
    shortfall then being non-public rows). Anything less means rows were missed (a grid that
    didn't repaint, a row we couldn't parse): raise on a non-final pass so a fresh session tries
    again; on the final pass deliver what we have rather than nothing, and log it."""
    public = sum(1 for r in refs if r.access == PUBLIC)
    if public >= wanted or len(refs) >= total:
        return
    if not final:
        raise _ShortListing(f"{what}: read {len(refs)} of {total} rows, {public} public of {wanted} wanted")
    log.warning("uarb.short_listing", what=what, read=len(refs), total=total, public=public, wanted=wanted)


def _artifact_path(download: Download) -> str | None:
    """Where the browser writes this download (a Playwright internal: None if that changes)."""
    try:
        path = download._impl_obj._artifact.absolute_path  # type: ignore[attr-defined]
    except AttributeError:
        return None
    return path if isinstance(path, str) and path else None


def _bytes_so_far(path: str) -> int:
    """Chromium writes a download in progress to "<path>.crdownload" and renames it when done."""
    for candidate in (f"{path}.crdownload", path):
        with contextlib.suppress(OSError):
            return os.path.getsize(candidate)
    return 0


async def _discard(download: Download) -> None:
    """Cancel and delete a download (delete alone would wait for an unfinished transfer)."""
    with contextlib.suppress(PlaywrightError, TimeoutError):
        async with asyncio.timeout(10):
            await download.cancel()
            await download.delete()


class UarbProvider:
    name = "uarb"
    display_name = "Nova Scotia Utility and Review Board"
    portal_url = PORTAL_URL
    matter_pattern = MATTER_RE
    mention_pattern = MENTION_RE
    matter_example = "M12205"
    categories = CATEGORIES

    @staticmethod
    def normalise(raw: str) -> str | None:
        m = MENTION_RE.fullmatch(nfkc(raw))
        return f"M{m.group(1)}" if m else None

    def __init__(
        self,
        pool: BrowserPool,
        *,
        sessions_per_matter: int = 3,
        download_lock: Callable[[], AbstractAsyncContextManager] | None = None,
        file_policy: FilePolicy | None = None,
        download_stall_s: float | None = None,
        download_timeout_s: float = DOWNLOAD_TIMEOUT_S,
    ):
        self._pool = pool
        self._sessions_per_matter = sessions_per_matter
        # The portal prepares "GO GET IT" files in state shared across concurrent guest sessions
        # from one client: measured on the live portal, session A asking for 102674 was served
        # the files sessions B and C had just requested. Navigation stays parallel; only the
        # click -> served-file section is serialised (the transfer itself runs outside it). In
        # production this is a Redis lock (agent.limits.Limits.lock) so it also holds across
        # worker processes; it may raise agent.limits.LockTimeout.
        local = asyncio.Lock()
        self._download_lock = download_lock or (lambda: local)
        self._file_policy = file_policy
        # No byte written for this long is a stalled transfer: by default the pool's own timeout
        # for any page action.
        self._stall_s = download_stall_s or getattr(pool, "nav_timeout_ms", 60_000) / 1000
        self._download_timeout_s = download_timeout_s

    @property
    def _policy(self) -> FilePolicy:
        return self._file_policy or FilePolicy.from_settings()

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
        try:
            return await self._fetch_once(matter)
        except MatterNotFound:
            # Same rule as for listings: a negative answer must reproduce in an independent session.
            log.info("uarb.not_found_recheck", matter=matter)
            return await self._fetch_once(matter)

    async def _fetch_once(self, matter: str) -> MatterInfo:
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

    async def _open_tab(self, page: Page, doc_type: str) -> None:
        tab = page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(doc_type)}\s*-\s*\d+\s*$"))
        try:
            await tab.first.click(force=True)
            await page.locator("tr.v-grid-row-has-data").first.wait_for(state="attached")
        except PlaywrightTimeout as e:
            raise PortalUnavailable(f"{doc_type} grid did not load") from e
        except PlaywrightError as e:
            raise PortalUnavailable(f"browser error opening {doc_type}: {e}") from e

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

    async def _next_screen(self, page: Page) -> bool:
        """Scroll the grid one screen and wait (bounded) for the newly exposed rows to paint.
        False at the bottom. No repaint in time is not an error: the caller reads what's there."""
        signature = await page.evaluate(_ROW_SIGNATURE_JS)
        if not await self._scroll_grid(page):
            return False
        try:
            await page.wait_for_function(f"(sig) => ({_ROW_SIGNATURE_JS})() !== sig", arg=signature, timeout=4_000)
        except PlaywrightTimeout:
            pass
        return True

    async def _collect_rows(
        self, page: Page, matter: str, doc_type: str, limit: int, total: int = 0
    ) -> list[DocumentRef]:
        rows = _Rows(doc_type)
        stale_rounds = 0
        # Collect until `limit` *public* rows: confidential rows are listed (so the reply can say
        # they exist) but never downloaded or sent. Recordings are read to the end, as whether
        # their unlabelled rows are public depends on every row of the tab (_Rows.refs).
        whole_tab = doc_type == "Recordings"
        while (whole_tab or rows.public() < limit) and stale_rounds < 3:
            before = len(rows.parsed)
            # The grid positions recycled <tr>s with transforms, so DOM order isn't screen order:
            # read the rows top to bottom as the user sees them (newest first on most tabs).
            for row in sorted(await page.evaluate(_ROWS_JS), key=lambda r: r["top"]):
                rows.add(row)
            if not whole_tab and rows.public() >= limit:
                break
            if not await self._next_screen(page):
                break
            stale_rounds = stale_rounds + 1 if len(rows.parsed) == before else 0
        complete = bool(total) and len(rows.parsed) >= total
        return rows.refs(self.name, matter, limit, complete=complete)

    async def list_documents(self, matter: str, doc_type: str, limit: int) -> list[DocumentRef]:
        _, refs = await self.list_matter_and_documents(matter, doc_type, limit)
        return refs

    async def list_matter_and_documents(
        self, matter: str, doc_type: str, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        """One session for both: the common path for a request (saves a full portal round).

        A first pass that says "not found", or that read fewer rows than the portal's count
        implies, is repeated once in an independent session; that second pass is final.
        """
        try:
            return await self._list_once(matter, doc_type, limit)
        except MatterNotFound:
            # Telling a user their matter doesn't exist is the costliest mistake this agent can
            # make, so a negative answer must reproduce in an independent session.
            log.info("uarb.not_found_recheck", matter=matter)
        except _ShortListing as e:
            log.info("uarb.short_listing_recheck", matter=matter, doc_type=doc_type, error=str(e))
        return await self._list_once(matter, doc_type, limit, final=True)

    async def _list_once(
        self, matter: str, doc_type: str, limit: int, *, final: bool = False
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        async with self._pool.session() as page:
            await self._open_matter(page, matter)
            info = await self._read_matter(page, matter)
            total = info.counts.get(doc_type, 0)
            if total == 0 or limit <= 0:
                return info, []
            await self._open_tab(page, doc_type)
            wanted = min(limit, total)
            refs = await self._collect_rows(page, matter, doc_type, wanted, total)
        check_listing(refs, wanted=wanted, total=total, final=final, what=f"{matter}/{doc_type}")
        return info, refs

    async def _scroll_to_top(self, page: Page) -> None:
        await page.evaluate(
            "() => { const s = document.querySelector('.v-grid-scroller-vertical');"
            " if (s) { s.scrollTop = 0; s.dispatchEvent(new Event('scroll')); } }"
        )
        await page.wait_for_timeout(250)

    async def _row_for(self, page: Page, external_id: str):
        """Locate a row by file id, scrolling the virtualised grid if needed."""
        # spaces around a hyphen are allowed: exhibit A-5 of M12383 shows as "A -5"
        shown = r"\s*-\s*".join(re.escape(part) for part in external_id.split("-"))
        row = page.locator("tr.v-grid-row-has-data").filter(
            has=page.locator(".text", has_text=re.compile(rf"^\s*{shown}\s*$"))
        )
        if not await row.count():
            # rows are newest-first: scan from the top so we never scroll past the target
            await self._scroll_to_top(page)
        for _ in range(12):
            if await row.count():
                return row.first
            if not await self._scroll_grid(page):
                break
            await page.wait_for_timeout(250)  # virtual scroll repaint; bounded by the loop
        raise ScrapeError(f"row {external_id} not found in grid")

    async def _row_by_content(self, page: Page, ref: DocumentRef):
        """Locate a row without a file id, and its position. The grid is read from the top, as for
        the listing, since the id of one of several identical rows depends on the rows above."""
        await self._scroll_to_top(page)
        rows = _Rows(ref.doc_type)
        for _ in range(_MAX_SCREENS):
            for raw in sorted(await page.evaluate(_ROWS_JS), key=lambda r: r["top"]):
                rows.add(raw)
            y = rows.position.get(ref.external_id)
            if y is not None:
                await self._ensure_visible(page, await self._rendered_at(page, y))
                return await self._rendered_at(page, y), y  # that scroll may have recycled the <tr>
            if not await self._next_screen(page):
                break
        raise ScrapeError(f"row {ref.external_id} not found in grid")

    async def _rendered_at(self, page: Page, y: int):
        """The rendered row at offset `y`. Valid until the grid scrolls (rows are recycled)."""
        dom = next((r["dom"] for r in await page.evaluate(_ROWS_JS) if r["y"] == y), None)
        if dom is None:
            raise ScrapeError(f"no row rendered at offset {y}")
        return page.locator("tr.v-grid-row-has-data").nth(dom)

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
        if ref.file_ext:  # known from the listing: don't even ask the portal for it
            self._policy.check_ext(ref.file_ext, ref.external_id)
        row_y = None
        if ref.doc_type in _UNNUMBERED:
            row, row_y = await self._row_by_content(page, ref)
        else:
            row = await self._row_for(page, ref.external_id)
            await self._ensure_visible(page, row)
        go = row.locator("button").filter(has_text=re.compile("go get it", re.IGNORECASE)).first
        dialog = page.locator(".v-window").filter(has_text="Download Files").last
        button = dialog.locator(".fm-download-button").first
        return await self._click_and_save(page, ref, go, button, dest_dir, row_y=row_y)

    async def _click_and_save(
        self, page: Page, ref: DocumentRef, go, button, dest_dir: str, *, row_y: int | None = None
    ) -> DownloadedFile:
        tmp = os.path.join(dest_dir, f".{ref.external_id}.part")
        try:
            # The shared portal state only matters until the portal has started sending a file
            # named after our id: the lock is released there, before the (long) transfer.
            async with self._download_lock():
                download = await self._start_download(page, ref, go, button, row_y=row_y)
            await self._save(download, tmp, ref.external_id)
        finally:
            await self._dismiss_modals(page)
        # The served type wins over the listing's (a recording's is only known now).
        ext = os.path.splitext(download.suggested_filename or "")[1].lower()
        if ext and ext != ref.file_ext:
            ref = ref.model_copy(update={"file_ext": ext})
        return finalise_download(tmp, ref, download.suggested_filename, dest_dir, self._policy)

    async def _start_download(
        self, page: Page, ref: DocumentRef, go, button, *, row_y: int | None = None
    ) -> Download:
        """Click GO GET IT, then the dialog's file: the download, once verified to be our file.

        Most tabs go straight to the "Download Files" dialog; Transcripts and Recordings first ask
        for a filename (see _export). `row_y` is the clicked row's offset in the grid, if known.
        """
        export = page.locator(".v-window").filter(has_text="Export Field to File").last
        await self._open_dialog(page, ref, go, button.or_(export).first)
        if await export.is_visible():
            await self._export(page, ref, go, export, row_y)
            await button.wait_for(state="visible")
        elif ref.doc_type in _UNNUMBERED:
            raise ScrapeError(f"{ref.external_id}: no export dialog, nothing to verify the file by")
        caption = (await button.inner_text()).strip()
        async with page.expect_download(timeout=120_000) as dl_info:
            await button.click()
        download = await dl_info.value
        # Integrity: the portal names each file after its id (or, for an exported field, the
        # name we gave it, our id). GO GET IT acts on FileMaker's active record, which in a
        # fresh session can still be row 0 rather than the row we clicked; without this check
        # the user would get the wrong document under this title. (Clicking the id cell to
        # select the row navigates away, so it isn't an option.) The click does make our row
        # active, so the caller's retry gets the right file.
        served = _compact(os.path.splitext(download.suggested_filename or caption)[0])
        if served != ref.external_id:
            await _discard(download)
            raise ScrapeError(f"asked for {ref.external_id}, portal served {served!r}")
        return download

    async def _open_dialog(self, page: Page, ref: DocumentRef, go, dialog) -> None:
        """Click GO GET IT until `dialog` shows (the portal now and then ignores a click)."""
        for attempt in range(3):
            await self._cancel_script_prompt(page)  # it would swallow the click
            await go.click(force=True)
            try:
                await dialog.wait_for(state="visible", timeout=6_000 + attempt * 6_000)
                return
            except PlaywrightTimeout:
                continue
        raise PortalUnavailable(f"download dialog never opened for {ref.external_id}")

    async def _export(self, page: Page, ref: DocumentRef, go, dialog, row_y: int | None) -> None:
        """Answer the "Export Field to File" dialog that GO GET IT opens on Transcripts and
        Recordings rows, leaving the "Download Files" dialog for our file.

        The dialog is prefilled with the name the file was stored under, which is no use for
        checking we got the right file: it is often unrelated to the row ("Trk11.wav",
        "september 21.wav" for M10431 recordings), and the portal serves it verbatim and unquoted,
        so a comma in it ("NSUARB-M10431-September 12, 2022.pdf") makes Chromium refuse the
        download (ERR_RESPONSE_HEADERS_MULTIPLE_CONTENT_DISPOSITION). Instead:
          - the export must be for our row: two GO GET IT clicks in a row propose the same file
            (the first can still act on the previously active record, see _start_download), and
            the row the portal then marks as active is the one we clicked;
          - the file type (the proposed name's extension) must be one we deliver, before any byte;
          - the file is exported under our id, which the served name is then checked against.
        """
        field = dialog.locator("input")
        name = await self._proposed_name(page, field)
        for _ in range(3):
            await self._cancel(page, dialog)
            await self._open_dialog(page, ref, go, dialog)
            again = await self._proposed_name(page, field)
            if again == name:
                break
            name = again
        else:
            raise ScrapeError(f"{ref.external_id}: GO GET IT kept proposing different files")
        await self._check_active_row(page, ref, row_y)
        ext = os.path.splitext(name)[1] or ref.file_ext
        self._policy.check_ext(ext.lower(), ref.external_id)
        await field.fill(f"{ref.external_id}{ext}")
        await dialog.locator(".v-button").filter(has_text=re.compile(r"^\s*OK\s*$")).first.click()

    async def _proposed_name(self, page: Page, field) -> str:
        """The export dialog's filename (sent with the dialog, but an empty read is retried)."""
        try:
            await page.wait_for_function(
                "el => el.value.trim() !== ''", arg=await field.element_handle(), timeout=5_000
            )
        except PlaywrightTimeout as e:
            raise PortalUnavailable("export dialog without a filename") from e
        return (await field.input_value()).strip()

    async def _cancel(self, page: Page, dialog) -> None:
        """Cancel the export dialog, then the prompt FileMaker follows that with."""
        await dialog.locator(".v-button").filter(has_text=_CANCEL).first.click()
        await dialog.wait_for(state="detached", timeout=10_000)
        with contextlib.suppress(PlaywrightTimeout):
            await page.locator(".v-window").filter(has_text=_SCRIPT_PROMPT).last.wait_for(
                state="visible", timeout=5_000
            )
        await self._cancel_script_prompt(page)

    async def _cancel_script_prompt(self, page: Page) -> None:
        """"Export Field Contents has been canceled. Do you wish to continue with this script?"
        follows a cancelled export, a little later; until answered, GO GET IT does nothing."""
        prompt = page.locator(".v-window").filter(has_text=_SCRIPT_PROMPT)
        if await prompt.count():
            await prompt.last.locator(".v-button").filter(has_text=_CANCEL).first.click()
            await prompt.last.wait_for(state="detached", timeout=10_000)

    async def _check_active_row(self, page: Page, ref: DocumentRef, row_y: int | None) -> None:
        """The row FileMaker marks as its active record must be `ref`'s (and at `row_y`)."""
        active = [r for r in await page.evaluate(_ROWS_JS) if r["active"]]
        parsed = parse_row(active[0]["texts"], ref.doc_type) if len(active) == 1 else None
        wanted = _base_id(ref.external_id) if ref.doc_type in _UNNUMBERED else ref.external_id
        if parsed is None or parsed["external_id"] != wanted or row_y not in (None, active[0]["y"]):
            shown = [" ".join(r["texts"])[:80] for r in active]
            raise ScrapeError(f"{ref.external_id}: the portal's active row is not the one clicked: {shown}")

    async def _save(self, download: Download, tmp: str, what: str) -> None:
        """Wait, bounded, for the browser to finish `download`, then copy it to `tmp`.

        Playwright's failure() and save_as() wait for the transfer with no timeout of their own,
        so the file in progress is watched instead: no growth for the stall interval, more than
        max_file_bytes, or the overall download deadline cancels the transfer.
        """
        loop = asyncio.get_running_loop()
        path = _artifact_path(download)
        max_bytes = self._policy.max_bytes
        deadline = loop.time() + self._download_timeout_s
        last_size, last_growth = 0, loop.time()
        finished = asyncio.ensure_future(download.failure())
        try:
            while not (await asyncio.wait({finished}, timeout=_PROGRESS_POLL_S))[0]:
                now = loop.time()
                if path is not None:
                    size = _bytes_so_far(path)
                    if size > max_bytes:
                        raise TooLarge(f"download of {what}: over the {max_bytes}-byte limit per file")
                    if size > last_size:
                        last_size, last_growth = size, now
                    elif now - last_growth > self._stall_s:
                        raise PortalUnavailable(f"download of {what} stalled at {size} bytes")
                if now > deadline:
                    raise PortalUnavailable(f"download of {what} not finished in {self._download_timeout_s:.0f} s")
            failure = finished.result()
            if failure:
                raise PortalUnavailable(f"download of {what} failed: {failure}")
            try:
                async with asyncio.timeout(max(60.0, deadline - loop.time())):
                    await download.save_as(tmp)  # a local copy of the finished file
            except TimeoutError as e:
                raise PortalUnavailable(f"saving the download of {what} timed out") from e
        except BaseException:
            finished.cancel()
            await _discard(download)
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)
            raise

    async def _download_worker(
        self, matter: str, refs: list[DocumentRef], dest_dir: str, out: asyncio.Queue
    ) -> None:
        async with self._pool.session() as page:
            try:
                await self._open_matter(page, matter)
            except MatterNotFound as e:
                # The matter was listed moments ago, so "No Records Found" here is the portal's
                # search race, never an answer for the user: retryable.
                raise ScrapeError(f"{matter} not found in a download session (listed moments ago)") from e
            await self._open_tab(page, refs[0].doc_type)
            for ref in refs:
                await out.put(await self._download_with_retries(page, ref, dest_dir))

    async def _download_with_retries(
        self, page: Page, ref: DocumentRef, dest_dir: str
    ) -> DownloadedFile | AgentError:
        """One file, retried in this session on any portal or browser error. The final error is
        returned, not raised, so one bad file never costs the rest of the shard."""
        error: AgentError = ScrapeError(f"{ref.external_id}: not attempted")
        for attempt in range(3):
            try:
                return await self._download_one(page, ref, dest_dir)
            except LockTimeout as e:
                # Other sessions held the portal's download step the whole wait: skip this file
                # rather than wait as long again (the request's own retry picks it up).
                return PortalUnavailable(f"download lock timed out for {ref.external_id}: {e}")
            except AgentError as e:
                if not e.retryable:  # too large, a file type we don't deliver: final for this file
                    return e
                error = e
            except PlaywrightError as e:  # any browser failure, not only timeouts
                error = PortalUnavailable(f"browser error downloading {ref.external_id}: {e}")
            log.warning("uarb.download_retry", file=ref.external_id, attempt=attempt, error=str(error)[:200])
        return error

    async def _dismiss_modals(self, page: Page) -> None:
        """Close every open dialog so the modal curtain can't swallow the next click. ("Close" on
        "Download Files", "Cancel" on "Export Field to File".)"""
        for _ in range(3):
            closes = await page.locator(".v-window .v-button").filter(
                has_text=re.compile(r"^\s*(Close|Cancel)\s*$")
            ).all()
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
        delivered = 0
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
                        delivered += 1
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
            if not delivered:  # every file failed (per file, or its whole session did)
                raise error_to_raise(errors)

