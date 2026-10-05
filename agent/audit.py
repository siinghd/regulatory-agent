"""The audit trail: who did what, from which component and build, about whose request.

- Every event row carries `actor` (the database role, set by a trigger, so it can't be claimed),
  `component` (ingest | worker | web | cli | cron, from a contextvar set at process start),
  `app_version` and `subject_h`: HMAC-SHA256 of the requester's address under AUDIT_HMAC_KEY.
  Events never hold a raw address; `scrub` replaces any that reaches free text (an SMTP error
  quoting the recipient...) with its HMAC.
- `events` is append-only (migrations/005: a trigger refuses UPDATE and DELETE for every role;
  only the SECURITY DEFINER purge_events() removes rows past audit_retention_days).
- Each day's events are exported to {audit_export_dir}/YYYY-MM-DD.jsonl (mode 600). The first
  line of each file records the previous file's SHA-256, so the files form a hash chain:
  changing, removing or reordering any exported day breaks every later link (`verify_chain`).
  The backup job copies the directory off-site.
"""

import asyncio
import contextlib
import contextvars
import getpass
import hashlib
import hmac
import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import structlog
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent import crypto, db, metrics
from agent.config import get_settings
from agent.mail.auth import to_ascii_domain

log = structlog.get_logger()

COMPONENTS = ("ingest", "worker", "web", "cli", "cron")
_process_component: str | None = None
_component: contextvars.ContextVar[str | None] = contextvars.ContextVar("audit_component", default=None)

_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_UNSCRUBBED_KEYS = {"message_id"}  # our own Message-IDs look like addresses and identify no one
EXPORT_EPOCH_DAYS = 400  # catch-up never reaches further back than the audit retention


# ------------------------------------------------------------------ component


def set_component(name: str) -> None:
    """The process's component, set once at start (`ragent ingest` -> "ingest", ...)."""
    global _process_component
    if name not in COMPONENTS:
        raise ValueError(f"unknown component {name!r}")
    _process_component = name
    _component.set(name)


@contextlib.contextmanager
def component_scope(name: str) -> Iterator[None]:
    """Events written inside are attributed to `name` (e.g. "cron" for the worker's daily jobs)."""
    if name not in COMPONENTS:
        raise ValueError(f"unknown component {name!r}")
    token = _component.set(name)
    try:
        yield
    finally:
        _component.reset(token)


def component() -> str | None:
    return _component.get() or _process_component


def operator() -> str:
    """The human behind an admin command: the sudo caller, else the login user."""
    for var in ("SUDO_USER", "USER", "LOGNAME"):
        if os.environ.get(var):
            return os.environ[var]
    with contextlib.suppress(Exception):
        return getpass.getuser()
    return "unknown"


# ------------------------------------------------------------------ pseudonymous subject ids


@lru_cache(maxsize=4)
def _derived_key(material: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"regulatory-agent audit hmac v1").derive(
        material
    )


def _key() -> bytes:
    s = get_settings()
    secret = s.audit_hmac_key.get_secret_value()
    if secret:
        return secret.encode()
    # Development and tests: a per-install key next to the at-rest key. Production sets
    # AUDIT_HMAC_KEY so every process (and every year's DSAR lookup) agrees on the ids.
    return _derived_key(crypto._key_file(s.data_dir))


def normalise_address(value: str) -> str:
    """Lowercase, with the domain as its A-label: the From header may spell an IDN as a U-label
    (bücher.example) while replies go to the A-label (xn--bcher-kva.example); both must hash
    alike. Also for '@domain'. A domain that isn't a valid hostname is only lowercased."""
    value = value.strip()
    local, at, domain = value.rpartition("@")
    if not at:
        return value.lower()
    return f"{local.lower()}@{to_ascii_domain(domain) or domain.lower()}"


def subject_hash(value: str | None) -> str | None:
    """HMAC-SHA256 hex of an address (or of '@domain'); None for no address."""
    if not value or not value.strip():
        return None
    return hmac.new(_key(), normalise_address(value).encode(), hashlib.sha256).hexdigest()


def limit_key(kind: str, value: str) -> str:
    """Pseudonymous Redis key part for a rate limit or budget (`kind` = sender, domain, ip...):
    HMAC-SHA256 of "limit:<kind>:<value>", 32 hex characters. Separate from `subject_hash`, so a
    Redis key never equals an audit subject id."""
    return hmac.new(_key(), f"limit:{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:32]


def pseudonym(value: str) -> str:
    """What replaces an identifier once it may no longer be kept: 'h:' + its HMAC."""
    return "h:" + (subject_hash(value) or "")


def scrub(value: Any, _key_name: str | None = None) -> Any:
    """`value` with every email address in its strings replaced by its pseudonym."""
    if isinstance(value, str):
        if _key_name in _UNSCRUBBED_KEYS or "@" not in value:
            return value
        return _EMAIL.sub(lambda m: pseudonym(m.group(0)), value)
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, _key_name) for v in value]
    return value


