"""
wallet_tracker.py — ICP Wallet Movement Tracker
Fetches outgoing transfers from NP principals, follows chain to hop depth 3.
Schedule: hourly
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import psycopg

log = logging.getLogger(__name__)

LEDGER_URL = "https://ledger-api.internetcomputer.org/v2/accounts/{account_id}/transactions"
MIN_E8S = 10_000_000       # 100 ICP (ignore Neuron voting micro-transactions)
ICP_E8S = 100_000_000      # 1 ICP in e8s
MAX_HOP = 3
LIMIT_INITIAL = 500        # First run: up to 500 blocks per account
LIMIT_INCR = 100           # Subsequent runs: 100 is enough
PAGE_SIZE = 200            # Max per API page

EXCHANGE_CANDIDATES_MIN_NPS = 5    # ≥5 verschiedene NPs müssen an diesen Principal gesendet haben
EXCHANGE_CANDIDATES_MIN_ICP = 5_000.0  # Mindestvolumen in ICP

_WHITELIST_PATH = Path(__file__).parent / "config" / "exchange_whitelist.json"

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


# ── Principal → Account ID ────────────────────────────────────────────────────

def to_account_id(principal_text: str) -> str:
    enc = principal_text.replace("-", "").upper()
    padded = enc + "=" * ((8 - len(enc) % 8) % 8)
    raw = base64.b32decode(padded)
    principal_bytes = raw[4:]  # strip 4-byte CRC32 prefix
    sha = hashlib.sha224(b"\x0aaccount-id" + principal_bytes + bytes(32)).digest()
    crc = struct.pack(">I", zlib.crc32(sha) & 0xFFFFFFFF)
    return (crc + sha).hex()


# ── Ledger API ────────────────────────────────────────────────────────────────

async def fetch_outgoing(
    session: aiohttp.ClientSession,
    account_id: str,
    limit: int,
    min_block_height: Optional[int],
) -> list[dict]:
    """Fetch outgoing send-transfers >= 100 ICP, newest first. Stops at min_block_height."""
    url = LEDGER_URL.format(account_id=account_id)
    results: list[dict] = []
    cursor = None

    for _ in range(20):  # safety cap: max 20 pages
        params: dict = {"limit": min(PAGE_SIZE, limit - len(results))}
        if cursor:
            params["cursor"] = cursor

        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 404:
                    return results
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            log.warning("fetch error %s: %s", account_id[:16], e)
            return results

        blocks = data.get("blocks", [])
        if not blocks:
            break

        for block in blocks:
            bh = int(block.get("block_height", 0))
            # Blocks are descending — stop when we reach already-seen data
            if min_block_height and bh <= min_block_height:
                return results
            if (
                block.get("transfer_type") == "send"
                and block.get("from_account_identifier") == account_id
                and int(block.get("amount", 0)) >= MIN_E8S
            ):
                results.append(block)

        cursor = data.get("next_cursor")
        if not cursor or len(results) >= limit:
            break
        await asyncio.sleep(0.1)  # be nice to the API

    return results


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_max_block(conn, from_id: str) -> Optional[int]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT MAX(block_height) FROM wallet_movements WHERE from_principal = %s",
            (from_id,),
        )
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def upsert_batch(conn, movements: list[dict]) -> int:
    if not movements:
        return 0
    async with conn.cursor() as cur:
        for m in movements:
            await cur.execute(
                """
                INSERT INTO wallet_movements
                    (from_principal, to_principal, amount_icp, tx_hash, block_height, ts, hop_depth)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tx_hash) DO NOTHING
                """,
                (m["from"], m["to"], m["amount_icp"],
                 m["tx_hash"], m["block_height"], m["ts"], m["hop_depth"]),
            )
    await conn.commit()
    return len(movements)


async def get_untracked_hop_destinations(conn, from_hop: int) -> list[str]:
    """Return to_principals at from_hop that haven't been tracked as from_principal at from_hop+1."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT DISTINCT wm.to_principal
            FROM wallet_movements wm
            WHERE wm.hop_depth = %s
              AND NOT EXISTS (
                  SELECT 1 FROM wallet_movements wm2
                  WHERE wm2.from_principal = wm.to_principal
                    AND wm2.hop_depth = %s
              )
            """,
            (from_hop, from_hop + 1),
        )
        return [r[0] for r in await cur.fetchall()]


async def refresh_clusters(conn) -> None:
    async with conn.cursor() as cur:
        # Upsert cluster stats from Hop 0 movements
        await cur.execute("""
            INSERT INTO destination_clusters
                (to_principal, np_count, total_icp, first_seen_at, updated_at)
            SELECT
                wm.to_principal,
                COUNT(DISTINCT np.principal),
                SUM(wm.amount_icp),
                MIN(wm.ts),
                NOW()
            FROM wallet_movements wm
            JOIN np_providers np ON np.principal = wm.from_principal
            WHERE wm.hop_depth = 0
            GROUP BY wm.to_principal
            ON CONFLICT (to_principal) DO UPDATE SET
                np_count   = EXCLUDED.np_count,
                total_icp  = EXCLUDED.total_icp,
                updated_at = NOW()
        """)
        # Auto-flag trading desks (≥3 NPs converging)
        await cur.execute("""
            UPDATE destination_clusters
            SET is_trading_desk = TRUE
            WHERE np_count >= 3 AND is_trading_desk = FALSE
        """)
    await conn.commit()
    log.info("clusters refreshed")


# ── Exchange classification ───────────────────────────────────────────────────

async def classify_destination(conn, principal: str) -> Optional[dict]:
    """Gibt Label-Eintrag aus np_wallet_labels zurück, oder None wenn unbekannt."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, label, exchange, confirmed FROM np_wallet_labels WHERE principal = %s",
            (principal,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"principal": row[0], "label": row[1], "exchange": row[2], "confirmed": row[3]}


