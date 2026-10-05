#!/usr/bin/env python3
"""Generates the Grafana dashboards in ./dashboards/*.json (provisioned read-only).

    python3 deploy/observability/grafana/build_dashboards.py

Edit here, re-run, commit both. Grafana re-reads the files within a minute.
Metric names and labels: agent/metrics.py and ../METRICS_CONTRACT.md. Panels on metrics the app
does not export yet show "No data" and say so in their description.

No template variables on purpose: the public path refuses /api/datasources/*, which variable
queries need. Every panel is a fixed query.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "regagent-prometheus"}
TITLE = "Regulatory Document Agent"
DISCLAIMER = (
    "**MVP monitoring for evaluation of the Regulatory Document Agent.** "
    "Aggregate metrics only — no personal data or request contents. Not a production SLA."
)
PENDING = "Pending instrumentation: `{m}` (deploy/observability/METRICS_CONTRACT.md). No data until the app exports it."

GREEN, YELLOW, RED, NONE = "green", "#EAB839", "red", "transparent"  # NONE: no data is not "bad"


# ---------------------------------------------------------------- building blocks


def q(expr: str, legend: str = "", *, instant: bool = False, ref: str = "A", fmt: str = "time_series", interval: str = "") -> dict:
    t = {"datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "refId": ref,
         "range": not instant, "instant": instant, "format": fmt, "editorMode": "code"}
    if interval:
        t["interval"] = interval
    return t


def thresholds(*steps) -> dict:
    """steps: (color, from_value) pairs; the first one's value is ignored (base)."""
    return {"mode": "absolute", "steps": [{"color": c, "value": None if i == 0 else v} for i, (c, v) in enumerate(steps)]}


def updown() -> list:
    return [{"type": "value", "options": {"0": {"text": "DOWN", "color": RED, "index": 0},
                                          "1": {"text": "UP", "color": GREEN, "index": 1}}}]


def openclosed() -> list:
    return [{"type": "value", "options": {"0": {"text": "closed", "color": GREEN, "index": 0},
                                          "1": {"text": "OPEN", "color": RED, "index": 1}}}]


def panel(kind: str, title: str, targets: list, *, w: int, h: int, desc: str = "", unit: str = "short",
          th: dict | None = None, mappings: list | None = None, options: dict | None = None,
          custom: dict | None = None, decimals: int | None = None, no_value: str | None = None,
          interval: str = "", minv=None, maxv=None, overrides: list | None = None,
          transformations: list | None = None) -> dict:
    defaults = {"unit": unit, "thresholds": th or thresholds((GREEN, None)), "mappings": mappings or [],
                "color": {"mode": "thresholds" if kind in ("stat", "gauge", "bargauge", "state-timeline") else "palette-classic"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value is not None:
        defaults["noValue"] = no_value
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    if custom:
        defaults["custom"] = custom
    p = {"type": kind, "title": title, "description": desc, "datasource": DS, "targets": targets,
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {},
         "_w": w, "_h": h}
    if interval:
        p["interval"] = interval
    if transformations:
        p["transformations"] = transformations
    return p


def stat(title, expr, *, w=4, h=4, unit="short", th=None, desc="", mappings=None, decimals=None,
         no_value=None, legend="", instant=True, text_mode="auto", color_mode="background"):
    return panel("stat", title, [q(expr, legend, instant=instant)], w=w, h=h, unit=unit, th=th, desc=desc,
                 mappings=mappings, decimals=decimals, no_value=no_value,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": color_mode, "graphMode": "none", "textMode": text_mode,
                          "justifyMode": "auto", "orientation": "auto", "showPercentChange": False,
                          "wideLayout": True})


def series(title, targets, *, w=12, h=8, unit="short", desc="", bars=False, stack=False, interval="",
           th=None, threshold_line=False, minv=None, maxv=None, overrides=None, decimals=None):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 1 if bars else 2,
              "fillOpacity": 80 if bars else 10, "showPoints": "never", "spanNulls": False,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "thresholdsStyle": {"mode": "line+area" if threshold_line else "off"},
              "axisSoftMin": 0}
    return panel("timeseries", title, targets, w=w, h=h, unit=unit, desc=desc, custom=custom,
                 interval=interval, th=th, minv=minv, maxv=maxv, overrides=overrides, decimals=decimals,
                 options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                          "tooltip": {"mode": "multi", "sort": "desc"}})


