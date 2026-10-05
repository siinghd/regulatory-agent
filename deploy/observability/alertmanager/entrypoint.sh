#!/bin/sh
# Renders alertmanager.yml.tmpl with the ALERT_* variables (deploy/observability/.env via
# compose env_file) into /tmp/alertmanager.yml, then starts Alertmanager on it.
# Values are inserted inside single-quoted YAML strings, so a single quote is doubled.
# Defaults keep every receiver syntactically valid while it is unused: with no .env at all,
# alerts go to the black hole.
set -eu

: "${ALERT_RECEIVER:=blackhole}"
: "${ALERT_WATCHDOG_RECEIVER:=blackhole}"
: "${ALERT_EMAIL_TO:=alerts-not-configured@invalid.invalid}"
: "${ALERT_EMAIL_FROM:=regagent-alertmanager@invalid.invalid}"
: "${ALERT_SMTP_SMARTHOST:=127.0.0.1:25}"
: "${ALERT_SMTP_USERNAME:=}"
: "${ALERT_SMTP_PASSWORD:=}"
: "${ALERT_SMTP_REQUIRE_TLS:=true}"
: "${ALERT_WEBHOOK_URL:=http://127.0.0.1:9/not-configured}"
: "${ALERT_WATCHDOG_URL:=http://127.0.0.1:9/not-configured}"
export ALERT_RECEIVER ALERT_WATCHDOG_RECEIVER ALERT_EMAIL_TO ALERT_EMAIL_FROM ALERT_SMTP_SMARTHOST \
  ALERT_SMTP_USERNAME ALERT_SMTP_PASSWORD ALERT_SMTP_REQUIRE_TLS ALERT_WEBHOOK_URL ALERT_WATCHDOG_URL

case "$ALERT_RECEIVER" in blackhole|email|webhook) ;; *)
  echo "ALERT_RECEIVER must be blackhole, email or webhook (got '$ALERT_RECEIVER')" >&2; exit 1 ;;
esac
case "$ALERT_WATCHDOG_RECEIVER" in blackhole|watchdog) ;; *)
  echo "ALERT_WATCHDOG_RECEIVER must be blackhole or watchdog (got '$ALERT_WATCHDOG_RECEIVER')" >&2; exit 1 ;;
esac

umask 077
awk '{
  line = $0; out = ""
  while (match(line, /\$\{ALERT_[A-Z_]+\}/)) {
    val = ENVIRON[substr(line, RSTART + 2, RLENGTH - 3)]
    gsub(/\047/, "\047\047", val)
    out = out substr(line, 1, RSTART - 1) val
    line = substr(line, RSTART + RLENGTH)
  }
  print out line
}' /etc/alertmanager/alertmanager.yml.tmpl > /tmp/alertmanager.yml

exec /bin/alertmanager --config.file=/tmp/alertmanager.yml "$@"
