"""Entry points: `ragent ingest | worker | web | migrate`."""

import argparse
import asyncio


def main() -> None:
    parser = argparse.ArgumentParser(prog="ragent")
    parser.add_argument("command", choices=["ingest", "worker", "web", "migrate"])
    args = parser.parse_args()

    if args.command == "worker":
        from arq.worker import run_worker

        from agent.worker import WorkerSettings

        run_worker(WorkerSettings)
    elif args.command == "web":
        import uvicorn

        from agent.config import get_settings

        uvicorn.run("agent.web.app:app", host="127.0.0.1", port=get_settings().web_port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False)
    else:
        asyncio.run(_async(args.command))


async def _async(command: str) -> None:
    from arq import create_pool
    from arq.connections import RedisSettings

    from agent import db
    from agent.config import get_settings
    from agent.logs import configure_logging

    configure_logging()
    s = get_settings()
    pool = await db.create_pool()
    async with pool.acquire() as conn:
        await db.migrate(conn)
    if command == "migrate":
        return
    from agent.mail.ingest import Ingestor

    redis = await create_pool(RedisSettings.from_dsn(s.redis_url))
    await Ingestor(s, redis).run_forever()


if __name__ == "__main__":
    main()
