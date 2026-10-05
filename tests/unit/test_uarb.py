"""UARB provider without a portal: row parsing, matter numbers, listing passes, the download
worker's per-file error handling, the download lock's extent, and the bounded transfer wait.
The Transcripts/Recordings export flow runs in Chromium against a fake portal built from the
portal's own dialogs (skipped where Chromium isn't installed).

Grid rows below are as the live portal renders them (M12205, M10431, M09548 and M12383, read
2026-10-04); tests/fixtures/uarb_*_rows.json are the screens _ROWS_JS read while scrolling.
"""

import asyncio
import html
import json
import re
import urllib.parse
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from agent.limits import LockTimeout
from agent.models import (
    DocumentRef,
    DownloadedFile,
    MatterInfo,
    MatterNotFound,
    PortalUnavailable,
    ScrapeError,
    TooLarge,
)
from agent.providers import uarb
from agent.providers.browser import BrowserPool
from agent.providers.files import FilePolicy, UnsupportedFileType
from agent.providers.uarb import UarbProvider, check_listing, parse_row

MATTER = "M12205"
PDF = b"%PDF-1.6\n" + b"x" * 2_000
POLICY = FilePolicy.of(1_000_000, [".pdf"])
FIXTURES = Path(__file__).parent.parent / "fixtures"


def screens(name: str) -> list[list[dict]]:
    return json.loads((FIXTURES / f"{name}_rows.json").read_text())["screens"]


def read_tab(name: str, doc_type: str) -> uarb._Rows:
    """Every row of a captured tab, read screen by screen as _collect_rows does."""
    rows = uarb._Rows(doc_type)
    for screen in screens(name):
        for row in sorted(screen, key=lambda r: r["top"]):
            rows.add(row)
    return rows


class FakePool:
    nav_timeout_ms = 1_000

    @asynccontextmanager
    async def session(self):
        yield object()


def info(**counts: int) -> MatterInfo:
    return MatterInfo(
        provider="uarb", matter=MATTER, title="t",
        counts={c.name: counts.get(c.name.split()[0], 0) for c in uarb.CATEGORIES},
        portal_url=uarb.PORTAL_URL, fetched_at=datetime.now(UTC),
    )


def ref(external_id: str, access: str = "Public", **kw: Any) -> DocumentRef:
    return DocumentRef(provider="uarb", matter=MATTER, doc_type="Other Documents", external_id=external_id,
                       title=f"Doc {external_id}", access=access, **kw)


# ---------------------------------------------------------------- rows and access labels


@pytest.mark.parametrize(
    ("texts", "external_id", "access"),
    [
        (["102674", "Board Order", "07/08/2026", "Public", "Preview", "GO GET IT", ".pdf"], "102674", "Public"),
        (["General Rate Application - Direct Evidence - Confidential", "01/27/2022", "Confidential", "N-2(C)",
          "Preview", "GO GET IT", ".pdf"], "N-2(C)", "Confidential"),
        (["General Rate Application - Direct Evidence - Board Confidential", "01/27/2022", "Board Only",
          "N-2(BC)", "Preview", "GO GET IT", ".pdf"], "N-2(BC)", "Board Only"),
        # No label, or one we don't know: fail closed.
        (["102674", "Board Order", "07/08/2026", "Preview", "GO GET IT", ".pdf"], "102674", "Unknown"),
        (["102674", "Board Order", "07/08/2026", "Sealed", "Preview", "GO GET IT", ".pdf"], "102674", "Unknown"),
        (["102674", "Board Order", "07/08/2026", "public", "Preview", "GO GET IT", ".pdf"], "102674", "Unknown"),
    ],
    ids=["public", "confidential", "board-only", "no-label", "unknown-label", "not-exactly-public"],
)
def test_access_labels_fail_closed(texts, external_id, access):
    row = parse_row(texts)
    assert (row["external_id"], row["access"]) == (external_id, access)
    assert row["title"] not in {"Board Only", "Sealed"}


# ---------------------------------------------------------------- matter numbers


def test_matter_numbers_are_ascii_only_but_full_width_input_is_normalised():
    assert UarbProvider.normalise("Ｍ１２２０５") == MATTER  # NFKC first
    assert UarbProvider.normalise("matter no. １２２０５") == MATTER
    assert uarb.MATTER_RE.fullmatch("M１２２０５") is None
    assert uarb.MENTION_RE.search("please send M１２２０５") is None