async def propose_exchange_candidates(conn) -> list[dict]:
    """
    Heuristik: Principals die von ≥5 verschiedenen NPs Hop-0-Transfers empfangen haben
    und Gesamtvolumen >5.000 ICP — aber noch kein Eintrag in np_wallet_labels.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT wm.to_principal,
                   COUNT(DISTINCT np.principal) AS np_count,
                   SUM(wm.amount_icp)           AS total_icp
            FROM wallet_movements wm
            JOIN np_providers np ON np.principal = wm.from_principal
            WHERE wm.hop_depth = 0
              AND NOT EXISTS (
                  SELECT 1 FROM np_wallet_labels wl WHERE wl.account_id = wm.to_principal
              )
            GROUP BY wm.to_principal
            HAVING COUNT(DISTINCT np.principal) >= %s
               AND SUM(wm.amount_icp) >= %s
            ORDER BY np_count DESC, total_icp DESC
            """,
            (EXCHANGE_CANDIDATES_MIN_NPS, EXCHANGE_CANDIDATES_MIN_ICP),
        )
        rows = await cur.fetchall()
    return [{"principal": r[0], "np_count": r[1], "total_icp": round(float(r[2]), 2)} for r in rows]


async def seed_wallet_labels(conn) -> None:
    """Seed exchange_whitelist.json in np_wallet_labels.
    Unterstützt zwei Formate:
      - {"principal": "...", ...}  → account_id wird berechnet
      - {"account_id": "...", ...} → account_id direkt verwenden, principal = account_id
    """
    if not _WHITELIST_PATH.exists():
        log.warning("exchange_whitelist.json nicht gefunden: %s", _WHITELIST_PATH)
        return
    data = json.loads(_WHITELIST_PATH.read_text())
    entries = data.get("entries", [])
    async with conn.cursor() as cur:
        for e in entries:
            if "account_id" in e:
                # Direkte Account ID (hex, 64 Zeichen) — kein Principal bekannt
                acc_id = e["account_id"]
                principal = acc_id  # PK muss gefüllt sein, verwende account_id als Fallback
            else:
                principal = e["principal"]
                try:
                    acc_id = to_account_id(principal)
                except Exception:
                    acc_id = None
            await cur.execute(
                """
                INSERT INTO np_wallet_labels (principal, label, exchange, confirmed, account_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (principal) DO UPDATE SET
                    label      = EXCLUDED.label,
                    exchange   = EXCLUDED.exchange,
                    confirmed  = EXCLUDED.confirmed,
                    account_id = EXCLUDED.account_id
                """,
                (principal, e["label"], e.get("exchange"), e.get("confirmed", False), acc_id),
            )
    await conn.commit()
    log.info("wallet_labels: seed abgeschlossen (%d Einträge)", len(entries))


