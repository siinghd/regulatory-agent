"""Application-level SOC 2 controls that need no database: pseudonymous ids, scrubbing, the
audit export chain, deletion intent, suppression targets, health checks, metrics, the privacy
pages and the operator CLI."""

import asyncio
import hashlib
import http.server
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from agent import admin, audit, health, metrics, reconcile, store
from agent.config import Settings, get_settings
from agent.mail import ingest
from agent.models import InboundEmail
from agent.web import app as web
from agent.web import progress

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-audit-key")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


# ---------------------------------------------------------------- pseudonymous subject ids


def test_subject_hash_is_a_keyed_hmac_of_the_normalised_address(settings, monkeypatch):
    h = audit.subject_hash("Alice@Example.com ")
    assert h == audit.subject_hash("alice@example.com")
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h != hashlib.sha256(b"alice@example.com").hexdigest()  # not a plain (guessable) hash
    assert audit.subject_hash("") is None and audit.subject_hash(None) is None
    monkeypatch.setenv("AUDIT_HMAC_KEY", "another-key")
    get_settings.cache_clear()
    assert audit.subject_hash("alice@example.com") != h


def test_without_a_configured_key_the_hmac_is_stable_per_install(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert audit.subject_hash("a@b.example") == audit.subject_hash("a@b.example")
        assert (tmp_path / "keys" / "at-rest.key").is_file()
    finally:
        get_settings.cache_clear()


def test_scrub_replaces_addresses_anywhere_but_keeps_message_ids(settings):
    data = {
        "error": "550 5.1.1 <bob@example.org>: Recipient address rejected",
        "nested": [{"note": "from Carol@Example.org"}],
        "message_id": "<reply.123@hsingh.app>",
        "count": 3,
    }
    clean = audit.scrub(data)
    assert "bob@example.org" not in json.dumps(clean) and "Carol@Example.org" not in json.dumps(clean)
    assert f"<{audit.pseudonym('bob@example.org')}>" in clean["error"]
    assert clean["nested"][0]["note"] == f"from {audit.pseudonym('carol@example.org')}"
    assert clean["message_id"] == "<reply.123@hsingh.app>" and clean["count"] == 3


def test_component_is_set_per_process_and_scoped_per_task():
    audit.set_component("worker")
    assert audit.component() == "worker"
    with audit.component_scope("cron"):
        assert audit.component() == "cron"
    assert audit.component() == "worker"
    with pytest.raises(ValueError):
        audit.set_component("anything")


def test_operator_is_the_sudo_caller_else_the_login_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("USER", "root")
    assert audit.operator() == "alice"
    monkeypatch.delenv("SUDO_USER")
    assert audit.operator() == "root"


def test_outbound_audit_record_names_recipient_by_hmac_and_lists_attachments(settings):
    msg = EmailMessage()
    msg["To"] = "alice@example.com"
    msg.set_content("hi")
    msg.add_attachment(b"PK", maintype="application", subtype="zip", filename="M12205 Exhibits.zip")
    data = audit.outbound_data("<reply.1@hsingh.app>", "reply", msg, size=1234, code=250, drop_id="abc123")
    assert data == {"message_id": "<reply.1@hsingh.app>", "kind": "reply", "to_h": audit.subject_hash("alice@example.com"),
                    "size": 1234, "attachments": ["M12205 Exhibits.zip"], "drop_id": "abc123", "smtp_code": 250}
    assert "alice@example.com" not in json.dumps(data)


# ---------------------------------------------------------------- export hash chain


def _write_day(directory: Path, day: str, prev: Path | None, events: list[dict]) -> Path:
    header = {"type": "header", "date": day, "events": len(events), "prev_file": prev.name if prev else None,
              "prev_sha256": hashlib.sha256(prev.read_bytes()).hexdigest() if prev else None}
    path = directory / f"{day}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in [header, *events]) + "\n")
    return path


def test_verify_chain_detects_edits_removals_and_truncation(tmp_path):
    a = _write_day(tmp_path, "2026-10-01", None, [{"id": 1, "kind": "received"}])
    b = _write_day(tmp_path, "2026-10-02", a, [{"id": 2, "kind": "state:done"}])
    c = _write_day(tmp_path, "2026-10-03", b, [])
    assert audit.verify_chain(tmp_path) == []

    a.write_text(a.read_text().replace("received", "rejected"))  # history rewritten
    problems = audit.verify_chain(tmp_path)
    assert len(problems) == 1 and problems[0].startswith("2026-10-02.jsonl: does not link")

    a.unlink()  # a day removed
    assert any(p.startswith("2026-10-02.jsonl") for p in audit.verify_chain(tmp_path))

    lines = c.read_text().splitlines()
    c.write_text("\n".join([lines[0].replace('"events": 0', '"events": 1')]) + "\n")
    assert any("header says 1 events" in p for p in audit.verify_chain(tmp_path))


def test_export_files_are_written_once_and_private(tmp_path):
    path = tmp_path / "2026-10-01.jsonl"
    assert audit._write_exclusive(path, b"one\n") is True
    assert audit._write_exclusive(path, b"two\n") is False  # never overwritten
    assert path.read_bytes() == b"one\n" and path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]  # no temporary file left behind


