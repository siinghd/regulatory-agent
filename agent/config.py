"""Runtime settings. Every secret comes from the environment (.env in dev, compose env in prod)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    # --- limits
    max_docs_per_request: int = 10
    rate_per_sender_hour: int = 6
    rate_per_domain_hour: int = 30
    rate_global_hour: int = 300
    max_requests_per_thread: int = 5
    max_inbound_bytes: int = 5_000_000

    # --- LLM (OpenRouter, OpenAI-compatible)
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_models: list[str] = Field(
        default_factory=lambda: [
            "deepseek/deepseek-v4.1-flash",
            "qwen/qwen3.8-27b",
        ]
    )
    llm_timeout_s: float = 90.0  # summaries over ~60k chars take 20-70s

    # --- providers / egress
    # Per-provider egress proxy. UARB blocks non-North-American IPs, so its traffic leaves via
    # a Canadian SOCKS tunnel; a residential proxy is the fallback.
    uarb_proxy: str | None = "socks5://127.0.0.1:1080"
    uarb_fallback_proxy: SecretStr | None = None
    uarb_sessions_per_matter: int = 3
    uarb_max_concurrent_sessions: int = 4  # politeness cap across all workers
    # The OEB's document server is reachable directly; set a proxy only if that changes.
    oeb_proxy: str | None = None
    oeb_max_concurrency: int = 4  # concurrent HTTP requests per worker
    browser_nav_timeout_ms: int = 60_000

    # --- delivery
    drop_base_url: str = "https://drop.hsingh.app"
    drop_upload_url: str = "http://127.0.0.1:3060"  # same host: skip Caddy for uploads
    drop_expiry_s: int = 7 * 24 * 3600
    drop_max_downloads: int = 25
    attach_inline_max_bytes: int = 10_000_000

    # --- web viewer
    public_base_url: str = "https://uarb.hsingh.app"
    web_port: int = 8710

    # --- caching
    matter_cache_ttl_s: int = 6 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