# ---------------------------------------------------------------- listings


class GridPage:
    """Just enough of a Page for _collect_rows: screenfuls of rows, one per scroll, then the bottom."""

    def __init__(self, *screens: list[dict]):
        self.screens = screens
        self.shown = 0

    async def evaluate(self, script: str, *args: Any) -> Any:
        if script == uarb._ROWS_JS:
            return self.screens[self.shown]
        if script == uarb._ROW_SIGNATURE_JS:
            return f"sig{self.shown}"
        if "clientHeight" in script and self.shown + 1 < len(self.screens):  # _scroll_grid
            self.shown += 1
            return True
        return False  # the scroller is at the bottom

    async def wait_for_function(self, *args: Any, **kw: Any) -> None:
        return None


async def test_rows_are_read_in_screen_order_not_dom_order():
    # The grid recycles <tr>s and positions them with transforms: DOM order is not screen order.
    rows = [
        {"texts": ["102417", "Letter", "06/17/2026", "Public", ".pdf"], "top": 474},
        {"texts": ["102674", "Board Order", "07/08/2026", "Public", ".pdf"], "top": 338},
        {"texts": ["102454", "Reply", "06/22/2026", "Public", ".pdf"], "top": 406},
    ]
    provider = UarbProvider(FakePool(), file_policy=POLICY)

    refs = await provider._collect_rows(GridPage(rows), MATTER, "Other Documents", 3)

    assert [(r.external_id, r.row_index) for r in refs] == [("102674", 0), ("102454", 1), ("102417", 2)]


@pytest.mark.parametrize(
    ("refs", "wanted", "total", "short"),
    [
        ([ref("1"), ref("2")], 2, 5, False),  # the limit reached
        ([ref("1"), ref("2", "Confidential"), ref("3", "Board Only")], 3, 3, False),  # every row read
        ([ref("1")], 2, 5, True),  # rows missed
        ([ref("1"), ref("2", "Confidential")], 2, 3, True),  # rows missed, not explained by confidential ones
        ([], 1, 1, True),
    ],
)
def test_a_short_listing_is_detected(refs, wanted, total, short):
    if short:
        with pytest.raises(uarb._ShortListing):
            check_listing(refs, wanted=wanted, total=total, final=False, what="x")
    else:
        check_listing(refs, wanted=wanted, total=total, final=False, what="x")
    check_listing(refs, wanted=wanted, total=total, final=True, what="x")  # the final pass never raises


def listing_provider(monkeypatch, passes: list[list[DocumentRef]]) -> tuple[UarbProvider, list[int]]:
    provider = UarbProvider(FakePool(), file_policy=POLICY)
    seen: list[int] = []

    async def noop(*args):
        return None

    async def read_matter(page, matter):
        return info(Other=5)

    async def collect_rows(page, matter, doc_type, limit, total=0):
        seen.append(limit)
        return passes[len(seen) - 1]

    monkeypatch.setattr(provider, "_open_matter", noop)
    monkeypatch.setattr(provider, "_open_tab", noop)
    monkeypatch.setattr(provider, "_read_matter", read_matter)
    monkeypatch.setattr(provider, "_collect_rows", collect_rows)
    return provider, seen


async def test_a_short_first_pass_is_repeated_in_a_fresh_session(monkeypatch):
    provider, seen = listing_provider(monkeypatch, [[ref("1")], [ref("1"), ref("2"), ref("3")]])

    _, refs = await provider.list_matter_and_documents(MATTER, "Other Documents", 3)

    assert len(seen) == 2 and [r.external_id for r in refs] == ["1", "2", "3"]


async def test_a_short_final_pass_delivers_what_it_has(monkeypatch):
    provider, seen = listing_provider(monkeypatch, [[ref("1")], [ref("1"), ref("2")]])

    refs = await provider.list_documents(MATTER, "Other Documents", 3)

    assert len(seen) == 2 and [r.external_id for r in refs] == ["1", "2"]


async def test_a_full_first_pass_is_not_repeated(monkeypatch):
    provider, seen = listing_provider(monkeypatch, [[ref("1"), ref("2")]])

    await provider.list_matter_and_documents(MATTER, "Other Documents", 2)

    assert len(seen) == 1


# ---------------------------------------------------------------- the download worker


