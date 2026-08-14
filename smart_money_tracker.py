"""
smart_money_tracker.py - Builds a local "smart money" database from your
own profitable paper trades. No paid APIs needed.

HOW IT WORKS:
  1. Whenever you close a winning trade (pnl > 0), the bot checks if the
     token's dev wallet is already in the database.
  2. If that dev has launched multiple winners, they get a "smart money" tag.
  3. When a NEW token launches from a known smart-money dev, the bot flags it.
  4. You can also MANUALLY add wallets you found on Solscan/GMGN.

LIMITATIONS (be honest):
  - Only tracks DEV wallets, not individual buyer wallets (PumpPortal doesn't
    expose the full buyer list in the free WebSocket).
  - True "bundle detection" requires parsing every instruction in the creation
    tx block — that's a paid-tier feature (Geyser gRPC or Bitquery).
  - This is a PROXY: devs who consistently launch winners are correlated with
    bundles/insider activity, even if we can't see the bundle directly.
"""

import json
import os
import time
from typing import Dict, List

SMART_MONEY_FILE = "smart_money.json"


class SmartMoneyTracker:
    """
    Tracks dev wallets and their win/loss record.
    Also supports manual wallet tagging.
    """

    def __init__(self, filepath: str = SMART_MONEY_FILE):
        self.filepath = filepath
        self.wallets: Dict[str, dict] = {}  # wallet -> {wins, losses, tags, first_seen, last_seen}
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self.wallets = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.wallets = {}

    def _save(self):
        with open(self.filepath, "w") as f:
            json.dump(self.wallets, f, indent=2)

    def record_trade(self, dev_wallet: str, symbol: str, pnl_sol: float, mint: str):
        """Call this after every closed trade."""
        if not dev_wallet:
            return

        if dev_wallet not in self.wallets:
            self.wallets[dev_wallet] = {
                "wins": 0,
                "losses": 0,
                "tokens_launched": [],
                "first_seen": time.time(),
                "last_seen": time.time(),
                "tags": [],
                "total_pnl_sol": 0.0,
            }

        w = self.wallets[dev_wallet]
        w["last_seen"] = time.time()
        w["tokens_launched"].append({"mint": mint, "symbol": symbol, "pnl": pnl_sol, "time": time.time()})
        w["total_pnl_sol"] += pnl_sol

        if pnl_sol > 0:
            w["wins"] += 1
        else:
            w["losses"] += 1

        # Auto-tag based on performance
        total = w["wins"] + w["losses"]
        if total >= 3:
            win_rate = w["wins"] / total
            if win_rate >= 0.7 and w["wins"] >= 3:
                if "smart_money" not in w["tags"]:
                    w["tags"].append("smart_money")
            if win_rate <= 0.3 and total >= 3:
                if "serial_rugger" not in w["tags"]:
                    w["tags"].append("serial_rugger")

        self._save()

    def add_manual_wallet(self, wallet: str, tag: str = "watched", note: str = ""):
        """Manually add a wallet you found on Solscan/GMGN."""
        if wallet not in self.wallets:
            self.wallets[wallet] = {
                "wins": 0, "losses": 0, "tokens_launched": [],
                "first_seen": time.time(), "last_seen": time.time(),
                "tags": [tag], "note": note, "total_pnl_sol": 0.0,
            }
        else:
            if tag not in self.wallets[wallet]["tags"]:
                self.wallets[wallet]["tags"].append(tag)
            if note:
                self.wallets[wallet]["note"] = note
        self._save()

    def check_wallet(self, wallet: str) -> dict:
        """Returns smart money info for a wallet, or None."""
        return self.wallets.get(wallet)

    def get_summary(self, wallet: str) -> str:
        w = self.wallets.get(wallet)
        if not w:
            return ""
        total = w["wins"] + w["losses"]
        wr = (w["wins"] / total * 100) if total > 0 else 0
        tags = ", ".join(w["tags"]) if w["tags"] else "no tags"
        return (
            f"👤 *Dev Track Record*\n"
            f"Wins: {w['wins']} | Losses: {w['losses']} | WR: {wr:.0f}%\n"
            f"Total P&L: {w['total_pnl_sol']:+.4f} SOL\n"
            f"Tags: `{tags}`"
        )

    def is_smart_money(self, wallet: str) -> bool:
        w = self.wallets.get(wallet)
        if not w:
            return False
        return "smart_money" in w.get("tags", [])

    def is_serial_rugger(self, wallet: str) -> bool:
        w = self.wallets.get(wallet)
        if not w:
            return False
        return "serial_rugger" in w.get("tags", [])
