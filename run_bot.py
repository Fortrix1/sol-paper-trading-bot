"""
run_bot.py - Continuous shadow-trading loop that ties together
honeypot_check.py, risk_manager.py, and shadow_trader.py.

This is the missing piece: the other four files are one-shot CLI
commands. This script is what actually "starts the bot" and keeps
running, printing live status to your terminal and writing every
decision to shadow_trades.jsonl (which you can tail in another window).

IMPORTANT - what this does NOT do yet:
  - It does not execute real trades. It is shadow/paper-trading only,
    per the report's mandatory pre-live-trading phase.
  - It does not discover brand-new pools the instant they launch.
    True sniping needs Geyser gRPC streaming (Level 3 in the report).
    This script polls DexScreener's public API for the tokens YOU give
    it, on an interval - good for monitoring/validation, not racing.

Usage:
    python run_bot.py --watch <mint1,mint2,...> --interval 30

    # or watch a file with one mint address per line:
    python run_bot.py --watchlist mints.txt --interval 30
"""

import argparse
import datetime
import time
import sys

import requests

from risk_manager import RiskManager
from shadow_trader import ShadowTrader
from honeypot_check import HoneypotChecker

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"


def fetch_price_data(mint: str) -> dict:
    """Pull current price/liquidity for a mint from DexScreener's public API."""
    try:
        resp = requests.get(DEXSCREENER_TOKENS_URL.format(mint), timeout=8)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return {"ok": False, "error": "No pairs found for this mint"}
        # Use the highest-liquidity pair as the reference price
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        return {
            "ok": True,
            "price_usd": float(best.get("priceUsd", 0) or 0),
            "liquidity_usd": float((best.get("liquidity") or {}).get("usd", 0) or 0),
            "pair_url": best.get("url"),
            "deployer": None,  # DexScreener doesn't expose this; wire in Helius for real deployer data
        }
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}


def evaluate_and_log(mint: str, risk_mgr: RiskManager, honeypot: HoneypotChecker,
                      shadow: ShadowTrader, position_size: float):
    """One full pass: price -> honeypot gate -> risk gate -> log -> print."""
    ts = datetime.datetime.now()
    price_info = fetch_price_data(mint)

    if not price_info["ok"]:
        print(f"[{ts:%H:%M:%S}] {mint[:8]}...  PRICE DATA FAILED: {price_info['error']}")
        return

    hp_result = honeypot.check_token_safety(mint)
    deployer = price_info.get("deployer") or "unknown_deployer"
    risk_ok = risk_mgr.can_open_position(deployer, position_size)

    decision = "BUY" if (hp_result["is_safe"] and risk_ok) else "SKIP"

    status_bits = []
    status_bits.append("safe" if hp_result["is_safe"] else f"UNSAFE({hp_result['reason']})")
    status_bits.append("risk-ok" if risk_ok else "risk-BLOCKED")

    print(f"[{ts:%H:%M:%S}] {mint[:10]}...  "
          f"price=${price_info['price_usd']:.8f}  "
          f"liq=${price_info['liquidity_usd']:,.0f}  "
          f"{' | '.join(status_bits)}  -> {decision}")

    # Always log the decision - taken or skipped - for the feedback loop.
    shadow.record_potential_trade(
        timestamp=ts,
        token_mint=mint,
        action="BUY" if decision == "BUY" else "SKIP",
        proposed_amount=position_size,
        proposed_price=price_info["price_usd"],
        decision_features={
            "liquidity_usd": price_info["liquidity_usd"],
            "honeypot_safe": hp_result["is_safe"],
            "honeypot_reason": hp_result["reason"],
            "risk_ok": risk_ok,
        },
        outcome="PENDING",
    )


def load_watchlist(args) -> list:
    mints = []
    if args.watch:
        mints.extend([m.strip() for m in args.watch.split(",") if m.strip()])
    if args.watchlist:
        with open(args.watchlist) as f:
            mints.extend([line.strip() for line in f if line.strip() and not line.startswith("#")])
    return mints


def main():
    parser = argparse.ArgumentParser(description="Solana shadow-trading bot runner")
    parser.add_argument("--watch", help="Comma-separated list of token mint addresses to monitor")
    parser.add_argument("--watchlist", help="Path to a text file, one mint address per line")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between polling rounds")
    parser.add_argument("--position-size", type=float, default=20.0, help="Simulated USD size per position, for risk gating")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting shadow capital (only applied once)")
    args = parser.parse_args()

    mints = load_watchlist(args)
    if not mints:
        print("No tokens to watch. Pass --watch <mint1,mint2> or --watchlist file.txt")
        sys.exit(1)

    risk_mgr = RiskManager()
    risk_mgr.set_initial_capital(args.capital)
    shadow = ShadowTrader()
    honeypot = HoneypotChecker(api_endpoint=RUGCHECK_URL.format(""), sell_tax_threshold=0.1)

    print(f"Watching {len(mints)} token(s), polling every {args.interval}s. Ctrl+C to stop.")
    print(f"Live log: tail -f shadow_trades.jsonl   |   Risk state: risk_state.json\n")

    try:
        while True:
            for mint in mints:
                # Build a per-mint honeypot checker since the URL is mint-specific
                hp = HoneypotChecker(api_endpoint=RUGCHECK_URL.format(mint), sell_tax_threshold=0.1)
                evaluate_and_log(mint, risk_mgr, hp, shadow, args.position_size)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped. Run `python bot_cli.py analyze` anytime to see shadow performance so far.")


if __name__ == "__main__":
    main()
