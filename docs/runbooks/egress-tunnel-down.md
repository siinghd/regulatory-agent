# Runbook: egress tunnel down

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentEgressTunnelDown` | `page` | For 5 min, no process listens on 127.0.0.1:1080. |
| `RegagentEgressViaTunnelFailing` | `warning` | For 15 min, the port is open, but HTTPS through the tunnel fails. |

Incident severity: SEV3. UARB requests wait. OEB and FERC are not affected.

The UARB portal answers only IP addresses in North America. All UARB traffic uses 1 SSH SOCKS tunnel (`uarb-egress-tunnel.service`) to the Azure VM `azuretest` in Canada. No fallback proxy exists. The tunnel is a single point of failure for UARB. The upgrade path is an egress pool ([ADR-022](../guide/decisions-log.md#adr-022-egress-pool-for-uarb), proposed).

While the tunnel is down, UARB fetches fail as "portal unavailable". The requests retry, and the `uarb` breaker parks them. At the 2 h deadline, each requester gets 1 apology.

## 1. Diagnose

1. Examine the tunnel unit.

   ```bash
   systemctl status uarb-egress-tunnel.service --no-pager
   ```

   Expected result: `active (running)`.

2. Read the log of the unit.

   ```bash
   journalctl -u uarb-egress-tunnel.service --since -1h | tail -30
   ```

3. Send a test request through the tunnel to a neutral site.

   ```bash
   curl -s --max-time 15 -x socks5h://127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace | grep -E 'ip=|loc='
   ```

   Expected result: `loc=CA` and the IP address of the egress VM.

4. Make sure that SSH to the egress VM operates.

   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=10 azuretest true && echo ssh ok
   ```

CAUTION: Do not use a UARB matter as a tunnel test. Each portal visit counts against the daily UARB budget of 400 visits.

## 2. Repair

The unit has `Restart=always`, so systemd restarts it after each failure.

1. If the unit fails again and again, read the SSH error in the log. Usual causes are a changed host key, a refused key, or a remote VM that does not run.
2. If the VM does not run, start it in the Azure portal.
3. When the remote side is available, restart the tunnel.

   ```bash
   sudo systemctl restart uarb-egress-tunnel.service
   ```

4. Do the test request of section 1 again.

NOTE: For a long outage, tell the requesters who wait, if necessary. Do not set the kill switch (`ragent pause`) for a UARB-only outage. The kill switch also stops OEB and FERC requests and all outbound mail.

[Operations, section 10.1](../guide/operations.md#101-examine-the-egress-tunnel) has the same tunnel checks.
