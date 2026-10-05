#!/usr/bin/env bash
# Backup freshness for node_exporter's textfile collector: reads the backup log
# (deploy/backup.sh appends `<ISO time> ok pg=<n>B files=<n>B requests=<n> remote=<r>` per good run)
# and writes regagent_backup.prom atomically into the textfile directory.
#
#   deploy/observability/backup_metrics.sh [TEXTFILE_DIR]     (default /var/lib/regagent-textfile)
#
# Every 5 minutes via regagent-backup-metrics.timer. Alerts: RegagentBackupStale (> 26 h, the same
# limit as deploy/compliance_check.sh), RegagentBackupMetricsMissing (this script stopped).
set -euo pipefail
. "$(dirname "$0")/../lib/common.sh"

out_dir=${1:-${TEXTFILE_DIR:-/var/lib/regagent-textfile}}
bdir=$(envget BACKUP_DIR); bdir=${bdir:-/home/deploy/backups/regulatory-agent}
log="$bdir/backup.log"

epoch() { date -u -d "$1" +%s 2>/dev/null || true; }
field() { sed -nE "s/.* $1=([0-9]+)B.*/\1/p" <<<"$2"; }

readable=0 last_ok_ts="" last_run_ts="" last_run_ok=0 pg_b="" files_b="" offsite=0
if [ -r "$log" ]; then
  readable=1
  last_line=$(tail -n 1 "$log")
  ok_line=$(grep ' ok ' "$log" | tail -n 1 || true)
  [ -n "$last_line" ] && last_run_ts=$(epoch "${last_line%% *}")
  [[ $last_line == *" ok "* ]] && last_run_ok=1
  if [ -n "$ok_line" ]; then
    last_ok_ts=$(epoch "${ok_line%% *}")
    pg_b=$(field pg "$ok_line"); files_b=$(field files "$ok_line")
    [[ $ok_line == *" remote="* && $ok_line != *" remote=none"* ]] && offsite=1
  fi
fi

tmp=$(mktemp "$out_dir/.regagent_backup.prom.XXXXXX")
trap 'rm -f "$tmp"' EXIT
{
  echo "# HELP regagent_backup_log_readable 1 if the backup log could be read."
  echo "# TYPE regagent_backup_log_readable gauge"
  echo "regagent_backup_log_readable $readable"
  if [ -n "$last_ok_ts" ]; then
    echo "# HELP regagent_backup_last_success_timestamp_seconds Time of the newest restore-tested backup."
    echo "# TYPE regagent_backup_last_success_timestamp_seconds gauge"
    echo "regagent_backup_last_success_timestamp_seconds $last_ok_ts"
  fi
  if [ -n "$last_run_ts" ]; then
    echo "# HELP regagent_backup_last_run_timestamp_seconds Time of the newest backup log line, whatever its result."
    echo "# TYPE regagent_backup_last_run_timestamp_seconds gauge"
    echo "regagent_backup_last_run_timestamp_seconds $last_run_ts"
    echo "# HELP regagent_backup_last_run_ok 1 if the newest backup log line is a success."
    echo "# TYPE regagent_backup_last_run_ok gauge"
    echo "regagent_backup_last_run_ok $last_run_ok"
  fi
  if [ -n "$pg_b$files_b" ]; then
    echo "# HELP regagent_backup_size_bytes Size of the newest good backup's encrypted archives."
    echo "# TYPE regagent_backup_size_bytes gauge"
    [ -n "$pg_b" ] && echo "regagent_backup_size_bytes{kind=\"pg\"} $pg_b"
    [ -n "$files_b" ] && echo "regagent_backup_size_bytes{kind=\"files\"} $files_b"
  fi
  echo "# HELP regagent_backup_offsite 1 if the newest good backup was also copied off-site."
  echo "# TYPE regagent_backup_offsite gauge"
  echo "regagent_backup_offsite $offsite"
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$out_dir/regagent_backup.prom"
trap - EXIT
