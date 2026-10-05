"""The real FERC eLibrary: list a docket's filings and download the tariff filing's PDF.

Run: .venv/bin/pytest -m live tests/live/test_ferc_live.py -q
"""

import hashlib
from datetime import date
from pathlib import Path

import pytest

from agent.providers.ferc import CATEGORIES, FercProvider, make_client

pytestmark = pytest.mark.live

MATTER = "ER24-1234-000"  # NorthWestern's AMPS agreement: filing, notice, letter order (settled)


async def test_lists_and_downloads_a_filing_for_a_real_docket(tmp_path: Path):
    client = make_client()
    try:
        provider = FercProvider(client)
        info, refs = await provider.list_matter_and_documents(MATTER, "Applications and Filings", 3)
        files = [f async for f in provider.download(MATTER, refs[:1], str(tmp_path))]
    finally:
        await client.aclose()

    assert info.title.startswith("NorthWestern Corporation submits tariff filing")
    assert list(info.counts) == [c.name for c in CATEGORIES]
    assert {k: v for k, v in info.counts.items() if v} == {
        "Orders and Decisions": 1, "Notices": 1, "Applications and Filings": 1,
    }
    assert info.type == "Tariff Filing"
    assert (info.date_received, info.decision_date) == (date(2024, 2, 12), date(2024, 4, 8))

    (ref,) = refs
    assert (ref.external_id, ref.filed_on, ref.access, ref.file_ext) == (
        "20240212-5063", date(2024, 2, 12), "Public", ".pdf",
    )

    (f,) = files
    data = Path(f.path).read_bytes()
    assert f.filename == "20240212-5063_TransmittalLetter_AMPS_Agreement_Final.pdf" and data.startswith(b"%PDF-")
    assert f.size == len(data) > 10_000 and hashlib.sha256(data).hexdigest() == f.sha256
