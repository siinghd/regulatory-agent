"""Entry points.

Services:    ragent ingest | worker | web | migrate
Retention:   ragent purge [--dry-run]
Monitoring:  ragent reconcile [--since 24h] | ragent healthcheck web|worker|ingest
Audit:       ragent audit <request_id> | --email ADDR | --export | --verify
Incidents:   ragent pause [--reason TEXT] | resume | revoke <request_id> | block <addr|@domain>
Privacy:     ragent dsar export <email> [--out FILE] | ragent dsar delete <email> --yes

Operator commands record an `admin.*` event naming the operator (SUDO_USER, else USER).
"""

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID

SERVICES = ("ingest", "worker", "web", "migrate")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ragent")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name in SERVICES:
        sub.add_parser(name, help=f"run the {name} service" if name != "migrate" else "apply migrations")

    p = sub.add_parser("purge", help="retention and disposal (daily)")
    p.add_argument("--dry-run", action="store_true", help="count what would go, change nothing")

    p = sub.add_parser("reconcile", help="consistency checks; exit 1 on anomalies")
    p.add_argument("--since", default="24h", help="window, e.g. 30m, 24h, 7d (default 24h)")

    p = sub.add_parser("healthcheck", help="container healthcheck; exit 0 when healthy")
    p.add_argument("service", choices=["web", "worker", "ingest"])

    p = sub.add_parser("audit", help="print a request's or a person's audit trail")
    p.add_argument("request_id", nargs="?", type=UUID)
    p.add_argument("--email", help="everything about this address (by its HMAC)")
    p.add_argument("--export", action="store_true", help="export every complete day not exported yet")
    p.add_argument("--verify", action="store_true", help="verify the exported files' hash chain")

    p = sub.add_parser("pause", help="kill switch: park all requests and hold all outbound mail")
    p.add_argument("--reason", default="")
    sub.add_parser("resume", help="lift the kill switch")

    p = sub.add_parser("revoke", help="delete a request's download link")
    p.add_argument("request_id", type=UUID)

    p = sub.add_parser("block", help="drop all mail from an address or @domain, unanswered")
    p.add_argument("value", metavar="address|@domain")
    p.add_argument("--reason", default="")

    p = sub.add_parser("dsar", help="data subject requests")
    dsar = p.add_subparsers(dest="action", required=True, metavar="action")
    e = dsar.add_parser("export", help="everything held about an address, as JSON")
    e.add_argument("email")
    e.add_argument("--out", help="write here (mode 600) instead of stdout")
    d = dsar.add_parser("delete", help="erase an address now, revoke its links, suppress it")
    d.add_argument("email")
    d.add_argument("--yes", action="store_true", help="confirm: this cannot be undone")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    from agent import audit

    audit.set_component(args.command if args.command in {"ingest", "worker", "web"} else "cli")
    if args.command == "worker":
        from arq.worker import run_worker

        from agent.worker import WorkerSettings

        run_worker(WorkerSettings)
    elif args.command == "web":
        import uvicorn

        from agent.config import get_settings

        uvicorn.run("agent.web.app:app", host="127.0.0.1", port=get_settings().web_port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False)
    elif args.command in {"ingest", "migrate"}:
        asyncio.run(_async(args.command))
    else:
        sys.exit(asyncio.run(_ops(args)))


async def _async(command: str) -> None:
    from agent import db, metrics, queue
    from agent.config import get_settings
    from agent.logs import configure_logging

    configure_logging()
    if command == "migrate":
        # The only place schema changes happen, as the owner role (MIGRATION_DATABASE_URL when set);
        # the app processes connect as an unprivileged role and never migrate.
        await db.run_migrations()
        return
    from agent.mail.ingest import Ingestor

    s = get_settings()
    metrics.start_server(s.metrics_ingest_port, component="ingest")  # pre-auth limits, inbound ceiling
    await db.create_pool()
    redis = await queue.create_pool(s.redis_url)
    await Ingestor(s, redis).run_forever()


