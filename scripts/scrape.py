"""Manual live check: python scripts/scrape.py M12205 "Other Documents" 10"""
import asyncio, sys, time, tempfile
from agent.config import get_settings
from agent.models import DocType
from agent.providers.browser import BrowserPool
from agent.providers.uarb import UarbProvider

async def main(matter: str, tab: str, n: int) -> None:
    s = get_settings()
    pool = BrowserPool(proxy=s.uarb_proxy, max_sessions=s.uarb_max_concurrent_sessions, nav_timeout_ms=s.browser_nav_timeout_ms)
    p = UarbProvider(pool, sessions_per_matter=s.uarb_sessions_per_matter)
    t = time.perf_counter()
    info, refs = await p.list_matter_and_documents(matter, DocType(tab), n)
    print(f"[{time.perf_counter()-t:.1f}s] {info.model_dump_json(indent=1)}")
    for r in refs: print("  ", r.external_id, r.filed_on, r.title[:70])
    with tempfile.TemporaryDirectory() as d:
        t2 = time.perf_counter()
        async for f in p.download(matter, refs, d):
            print(f"  [{time.perf_counter()-t2:.1f}s] {f.filename} {f.size:,}B {f.sha256[:10]}")
    print(f"total {time.perf_counter()-t:.1f}s")
    await pool.close()

asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
