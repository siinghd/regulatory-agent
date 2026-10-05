"""Runtime settings. Every secret comes from the environment (.env in dev, compose env in prod)."""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # AGENT_ENV_FILE="" disables the file (the test suite sets it, so tests never read the
    # operator's secrets); unset means ./.env as before.
    model_config = SettingsConfigDict(env_file=os.environ.get("AGENT_ENV_FILE", ".env") or None, extra="ignore")

    # --- storage / queue
    database_url: str = "postgresql://agent:agent@127.0.0.1:5442/agent"
    redis_url: str = "redis://127.0.0.1:6392/0"
    data_dir: str = "./data"  # content-addressed blobs, raw MIME, failure snapshots

    # --- mail
    agent_mail_address: str = "agent@hsingh.app"
    agent_mail_password: SecretStr = SecretStr("")
    imap_host: str = "mail.hsingh.app"
    imap_port: int = 993
    smtp_host: str = "mail.hsingh.app"
    smtp_port: int = 587
    # Our own MTA's hostname: the topmost Received header it writes is the only one we trust
    # for the connecting client IP (everything below it is attacker-controlled).
    trusted_mta_hostname: str = "mail.hsingh.app"
    # "dmarc" = require an aligned SPF or DKIM pass; "allowlist" = additionally require the
    # sender to be listed; "off" = only for local tests.
    sender_auth_mode: str = "dmarc"
    sender_allowlist: list[str] = Field(default_factory=list)  # addresses or @domains
    # Replay window: a Date header, or an aligned DKIM signature's t=, older than this before
    # we received the message does not authenticate the sender.
    mail_max_age_days: int = 3

    # --- limits (agent.limits). Senders are keyed normalised (lowercase, no +tag, Gmail dots
    # removed, A-label domain), domains by their organizational domain; every key is an HMAC.
    max_docs_per_request: int = 10
    rate_per_sender_hour: int = 6
    rate_per_domain_hour: int = 30
    rate_global_hour: int = 300
    rate_per_sender_day: int = 20  # sliding 24 h windows, on top of the hourly ones
    rate_per_domain_day: int = 100
    rate_global_day: int = 1000
    rate_notices_per_day: int = 3  # "slow down" replies per sender: at most one an hour, this many a day
    max_inflight_per_sender: int = 2  # requests fetching/packaging at once; more wait their turn
    max_requests_per_thread: int = 5
    max_inbound_bytes: int = 5_000_000
    # Pre-authentication (ingest, before raw MIME is stored or DNS is asked): per connecting
    # client IP (IPv6 per /64) and per claimed From organizational domain. Over either: headers
    # only, rejected, no reply. Over the global ceiling: left unseen for a later sweep.
    preauth_per_ip_hour: int = 30
    preauth_per_domain_hour: int = 60
    inbound_per_minute: int = 120

    # --- daily budgets (UTC day, Redis counters)
    llm_daily_budget_usd: float = 2.0  # spent: rules-only gate, no summaries (documents still go out)
    # Portal visits (a listing, a matter lookup, a download batch) per provider and day; over it,
    # requests needing that provider wait (one delay email each) until their deadline.
    portal_daily_visits: dict[str, int] = Field(default_factory=lambda: {"uarb": 400, "oeb": 2000, "ferc": 2000})
    bytes_per_sender_day: int = 1_500_000_000  # delivered bytes; a request delivers what fits and says so

    # --- web rate limits: token bucket per client IP (Caddy's X-Real-IP), shared through Redis
    web_rate_progress_json_per_min: int = 60  # /r/{token}.json
    web_rate_progress_per_min: int = 30  # /r/{token}
    web_rate_files_per_min: int = 30  # /files/*
    web_rate_files_per_hour: int = 200
    web_rate_citation_per_min: int = 60  # /c/*
    web_rate_default_per_min: int = 120  # everything else except /health and /health/deep

    # --- LLM (OpenRouter, OpenAI-compatible)
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # In fallback order. Each must have a zero-data-retention endpoint on OpenRouter (GET
    # /endpoints/zdr) while llm_zero_data_retention is on: one without is left out at startup
    # (agent.llm.check_zero_data_retention).
    llm_models: list[str] = Field(
        default_factory=lambda: [
            "deepseek/deepseek-v4.1-flash",
            "qwen/qwen3.8-27b",
        ]
    )
    llm_timeout_s: float = 90.0  # hard deadline per attempt; summaries over ~60k chars take 20-70s
    # Entailment check (a second model call, see citation_check) for claims whose quote is not one
    # exact sentence of the page.
    llm_check_support: bool = True

    # --- TypeSafe System One (Jev): typed judgments for the gate and the citation check (agent.typesafe)
    # Not covered by llm_zero_data_retention, which governs OpenRouter routing only: TypeSafe keeps no
    # zero-retention agreement on our plan. Owner decision (MVP): accepted, and disclosed in the privacy
    # notice and the vendor register. Email subject and body (gate) and public document excerpts
    # (citation check) are sent to it.
    typesafe_api_key: SecretStr = SecretStr("")
    typesafe_base_url: str = "https://api.typesafe.ai"
    # Pinned, not an alias (jev-latest moves when a release ships): the gate's and the citation check's
    # thresholds were tuned on this version (evals/gate/report_jev.md, evals/output/report_jev_check.md).
    typesafe_model: str = "jev-1.13.0"
    typesafe_deadline_s: float = 10.0  # whole call, retries included
    typesafe_connect_timeout_s: float = 5.0
    # "jev": rules, then one Jev request (agent.gate.jev); "llm": rules, then the LLM classifier
    # (agent.gate.classify).
    gate_classifier: Literal["llm", "jev"] = "jev"
    # When Jev is unsure (a confidence gate fired) or unavailable: "llm" asks the LLM classifier;
    # "clarify" keeps Jev's clarifying question (or, with Jev down, the rules' conservative answer).
    gate_jev_low_confidence: Literal["llm", "clarify"] = "llm"
    # Who checks that a quote supports its claim: "jev" (agent.citations.jev_check; a claim Jev can't
    # answer is re-checked by the LLM) or "llm" (agent.citations.ground.verify_support).
    citation_check: Literal["llm", "jev"] = "jev"

    # --- providers / egress
    # Per-provider egress proxy. UARB blocks non-North-American IPs, so its traffic leaves via
    # a Canadian SOCKS tunnel (one egress: while it is down, UARB requests wait and retry).
    uarb_proxy: str | None = "socks5://127.0.0.1:1080"
    uarb_sessions_per_matter: int = 3
    uarb_max_concurrent_sessions: int = 4  # politeness cap across all workers
    # The OEB's document server is reachable directly; set a proxy only if that changes.
    oeb_proxy: str | None = None
    oeb_max_concurrency: int = 4  # concurrent HTTP requests per worker
    # FERC eLibrary's JSON API is reachable directly too (behind Cloudflare, no challenge so far).
    ferc_proxy: str | None = None
    ferc_max_concurrency: int = 3  # concurrent HTTP requests per worker
    browser_nav_timeout_ms: int = 60_000

    # --- delivery
    drop_base_url: str = "https://drop.hsingh.app"
    drop_upload_url: str = "http://127.0.0.1:3060"  # same host: skip Caddy for uploads
    drop_expiry_s: int = 7 * 24 * 3600
    drop_max_downloads: int = 25
    # Base64 inflates ~1.37x; this host's Postfix message_size_limit is 10,240,000 bytes.
    attach_inline_max_bytes: int = 7_000_000

    # --- web viewer
    public_base_url: str = "https://uarb.hsingh.app"
    web_port: int = 8710

    # --- caching
    matter_cache_ttl_s: int = 6 * 3600

    # --- retry policy: exponential backoff, each delay scaled by a random 0.5-1.0 factor so
    # retries spread out; attempts counted in Postgres
    max_attempts: int = 8
    request_deadline_s: int = 2 * 3600  # after this the user gets one apology, whatever the cause
    retry_base_s: float = 30.0
    retry_cap_s: float = 900.0
    pipeline_timeout_s: int = 1080  # internal per-try bound, below the queue's job timeout
    job_timeout_s: int = 1200

    # --- circuit breakers (per dependency: uarb, oeb, ferc, typesafe, openrouter:<model>, drop, smtp)
    breaker_failures: int = 5  # consecutive availability failures that open it
    breaker_open_s: float = 60.0  # first open interval, doubles up to the cap
    breaker_open_cap_s: float = 600.0

    # --- size budgets (disk and abuse protection)
    max_file_bytes: int = 200_000_000
    max_request_bytes: int = 600_000_000
    allowed_file_exts: list[str] = Field(
        default_factory=lambda: [".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm", ".csv", ".txt", ".mp3", ".mp4", ".wav"]
    )
    disk_min_free_bytes: int = 5_000_000_000  # stop ingesting/downloading below this

    # --- privacy / retention (SOC 2: P4, C1.2)
    raw_mime_retention_days: int = 30
    rejected_raw_retention_days: int = 7
    request_pseudonymise_days: int = 90
    blob_retention_days: int = 395
    audit_hmac_key: SecretStr = SecretStr("")  # HMAC for pseudonymous subject ids in the audit log
    # Seals secrets kept at rest (drop links, delete tokens, queued outbound mail). Falls back to
    # audit_hmac_key, then to a random key generated once under data_dir/keys/.
    data_encryption_key: SecretStr = SecretStr("")
    # OpenRouter only: route only to providers that don't store/train on prompts. TypeSafe is outside
    # it (see typesafe_* above).
    llm_zero_data_retention: bool = True

    # --- database roles: the app never connects as the owner/superuser
    migration_database_url: SecretStr | None = None
    app_version: str = "dev"

    # --- audit trail, disposal, incident response, monitoring (SOC 2: CC4, CC7, P4, P5)
    request_delete_days: int = 400  # request rows (pseudonymised since request_pseudonymise_days)
    audit_retention_days: int = 400  # events; never below 400 (enforced by purge_events())
    audit_export_dir: str = ""  # daily hash-chained JSONL exports; default {data_dir}/audit
    backup_log_path: str = "/home/deploy/backups/regulatory-agent/backup.log"  # read by /health/deep
    metrics_port: int = 9710  # the worker's Prometheus endpoint, on 127.0.0.1; 0 = off
    metrics_ingest_port: int = 9711  # ingest's, on 127.0.0.1; 0 = off (web: GET /metrics, loopback only)
    privacy_contact: str = "privacy@hsingh.app"
    security_contact: str = "security@hsingh.app"
    access_log_retention_days: int = 30  # Caddy's roll_keep_for (720h), as stated in the privacy notice
    backup_retention_days: int = 14  # deploy/backup.sh, as stated in the privacy notice
    offsite_backup_retention_days: int = 35  # the off-site bucket's lifecycle rule


@lru_cache
def get_settings() -> Settings:
    return Settings()