async def _ops(args: argparse.Namespace) -> int:
    """Operator and scheduled commands. Returns the process exit code."""
    from agent import health
    from agent.logs import configure_logging

    if args.command == "healthcheck":
        ok, detail = await health.healthcheck(args.service)
        print(("healthy: " if ok else "unhealthy: ") + detail)
        return 0 if ok else 1

    if args.command == "dsar" and args.action == "delete" and not args.yes:
        print("refusing without --yes: this erases everything held about the address", file=sys.stderr)
        return 2
    from agent import db

    configure_logging("WARNING" if args.command in {"audit", "dsar"} else "INFO")
    await db.create_pool(max_size=4)
    try:
        return await _run(args)
    finally:
        await db.close_pool()


async def _run(args: argparse.Namespace) -> int:
    from redis.asyncio import Redis

    from agent import admin, reconcile, retention
    from agent.config import get_settings

    s = get_settings()
    if args.command == "purge":
        counts = await retention.purge(dry_run=args.dry_run)
        print(json.dumps({"dry_run": args.dry_run, **counts}, indent=2))
        return 0
    if args.command == "reconcile":
        findings = await reconcile.reconcile(reconcile.parse_since(args.since))
        print(json.dumps(findings, indent=2, default=str))
        return 1 if findings["anomalies"] else 0
    if args.command == "audit":
        return await _audit(args)
    if args.command in {"pause", "resume"}:
        redis = Redis.from_url(s.redis_url, socket_timeout=5)
        try:
            if args.command == "pause":
                await admin.pause(redis, reason=args.reason)
                print("paused: requests park and no mail is sent until `ragent resume`")
            else:
                print("resumed" if await admin.resume(redis) else "was not paused")
        finally:
            await redis.aclose()
        return 0
    if args.command == "revoke":
        revoked = await admin.revoke(args.request_id)
        print({True: "revoked", False: "NOT revoked (drop refused or unreachable; see logs)",
               None: "no download link recorded for this request"}[revoked])
        return 1 if revoked is False else 0
    if args.command == "block":
        h = await admin.block(args.value, reason=args.reason)
        print(f"blocked {args.value} (suppression {h[:12]}...)")
        return 0
    if args.command == "dsar":
        return await _dsar(args)
    raise AssertionError(args.command)


async def _audit(args: argparse.Namespace) -> int:
    from agent import audit

    if args.export:
        written = await audit.export_pending()
        print("\n".join(str(p) for p in written) or "nothing to export")
        return 0
    if args.verify:
        problems = await asyncio.to_thread(audit.verify_chain)
        print("\n".join(problems) or f"chain intact ({audit.export_dir()})")
        return 1 if problems else 0
    if (args.request_id is None) == (args.email is None):
        print("give a request id or --email", file=sys.stderr)
        return 2
    if args.request_id is not None:
        rows = await audit.timeline(request_id=args.request_id)
        target = {"request_id": str(args.request_id)}
    else:
        h = audit.subject_hash(args.email)
        rows = await audit.timeline(subject_h=h)
        target = {"subject_h": h}
    await audit.admin_event("audit_viewed", {**target, "events": len(rows)},
                            request_id=args.request_id, subject_h=target.get("subject_h"))
    print(audit.format_timeline(rows) or "no events")
    return 0


async def _dsar(args: argparse.Namespace) -> int:
    from agent import admin

    if args.action == "export":
        bundle = await admin.dsar_export(args.email)
        text = json.dumps(bundle, indent=2, default=str)
        if args.out:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            print(f"wrote {args.out}: {len(bundle['requests'])} requests, {len(bundle['raw_emails'])} raw emails, "
                  f"{len(bundle['events'])} events")
        else:
            print(text)
        return 0
    counts = await admin.dsar_delete(args.email)
    print(json.dumps(counts, indent=2))
    return 1 if counts.get("links_not_revoked") else 0


if __name__ == "__main__":
    main()