def timeline(title, expr, legend, *, w=12, h=8, mappings=None, desc=""):
    return panel("state-timeline", title, [q(expr, legend)], w=w, h=h, desc=desc, mappings=mappings,
                 th=thresholds((GREEN, None)),
                 options={"showValue": "auto", "mergeValues": True, "alignValue": "left", "rowHeight": 0.8,
                          "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                          "tooltip": {"mode": "single", "sort": "none"}})


def bars(title, expr, legend, *, w=8, h=8, unit="short", th=None, desc="", minv=None, maxv=None, decimals=None):
    return panel("bargauge", title, [q(expr, legend, instant=True)], w=w, h=h, unit=unit, th=th, desc=desc,
                 minv=minv, maxv=maxv, decimals=decimals,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                          "valueMode": "color", "namePlacement": "auto", "sizing": "auto"})


def text(content: str, *, w=24, h=3, title="") -> dict:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content,
            "code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False}},
            "_w": w, "_h": h, "transparent": not title}


def dashboard(uid: str, title: str, rows: list[list[dict]], *, time_from="now-24h", desc="") -> dict:
    panels, y, pid = [], 0, 1
    for row in [[text(DISCLAIMER)]] + rows:
        x, height = 0, 0
        for p in row:
            w, h = p.pop("_w"), p.pop("_h")
            p["id"], p["gridPos"] = pid, {"x": x, "y": y, "w": w, "h": h}
            pid, x, height = pid + 1, x + w, max(height, h)
            panels.append(p)
        assert x <= 24, f"{uid}: row wider than 24"
        y += height
    return {
        "uid": uid, "title": f"{TITLE} — {title}" if title else TITLE, "description": desc,
        "tags": ["regagent"], "timezone": "utc", "editable": False, "graphTooltip": 1,
        "schemaVersion": 41, "version": 1, "refresh": "1m", "liveNow": False,
        "time": {"from": time_from, "to": "now"},
        "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "1h"]},
        "annotations": {"list": []}, "templating": {"list": []},
        "links": [{"title": "Dashboards", "type": "dashboards", "tags": ["regagent"], "asDropdown": True,
                   "includeVars": False, "keepTime": True, "targetBlank": False, "icon": "external link"}],
        "panels": panels,
    }


# ---------------------------------------------------------------- shared expressions

DONE = 'sum(increase(requests_total{final_state="done"}[%s]))'
SETTLED = 'sum(increase(requests_total{final_state=~"done|failed"}[%s]))'
SUCCESS = f"{DONE} / {SETTLED}"
SUCCESS_DESC = "Of the requests that reached done or failed (rejected and clarify are gate outcomes, not failures)."
SLO_TH = thresholds((GREEN, None), (RED, 180))
PCT_GOOD = thresholds((NONE, None), (RED, 0), (YELLOW, 0.9), (GREEN, 0.98))
BUDGET_TH = thresholds((GREEN, None), (YELLOW, 0.8), (RED, 1))
FIRING = 'ALERTS{alertstate="firing", alertname!="RegagentWatchdog"}'
FS = 'fstype!~"tmpfs|overlay|squashfs|nsfs|ramfs|fuse.*"'


def home() -> dict:
    return dashboard("regagent-home", "", [
        [
            stat("Requests", "round(sum(increase(requests_total[24h])))", desc="Requests that reached a final state in the last 24 h.", no_value="0"),
            stat("Success", SUCCESS % ("24h", "24h"), unit="percentunit", th=PCT_GOOD, desc="Last 24 h. " + SUCCESS_DESC, decimals=1),
            stat("Reply p95", "regagent:request_e2e:p95_1h", unit="s", th=SLO_TH, no_value="pending",
                 desc="Email received → reply sent, 95th percentile over the last hour. SLO: 95 % within 180 s. " + PENDING.format(m="request_e2e_seconds")),
            stat("Queue", "max(queue_depth)", th=thresholds((GREEN, None), (YELLOW, 10), (RED, 20)), desc="Jobs waiting in the arq queue."),
            stat("Breakers", "sum(breaker_open)", th=thresholds((GREEN, None), (RED, 1)), desc="Dependencies whose breaker is open now."),
            stat("Alerts", f"count({FIRING}) or vector(0)", th=thresholds((GREEN, None), (RED, 1)), desc="Alerts firing now."),
        ],
        [
            stat("Service checks", "max by (probe) (probe_success)", w=16, h=8, mappings=updown(), legend="{{probe}}",
                 th=thresholds((RED, None), (GREEN, 1)), desc="Black-box probes: public /health endpoints, mail, egress tunnel, Postgres, Redis, origin TLS."),
            text("\n".join(f"- [{name}](/grafana/d/regagent-{uid})" for uid, name in (
                ("overview", "Overview: outcomes, reply latency vs SLO, queue, breakers"),
                ("portals", "Regulator portals: fetches, latency, visits vs budget"),
                ("models", "Models: Jev vs LLM, errors, escalation, spend"),
                ("delivery", "Delivery & mail: outbound, drop vs attachment, sender auth"),
                ("abuse", "Abuse & rate limits: limiters, pre-auth rejections, 429s"),
                ("host", "Host: CPU, memory, disk, backups, certificates"),
            )), w=8, h=8, title="Dashboards"),
        ],
        [
            panel("table", "Firing alerts", [q(FIRING, instant=True, fmt="table")], w=24, h=7,
                  desc="Alerts currently firing (aggregate conditions; see the runbooks in docs/runbooks/).",
                  transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True}}}],
                  options={"showHeader": True, "cellHeight": "sm"}),
        ],
    ], desc="Start page: health at a glance and links to every dashboard.")


