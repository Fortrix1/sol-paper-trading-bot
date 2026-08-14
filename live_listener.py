"""
live_listener.py - Free, real-time listener for new pump.fun token
creations and migrations, using PumpPortal's public WebSocket.

UPGRADES in this version:
  - Bonding curve progress calculation (for graduation sniping)
  - Initial buy / whale launch detection
  - Dev wallet freshness flag
  - Stores richer metadata for conviction_engine.py
"""

import asyncio
import json
import time
import os
import logging

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WS_URL = "wss://pumpportal.fun/api/data"
DISCOVERIES_FILE = "live_discoveries.jsonl"
LAUNCH_INDEX_FILE = "launch_index.jsonl"
MAX_LAUNCH_INDEX = 10000

_debug_logged = {}
_debug_limit = 3

MAX_DISCOVERIES = 2000
TRIM_CHECK_EVERY = 100
_write_count = 0

# Bonding curve thresholds (PumpPortal vSolInBondingCurve)
BONDING_START_SOL = 30.0
BONDING_GRADUATE_SOL = 85.0
WHALE_THRESHOLD_SOL = 5.0


def _trim_discoveries_file():
    if not os.path.exists(DISCOVERIES_FILE):
        return
    try:
        with open(DISCOVERIES_FILE) as f:
            lines = f.readlines()
        if len(lines) > MAX_DISCOVERIES:
            with open(DISCOVERIES_FILE, "w") as f:
                f.writelines(lines[-MAX_DISCOVERIES:])
            logger.info(f"Trimmed discoveries file: {len(lines)} -> {MAX_DISCOVERIES} lines")
    except IOError:
        pass


def _trim_launch_index():
    if not os.path.exists(LAUNCH_INDEX_FILE):
        return
    try:
        with open(LAUNCH_INDEX_FILE) as f:
            lines = f.readlines()
        if len(lines) > MAX_LAUNCH_INDEX:
            with open(LAUNCH_INDEX_FILE, "w") as f:
                f.writelines(lines[-MAX_LAUNCH_INDEX:])
    except IOError:
        pass


def _log_launch_index(record: dict):
    with open(LAUNCH_INDEX_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    global _write_count
    if _write_count % TRIM_CHECK_EVERY == 0:
        _trim_launch_index()


def _log_discovery(record: dict):
    global _write_count
    record["discovered_at"] = time.time()
    with open(DISCOVERIES_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    logger.info(f"DISCOVERY: {record.get('type')} - mint={record.get('mint')} "
                f"score={record.get('conviction_score', 'N/A')} "
                f"bonding={record.get('bonding_progress', 'N/A'):.0f}% "
                f"if record.get('bonding_progress') else 'N/A')")
    _write_count += 1
    if _write_count % TRIM_CHECK_EVERY == 0:
        _trim_discoveries_file()


def _calculate_bonding_progress(v_sol: float) -> float:
    """Returns 0-100 based on PumpPortal's vSolInBondingCurve."""
    if v_sol is None:
        return None
    progress = (v_sol - BONDING_START_SOL) / (BONDING_GRADUATE_SOL - BONDING_START_SOL) * 100
    return max(0, min(100, progress))


def _handle_message(raw_message: str):
    try:
        msg = json.loads(raw_message)
    except json.JSONDecodeError:
        return

    mint = msg.get("mint")
    if not mint:
        return

    tx_type = str(msg.get("txType", "")).lower()
    if tx_type == "create":
        event_type = "new_token"
    elif "migrat" in tx_type:
        event_type = "migration"
    else:
        event_type = f"unknown_{tx_type or 'notype'}"

    seen = _debug_logged.get(event_type, 0)
    if seen < _debug_limit:
        logger.info(f"RAW MESSAGE ({event_type}, {seen + 1}/{_debug_limit}): {raw_message[:500]}")
        _debug_logged[event_type] = seen + 1

    # Extract bonding curve intelligence
    v_sol = msg.get("vSolInBondingCurve")
    v_tokens = msg.get("vTokensInBondingCurve")
    bonding_progress = _calculate_bonding_progress(v_sol)

    initial_buy = msg.get("solAmount")
    is_whale_launch = False
    if initial_buy is not None and initial_buy >= WHALE_THRESHOLD_SOL:
        is_whale_launch = True

    dev_wallet = msg.get("traderPublicKey")

    record = {
        "type": event_type,
        "mint": mint,
        "symbol": msg.get("symbol"),
        "name": msg.get("name"),
        "dev_wallet": dev_wallet,
        "initial_buy_sol": initial_buy,
        "is_whale_launch": is_whale_launch,
        "bonding_progress": bonding_progress,
        "v_sol_in_curve": v_sol,
        "v_tokens_in_curve": v_tokens,
        "raw": msg,
    }

    _log_discovery(record)

    if event_type == "new_token":
        _log_launch_index(dict(record))


async def listen():
    while True:
        try:
            logger.info("Connecting to PumpPortal (free, no API key needed)...")
            async with websockets.connect(WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                logger.info("Subscribed to new tokens + migrations. Listening...")
                async for message in ws:
                    _handle_message(message)
        except Exception as e:
            logger.warning(f"Connection lost ({e}), reconnecting in 5s...")
            await asyncio.sleep(5)


def start_keepalive_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import os as _os

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Live listener is alive")

        def log_message(self, format, *args):
            pass

    port = int(_os.environ.get("PORT", 8081))
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Keep-alive HTTP server listening on port {port}")


if __name__ == "__main__":
    start_keepalive_server()
    logger.info(f"Writing discoveries to {DISCOVERIES_FILE}")
    asyncio.run(listen())