# ------------------------------------------------------------------ writing events


async def write_in(
    conn: asyncpg.Connection,
    request_id: UUID | None,
    kind: str,
    data: dict | None = None,
    *,
    subject_h: str | None = None,
) -> None:
    """The one place events are written. `subject_h` defaults (in the database) to the
    request's sender; pass it for events about a person that belong to no request."""
    clean = scrub(data or {})
    await conn.execute(
        "INSERT INTO events (request_id, kind, data, component, app_version, subject_h) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        request_id, kind, clean, component(), get_settings().app_version, subject_h,
    )


async def write(request_id: UUID | None, kind: str, data: dict | None = None, *, subject_h: str | None = None) -> None:
    async with db.acquire() as conn:
        await write_in(conn, request_id, kind, data, subject_h=subject_h)


async def admin_event(kind: str, data: dict | None = None, *, request_id: UUID | None = None,
                      subject_h: str | None = None) -> None:
    """An operator action (`admin.*`), naming the operator."""
    await write(request_id, f"admin.{kind}", {"operator": operator(), **(data or {})}, subject_h=subject_h)


async def record_llm_call(
    request_id: UUID | None,
    meta: dict | None,
    *,
    purpose: str | None = None,
    data_class: str | None = None,
    input_chars: int | None = None,
) -> None:
    """One `llm.call` event per call to a model: what kind of data left us, to whom, at what cost.

    `meta` is what agent.llm.structured or agent.typesafe.ask returns (purpose, model, provider, zdr,
    cost, tokens...); `purpose`, `data_class` (email_body | public_document) and `input_chars` come
    from `meta` when the caller put them there, else from these arguments. A call that handed its
    input on to another model (`escalation`: that call's meta; agent.gate.jev, agent.citations.jev_check)
    is recorded as its own event too, with its own cost: another provider saw the same data. Every
    cost counts against the day's model budget. Never raises: the audit of a call must not fail the
    request it belongs to (a lost event is logged).
    """
    meta = meta or {}
    cost = meta.get("cost_total", meta.get("cost"))
    data = {
        "purpose": meta.get("purpose") or purpose,
        "model": meta.get("model"),
        "provider": meta.get("provider"),
        "data_class": meta.get("data_class") or data_class,
        "input_chars": meta.get("input_chars", input_chars),
        "zdr": meta.get("zdr", get_settings().llm_zero_data_retention),
        "cost": cost,
        "attempts": meta.get("attempts"),
        "outcome": meta.get("outcome", "ok"),
        # TypeSafe reports input/output tokens, OpenRouter prompt/completion tokens
        "input_tokens": meta.get("input_tokens", meta.get("prompt_tokens")),
        "output_tokens": meta.get("output_tokens", meta.get("completion_tokens")),
    }
    data.update({k: meta[k] for k in ("escalated", "escalation_reason", "escalated_from") if k in meta})
    try:
        await write(request_id, "llm.call", data)
    except Exception as e:  # noqa: BLE001
        log.warning("audit.llm_call_lost", error=f"{type(e).__name__}: {str(e)[:200]}")
    if isinstance(cost, (int, float)) and cost > 0:
        metrics.LLM_COST.inc(cost)
        from agent import limits  # imports agent.audit itself

        await limits.record_llm_cost(cost)  # the daily LLM budget; never raises
    escalation = meta.get("escalation")
    if isinstance(escalation, dict):
        await record_llm_call(
            request_id,
            {**escalation, "escalated_from": data["provider"], "escalation_reason": meta.get("escalation_reason")},
            purpose=data["purpose"], data_class=data["data_class"], input_chars=data["input_chars"],
        )


def outbound_data(message_id: str, kind: str, msg, *, size: int | None, code: int | None,
                  drop_id: str | None = None, error: str | None = None) -> dict:
    """The audit record of one email we sent (or gave up on): to whom (HMAC), what, how big."""
    to = msg["To"] if msg is not None else None
    names = [p.get_filename() for p in msg.iter_attachments()] if msg is not None else []
    data = {
        "message_id": message_id, "kind": kind, "to_h": subject_hash(str(to)) if to else None,
        "size": size, "attachments": [n for n in names if n], "drop_id": drop_id, "smtp_code": code,
    }
    if error is not None:
        data["error"] = error[:500]
    return data


# ------------------------------------------------------------------ reading the trail


async def timeline(*, request_id: UUID | None = None, subject_h: str | None = None,
                   limit: int = 5000) -> list[asyncpg.Record]:
    if (request_id is None) == (subject_h is None):
        raise ValueError("pass exactly one of request_id, subject_h")
    col, value = ("request_id", request_id) if request_id is not None else ("subject_h", subject_h)
    return await db.fetch(
        f"SELECT id, request_id, kind, data, at, actor, component, app_version, subject_h "
        f"FROM events WHERE {col} = $1 ORDER BY id LIMIT $2",
        value, limit,
    )