def downloaded(r: DocumentRef) -> DownloadedFile:
    return DownloadedFile(ref=r, path=f"/x/{r.external_id}", sha256="0" * 64, size=1, filename=r.external_id)


def download_provider(monkeypatch, outcomes: dict[str, list[Any]], **kw: Any) -> tuple[UarbProvider, list[str]]:
    """_download_one plays `outcomes[id]` in order: an exception to raise, or anything else to succeed."""
    provider = UarbProvider(FakePool(), sessions_per_matter=1, file_policy=POLICY, **kw)
    calls: list[str] = []

    async def noop(*args):
        return None

    async def download_one(page, r, dest_dir):
        calls.append(r.external_id)
        outcome = outcomes[r.external_id].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return downloaded(r)

    monkeypatch.setattr(provider, "_open_matter", noop)
    monkeypatch.setattr(provider, "_open_tab", noop)
    monkeypatch.setattr(provider, "_download_one", download_one)
    return provider, calls


async def collect(provider: UarbProvider, refs: list[DocumentRef], tmp_path: Path) -> list[DownloadedFile]:
    return [f async for f in provider.download(MATTER, refs, str(tmp_path))]


async def test_a_lock_timeout_skips_one_file_not_the_rest_of_the_shard(monkeypatch, tmp_path):
    provider, calls = download_provider(monkeypatch, {"1": [LockTimeout("uarb:download")], "2": [None]})

    files = await collect(provider, [ref("1"), ref("2")], tmp_path)

    assert [f.ref.external_id for f in files] == ["2"]
    assert calls == ["1", "2"]  # the lock timeout is not waited out again for the same file


async def test_any_browser_error_is_retried_per_file(monkeypatch, tmp_path):
    crash = PlaywrightError("Target page, context or browser has been closed")
    provider, calls = download_provider(monkeypatch, {"1": [crash, None], "2": [None]})

    files = await collect(provider, [ref("1"), ref("2")], tmp_path)

    assert sorted(f.ref.external_id for f in files) == ["1", "2"]
    assert calls == ["1", "1", "2"]


async def test_a_file_over_the_budget_is_skipped_without_retrying(monkeypatch, tmp_path):
    provider, calls = download_provider(monkeypatch, {"1": [TooLarge("1: too big")], "2": [None]})

    files = await collect(provider, [ref("1"), ref("2")], tmp_path)

    assert [f.ref.external_id for f in files] == ["2"] and calls == ["1", "2"]


async def test_when_every_file_fails_the_retryable_error_is_raised(monkeypatch, tmp_path):
    provider, _ = download_provider(monkeypatch, {
        "1": [TooLarge("1: too big")],
        "2": [PortalUnavailable("slow")] * 3,
    })

    with pytest.raises(PortalUnavailable):
        await collect(provider, [ref("1"), ref("2")], tmp_path)


async def test_when_every_file_is_too_large_the_user_hears_so(monkeypatch, tmp_path):
    provider, _ = download_provider(monkeypatch, {"1": [TooLarge("1: too big")]})

    with pytest.raises(TooLarge):
        await collect(provider, [ref("1")], tmp_path)


async def test_a_listed_type_outside_the_allowlist_is_never_clicked(tmp_path):
    provider = UarbProvider(FakePool(), file_policy=POLICY)

    with pytest.raises(UnsupportedFileType):
        await provider._download_one(object(), ref("1", file_ext=".zip"), str(tmp_path))


async def test_a_download_session_not_found_is_retryable(monkeypatch, tmp_path):
    provider = UarbProvider(FakePool(), file_policy=POLICY)

    async def not_found(page, matter):
        raise MatterNotFound(matter)

    monkeypatch.setattr(provider, "_open_matter", not_found)

    with pytest.raises(Exception) as exc:
        await collect(provider, [ref("1")], tmp_path)
    assert not isinstance(exc.value, MatterNotFound) and exc.value.retryable


# ---------------------------------------------------------------- the download lock and the transfer


class FakeDownload:
    """A Playwright Download whose transfer the test controls; the browser's in-progress file is
    `<path>.crdownload`, like Chromium's."""

    def __init__(self, path: Path | None, name: str = "102674.pdf"):
        self.suggested_filename = name
        self.finished: asyncio.Future = asyncio.get_running_loop().create_future()
        self.cancelled = False
        if path is not None:
            self._impl_obj = SimpleNamespace(_artifact=SimpleNamespace(absolute_path=str(path)))

    async def failure(self):
        return await asyncio.shield(self.finished)

    async def save_as(self, target):
        Path(target).write_bytes(PDF)

    async def cancel(self):
        self.cancelled = True
        if not self.finished.done():
            self.finished.set_result("canceled")

    async def delete(self):
        return None


