"""The metrics privacy rules of deploy/observability/METRICS_CONTRACT.md, as a check over a registry.

The dashboards are public, so every series must be an aggregate: label names from the contract's
complete list, enum labels within their declared values, and no label value that looks like an
email address, an IP address, a matter number or a UUID. `violations()` is run by
tests/unit/test_metrics.py after exercising the instrumentation, and at the end of every test
session (tests/conftest.py), over every value any test made the code record.
"""

import ipaddress
import re

from prometheus_client import REGISTRY

from agent.metrics import RETRY_CAUSES

# The contract's complete list, the labels already shipped with fixed values, and the client's own.
ALLOWED_LABELS = frozenset({
    "provider", "outcome", "stage", "model", "classifier", "escalated", "verdict", "key_type", "kind", "state",
    "reason", "cause",
    "final_state", "dependency", "limiter", "decision", "budget",
    "le", "quantile",
})

STATES = frozenset({"received", "accepted", "fetching", "packaging", "replying", "done", "failed", "rejected",
                    "clarify"})
FINAL_STATES = frozenset({"done", "failed", "rejected", "clarify"})
_ANY = None  # checked by the patterns only

# (metric family, label) -> declared values; labels not listed here are checked by the patterns only.
ENUMS: dict[tuple[str, str], frozenset[str] | None] = {
    ("requests", "final_state"): FINAL_STATES,
    ("retries", "cause"): RETRY_CAUSES,
    ("stage_duration_seconds", "stage"): STATES,
    ("limiter_decisions", "decision"): frozenset({"allowed", "limited", "deferred", "unavailable"}),
    ("request_e2e_seconds", "outcome"): frozenset({"done", "failed", "clarify"}),
    ("provider_requests", "state"): FINAL_STATES,
    ("provider_fetch_seconds", "outcome"): frozenset({"ok", "not_found", "error", "timeout", "blocked", "deferred"}),
    ("model_calls", "kind"): frozenset({"jev", "llm"}),
    ("model_calls", "outcome"): frozenset({"ok", "error", "timeout", "refused", "budget"}),
    ("model_call_seconds", "kind"): frozenset({"jev", "llm"}),
    ("gate_decisions", "classifier"): frozenset({"rules", "jev", "llm"}),
    ("gate_decisions", "escalated"): frozenset({"true", "false"}),
    ("gate_decisions", "outcome"): frozenset({"accept", "reject", "clarify"}),
    ("outbound_messages", "kind"): frozenset({"ack", "reply", "notice"}),
    ("outbound_messages", "outcome"): frozenset({"sent", "undeliverable", "deferred", "suppressed"}),
    ("deliveries", "kind"): frozenset({"attachment", "drop"}),
    ("deliveries", "outcome"): frozenset({"ok", "error"}),
    ("auth_verdicts", "verdict"): frozenset({"pass", "fail", "none"}),
    ("web_rate_limited", "kind"): frozenset({"progress", "progress_json", "files", "citation", "default"}),
    ("citations", "outcome"): frozenset({"kept", "dropped", "support_failed"}),
}

_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,31}")  # a provider's `name`, or "none"
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)
# UARB M12205, OEB EB-2024-0111, FERC ER24-1234(-000): the providers' canonical forms, anywhere.
_MATTER = re.compile(r"(?<![A-Za-z0-9])(?:M\d{5}|EB-\d{4}-\d{4}|[A-Z]{2,4}\d{2}-\d{1,6}(?:-\d{3})?)(?![0-9])")
_IP_CANDIDATE = re.compile(r"[0-9A-Fa-f:.]{3,}(?:/\d{1,3})?")


def personal(value: str) -> str | None:
    """What `value` looks like that a public label must never be (None: nothing)."""
    if _EMAIL.search(value):
        return "an email address"
    if _UUID.search(value):
        return "a UUID"
    if _MATTER.search(value):
        return "a matter number"
    for token in _IP_CANDIDATE.findall(value):
        try:
            ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if ":" in token or token.count(".") == 3:  # not a bare number like "180" or "1.5"
            return "an IP address"
    return None


def violations(registry=REGISTRY) -> list[str]:
    found: list[str] = []
    for family in registry.collect():
        for sample in family.samples:
            for label, value in sample.labels.items():
                if label not in ALLOWED_LABELS:
                    found.append(f"{family.name}: label {label!r} is not in the contract's allowlist")
                    continue
                if label == "provider" and not _PROVIDER.fullmatch(value):
                    found.append(f"{family.name}: provider={value!r} is not a provider name")
                allowed = ENUMS.get((family.name, label), _ANY)
                if allowed is not None and value not in allowed and label not in {"le", "quantile"}:
                    found.append(f"{family.name}: {label}={value!r} is not one of {sorted(allowed)}")
                what = personal(value) if label not in {"le", "quantile"} else None
                if what:
                    found.append(f"{family.name}: {label}={value!r} looks like {what}")
    return sorted(set(found))
