import datetime
import json
import os
from typing import List, Dict

class ShadowTrader:
    """
    Simulates trading activity and logs decision features for future ML training.
    Uses JSON-lines for robust, structured logging and pairs BUY/SELL legs for P&L.
    """
    def __init__(self, log_file_path: str = "shadow_trades.jsonl"):
        self.log_file_path = log_file_path

    def record_potential_trade(
        self, 
        timestamp: datetime.datetime, 
        token_mint: str, 
        action: str, # "BUY" or "SELL"
        proposed_amount: float, 
        proposed_price: float, 
        decision_features: Dict, 
        outcome: str = "PENDING", 
        actual_price: float = None
    ):
        """Records a hypothetical trade event to the JSON-lines log."""
        trade_record = {
            "timestamp": timestamp.isoformat(),
            "token_mint": token_mint,
            "action": action,
            "proposed_amount": proposed_amount,
            "proposed_price": proposed_price,
            "decision_features": decision_features,
            "outcome": outcome,
            "actual_price": actual_price
        }
        self._log_trade(trade_record)

    def _log_trade(self, trade_record: Dict):
        with open(self.log_file_path, "a") as f:
            f.write(json.dumps(trade_record) + "\n")

    def load_shadow_trades(self) -> List[Dict]:
        """Reads all recorded trades from the log file."""
        trades = []
        if not os.path.exists(self.log_file_path):
            return trades
            
        with open(self.log_file_path, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return trades

    def analyze_shadow_trades(self):
        """
        Calculates hypothetical performance metrics by pairing BUY and SELL legs.
        Matches SELL events to the earliest preceding BUY for the same mint (FIFO).
        """
        all_events = self.load_shadow_trades()
        if not all_events:
            print("No shadow trades to analyze.")
            return

        total_hypothetical_pnl = 0.0
        wins = 0
        losses = 0
        
        # Track open positions for pairing: token_mint -> list of (entry_price, amount)
        open_buys: Dict[str, List[tuple]] = {}

        for event in all_events:
            mint = event["token_mint"]
            action = event["action"]
            price = event["proposed_price"]
            amount = event["proposed_amount"]

            if action == "BUY":
                if mint not in open_buys:
                    open_buys[mint] = []
                open_buys[mint].append((price, amount))
            
            elif action == "SELL":
                if mint in open_buys and open_buys[mint]:
                    # Pair with the oldest open BUY (FIFO)
                    entry_price, entry_amount = open_buys[mint].pop(0)

                    # Use the SMALLER of the two sizes as the closed amount.
                    # This is what actually changed hands, regardless of which
                    # leg's --amount the CLI call happened to specify.
                    closed_amount = min(entry_amount, amount)

                    # Calculate P&L on the closed portion only: (exit - entry) * closed_amount
                    pnl = (price - entry_price) * closed_amount
                    total_hypothetical_pnl += pnl

                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1

                    # Push back whatever wasn't closed so it stays trackable.
                    if entry_amount > amount:
                        # BUY was bigger than this SELL: remainder is still open.
                        open_buys[mint].insert(0, (entry_price, entry_amount - amount))
                    elif amount > entry_amount:
                        # SELL was bigger than this BUY: treat the excess as an
                        # unmatched sell (no earlier BUY exists for it), so we
                        # don't silently invent P&L for size we have no entry for.
                        pass
                else:
                    # No open BUY to match against - can't compute P&L for this leg.
                    pass
        
        total_closed = wins + losses
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
        total_open = sum(len(buys) for buys in open_buys.values())

        print(f"--- Shadow Trading Analysis ---")
        print(f"Total Hypothetical P&L: {total_hypothetical_pnl:.4f}")
        print(f"Total Trades (Closed Pairs): {total_closed}")
        print(f"Wins: {wins}, Losses: {losses}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Open Positions: {total_open}")
        print(f"-------------------------------")
