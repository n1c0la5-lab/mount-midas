"""
hl_ws_collector.py — Hyperliquid WebSocket Trades Collector
Persistent WebSocket connection to HL trades feed for ICP.
Writes trades to spot_trades with source='hl'.

Läuft als eigener Container (mount-midas-hl-ws), getrennt von runner.py.

Side-Mapping HL → is_buyer_maker:
  "A" (ask aggressor = seller taker) → is_buyer_maker = True  (buyer ist passive MM)
  "B" (bid aggressor = buyer taker)  → is_buyer_maker = False (buyer ist Aggressor)
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

log = logging.getLogger(__name__)

HL_WS_URL  = "wss://api.hyperliquid.xyz/ws"
HL_ASSET   = "ICP"
RECONNECT_DELAY = 10  # Sekunden zwischen Reconnect-Versuchen
BATCH_SIZE      = 50
BATCH_TIMEOUT   = 5.0  # Sekunden — auch bei wenig Volumen regelmäßig flushen

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


async def _flush(conn, batch: list) -> None:
    if not batch:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO spot_trades (ts, price, quantity_icp, is_buyer_maker, source)
               VALUES (%s, %s, %s, %s, 'hl')
               ON CONFLICT DO NOTHING""",
            batch,
        )
    await conn.commit()
    log.debug("hl_ws: flushed %d trades", len(batch))


async def _run_once(conn) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            HL_WS_URL,
            heartbeat=30,
            receive_timeout=90,
        ) as ws:
            await ws.send_json({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": HL_ASSET},
            })
            log.info("hl_ws: connected — subscribed to %s trades", HL_ASSET)

            batch: list = []
            last_flush = asyncio.get_event_loop().time()

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("channel") != "trades":
                        continue

                    trades_raw = data.get("data", [])
                    if isinstance(trades_raw, dict):
                        trades_raw = [trades_raw]

                    for t in trades_raw:
                        if t.get("coin") != HL_ASSET:
                            continue
                        ts            = datetime.fromtimestamp(int(t["time"]) / 1000, tz=timezone.utc)
                        price         = float(t["px"])
                        qty           = float(t["sz"])
                        is_buyer_maker = t["side"] == "A"
                        batch.append((ts, price, qty, is_buyer_maker))

                    now = asyncio.get_event_loop().time()
                    if len(batch) >= BATCH_SIZE or (batch and now - last_flush >= BATCH_TIMEOUT):
                        await _flush(conn, batch)
                        batch.clear()
                        last_flush = now

                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    log.warning("hl_ws: WS %s — reconnecting", msg.type.name)
                    break

            if batch:
                await _flush(conn, batch)


async def run_forever() -> None:
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(_DSN) as conn:
                await _run_once(conn)
        except Exception as e:
            log.warning("hl_ws: error: %s — retry in %ds", e, RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(run_forever())