async def test_the_lock_covers_the_click_but_not_the_transfer(monkeypatch, tmp_path):
    lock = asyncio.Lock()
    provider = UarbProvider(FakePool(), download_lock=lambda: lock, file_policy=POLICY)
    held: dict[str, bool] = {}

    async def start_download(page, r, go, button, **kw):
        held["start"] = lock.locked()
        download = FakeDownload(None)
        download.finished.set_result(None)
        return download

    real_save = provider._save

    async def save(download, tmp, what):
        held["save"] = lock.locked()
        await real_save(download, tmp, what)

    async def no_modals(page):
        return None

    monkeypatch.setattr(provider, "_start_download", start_download)
    monkeypatch.setattr(provider, "_save", save)
    monkeypatch.setattr(provider, "_dismiss_modals", no_modals)

    f = await provider._click_and_save(object(), ref("102674"), None, None, str(tmp_path))

    assert held == {"start": True, "save": False}
    assert Path(f.path).read_bytes() == PDF and f.filename == "102674.pdf"


async def test_a_stalled_transfer_is_cancelled(tmp_path):
    artifact = tmp_path / "artifact"
    (tmp_path / "artifact.crdownload").write_bytes(b"%PDF-")  # five bytes, then nothing
    download = FakeDownload(artifact)
    provider = UarbProvider(FakePool(), file_policy=POLICY, download_stall_s=0.3)

    with pytest.raises(PortalUnavailable, match="stalled"):
        await asyncio.wait_for(provider._save(download, str(tmp_path / ".part"), "102674"), timeout=5)
    assert download.cancelled and not (tmp_path / ".part").exists()


async def test_a_transfer_over_the_budget_is_cancelled(tmp_path):
    artifact = tmp_path / "artifact"
    (tmp_path / "artifact.crdownload").write_bytes(b"x" * 2_000)
    download = FakeDownload(artifact)
    provider = UarbProvider(FakePool(), file_policy=FilePolicy.of(1_000, [".pdf"]), download_stall_s=30)

    with pytest.raises(TooLarge):
        await asyncio.wait_for(provider._save(download, str(tmp_path / ".part"), "102674"), timeout=5)
    assert download.cancelled


async def test_a_transfer_is_bounded_overall_even_without_progress_information(tmp_path):
    download = FakeDownload(None)  # Playwright internals changed: no file to watch
    provider = UarbProvider(FakePool(), file_policy=POLICY, download_stall_s=30, download_timeout_s=0.3)

    with pytest.raises(PortalUnavailable, match="not finished"):
        await asyncio.wait_for(provider._save(download, str(tmp_path / ".part"), "102674"), timeout=5)
    assert download.cancelled


async def test_a_growing_transfer_is_not_a_stall(tmp_path):
    artifact = tmp_path / "artifact"
    progress = tmp_path / "artifact.crdownload"
    download = FakeDownload(artifact)
    provider = UarbProvider(FakePool(), file_policy=POLICY, download_stall_s=0.8)

    async def transfer():
        for i in range(1, 6):
            progress.write_bytes(b"x" * 100 * i)
            await asyncio.sleep(0.4)
        progress.rename(artifact)
        download.finished.set_result(None)

    writer = asyncio.create_task(transfer())
    await asyncio.wait_for(provider._save(download, str(tmp_path / ".part"), "102674"), timeout=10)
    await writer
    assert (tmp_path / ".part").read_bytes() == PDF and not download.cancelled


def test_the_stall_interval_follows_the_pools_action_timeout():
    pool = BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=3_000)
    assert UarbProvider(pool)._stall_s == 3.0
    assert UarbProvider(pool, download_stall_s=9)._stall_s == 9


# ---------------------------------------------------------------- browser pool


async def test_closing_a_pool_twice_or_unlaunched_is_harmless():
    pool = BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000)
    await pool.close()
    await pool.close()


