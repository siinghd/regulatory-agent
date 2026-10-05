"""Daily reconciliation (`ragent reconcile --since 24h`, and a worker cron): does the database
agree with itself, the disk and recent history? Writes one `reconcile` event with the findings
and logs ERROR (alert) when any of them is an anomaly.

Checks:
- requests stuck in a non-final state for more than 30 minutes;
- verified requests marked done whose reply was never sent;
- outbound mail queued for more than an hour, or given up on (undeliverable) in the window;
- documents with a stored hash whose file is missing;
- citations made in the window whose file version differs from their document's current one,
  or whose pinned file is missing;
- rejection reasons in the window against their 7-day daily average (a spoofing wave, a broken
  parser, a rate-limit storm).
"""

import asyncio
import re
from collections import Counter
from datetime import timedelta

import structlog

from agent import audit, blobs, db
from agent.store import SETTLED

log = structlog.get_logger()

STUCK_AFTER = timedelta(minutes=30)
OUTBOUND_LATE = timedelta(hours=1)
SPIKE_MIN = 10  # fewer rejections than this in the window is never a spike
SPIKE_FACTOR = 3.0
SAMPLE = 10


def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text or "")
    if not m:
        raise ValueError(f"expected a duration like 30m, 24h or 7d, got {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def _missing_blobs(shas: list[str]) -> list[str]:
    out = []
    for sha in shas:
        try:
            if not blobs.has_blob(sha):
                out.append(sha)
        except ValueError:  # not a hash at all
            out.append(sha)
    return out


async def reconcile(since: timedelta = timedelta(hours=24)) -> dict:
    findings: dict = {"since_s": int(since.total_seconds())}
    anomalies: list[str] = []

    stuck = await db.fetch(
        "SELECT id, state FROM requests WHERE state <> ALL($1::text[]) AND updated_at < now() - $2::interval "
        "ORDER BY updated_at", list(SETTLED), STUCK_AFTER)
    findings["stuck"] = len(stuck)
    findings["stuck_sample"] = [f"{r['id']}:{r['state']}" for r in stuck[:SAMPLE]]

    no_reply = await db.fetch(
        """SELECT id FROM requests WHERE state = 'done' AND reply_sent_at IS NULL
           AND auth->>'verdict' = 'pass' AND updated_at > now() - $1::interval""", since)
    findings["done_without_reply"] = len(no_reply)
    findings["done_without_reply_sample"] = [str(r["id"]) for r in no_reply[:SAMPLE]]

    findings["outbound_late"] = await db.fetchval(
        "SELECT count(*) FROM outbound WHERE status = 'queued' AND created_at < now() - $1::interval", OUTBOUND_LATE)
    findings["outbound_undeliverable"] = await db.fetchval(
        "SELECT count(*) FROM outbound WHERE status = 'undeliverable' AND created_at > now() - $1::interval", since)

    shas = [r["sha256"] for r in await db.fetch("SELECT DISTINCT sha256 FROM documents WHERE sha256 IS NOT NULL")]
    missing = await asyncio.to_thread(_missing_blobs, shas)
    findings["documents_blob_missing"] = len(missing)
    findings["documents_blob_missing_sample"] = missing[:SAMPLE]

    cites = await db.fetch(
        """SELECT c.id, c.sha256 FROM citations c JOIN documents d ON d.id = c.document_id
           WHERE c.created_at > now() - $1::interval AND c.sha256 IS NOT NULL
             AND c.sha256 IS DISTINCT FROM d.sha256""", since)
    findings["citations_sha_mismatch"] = len(cites)
    pinned = [r["sha256"] for r in await db.fetch(
        "SELECT DISTINCT sha256 FROM citations WHERE created_at > now() - $1::interval AND sha256 IS NOT NULL", since)]
    findings["citations_blob_missing"] = len(await asyncio.to_thread(_missing_blobs, pinned))

    window = Counter({r["reason"]: r["n"] for r in await db.fetch(
        """SELECT split_part(reject_reason, ':', 1) AS reason, count(*) AS n FROM requests
           WHERE state = 'rejected' AND reject_reason IS NOT NULL AND received_at > now() - $1::interval
           GROUP BY 1""", since)})
    baseline = Counter({r["reason"]: r["n"] for r in await db.fetch(
        """SELECT split_part(reject_reason, ':', 1) AS reason, count(*) AS n FROM requests
           WHERE state = 'rejected' AND reject_reason IS NOT NULL
             AND received_at <= now() - $1::interval AND received_at > now() - $1::interval - interval '7 days'
           GROUP BY 1""", since)})
    days = max(since / timedelta(days=1), 1 / 24)
    spikes = {}
    for reason, n in window.items():
        expected = baseline.get(reason, 0) / 7 * days
        if n >= SPIKE_MIN and n > SPIKE_FACTOR * expected:
            spikes[reason] = {"count": n, "baseline": round(expected, 1)}
    findings["rejections"] = dict(window)
    findings["rejection_spikes"] = spikes

    for key in ("stuck", "done_without_reply", "outbound_late", "outbound_undeliverable",
                "documents_blob_missing", "citations_sha_mismatch", "citations_blob_missing"):
        if findings[key]:
            anomalies.append(key)
    anomalies += [f"rejection_spike:{r}" for r in spikes]
    findings["anomalies"] = anomalies

    await audit.write(None, "reconcile", findings)
    if anomalies:
        log.error("reconcile.anomaly", alert=True, anomalies=anomalies,
                  **{k: v for k, v in findings.items() if k != "anomalies" and not k.endswith("_sample")})
    else:
        log.info("reconcile.ok", since_s=findings["since_s"])
    return findings


async def reconcile_job(ctx: dict) -> None:
    """arq cron: the daily reconciliation over the last 24 hours."""
    with audit.component_scope("cron"):
        await reconcile(timedelta(hours=24))