# ---------------------------------------------------------------- deletion intent and suppression


def _email(subject: str = "", text: str = "") -> InboundEmail:
    return InboundEmail(message_id="<m@x>", from_addr="alice@example.com", subject=subject, text=text,
                        received_at=datetime.now(UTC))


@pytest.mark.parametrize(("subject", "text", "wanted"), [
    ("DELETE MY DATA", "", True),
    ("Re: please delete my data", "", True),
    ("Hi", "Hello,\n\nDELETE MY DATA\n\nThanks", True),
    ("Hi", "  delete my data.  ", True),
    ("Hi", "Can you send the Exhibits for M12205? Don't delete my data though", False),
    ("Exhibits for M12205", "Thanks", False),
])
def test_asks_for_deletion(subject, text, wanted):
    assert admin.asks_for_deletion(_email(subject, text)) is wanted


def test_suppression_targets_are_an_address_or_an_at_domain():
    assert admin._suppression_target(" Bob@Example.org ") == ("address", "bob@example.org")
    assert admin._suppression_target("@Spam.Example") == ("domain", "@spam.example")
    for bad in ("", "example.org", "@", "a@b", "@a@b.c"):
        with pytest.raises(ValueError):
            admin._suppression_target(bad)


# ---------------------------------------------------------------- reconcile, metrics


def test_reconcile_window_parsing():
    assert reconcile.parse_since("24h") == timedelta(hours=24)
    assert reconcile.parse_since("30m") == timedelta(minutes=30)
    assert reconcile.parse_since(" 7d ") == timedelta(days=7)
    with pytest.raises(ValueError):
        reconcile.parse_since("yesterday")


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_count_final_states_retries_and_stage_time():
    done, failed = _sample("requests_total", final_state="done"), _sample("requests_total", final_state="failed")
    metrics.observe_transition("replying", "done", 2.5, final=True)
    metrics.observe_transition("fetching", "fetching", 9.0, final=False)  # re-entry: not a stage
    assert _sample("requests_total", final_state="done") == done + 1
    assert _sample("requests_total", final_state="failed") == failed
    assert _sample("stage_duration_seconds_count", stage="replying") >= 1
    assert _sample("stage_duration_seconds_count", stage="fetching") == 0

    before = _sample("retries_total", cause="portal_unavailable")
    metrics.observe_retry("portal_unavailable")
    metrics.observe_retry("PortalUnavailable: timeout at someone@example.com")  # never free text
    assert _sample("retries_total", cause="portal_unavailable") == before + 1
    assert _sample("retries_total", cause="internal") >= 1
    assert _sample("retries_total", cause="PortalUnavailable: timeout at someone@example.com") == 0
    assert metrics.start_server(0) is False  # 0 = off


def test_metrics_refresh_reads_queue_depth_and_breakers(settings, monkeypatch):
    now = datetime.now(UTC).timestamp()

    class FakeRedis:
        async def zcard(self, key):
            return 7

        async def hget(self, key, field):
            return str(now + 60) if key == "breaker:smtp" else None

    monkeypatch.setattr("agent.providers.base.all_providers", lambda: [SimpleNamespace(name="uarb")])
    asyncio.run(metrics.refresh({"redis": FakeRedis()}))
    assert _sample("queue_depth") == 7
    assert _sample("breaker_open", dependency="smtp") == 1
    assert _sample("breaker_open", dependency="uarb") == 0