async def test_a_failing_session_setup_does_not_leak_the_context(monkeypatch):
    closed: list[bool] = []

    class Ctx:
        def set_default_timeout(self, ms): ...
        def set_default_navigation_timeout(self, ms): ...

        async def route(self, *args):
            raise PlaywrightError("route setup failed")

        async def close(self):
            closed.append(True)

    class Browser:
        async def new_context(self, **kw):
            return Ctx()

    pool = BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000)

    async def ensure_browser():
        return Browser()

    monkeypatch.setattr(pool, "_ensure_browser", ensure_browser)

    with pytest.raises(PlaywrightError):
        async with pool.session():
            pass
    assert closed == [True]
    async with asyncio.timeout(1):  # and the session slot was given back
        async with pool._sem:
            pass


def test_dates_on_rows_are_parsed():
    assert parse_row(["102674", "Board Order", "07/08/2026", "Public", ".pdf"])["filed_on"] == date(2026, 7, 8)


# ---------------------------------------------------------------- rows without a file id


SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # agent.citations.claims._SAFE_ID


def test_transcript_rows_get_stable_ids_from_what_they_show():
    refs = read_tab("uarb_m10431_transcripts", "Transcripts").refs("uarb", "M10431", 99, complete=True)

    assert len(refs) == 10 and len({r.external_id for r in refs}) == 10
    # Persisted ids (documents table, citations): the derivation must not drift.
    assert [(r.external_id, r.title) for r in refs[:2]] == [
        ("TR-20220912-6036cb0ed5", "September 12, 2022"),
        ("TR-20220912-a1cfd07845", "September 12, 2022 - Evening Session"),
    ]
    assert all(SAFE_ID.fullmatch(r.external_id) for r in refs)
    assert {(r.access, r.file_ext) for r in refs} == {("Public", ".pdf")}  # "Pdf" and "PDF" alike
    assert refs[0].filed_on == date(2022, 9, 12)


def test_a_derived_id_ignores_the_type_and_security_cells():
    row = ["09/13/2022", "September 13, 2022", "Pdf", "Public", "Preview", "GO GET IT"]
    relabelled = ["09/13/2022", "September 13, 2022", "PDF", "Confidential", "Preview", "GO GET IT"]
    assert parse_row(row, "Transcripts")["external_id"] == parse_row(relabelled, "Transcripts")["external_id"]
    assert parse_row(row, "Recordings")["external_id"].startswith("REC-20220913-")  # tab-scoped


def test_identical_recordings_get_distinct_ids_in_screen_order():
    rows = read_tab("uarb_m10431_recordings", "Recordings")
    refs = rows.refs("uarb", "M10431", 99, complete=True)

    assert len(refs) == 23  # the tab's count, twins included
    twins = [r for r in refs if r.title.endswith("Tuesday (2(1of2))")]
    assert [r.external_id for r in twins] == ["REC-20220921-90f0524996", "REC-20220921-90f0524996_2"]
    assert rows.position[twins[0].external_id] < rows.position[twins[1].external_id]
    assert {r.file_ext for r in refs} == {""}  # a recording's type only shows once asked for


def test_blank_security_on_a_recordings_tab_with_no_other_label_is_public():
    refs = read_tab("uarb_m10431_recordings", "Recordings").refs("uarb", "M10431", 99, complete=True)
    # 20 of the 23 rows have an empty Security cell, 3 say "Public"; none says anything else.
    assert {r.access for r in refs} == {"Public"}


def with_row(name: str, y: int, texts: list[str]) -> list[list[dict]]:
    """The captured screens with the row at offset `y` showing `texts` instead."""
    return [[{**r, "texts": texts} if r["y"] == y else r for r in screen] for screen in screens(name)]


@pytest.mark.parametrize(
    ("change", "complete"),
    [
        ((1428, ["09/22/2022", "M10431 - NS Power GRA - Thursday 2/3", "Confidential"]), True),
        ((1428, ["09/22/2022", "M10431 - NS Power GRA - Thursday 2/3", "Board Only"]), True),
        ((1428, ["09/22/2022", "M10431 - NS Power GRA - Thursday 2/3", "Sealed"]), True),  # unknown label
        (None, False),  # a row of the tab not read: can't say no row is marked
    ],
    ids=["confidential", "board-only", "unknown-label", "incomplete"],
)
def test_blank_security_stays_unknown_unless_the_whole_tab_is_unmarked(change, complete):
    rows = uarb._Rows("Recordings")
    for screen in with_row("uarb_m10431_recordings", *change) if change else screens("uarb_m10431_recordings"):
        for row in sorted(screen, key=lambda r: r["top"]):
            rows.add(row)

    refs = rows.refs("uarb", "M10431", 99, complete=complete)

    by_title = {r.title: r.access for r in refs}
    assert by_title["M10431 – NS Power 2022 GRA - Monday"] == "Unknown"  # a blank one: fail closed
    assert by_title["M10431 - NS Power GRA - thusday 3/3"] == "Public"  # labelled ones keep their label
    assert sum(r.access == "Public" for r in refs) == 1 + (change is None)


