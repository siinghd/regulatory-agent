"""The real OEB RDS: list a case's decisions and download the newest one.

Run: .venv/bin/pytest -m live tests/live/test_oeb_live.py -q
"""

import hashlib
from datetime import date
from pathlib import Path

import pytest

from agent.providers.oeb import CATEGORIES, OebProvider, make_client

pytestmark = pytest.mark.live

MATTER = "EB-2024-0111"  # Enbridge Gas 2024 rebasing, phase 2: 447 records as of 2026-10-04


async def test_lists_and_downloads_decisions_for_a_real_case(tmp_path: Path):
    client = make_client()
    try:
        provider = OebProvider(client)
        info, refs = await provider.list_matter_and_documents(MATTER, "Decisions and Orders", 3)
        files = [f async for f in provider.download(MATTER, refs[:1], str(tmp_path))]
    finally:
        await client.aclose()

    assert info.title.startswith("Enbridge Gas Inc. – ")
    assert list(info.counts) == [c.name for c in CATEGORIES]
    assert sum(info.counts.values()) >= 447 and info.counts["Decisions and Orders"] >= 6
    assert info.decision_date is not None and info.date_received < info.decision_date

    assert len(refs) == 3
    dates = [r.filed_on for r in refs]
    assert all(dates) and dates == sorted(dates, reverse=True) and dates[0] >= date(2025, 7, 29)

    (f,) = files
    data = Path(f.path).read_bytes()
    assert f.filename == f"{refs[0].external_id}.pdf" and data.startswith(b"%PDF-")
    assert f.size == len(data) > 10_000 and hashlib.sha256(data).hexdigest() == f.sha256