# ---------------------------------------------------------------- health checks


def test_last_backup_line_age_and_status(tmp_path):
    log = tmp_path / "backup.log"
    old = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.write_text(f"2026-01-01T00:00:00Z ok pg=1B\n{old} ok pg=2B files=3B\n")
    facts = health.last_backup(str(log))
    assert facts["readable"] and facts["ok"] and 3 * 3600 - 5 <= facts["age_s"] <= 3 * 3600 + 5
    assert health.last_backup(str(tmp_path / "missing.log")) == {"readable": False, "error": "FileNotFoundError"}


class _FakeRedis:
    def __init__(self, values: dict):
        self.values = values

    async def get(self, key):
        return self.values.get(key)

    async def aclose(self):
        pass


@pytest.mark.parametrize(("values", "ok"), [
    ({}, False),
    ({health.INGEST_HEARTBEAT_KEY: str(datetime.now(UTC).timestamp() - 30).encode()}, True),
    ({health.INGEST_HEARTBEAT_KEY: str(datetime.now(UTC).timestamp() - 700).encode()}, False),
])
def test_ingest_healthcheck_wants_a_recent_heartbeat(settings, monkeypatch, values, ok):
    monkeypatch.setattr(health, "_redis", lambda s: _FakeRedis(values))
    assert asyncio.run(health.healthcheck("ingest"))[0] is ok


def test_worker_healthcheck_wants_arqs_health_key(settings, monkeypatch):
    monkeypatch.setattr(health, "_redis", lambda s: _FakeRedis({}))
    assert asyncio.run(health.healthcheck("worker"))[0] is False
    monkeypatch.setattr(health, "_redis", lambda s: _FakeRedis({health.WORKER_HEALTH_KEY: b"Oct-04 j_complete=1"}))
    assert asyncio.run(health.healthcheck("worker")) == (True, "Oct-04 j_complete=1")


def test_worker_reports_health_once_a_minute():
    from agent.worker import WorkerSettings

    assert WorkerSettings.health_check_interval == 60
    names = {c.name for c in WorkerSettings.cron_jobs}
    assert {"cron:sweep", "cron:refresh", "cron:export_job", "cron:purge_job", "cron:reconcile_job"} <= names


@pytest.mark.parametrize(("body", "ok"), [({"ok": True, "db": True}, True), ({"ok": True, "db": False}, False)])
def test_web_healthcheck_asks_the_local_health_endpoint(monkeypatch, tmp_path, body, ok):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(body).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        s = Settings(web_port=server.server_address[1], data_dir=str(tmp_path))
        assert asyncio.run(health.healthcheck("web", s))[0] is ok
    finally:
        server.shutdown()


def test_web_healthcheck_fails_when_nothing_listens(tmp_path):
    assert asyncio.run(health.healthcheck("web", Settings(web_port=1, data_dir=str(tmp_path))))[0] is False


class _FakeImap:
    def __init__(self, pushes_after: int | None):
        self.waits: list[float] = []
        self.pushes_after = pushes_after
        self.done = False

    async def idle_start(self, timeout):
        return asyncio.sleep(0)

    async def wait_server_push(self, timeout):
        self.waits.append(timeout)
        if self.pushes_after is not None and len(self.waits) > self.pushes_after:
            return ["EXISTS"]
        await asyncio.sleep(timeout)
        raise TimeoutError

    def idle_done(self):
        self.done = True


class _HeartbeatRedis:
    def __init__(self):
        self.sets: list[tuple] = []

    async def set(self, key, value, ex=None):
        self.sets.append((key, value, ex))


async def test_ingest_beats_every_heartbeat_interval_of_a_quiet_idle(monkeypatch):
    monkeypatch.setattr(ingest, "IDLE_SECONDS", 0.25)
    monkeypatch.setattr(ingest, "HEARTBEAT_EVERY_S", 0.05)
    redis = _HeartbeatRedis()
    client = _FakeImap(pushes_after=None)
    await ingest.Ingestor(Settings(), redis)._idle(client)
    assert client.done and all(w <= 0.05 for w in client.waits)  # IDLE is never waited on longer
    assert len(redis.sets) >= 3
    assert all(k == "ingest:heartbeat" and ex == 900 for k, _, ex in redis.sets)


