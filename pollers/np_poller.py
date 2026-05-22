import asyncio
import logging
import os

import aiohttp
import psycopg

log = logging.getLogger(__name__)

IC_API_URL = "https://ic-api.internetcomputer.org/api/v3/node-providers?limit=500"

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


async def _fetch() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(IC_API_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return data.get("node_providers", [])


async def _upsert(providers: list[dict]) -> int:
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with conn.cursor() as cur:
            for p in providers:
                await cur.execute(
                    """
                    INSERT INTO np_providers (principal, name, node_count, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (principal) DO UPDATE SET
                        name       = EXCLUDED.name,
                        node_count = EXCLUDED.node_count,
                        updated_at = NOW()
                    """,
                    (
                        p["principal_id"],
                        p.get("display_name"),
                        p.get("total_rewardable_nodes", 0),
                    ),
                )
        await conn.commit()
    return len(providers)


async def run() -> None:
    log.info("np_poller: start")
    providers = await _fetch()
    log.info("np_poller: fetched %d providers", len(providers))
    count = await _upsert(providers)
    log.info("np_poller: upserted %d rows", count)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())
