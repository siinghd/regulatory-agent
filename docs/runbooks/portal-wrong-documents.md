# Runbook: portal sends wrong documents

This document is written in ASD-STE100 Simplified Technical English.

No alert links here. Start this runbook when the UARB portal (FileMaker WebDirect) gives a file under the wrong id.

Known cause: GO GET IT serves the active record of FileMaker. The portal shares the prepared file across guest sessions from 1 client IP. On the live portal, session A asked for 102674 and got the files that sessions B and C had requested.

Signals:

- `uarb.download_retry` lines in the worker log with `asked for <id>, portal served <name>`;
- retries that occur again and again for 1 matter;
- a requester who says that a document is not what its title says.

Incident severity: an occasional mismatch that a retry corrects is normal operation. Mismatches that occur again and again are SEV2: the portal serves wrong documents, and the check stops them before the send. SEV1 if a wrong document reached a requester. Parent document: [Incident response](../policies/incident-response.md).

## 1. Make sure that the guard holds

The provider compares each downloaded file with the requested id (the file name). It also examines that the file is not empty, its magic bytes and its SHA-256. The Redis lock `lock:uarb:download` serialises the step from the click to the served file across all workers. 1 egress IP has 1 shared portal state.

1. Search the worker log for mismatches.

   ```bash
   docker compose logs --since 24h worker | grep -E 'download_retry|portal served' | tail -50
   ```

2. If the provider catches the mismatches and the retries succeed, the guard operates. Continue to watch. No other action is necessary.
3. If mismatches occur frequently, find the other user of the portal state of the egress IP. Examples are another client on the egress VM, or a second worker host without the lock. Stop it.

## 2. If a wrong document possibly reached a requester

1. Stop UARB deliveries. Stop the egress tunnel. UARB fetches then fail as "portal unavailable" and retry later. OEB and FERC continue.

   ```bash
   sudo systemctl stop uarb-egress-tunnel.service
   ```

   NOTE: `RegagentEgressTunnelDown` then fires. To stop all providers, use `docker compose stop worker` in place of this step.

2. Find the scope. For the time window, list what the agent sent and to whom. Run this query as the superuser (`docker compose exec -T postgres psql -U agent -d agent`).

   ```sql
   SELECT r.id, r.from_addr, r.matter, r.doc_type, r.reply_sent_at, d.external_id, d.title, d.sha256
   FROM requests r
   JOIN documents d ON d.provider = r.provider AND d.matter = r.matter AND d.doc_type = r.doc_type
   WHERE r.provider = 'uarb' AND r.reply_sent_at BETWEEN '<from>' AND '<to>'
   ORDER BY r.reply_sent_at;
   ```

3. For each document, compare the stored file with the file on the portal. Open the blob (`data/blobs/<sha[:2]>/<sha>`). Examine its title page and its id. The same SHA-256 under 2 different ids is a strong signal.
4. Remove the bad content, so that the cache cannot serve it again. For each wrong SHA-256, do these tasks as the superuser:
   - Set `documents.sha256` to NULL for the affected rows.
   - Delete the `pages` rows of that SHA-256.
   - Delete the blob file.
   - Delete the `summaries` rows whose claims cite the document.

   The next request downloads the file again and examines it again.

5. Send a correction to each affected requester. Use only the authenticated address of the request. Tell which file was wrong. Give the correct document or a new link. Tell that the cited summary possibly quoted the wrong file.
6. Correct the cause through an emergency change. Add a regression test with the observed portal behaviour.
7. Start the egress tunnel again.

   ```bash
   sudo systemctl start uarb-egress-tunnel.service
   ```

## 3. After the incident

1. Record the time window, the number of affected requests and requesters, and how you found the problem.
2. Decide if the mismatch check must send an alert, not only write a log line (Open).