async def test_new_mail_ends_the_idle_at_once(monkeypatch):
    monkeypatch.setattr(ingest, "IDLE_SECONDS", 10.0)
    monkeypatch.setattr(ingest, "HEARTBEAT_EVERY_S", 0.01)
    redis = _HeartbeatRedis()
    client = _FakeImap(pushes_after=2)
    await ingest.Ingestor(Settings(), redis)._idle(client)
    assert client.done and len(client.waits) == 3 and len(redis.sets) == 2


# ---------------------------------------------------------------- web: privacy, security.txt, deep health


@pytest.fixture
def client(tmp_path) -> TestClient:
    web.app.dependency_overrides[get_settings] = lambda: Settings(
        data_dir=str(tmp_path), public_base_url="https://uarb.example", backup_log_path=str(tmp_path / "none.log"))
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def test_privacy_notice_states_collection_processors_retention_and_rights(client):
    r = client.get("/privacy")
    assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
    text = re.sub(r"\s+", " ", r.text)
    for phrase in ("sender", "display name", "IP address", "OpenRouter", "zero data retention", "Cloudflare",
                   "Hetzner", "Azure", "DELETE MY DATA", "privacy@hsingh.app", "security@hsingh.app",
                   "30 days after we finish with it (7 days", "90 days with your address", "deleted after 400 days",
                   "Expire after 7 days"):
        assert phrase in text, phrase
    assert 'href="/privacy"' in text  # footer link on every page
    for phrase in ("TypeSafe", "api.typesafe.ai", "its subject and text are sent to TypeSafe",
                   "excerpts of the public documents", "not zero data retention"):
        assert phrase in text, phrase


def test_the_privacy_notice_names_typesafe_only_while_it_is_used(tmp_path):
    web.app.dependency_overrides[get_settings] = lambda: Settings(
        data_dir=str(tmp_path), gate_classifier="llm", citation_check="llm")
    try:
        text = re.sub(r"\s+", " ", TestClient(web.app).get("/privacy").text)
    finally:
        web.app.dependency_overrides.clear()
    assert "TypeSafe" not in text and "When our rules can't work out what an email asks for" in text


def test_security_txt_follows_rfc_9116(client):
    r = client.get("/.well-known/security.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    fields = dict(line.split(": ", 1) for line in r.text.strip().splitlines())
    assert fields["Contact"] == "mailto:security@hsingh.app"
    assert fields["Preferred-Languages"] == "en"
    assert fields["Policy"] == "https://uarb.example/privacy#security"
    assert fields["Canonical"] == "https://uarb.example/.well-known/security.txt"
    expires = datetime.strptime(fields["Expires"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=UTC)
    assert timedelta(days=363) < expires - datetime.now(UTC) <= timedelta(days=365)


def _deep_client(tmp_path, monkeypatch, host="127.0.0.1"):
    async def db_ok():
        return True

    async def oldest():
        return 42.4

    async def redis_facts(s):
        return {"ok": True, "queue_depth": 3, "ingest_heartbeat_age_s": 12}

    monkeypatch.setattr(web, "db_ok", db_ok)
    monkeypatch.setattr(web, "oldest_open_request_age", oldest)
    monkeypatch.setattr(health, "redis_facts", redis_facts)
    log = tmp_path / "backup.log"
    log.write_text(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} ok pg=1B\n")
    web.app.dependency_overrides[get_settings] = lambda: Settings(data_dir=str(tmp_path), backup_log_path=str(log))
    return TestClient(web.app, client=(host, 50000))


def test_deep_health_reports_dependencies_to_loopback_clients(tmp_path, monkeypatch):
    try:
        r = _deep_client(tmp_path, monkeypatch).get("/health/deep")
        body = r.json()
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert body["ok"] is True and body["db"] == {"ok": True, "oldest_open_request_age_s": 42}
        assert body["redis"]["queue_depth"] == 3 and body["disk_free_bytes"] > 0
        assert body["backup"]["ok"] is True and body["backup"]["age_s"] < 60
    finally:
        web.app.dependency_overrides.clear()


@pytest.mark.parametrize(("host", "headers"), [
    ("203.0.113.9", {}),
    ("127.0.0.1", {"X-Real-IP": "203.0.113.9"}),  # through Caddy
    ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}),
    ("testclient", {}),
])
def test_deep_health_is_invisible_from_outside(tmp_path, monkeypatch, host, headers):
    try:
        r = _deep_client(tmp_path, monkeypatch, host).get("/health/deep", headers=headers)
        assert r.status_code == 404 and "queue_depth" not in r.text
    finally:
        web.app.dependency_overrides.clear()