def test_parse_row_alone_never_calls_an_unlabelled_row_public():
    recording = parse_row(["09/12/2022", "M10431 – NS Power 2022 GRA - Monday", "GO GET IT"], "Recordings")
    transcript = parse_row(["09/12/2022", "September 12, 2022", "Pdf", "Preview", "GO GET IT"], "Transcripts")
    assert recording["access"] == uarb.UNLABELLED != "Public"  # only _Rows.refs may resolve it
    assert transcript["access"] == "Unknown"  # no benefit of the doubt outside Recordings


async def test_the_recordings_tab_is_read_to_the_end_even_for_a_few_files():
    page = GridPage(*screens("uarb_m10431_recordings"))
    provider = UarbProvider(FakePool(), file_policy=POLICY)

    refs = await provider._collect_rows(page, "M10431", "Recordings", 2, 23)

    assert page.shown == 2  # all three screens read
    assert [(r.title, r.access, r.row_index) for r in refs] == [
        ("M10431 – NS Power 2022 GRA - Monday", "Public", 0),
        ("M10431 – NS Power 2022 GRA - Monday Evening Session", "Public", 1),
    ]


async def test_a_recordings_tab_read_short_of_its_count_keeps_blank_rows_unknown():
    provider = UarbProvider(FakePool(), file_policy=POLICY)

    page = GridPage(*screens("uarb_m10431_recordings"))

    refs = await provider._collect_rows(page, "M10431", "Recordings", 2, 24)

    assert len(refs) == 23 and sum(r.access == "Public" for r in refs) == 2  # the two labelled "Public"


def test_an_exhibit_number_with_a_stray_space():
    # M12383 lists six exhibits; A-5 shows as "A -5" (and is served as "A -5.pdf").
    refs = read_tab("uarb_m12383_exhibits", "Exhibits").refs("uarb", "M12383", 99, complete=True)
    assert sorted(r.external_id for r in refs) == ["A-1", "A-2", "A-3", "A-4", "A-5", "A-6"]


# ---------------------------------------------------------------- the export flow, in Chromium


@pytest.fixture
async def chromium():
    pool = BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=5_000)
    try:
        async with pool.session():
            pass
    except PlaywrightError as e:
        await pool.close()
        pytest.skip(f"needs Chromium (playwright install chromium): {str(e)[:80]}")
    yield pool
    await pool.close()


# GO GET IT on a fake portal: like the real one, it acts on FileMaker's active record (in a fresh
# session the first click can act on the previous one: `stale`) and makes the clicked row active
# (unless `activate` is off); on Transcripts and Recordings it opens "Export Field to File"
# prefilled with the stored name, whose Cancel is followed by the script prompt, and whose OK
# opens "Download Files" for the exported record under the name typed. The dialogs are the
# portal's own HTML.
_PORTAL_JS = """
let active = 0, fresh = true;
function add(html) {
  const d = document.createElement('div'); d.innerHTML = html;
  return document.body.appendChild(d.firstElementChild);
}
function on(w, caption, fn) {
  [...w.querySelectorAll('.v-button')].find(b => b.innerText.trim() === caption)
    .onclick = () => { w.remove(); fn(); };
}
function goGetIt(i) {
  if (document.querySelector('.v-window')) return;  // a dialog is open: the click goes nowhere
  const rec = CFG.stale && fresh ? active : i;
  fresh = false;
  if (CFG.activate) {
    active = i;
    document.querySelectorAll('.body').forEach((b, j) => b.classList.toggle('iwps_body_active', j === i));
  }
  if (!CFG.export) return downloads(rec, CFG.stored[rec]);
  const w = add(CFG.dialogs.export);
  const field = w.querySelector('input');
  field.value = CFG.stored[rec];
  on(w, 'Cancel', () => setTimeout(() => {
    const p = add(CFG.dialogs.prompt); on(p, 'Cancel', () => {}); on(p, 'Continue', () => {});
  }, 150));
  on(w, 'OK', () => downloads(rec, field.value));
}
function downloads(rec, name) {
  const w = add(CFG.dialogs.download);
  const b = w.querySelector('.fm-download-button');
  b.querySelector('.v-button-caption').textContent = name;
  b.onclick = () => {
    const f = document.createElement('iframe'); f.style.display = 'none';
    f.src = `/file/${rec}/${encodeURIComponent(name)}`; document.body.appendChild(f);
  };
  on(w, 'Close', () => {});
}
"""