def format_timeline(rows: list) -> str:
    lines = []
    for r in rows:
        who = "/".join(x for x in (r["component"], r["actor"]) if x) or "-"
        data = json.dumps(r["data"] or {}, sort_keys=True, separators=(",", ":"))
        rid = str(r["request_id"]) if r["request_id"] else "-"
        lines.append(f"{r['at'].astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}  {r['kind']:<22} {rid}  [{who}]  {data}")
    return "\n".join(lines)


# ------------------------------------------------------------------ daily export (hash chain)


def export_dir() -> Path:
    s = get_settings()
    return Path(s.audit_export_dir) if s.audit_export_dir else Path(s.data_dir) / "audit"


def _file_for(directory: Path, day: date) -> Path:
    return directory / f"{day.isoformat()}.jsonl"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _exported_days(directory: Path) -> list[date]:
    days = []
    for p in directory.glob("*.jsonl"):
        with contextlib.suppress(ValueError):
            days.append(date.fromisoformat(p.stem))
    return sorted(days)


def _event_line(r) -> dict:
    return {
        "id": r["id"], "at": r["at"].astimezone(UTC).isoformat(), "kind": r["kind"],
        "request_id": str(r["request_id"]) if r["request_id"] else None, "data": r["data"],
        "actor": r["actor"], "component": r["component"], "app_version": r["app_version"],
        "subject_h": r["subject_h"],
    }


def _write_exclusive(path: Path, payload: bytes) -> bool:
    """Write `path` once, atomically, mode 600. False if it already exists (never overwritten)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


async def export_day(day: date, directory: Path | None = None) -> Path | None:
    """Export `day`'s events (UTC). The file's first line links it to the previous day's file.

    Returns the file written, or None if it already existed. Days must be exported in order.
    """
    directory = directory or export_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _file_for(directory, day)
    if path.exists():
        return None
    earlier = [d for d in _exported_days(directory) if d < day]
    prev = _file_for(directory, earlier[-1]) if earlier else None
    start = datetime.combine(day, time.min, tzinfo=UTC)
    rows = await db.fetch(
        "SELECT id, request_id, kind, data, at, actor, component, app_version, subject_h FROM events "
        "WHERE at >= $1 AND at < $2 ORDER BY id",
        start, start + timedelta(days=1),
    )
    header = {
        "type": "header", "date": day.isoformat(), "events": len(rows),
        "first_id": rows[0]["id"] if rows else None, "last_id": rows[-1]["id"] if rows else None,
        "prev_file": prev.name if prev else None,
        "prev_sha256": await asyncio.to_thread(_sha256_file, prev) if prev else None,
        "exported_at": datetime.now(UTC).isoformat(), "app_version": get_settings().app_version,
    }
    lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
    lines += [json.dumps(_event_line(r), sort_keys=True, separators=(",", ":"), default=str) for r in rows]
    payload = ("\n".join(lines) + "\n").encode()
    if not await asyncio.to_thread(_write_exclusive, path, payload):
        return None
    log.info("audit.exported", day=day.isoformat(), events=len(rows), file=str(path))
    return path


async def export_pending(today: date | None = None, directory: Path | None = None) -> list[Path]:
    """Export every complete day not exported yet: from the day after the newest file (or just
    yesterday on the first run) up to yesterday."""
    directory = directory or export_dir()
    today = today or datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    done = [d for d in _exported_days(directory) if d <= yesterday] if directory.exists() else []
    day = (done[-1] + timedelta(days=1)) if done else yesterday
    day = max(day, today - timedelta(days=EXPORT_EPOCH_DAYS))
    written = []
    while day <= yesterday:
        path = await export_day(day, directory)
        if path:
            written.append(path)
        day += timedelta(days=1)
    return written


def verify_chain(directory: Path | None = None) -> list[str]:
    """Problems in the exported chain (empty when every file links to its predecessor)."""
    directory = directory or export_dir()
    problems = []
    prev: Path | None = None
    for day in _exported_days(directory):
        path = _file_for(directory, day)
        try:
            first = path.read_text().split("\n", 1)[0]
            header = json.loads(first)
        except (OSError, ValueError) as e:
            problems.append(f"{path.name}: unreadable header ({e})")
            prev = path
            continue
        want_name = prev.name if prev else None
        want_sha = _sha256_file(prev) if prev else None
        if header.get("prev_file") != want_name or header.get("prev_sha256") != want_sha:
            problems.append(f"{path.name}: does not link to {want_name or 'nothing'} "
                            f"(records {header.get('prev_file')} {str(header.get('prev_sha256'))[:12]})")
        body = path.read_text().splitlines()[1:]
        if header.get("events") != len(body):
            problems.append(f"{path.name}: header says {header.get('events')} events, file has {len(body)}")
        prev = path
    return problems


async def export_job(ctx: dict) -> None:
    """arq cron: export yesterday's events (and any day missed while the worker was down)."""
    with component_scope("cron"):
        written = await export_pending()
        if written:
            await write(None, "audit.export", {"files": [p.name for p in written]})
