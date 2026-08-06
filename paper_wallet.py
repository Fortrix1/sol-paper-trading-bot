"""
paper_wallet.py - Fake SOL wallet with capped daily top-ups and open-position
tracking. Persisted to a JSON file so state survives bot restarts.

One wallet per Telegram user_id, so this is safe to use even if more than
one person ends up talking to the bot.
"""

import json
import os
import time
import datetime
from typing import Dict, Optional


class PaperWallet:
    def __init__(self, state_file: str, starting_balance: float, daily_topup: float, cap: float):
        self.state_file = state_file
        self.starting_balance = starting_balance
        self.daily_topup = daily_topup
        self.cap = cap
        self.users: Dict[str, dict] = {}
        self._load()

    # ---------- persistence ----------

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    self.users = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.users = {}

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump(self.users, f, indent=2)

    # ---------- user lifecycle ----------

    def _ensure_user(self, user_id: str):
        if user_id not in self.users:
            self.users[user_id] = {
                "balance_sol": self.starting_balance,
                "last_topup_date": datetime.date.today().isoformat(),
                "open_positions": {},   # mint -> {entry_price_usd, sol_spent, tokens_held, symbol, timestamp}
                "closed_trades": [],
            }
            self._save()

    def apply_daily_topup(self, user_id: str):
        """Adds one day's top-up if a new day has started, capped at self.cap."""
        self._ensure_user(user_id)
        user = self.users[user_id]
        today = datetime.date.today().isoformat()
        if user["last_topup_date"] != today:
            # Apply one topup per elapsed day, still capped - avoids a huge
            # jump if the bot was offline for a while.
            days_missed = (
                datetime.date.today() - datetime.date.fromisoformat(user["last_topup_date"])
            ).days
            days_missed = max(1, days_missed)
            for _ in range(days_missed):
                if user["balance_sol"] < self.cap:
                    user["balance_sol"] = min(self.cap, user["balance_sol"] + self.daily_topup)
            user["last_topup_date"] = today
            self._save()

    def get_balance(self, user_id: str) -> float:
        self._ensure_user(user_id)
        return self.users[user_id]["balance_sol"]

    def get_open_positions(self, user_id: str) -> dict:
        self._ensure_user(user_id)
        return self.users[user_id]["open_positions"]

    # ---------- trading ----------

    def buy(self, user_id: str, mint: str, symbol: str, sol_amount: float,
            token_price_usd: float, sol_price_usd: float) -> dict:
        """Opens a fake position. Returns {ok, error} or {ok: True, tokens_bought}."""
        self._ensure_user(user_id)
        user = self.users[user_id]

        if mint in user["open_positions"]:
            return {"ok": False, "error": "You already have an open position in this token. Sell it first."}

        if sol_amount > user["balance_sol"]:
            return {"ok": False, "error": f"Not enough fake SOL. Balance: {user['balance_sol']:.3f} SOL"}

        if sol_price_usd <= 0 or token_price_usd <= 0:
            return {"ok": False, "error": "Could not price this trade (missing SOL or token price)."}

        usd_spent = sol_amount * sol_price_usd
        tokens_bought = usd_spent / token_price_usd

        user["balance_sol"] -= sol_amount
        user["open_positions"][mint] = {
            "symbol": symbol,
            "entry_price_usd": token_price_usd,
            "sol_spent": sol_amount,
            "tokens_held": tokens_bought,
            "timestamp": time.time(),
            "price_history": [[time.time(), token_price_usd]],  # [ts, price] snapshots for /activity
            "peak_price_usd": token_price_usd,
            "peak_timestamp": time.time(),
        }
        self._save()
        return {"ok": True, "tokens_bought": tokens_bought}

    def record_price_snapshot(self, user_id: str, mint: str, price_usd: float):
        """Called periodically (from the background job) to build price history for /activity."""
        self._ensure_user(user_id)
        pos = self.users[user_id]["open_positions"].get(mint)
        if not pos:
            return
        pos["price_history"].append([time.time(), price_usd])
        # Cap history length so the file doesn't grow forever on long-held positions
        if len(pos["price_history"]) > 2000:
            pos["price_history"] = pos["price_history"][-2000:]
        if price_usd > pos["peak_price_usd"]:
            pos["peak_price_usd"] = price_usd
            pos["peak_timestamp"] = time.time()
        self._save()

    def sell(self, user_id: str, mint: str, current_price_usd: float, sol_price_usd: float) -> dict:
        """Closes a fake position. Returns {ok, error} or trade result dict."""
        self._ensure_user(user_id)
        user = self.users[user_id]

        if mint not in user["open_positions"]:
            return {"ok": False, "error": "No open position in this token."}

        if sol_price_usd <= 0:
            return {"ok": False, "error": "Could not price SOL right now, try again shortly."}

        pos = user["open_positions"].pop(mint)
        current_value_usd = pos["tokens_held"] * current_price_usd
        sol_returned = current_value_usd / sol_price_usd
        pnl_sol = sol_returned - pos["sol_spent"]
        pnl_percent = (pnl_sol / pos["sol_spent"] * 100) if pos["sol_spent"] > 0 else 0.0

        user["balance_sol"] += sol_returned
        user["closed_trades"].append({
            "mint": mint,
            "symbol": pos["symbol"],
            "entry_price_usd": pos["entry_price_usd"],
            "exit_price_usd": current_price_usd,
            "sol_spent": pos["sol_spent"],
            "sol_returned": sol_returned,
            "pnl_sol": pnl_sol,
            "pnl_percent": pnl_percent,
            "opened_at": pos["timestamp"],
            "closed_at": time.time(),
            "price_history": pos.get("price_history", []),
            "peak_price_usd": pos.get("peak_price_usd", pos["entry_price_usd"]),
            "peak_timestamp": pos.get("peak_timestamp", pos["timestamp"]),
        })
        self._save()

        return {
            "ok": True,
            "symbol": pos["symbol"],
            "entry_price_usd": pos["entry_price_usd"],
            "exit_price_usd": current_price_usd,
            "sol_returned": sol_returned,
            "pnl_sol": pnl_sol,
            "pnl_percent": pnl_percent,
        }

    def get_stats(self, user_id: str) -> dict:
        self._ensure_user(user_id)
        closed = self.users[user_id]["closed_trades"]
        wins = sum(1 for t in closed if t["pnl_sol"] > 0)
        losses = sum(1 for t in closed if t["pnl_sol"] <= 0)
        total_pnl = sum(t["pnl_sol"] for t in closed)
        return {
            "total_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(closed) * 100) if closed else 0.0,
            "total_pnl_sol": total_pnl,
        }

    def get_activity_records(self, user_id: str) -> list:
        """
        Combines open + closed positions into one list of records for /activity,
        each annotated with peak info and 'what if you'd sold at the peak'.
        """
        self._ensure_user(user_id)
        user = self.users[user_id]
        records = []

        for mint, pos in user["open_positions"].items():
            records.append(self._annotate_record(mint, pos, is_open=True))

        for trade in user["closed_trades"]:
            records.append(self._annotate_record(trade["mint"], trade, is_open=False))

        records.sort(key=lambda r: r.get("opened_at", 0), reverse=True)
        return records

    def _annotate_record(self, mint: str, pos: dict, is_open: bool) -> dict:
        entry = pos["entry_price_usd"]
        peak = pos.get("peak_price_usd", entry)
        current_or_exit = pos.get("exit_price_usd") if not is_open else None
        peak_gain_pct = ((peak - entry) / entry * 100) if entry > 0 else 0.0
        return {
            "mint": mint,
            "symbol": pos["symbol"],
            "is_open": is_open,
            "entry_price_usd": entry,
            "peak_price_usd": peak,
            "peak_timestamp": pos.get("peak_timestamp"),
            "peak_gain_pct": peak_gain_pct,
            "exit_price_usd": current_or_exit,
            "pnl_sol": pos.get("pnl_sol"),
            "pnl_percent": pos.get("pnl_percent"),
            "opened_at": pos.get("timestamp") or pos.get("opened_at"),
            "closed_at": pos.get("closed_at"),
            "price_history": pos.get("price_history", []),
        }