def portal_page(rows: list[list[str]], stored: list[str], **cfg: bool) -> bytes:
    files = {"export": "export_dialog", "prompt": "script_prompt", "download": "download_dialog"}
    dialogs = {k: (FIXTURES / f"uarb_{f}.html").read_text() for k, f in files.items()}
    config = {"stale": False, "activate": True, "export": True, **cfg, "stored": stored, "dialogs": dialogs}
    trs = "".join(
        f"<tr class='v-grid-row v-grid-row-has-data' style='transform: translate3d(0px, {68 * i}px, 0px)'><td>"
        f"<div class='body{' iwps_body_active' if i == 0 else ''}'>"
        + "".join(f"<div class='fm-textarea'><div class='text'>{html.escape(t)}</div></div>" for t in texts)
        + f"<button onclick='goGetIt({i})'>GO GET IT</button></div></td></tr>"
        for i, texts in enumerate(rows)
    )
    return (f"<html><body><table><tbody>{trs}</tbody></table>"
            f"<script>const CFG = {json.dumps(config)};{_PORTAL_JS}</script></body></html>").encode()


async def from_portal(pool: BrowserPool, page_html: bytes, files: list[bytes], ref: DocumentRef, dest: Path,
                      policy: FilePolicy = POLICY) -> tuple[DownloadedFile | Exception, list[str]]:
    """_download_one of `ref` against the fake portal: the file (or the error) and the files asked for."""
    asked: list[str] = []

    async def handle(reader, writer):
        path = (await reader.readline()).split()[1].decode()
        await reader.readuntil(b"\r\n\r\n")
        if path.startswith("/file/"):
            asked.append(urllib.parse.unquote(path))
            _, _, rec, name = path.split("/", 3)
            body = files[int(rec)]
            head = (f"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Content-Disposition: attachment; filename=\"{urllib.parse.unquote(name)}\"\r\n")
        else:
            body = page_html
            head = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
            # (utf-8: the titles have en dashes, and a misread one changes the row's derived id)
        writer.write(head.encode() + b"Connection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    provider = UarbProvider(pool, file_policy=policy)
    try:
        async with pool.session() as page:
            await page.goto(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/")
            try:
                result: DownloadedFile | Exception = await provider._download_one(page, ref, str(dest))
            except Exception as e:  # noqa: BLE001 - the test inspects it
                result = e
    finally:
        server.close()
    return result, asked


TRANSCRIPTS = [screens("uarb_m10431_transcripts")[0][i]["texts"] for i in range(3)]
STORED_TRANSCRIPTS = [  # as the live portal proposes them
    "NSUARB-M10431-September 12, 2022.pdf",
    "NSUARB-M10431-September 12, 2022-EVENING SESSION.pdf",
    "NSUARB-M10431-September 13, 2022.pdf",
]
TRANSCRIPT_FILES = [b"%PDF-1.7 row 0", b"%PDF-1.7 row 1", b"%PDF-1.7 row 2"]


def listed(texts: list[list[str]], doc_type: str, index: int) -> DocumentRef:
    rows = uarb._Rows(doc_type)
    for i, t in enumerate(texts):
        rows.add({"texts": t, "y": 68 * i})
    return rows.refs("uarb", "M10431", 99, complete=True)[index]


async def test_a_transcript_is_exported_under_our_id_once_the_proposal_settles(chromium, tmp_path):
    # Row 0 is active; the first GO GET IT on row 1 proposes row 0's file (and a comma in the
    # stored name would make Chromium refuse the download), so the file is exported as our id.
    ref = listed(TRANSCRIPTS, "Transcripts", 1)
    page = portal_page(TRANSCRIPTS, STORED_TRANSCRIPTS, stale=True)

    f, asked = await from_portal(chromium, page, TRANSCRIPT_FILES, ref, tmp_path)

    assert isinstance(f, DownloadedFile), f
    assert asked == [f"/file/1/{ref.external_id}.pdf"]
    assert Path(f.path).read_bytes() == b"%PDF-1.7 row 1" and f.filename == f"{ref.external_id}.pdf"
    assert not list(tmp_path.glob(".*.part"))


async def test_identical_rows_are_told_apart_by_their_order(chromium, tmp_path):
    by_y = {r["y"]: r["texts"] for screen in screens("uarb_m10431_recordings") for r in screen}
    texts = [by_y[y] for y in (1020, 1088, 1156)]  # "Tuesday (1)", then "Tuesday (2(1of2))" twice
    ref = listed(texts, "Recordings", 2)  # the second "Tuesday (2(1of2))"
    page = portal_page(texts, ["Trk19.wav", "Tk20 (1of2).wav", "Tk20 (2of2).wav"])
    wav = FilePolicy.of(1_000_000, [".pdf", ".wav"])

    f, asked = await from_portal(chromium, page, [b"RIFF0", b"RIFF1", b"RIFF2"], ref, tmp_path, wav)

    assert ref.external_id.endswith("_2") and isinstance(f, DownloadedFile), f
    assert asked == [f"/file/2/{ref.external_id}.wav"]
    assert Path(f.path).read_bytes() == b"RIFF2" and f.filename == f"{ref.external_id}.wav"
    assert f.ref.file_ext == ".wav"  # learnt from the served file


async def test_an_export_of_another_row_is_refused(chromium, tmp_path):
    # The clicked row never becomes the active record: whatever is proposed can't be trusted.
    ref = listed(TRANSCRIPTS, "Transcripts", 1)

    err, asked = await from_portal(chromium, portal_page(TRANSCRIPTS, STORED_TRANSCRIPTS, activate=False),
                                   TRANSCRIPT_FILES, ref, tmp_path)

    assert isinstance(err, ScrapeError) and "active row" in str(err) and err.retryable
    assert asked == []


async def test_a_file_type_we_dont_deliver_is_refused_before_it_is_sent(chromium, tmp_path):
    ref = listed(TRANSCRIPTS, "Transcripts", 1)
    stored = [n.replace(".pdf", ".zip") for n in STORED_TRANSCRIPTS]

    err, asked = await from_portal(chromium, portal_page(TRANSCRIPTS, stored), TRANSCRIPT_FILES, ref, tmp_path)

    assert isinstance(err, UnsupportedFileType) and asked == []


async def test_a_row_without_an_id_served_without_the_export_dialog_is_refused(chromium, tmp_path):
    ref = listed(TRANSCRIPTS, "Transcripts", 1)

    err, asked = await from_portal(chromium, portal_page(TRANSCRIPTS, STORED_TRANSCRIPTS, export=False),
                                   TRANSCRIPT_FILES, ref, tmp_path)

    assert isinstance(err, ScrapeError) and "no export dialog" in str(err) and asked == []


async def test_rows_js_reads_the_captured_grids(chromium):
    async with chromium.session() as page:
        await page.route("**/*", lambda route: route.abort())  # the captured pages' own scripts
        for name in ("uarb_m10431_transcripts", "uarb_m10431_recordings"):
            await page.set_content((FIXTURES / f"{name}_grid.html").read_text())
            got = await page.evaluate(uarb._ROWS_JS)
            want = screens(name)[0]
            assert sorted((r["y"], r["texts"], r["active"]) for r in got) == sorted(
                (r["y"], r["texts"], r["active"]) for r in want)
        await page.set_content((FIXTURES / "uarb_m12205_other_documents.html").read_text(),
                               wait_until="domcontentloaded")
        rows = sorted(await page.evaluate(uarb._ROWS_JS), key=lambda r: r["y"])
        assert parse_row(rows[0]["texts"], "Other Documents")["external_id"] == "102674" and rows[0]["active"]


async def test_an_exhibit_shown_with_a_stray_space_is_found_by_its_id(chromium):
    texts = [r["texts"] for r in screens("uarb_m12383_exhibits")[0]]
    async with chromium.session() as page:
        await page.set_content(portal_page(texts, ["x"] * len(texts)).decode())
        row = await UarbProvider(chromium)._row_for(page, "A-5")
        assert "A -5" in await row.inner_text()