# ── Tracking logic ────────────────────────────────────────────────────────────

def _to_movement(block: dict, from_id: str, hop_depth: int) -> dict:
    return {
        "from": from_id,
        "to": block["to_account_identifier"],
        "amount_icp": int(block["amount"]) / ICP_E8S,
        "tx_hash": block["transaction_hash"],
        "block_height": int(block["block_height"]),
        "ts": datetime.fromtimestamp(block["created_at"], tz=timezone.utc),
        "hop_depth": hop_depth,
    }


async def track_account(
    session: aiohttp.ClientSession,
    conn,
    account_id: str,
    from_label: str,
    hop_depth: int,
) -> int:
    last_bh = await get_max_block(conn, from_label)
    limit = LIMIT_INCR if last_bh else LIMIT_INITIAL
    blocks = await fetch_outgoing(session, account_id, limit=limit, min_block_height=last_bh)
    movements = [_to_movement(b, from_label, hop_depth) for b in blocks]
    return await upsert_batch(conn, movements)


async def run() -> None:
    log.info("wallet_tracker: start")
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:

        await seed_wallet_labels(conn)

        async with aiohttp.ClientSession() as session:

            # ── Hop 0: All NP principals ──────────────────────────────────────
            async with conn.cursor() as cur:
                await cur.execute("SELECT principal FROM np_providers")
                principals = [r[0] for r in await cur.fetchall()]

            log.info("wallet_tracker: hop0 — %d NP principals", len(principals))
            n0 = 0
            for principal in principals:
                try:
                    acc_id = to_account_id(principal)
                except Exception as e:
                    log.warning("principal decode failed %s: %s", principal[:20], e)
                    continue
                n = await track_account(session, conn, acc_id, from_label=principal, hop_depth=0)
                if n:
                    log.info("hop0: %s → %d movements", principal[:24], n)
                n0 += n
            log.info("wallet_tracker: hop0 done — %d new", n0)

            # ── Hop 1–3: Follow the chain ─────────────────────────────────────
            for hop in range(3):
                destinations = await get_untracked_hop_destinations(conn, from_hop=hop)
                log.info("wallet_tracker: hop%d — %d destinations", hop + 1, len(destinations))
                total = 0
                for dest_account_id in destinations:
                    n = await track_account(
                        session, conn,
                        account_id=dest_account_id,
                        from_label=dest_account_id,
                        hop_depth=hop + 1,
                    )
                    if n:
                        log.info("hop%d: %s → %d movements", hop + 1, dest_account_id[:16], n)
                    total += n
                log.info("wallet_tracker: hop%d done — %d new", hop + 1, total)

        await refresh_clusters(conn)

        candidates = await propose_exchange_candidates(conn)
        if candidates:
            log.info("wallet_tracker: %d neue Exchange-Kandidaten erkannt", len(candidates))
            for c in candidates[:10]:
                log.info("  kandidat: %s | NPs: %d | ICP: %.0f", c["principal"][:32], c["np_count"], c["total_icp"])

    log.info("wallet_tracker: done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())