def overview() -> dict:
    return dashboard("regagent-overview", "Overview", [
        [
            stat("Requests (range)", "round(sum(increase(requests_total[$__range])))", w=6, no_value="0"),
            stat("Success rate (range)", SUCCESS % ("$__range", "$__range"), w=6, unit="percentunit", th=PCT_GOOD, desc=SUCCESS_DESC, decimals=1),
            stat("Reply p95 (1 h)", "regagent:request_e2e:p95_1h", w=6, unit="s", th=SLO_TH, no_value="pending",
                 desc=PENDING.format(m="request_e2e_seconds")),
            stat("Within 180 s (1 h)", "1 - regagent:request_e2e_slow:ratio_rate1h", w=6, unit="percentunit",
                 th=thresholds((NONE, None), (RED, 0), (GREEN, 0.95)), no_value="pending", decimals=1, desc=PENDING.format(m="request_e2e_seconds")),
        ],
        [
            series("Requests by outcome (per hour)", [q("sum by (final_state) (increase(requests_total[$__interval]))", "{{final_state}}")],
                   bars=True, stack=True, interval="1h"),
            series("Reply time p50 / p95 vs SLO (trailing 1 h)",
                   [q("regagent:request_e2e:p50_1h", "p50"), q("regagent:request_e2e:p95_1h", "p95", ref="B")],
                   unit="s", th=SLO_TH, threshold_line=True,
                   desc="Email received → reply sent. Red band: above the 180 s objective. " + PENDING.format(m="request_e2e_seconds")),
        ],
        [
            series("Success rate (rolling 24 h)", [q(SUCCESS % ("24h", "24h"), "success rate")], w=8, unit="percentunit",
                   minv=0, maxv=1, desc=SUCCESS_DESC),
            series("Requests by regulator (per hour)", [q("sum by (provider) (increase(provider_requests_total[$__interval]))", "{{provider}}")],
                   w=8, bars=True, stack=True, interval="1h", desc=PENDING.format(m="provider_requests_total")),
            series("Queue depth", [q("max(queue_depth)", "waiting jobs")], w=8, th=thresholds((GREEN, None), (RED, 20)), threshold_line=True),
        ],
        [
            timeline("Circuit breakers", "max by (dependency) (breaker_open)", "{{dependency}}", mappings=openclosed()),
            series("Time spent in each stage (p95, trailing 1 h)",
                   [q("histogram_quantile(0.95, sum by (le, stage) (rate(stage_duration_seconds_bucket[1h])))", "{{stage}}")], unit="s"),
        ],
        [
            series("Retries by cause (per hour)", [q("sum by (cause) (increase(retries_total[$__interval]))", "{{cause}}")],
                   bars=True, stack=True, interval="1h"),
            bars("Daily budgets used (UTC day)", "max by (budget) (budget_used / budget_limit)", "{{budget}}", w=12,
                 unit="percentunit", th=BUDGET_TH, minv=0, maxv=1),
        ],
    ], desc="Request outcomes, reply latency against the SLO, volume, queue and breakers.")