def test_progress_page_reads_only_columns_the_web_role_is_granted():
    grants = (REPO / "deploy" / "sql" / "grants.sql").read_text()
    m = re.search(r"'GRANT SELECT \((.*?)\) ON requests TO agent_web'", grants, re.DOTALL)
    assert m, "agent_web's column grant on requests not found in grants.sql"
    granted = {c.strip() for c in re.sub(r"'\s*'", "", m.group(1)).split(",")}
    assert set(store.PROGRESS_COLUMNS) <= granted


def test_progress_outcome_says_whether_the_requester_was_emailed():
    row = {"state": "failed", "reject_reason": None}
    assert progress._outcome(row, {"failure": "regulator"}, emailed=True).endswith("You've been emailed; please try again later.")
    assert progress._outcome(row, {"failure": "regulator"}).endswith(" Please try again later.")


# ---------------------------------------------------------------- CLI


def test_cli_parses_every_operator_command():
    from agent import cli

    p = cli._parser()
    assert p.parse_args(["purge", "--dry-run"]).dry_run is True
    assert p.parse_args(["reconcile"]).since == "24h"
    assert p.parse_args(["healthcheck", "ingest"]).service == "ingest"
    assert p.parse_args(["dsar", "export", "a@b.c", "--out", "x.json"]).out == "x.json"
    assert p.parse_args(["audit", "--email", "a@b.c"]).email == "a@b.c"
    assert str(p.parse_args(["revoke", "0b6f3c1e-8d2a-4f7b-9c41-5e2d7a9b1c3f"]).request_id).startswith("0b6f")
    assert p.parse_args(["block", "@spam.example", "--reason", "abuse"]).value == "@spam.example"
    for service in ("ingest", "worker", "web", "migrate", "resume"):
        assert p.parse_args([service]).command == service
    with pytest.raises(SystemExit):
        p.parse_args(["healthcheck", "postgres"])


def test_dsar_delete_needs_explicit_confirmation(capsys):
    from agent import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["dsar", "delete", "a@b.example"])  # refuses before touching the database
    assert exc.value.code == 2 and "--yes" in capsys.readouterr().err


# ---------------------------------------------------------------- drop link revocation


def _link_record(settings) -> dict:
    from agent.delivery.choose import Delivery
    from agent.delivery.drop import DropLink

    link = DropLink(url="https://drop.example/d/abcdef123456#key", id="abcdef123456", delete_token="tok-1",
                    expires_at=datetime.now(UTC) + timedelta(days=7), size=10, max_downloads=25)
    return Delivery(kind="link", filename="x.zip", size=10, sha256="0" * 64, file_count=1, link=link).to_record("k1")


@pytest.mark.parametrize(("status", "revoked"), [(200, True), (404, True), (403, False), (503, False)])
async def test_revoke_delivery_uses_the_sealed_delete_token(settings, status, revoked):
    import httpx
    import respx

    from agent import retention

    record = _link_record(settings)
    assert "tok-1" not in json.dumps(record)  # sealed at rest
    with respx.mock(assert_all_called=True) as drop:
        route = drop.delete(f"{settings.drop_upload_url}/api/file/abcdef123456").mock(
            return_value=httpx.Response(status))
        assert await retention.revoke_delivery(record) is revoked
    assert route.calls.last.request.headers["X-Delete-Token"] == "tok-1"


async def test_revoke_delivery_without_a_usable_record(settings):
    from agent import retention

    assert await retention.revoke_delivery(None) is None
    assert await retention.revoke_delivery({"kind": "attachment"}) is None
    tampered = {**_link_record(settings), "files": "another-package"}  # the AAD no longer matches
    assert await retention.revoke_delivery(tampered) is False
