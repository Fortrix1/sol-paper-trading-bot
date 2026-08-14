"""
auto_paper_trader.py - Background engine that auto-buys high-conviction
tokens in PAPER mode. Respects risk_manager limits.

WHEN YOU'RE READY FOR REAL MONEY:
  1. Generate a dedicated bot wallet (don't use your Phantom key)
  2. Fund it with small amounts from Phantom
  3. Replace wallet.buy() calls with real_trader.swap()
  4. That's it - the risk logic stays identical.
"""

import json
import time
import logging

from conviction_engine import ConvictionEngine
from risk_manager import RiskManager
from price_feed import get_sol_usd_price
import config

logger = logging.getLogger(__name__)


class AutoPaperTrader:
    def __init__(self, wallet, risk_mgr: RiskManager = None):
        """
        wallet: PaperWallet instance (or RealTrader in future)
        risk_mgr: RiskManager instance
        """
        self.wallet = wallet
        self.risk = risk_mgr or RiskManager()
        self.engine = ConvictionEngine()
        self.seen_mints = set()
        self.alert_threshold = getattr(config, 'GOLDMINE_ALERT_THRESHOLD', 75)
        self.auto_buy_threshold = getattr(config, 'AUTO_BUY_THRESHOLD', 85)

    def scan_for_goldmines(self, max_lines: int = 100) -> list:
        """
        Scans live_discoveries.jsonl for new tokens, evaluates them,
        returns list of high-conviction results sorted by score.
        """
        import os
        path = "live_discoveries.jsonl"
        if not os.path.exists(path):
            return []

        results = []
        try:
            with open(path) as f:
                lines = f.readlines()
        except IOError:
            return []

        for line in lines[-max_lines:]:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("type") != "new_token":
                continue

            mint = rec.get("mint")
            if not mint or mint in self.seen_mints:
                continue

            eval_result = self.engine.evaluate(mint, live_record=rec)
            self.seen_mints.add(mint)

            if eval_result["score"] >= self.alert_threshold:
                results.append(eval_result)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def attempt_paper_buy(self, user_id: str, eval_result: dict) -> dict:
        """
        Attempts a paper buy for a single user. Returns result dict.
        """
        mint = eval_result["mint"]
        info = eval_result.get("info", {})

        if not info or not info.get("ok"):
            return {"ok": False, "error": "No price data for auto-buy"}

        deployer = (eval_result.get("safety") or {}).get("creator", "unknown")
        sol_price = get_sol_usd_price()
        trade_size_usd = config.DEFAULT_BUY_SIZE_SOL * sol_price if sol_price > 0 else 10.0

        if not self.risk.can_open_position(deployer, trade_size_usd):
            return {"ok": False, "error": "Risk manager blocked this trade"}

        result = self.wallet.buy(
            user_id, mint, info.get("symbol", "?"),
            sol_amount=config.DEFAULT_BUY_SIZE_SOL,
            token_price_usd=info["price_usd"],
            sol_price_usd=sol_price if sol_price > 0 else 150.0,
        )

        if result["ok"]:
            self.risk.add_position(mint, trade_size_usd, deployer)
            logger.info(f"🤖 AUTO-BUY: {mint} | Score: {eval_result['score']} | User: {user_id}")
        else:
            logger.warning(f"🤖 AUTO-BUY FAILED: {mint} | {result['error']} | User: {user_id}")

        return result

    def run_auto_cycle(self, user_id: str, autopilot_enabled: bool = False) -> dict:
        """
        One full scan + auto-buy cycle. Call this from a background job.
        Returns summary dict.
        """
        goldmines = self.scan_for_goldmines()
        bought = []
        alerted = []

        for eval_result in goldmines:
            score = eval_result["score"]

            if score >= self.auto_buy_threshold and autopilot_enabled:
                result = self.attempt_paper_buy(user_id, eval_result)
                if result["ok"]:
                    bought.append(eval_result)
                else:
                    alerted.append(eval_result)
            elif score >= self.alert_threshold:
                alerted.append(eval_result)

        return {
            "scanned": len(self.seen_mints),
            "goldmines_found": len(goldmines),
            "auto_bought": len(bought),
            "alerted": len(alerted),
            "bought_mints": [b["mint"] for b in bought],
            "alerted_mints": [a["mint"] for a in alerted],
        }