def portals() -> dict:
    fetch = "provider_fetch_seconds"
    return dashboard("regagent-portals", "Regulator portals", [
        [
            stat("Fetch success by provider",
                 f'sum by (provider) (increase({fetch}_count{{outcome="ok"}}[$__range])) / sum by (provider) (increase({fetch}_count[$__range]))',
                 w=8, h=6, unit="percentunit", th=PCT_GOOD, legend="{{provider}}", no_value="pending", decimals=1,
                 desc=PENDING.format(m="provider_fetch_seconds")),
            bars("Portal visits today vs daily budget", 'max by (budget) (budget_used{budget=~"portal:.*"} / budget_limit{budget=~"portal:.*"})',
                 "{{budget}}", w=8, h=6, unit="percentunit", th=BUDGET_TH, minv=0, maxv=1,
                 desc="Visits counted against each provider's daily budget (resets at midnight UTC)."),
            timeline("Portal circuit breakers", 'max by (dependency) (breaker_open{dependency!~"drop|smtp|openrouter:.*"})',
                     "{{dependency}}", w=8, h=6, mappings=openclosed()),
        ],
        [
            series("Fetch latency p50 / p95 by provider (trailing 1 h)",
                   [q(f"histogram_quantile(0.95, sum by (le, provider) (rate({fetch}_bucket[1h])))", "{{provider}} p95"),
                    q(f"histogram_quantile(0.50, sum by (le, provider) (rate({fetch}_bucket[1h])))", "{{provider}} p50", ref="B")],
                   unit="s", desc=PENDING.format(m="provider_fetch_seconds")),
            series("Fetches by provider and outcome (per hour)",
                   [q(f"sum by (provider, outcome) (increase({fetch}_count[$__interval]))", "{{provider}} {{outcome}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="provider_fetch_seconds")),
        ],
        [
            series("Visits by provider (per hour)", [q("sum by (provider) (increase(provider_visits_total[$__interval]))", "{{provider}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="provider_visits_total")),
            series("Visits today vs limit", [q('max by (budget) (budget_used{budget=~"portal:.*"})', "{{budget}} used"),
                                             q('max by (budget) (budget_limit{budget=~"portal:.*"})', "{{budget}} limit", ref="B")],
                   desc="Daily portal budgets (UTC)."),
        ],
    ], desc="Per-regulator portal fetches: success, latency, visits against the daily budget, breakers.")


def models() -> dict:
    return dashboard("regagent-models", "Models", [
        [
            stat("Model calls", "sum by (kind) (increase(model_calls_total[$__range]))", w=6, legend="{{kind}}",
                 no_value="pending", decimals=0, desc="kind: jev (TypeSafe Jev) or llm (OpenRouter). " + PENDING.format(m="model_calls_total")),
            stat("Escalation rate",
                 'sum(increase(gate_decisions_total{escalated="true"}[$__range])) / sum(increase(gate_decisions_total[$__range]))',
                 w=6, unit="percentunit", no_value="pending", decimals=1, color_mode="value",
                 desc="Share of gate decisions escalated past the cheaper classifier. " + PENDING.format(m="gate_decisions_total")),
            stat("LLM spend today", 'max(budget_used{budget="llm_usd"})', w=6, unit="currencyUSD", decimals=2, color_mode="value"),
            stat("LLM budget used", 'max(budget_used{budget="llm_usd"} / budget_limit{budget="llm_usd"})', w=6,
                 unit="percentunit", th=BUDGET_TH, decimals=1),
        ],
        [
            series("Calls: Jev vs LLM (per hour)", [q("sum by (kind) (increase(model_calls_total[$__interval]))", "{{kind}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="model_calls_total")),
            series("Errors by model (per hour)", [q('sum by (model, outcome) (increase(model_calls_total{outcome!="ok"}[$__interval]))', "{{model}} {{outcome}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="model_calls_total")),
        ],
        [
            series("Call latency p95 by model (trailing 1 h)",
                   [q("histogram_quantile(0.95, sum by (le, model) (rate(model_call_seconds_bucket[1h])))", "{{model}}")],
                   unit="s", desc=PENDING.format(m="model_call_seconds")),
            series("Gate decisions by classifier (per hour)",
                   [q("sum by (classifier, escalated) (increase(gate_decisions_total[$__interval]))", "{{classifier}} escalated={{escalated}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="gate_decisions_total")),
        ],
        [
            series("LLM cost per day vs budget (USD)",
                   [q("sum(increase(llm_cost_usd_total[1d]))", "spent (trailing 24 h)"),
                    q('max(budget_limit{budget="llm_usd"})', "daily budget", ref="B")], unit="currencyUSD", decimals=2),
            timeline("LLM provider breakers", 'max by (dependency) (breaker_open{dependency=~"openrouter:.*"})', "{{dependency}}",
                     mappings=openclosed()),
        ],
    ], desc="Jev and LLM calls, errors, latency, gate escalation and spend against the daily budget.")


def delivery() -> dict:
    return dashboard("regagent-delivery", "Delivery & mail", [
        [
            stat("Emails sent", 'sum(increase(outbound_messages_total{outcome="sent"}[$__range]))', w=6, no_value="pending",
                 decimals=0, color_mode="value", desc=PENDING.format(m="outbound_messages_total")),
            stat("Undeliverable", 'sum(increase(outbound_messages_total{outcome="undeliverable"}[$__range]))', w=6,
                 th=thresholds((GREEN, None), (RED, 1)), no_value="pending", decimals=0, desc=PENDING.format(m="outbound_messages_total")),
            stat("Drop vs attachment", "sum by (kind) (increase(deliveries_total[$__range]))", w=6, legend="{{kind}}",
                 no_value="pending", decimals=0, color_mode="value", desc=PENDING.format(m="deliveries_total")),
            stat("Auth pass rate",
                 'sum(increase(auth_verdicts_total{verdict="pass"}[$__range])) / sum(increase(auth_verdicts_total[$__range]))',
                 w=6, unit="percentunit", no_value="pending", decimals=1, color_mode="value", desc=PENDING.format(m="auth_verdicts_total")),
        ],
        [
            series("Outbound email by kind and outcome (per hour)",
                   [q("sum by (kind, outcome) (increase(outbound_messages_total[$__interval]))", "{{kind}} {{outcome}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="outbound_messages_total")),
            series("Document delivery: drop link vs attachment (per hour)",
                   [q("sum by (kind, outcome) (increase(deliveries_total[$__interval]))", "{{kind}} {{outcome}}")],
                   bars=True, stack=True, interval="1h", desc=PENDING.format(m="deliveries_total")),
        ],
        [
            series("Sender authentication verdicts (per hour)", [q("sum by (verdict) (increase(auth_verdicts_total[$__interval]))", "{{verdict}}")],
                   bars=True, stack=True, interval="1h", desc="DMARC-style verdicts of inbound senders. " + PENDING.format(m="auth_verdicts_total")),
            timeline("Mail and drop endpoints", 'max by (probe) (probe_success{probe=~"mail-submission|mail-imaps|drop-health"})', "{{probe}}",
                     mappings=updown()),
        ],
        [
            timeline("SMTP and drop breakers", 'max by (dependency) (breaker_open{dependency=~"smtp|drop"})', "{{dependency}}",
                     w=24, h=5, mappings=openclosed()),
        ],
    ], desc="Outbound mail, document delivery method, inbound sender authentication.")


def abuse() -> dict:
    return dashboard("regagent-abuse", "Abuse & rate limits", [
        [
            stat("Pre-auth rejections (1 h)", "regagent:preauth_limited:increase1h", w=6, decimals=0, no_value="pending", color_mode="value",
                 desc="Inbound mail refused by the per-IP/per-domain pre-auth limits. Counted in the ingest process. " + PENDING.format(m="ingest /metrics")),
            stat("7-day hourly baseline", "regagent:preauth_limited:avg_hourly_7d", w=6, decimals=1, no_value="pending", color_mode="value"),
            stat("Unauthenticated (1 h)", "regagent:auth_not_pass:increase1h", w=6, decimals=0, no_value="pending", color_mode="value",
                 desc=PENDING.format(m="auth_verdicts_total")),
            stat("Web 429s",
                 'sum(increase(limiter_decisions_total{limiter=~"web_.*", decision="limited"}[$__range]))', w=6, decimals=0,
                 no_value="0", color_mode="value", desc="Viewer requests answered 429 by the per-IP token buckets."),
        ],
        [
            series("Limiter decisions by key type (per second, 5 m rate)",
                   [q("regagent:limiter_decisions:rate5m_by_key_type", "{{key_type}} {{decision}}")], unit="reqps",
                   desc="key_type is the limiter's prefix: sender, domain, global, preauth, inbound, inflight, llm, portal, bytes, web."),
            series("Limited or deferred, by limiter (per hour)",
                   [q('sum by (limiter, decision) (increase(limiter_decisions_total{decision!="allowed"}[$__interval]))', "{{limiter}} {{decision}}")],
                   bars=True, stack=True, interval="1h"),
        ],
        [
            series("Pre-auth rejections vs 3x baseline (trailing 1 h)",
                   [q("regagent:preauth_limited:increase1h", "rejections, last hour"),
                    q("3 * regagent:preauth_limited:avg_hourly_7d", "alert line (3x 7-day hourly average)", ref="B")],
                   desc=PENDING.format(m="ingest /metrics")),
            series("Web 429s by route class (per hour)",
                   [q("sum by (kind) (increase(web_rate_limited_total[$__interval]))", "{{kind}}"),
                    q('sum by (limiter) (increase(limiter_decisions_total{limiter=~"web_.*", decision="limited"}[$__interval]))', "{{limiter}} (limiter)", ref="B")],
                   bars=True, stack=True, interval="1h", desc="web_rate_limited_total: " + PENDING.format(m="web_rate_limited_total")),
        ],
    ], desc="Limiter decisions, pre-auth rejections against their baseline, unauthenticated senders, web 429s.")


def host() -> dict:
    return dashboard("regagent-host", "Host", [
        [
            stat("CPU busy", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', unit="percentunit",
                 th=thresholds((GREEN, None), (YELLOW, 0.7), (RED, 0.9)), decimals=0),
            stat("Memory used", "1 - max(node_memory_MemAvailable_bytes) / max(node_memory_MemTotal_bytes)", unit="percentunit",
                 th=thresholds((GREEN, None), (YELLOW, 0.8), (RED, 0.9)), decimals=0),
            stat("Disk free /", 'min(regagent:filesystem_avail:ratio{mountpoint="/"})', unit="percentunit",
                 th=thresholds((NONE, None), (RED, 0), (YELLOW, 0.1), (GREEN, 0.2)), decimals=0),
            stat("Backup age", "time() - max(regagent_backup_last_success_timestamp_seconds)", unit="s",
                 th=thresholds((GREEN, None), (RED, 26 * 3600)), desc="Restore-tested, age-encrypted backup (limit 26 h)."),
            stat("Cert expiry", "min(probe_ssl_earliest_cert_expiry - time())", unit="s",
                 th=thresholds((RED, None), (YELLOW, 3 * 86400), (GREEN, 14 * 86400))),
            stat("Egress tunnel", 'max(probe_success{probe="egress-tunnel"})', mappings=updown(), th=thresholds((RED, None), (GREEN, 1))),
        ],
        [
            series("CPU and load", [q('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval]))', "CPU busy"),
                                    q('max(node_load5) / count(node_cpu_seconds_total{mode="idle"})', "load5 per core", ref="B")],
                   w=8, unit="percentunit", minv=0),
            series("Memory", [q("max(node_memory_MemTotal_bytes) - max(node_memory_MemAvailable_bytes)", "used"),
                              q("max(node_memory_MemAvailable_bytes)", "available", ref="B")], w=8, unit="bytes", stack=True),
            series("Disk free by mount", [q("regagent:filesystem_avail:ratio", "{{mountpoint}}")], w=8, unit="percentunit",
                   minv=0, maxv=1, th=thresholds((RED, None), (YELLOW, 0.1), (GREEN, 0.2)), threshold_line=True),
        ],
        [
            bars("Certificate expiry (days, scale capped at 90)", "min by (probe) (probe_ssl_earliest_cert_expiry - time()) / 86400",
                 "{{probe}}", w=8, unit="none", th=thresholds((RED, None), (YELLOW, 3), (GREEN, 14)), decimals=0, minv=0, maxv=90),
            timeline("Service checks", "max by (probe) (probe_success)", "{{probe}}", w=16, mappings=updown()),
        ],
        [
            series("Probe duration", [q("max by (probe) (probe_duration_seconds)", "{{probe}}")], unit="s"),
            series("Backup age", [q("time() - max(regagent_backup_last_success_timestamp_seconds)", "since last good backup")],
                   unit="s", th=thresholds((GREEN, None), (RED, 26 * 3600)), threshold_line=True),
        ],
    ], desc="Host CPU, memory, disk, backups, certificates and black-box checks.")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for build in (home, overview, portals, models, delivery, abuse, host):
        d = build()
        name = d["uid"].removeprefix("regagent-")
        (OUT / f"{name}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        print(f"{name}.json: {d['title']} ({len(d['panels'])} panels)")


if __name__ == "__main__":
    main()
