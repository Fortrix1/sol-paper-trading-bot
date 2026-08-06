"""
live_listener.py - Free, real-time listener for new pump.fun token
creations and migrations, using PumpPortal's public WebSocket.

WHY THIS REPLACES THE HELIUS APPROACH: PumpPortal is a purpose-built
third-party API specifically for pump.fun (confirmed via their own
official docs + multiple independent tutorials, all consistent). It does
all the on-chain parsing for us and hands back plain JSON - no Anchor
discriminators, no byte-offset decoding, no RPC account lookups needed.
subscribeNewToken and subscribeMigration are BOTH FREE, no API key
required. This is dramatically simpler than the Helius WebSocket +
manual event-decoding approach, and costs nothing.

HONESTY NOTE: the subscribe request format below is verified against
PumpPortal's own official documentation (pumpportal.fun/data-api/real-time)
and matches multiple independent tutorials exactly - high confidence.
The exact full set of response fields isn't 100% confirmed beyond "mint"
being present (confirmed via a working example bot's source code) - so
this logs the raw shape of the first few messages of each type to your
console, same safety net as before, in case any other field names need
adjusting once you see real data.

Run:
    pip install -r requirements.txt
    python live_listener.py
"""

import asyncio
import json
import time
import logging

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WS_URL = "wss://pumpportal.fun/api/data"
DISCOVERIES_FILE = "live_discoveries.jsonl"

_debug_logged = {}
_debug_limit = 3


def _log_discovery(record: dict):
    record["discovered_at"] = time.time()
    with open(DISCOVERIES_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    logger.info(f"DISCOVERY: {record.get('type')} - mint={record.get('mint')}")


def _handle_message(raw_message: str):
    try:
        msg = json.loads(raw_message)
    except json.JSONDecodeError:
        return

    # PumpPortal sends different message shapes for different event types.
    # A migration event vs a new-token event isn't always distinguished by
    # an explicit "type" field in every version of their API, so we check
    # for the presence of characteristic fields defensively.
    mint = msg.get("mint")
    if not mint:
        return  # not a token event (could be a subscription ack, etc.)

    # CONFIRMED via real data: PumpPortal tags every event with a
    # "txType" field. "create" = new token, "migrate" = migration. The
    # previous heuristic (checking for a "pool" field) was wrong - pump.fun
    # messages apparently always include some pool/platform identifier,
    # so it misclassified every single new-token event as a migration.
    tx_type = str(msg.get("txType", "")).lower()
    if tx_type == "create":
        event_type = "new_token"
    elif "migrat" in tx_type:  # covers "migrate" and any variant naming
        event_type = "migration"
    else:
        # Unrecognized txType - log it distinctly so we can see what it
        # actually is rather than guessing again.
        event_type = f"unknown_{tx_type or 'notype'}"

    seen = _debug_logged.get(event_type, 0)
    if seen < _debug_limit:
        logger.info(f"RAW MESSAGE ({event_type}, {seen + 1}/{_debug_limit}): {raw_message[:500]}")
        _debug_logged[event_type] = seen + 1

    _log_discovery({
        "type": event_type,
        "mint": mint,
        "symbol": msg.get("symbol"),
        "name": msg.get("name"),
        "raw": msg,
    })


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
    """Same trick as telegram_bot.py - makes Render treat this as a free
    'web service' instead of a paid 'background worker'. Harmless locally."""
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
