# Runbook: TLS certificate expiry

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentTLSCertExpiringSoon` | `warning` | For 1 h, a probed certificate expires in less than 14 days. |
| `RegagentTLSCertExpiryCritical` | `page` | For 10 min, a probed certificate expires in less than 3 days. |

The blackbox probes give `probe_ssl_earliest_cert_expiry`. Incident severity: SEV3. SEV2 when less than 3 days remain.

| Probe | Certificate | Renewal |
|---|---|---|
| `uarb-health`, `drop-health` | The Cloudflare edge certificate | Cloudflare renews it. If it is near expiry, examine the edge certificates of the zone in Cloudflare (SSL/TLS). |
| `mail-submission`, `mail-imaps` | `/etc/ssl/mail/fullchain.pem` (mail.hsingh.app, Let's Encrypt) | certbot (`/etc/letsencrypt/renewal/mail.hsingh.app.conf`) |
| `origin-cert` | `/etc/caddy/certs/origin.crt` (Cloudflare origin CA) | By hand in the Cloudflare dashboard |

`deploy/compliance_check.sh` also examines `/etc/ssl/mail/fullchain.pem` and `/etc/caddy/certs/origin.crt` each week.

## 1. Mail certificate

1. Do a dry run of the renewal. The output tells why a renewal fails.

   ```bash
   sudo certbot renew --cert-name mail.hsingh.app --dry-run
   ```

2. Renew the certificate.

   ```bash
   sudo certbot renew --cert-name mail.hsingh.app
   ```

3. If the deployment uses copies or links in `/etc/ssl/mail/`, update them.
4. Reload Postfix and Dovecot.

   ```bash
   sudo systemctl reload postfix dovecot
   ```

## 2. Origin certificate

1. Make a new origin certificate in the Cloudflare dashboard.
2. Install it as `/etc/caddy/certs/origin.crt` and `/etc/caddy/certs/origin.key`. The owner is `caddy`. The key has mode 600.
3. Reload Caddy.

   ```bash
   sudo systemctl reload caddy
   ```
