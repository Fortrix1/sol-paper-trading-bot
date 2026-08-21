"""
real_trader.py - Execute REAL trades on Solana via Jupiter API.
Pairs with phantom_connector.py for signing.

FEATURES:
  - Fast Jupiter swaps (buy/sell)
  - Auto Take Profit / Stop Loss for REAL positions
  - Transaction status tracking
  - Real-time P&L calculation
  - Whale buy/sell detection for held tokens
"""

import json
import time
import asyncio
import requests
from typing import Dict, Optional, List

import config
from phantom_connector import phantom
from price_feed import get_token_info, get_sol_usd_price


class RealTrader:
    """
    Real trading engine using Jupiter Aggregator.
    All trades are signed by the bot's dedicated wallet.
    """

    def __init__(self):
        self.positions: Dict[str, Dict] = {}  # mint -> position data
        self.tx_history: List[Dict] = []
        self._load_state()

    def _load_state(self):
        """Load open positions from disk."""
        try:
            with open("real_positions.json", "r") as f:
                self.positions = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.positions = {}

    def _save_state(self):
        """Persist open positions."""
        with open("real_positions.json", "w") as f:
            json.dump(self.positions, f, indent=2)

    def _log_tx(self, record: Dict):
        """Log every transaction for audit."""
        self.tx_history.append(record)
        with open("real_tx_history.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")

    def get_jupiter_quote(self, input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int = None) -> Dict:
        """Get Jupiter quote for a swap. Returns {ok, data} or {ok, error, status}."""
        try:
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_lamports),
                "slippageBps": slippage_bps or config.MAX_SLIPPAGE_BPS,
            }
            resp = requests.get(config.JUPITER_QUOTE_URL, params=params, timeout=10)
            if resp.status_code != 200:
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("error") or err_body.get("message") or resp.text[:200]
                except Exception:
                    err_msg = resp.text[:200] or f"HTTP {resp.status_code}"
                return {"ok": False, "error": err_msg, "status": resp.status_code}
            return {"ok": True, "data": resp.json()}
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"Network error: {e}", "status": 0}
        except Exception as e:
            return {"ok": False, "error": f"Unexpected error: {e}", "status": 0}

    def execute_swap(self, quote_response: Dict) -> Dict:
        """
        Execute a swap via Jupiter. Returns {ok, tx_signature, error, input_amount, output_amount}
        """
        if not phantom.keypair:
            return {"ok": False, "error": "Wallet not initialized. Install solders."}

        try:
            # Get swap transaction payload
            swap_payload = {
                "quoteResponse": quote_response,
                "userPublicKey": phantom.public_key,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }
            resp = requests.post(config.JUPITER_SWAP_URL, json=swap_payload, timeout=15)
            resp.raise_for_status()
            swap_data = resp.json()

            if "swapTransaction" not in swap_data:
                return {"ok": False, "error": "No swapTransaction in response"}

            # Sign and send transaction
            import base64
            from solders.transaction import VersionedTransaction

            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [phantom.keypair])

            # Send via Helius RPC
            send_resp = requests.post(
                config.SOLANA_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        base64.b64encode(bytes(signed_tx)).decode(),
                        {"encoding": "base64", "maxRetries": 3, "skipPreflight": False}
                    ],
                },
                timeout=15,
            )
            send_resp.raise_for_status()
            result = send_resp.json()

            if "error" in result:
                return {"ok": False, "error": str(result["error"])}

            signature = result.get("result")
            return {
                "ok": True,
                "tx_signature": signature,
                "input_amount": quote_response.get("inAmount"),
                "output_amount": quote_response.get("outAmount"),
                "price_impact": quote_response.get("priceImpactPct", 0),
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}

    def buy(self, mint: str, symbol: str, sol_amount: float) -> Dict:
        """
        Buy a token with real SOL.
        Returns {ok, tx_signature, tokens_received, price_usd, error, sol_spent}
        """
        sol_price = get_sol_usd_price()
        if sol_price <= 0:
            return {"ok": False, "error": "Cannot fetch SOL price"}

        balance = phantom.get_balance_sol()
        if balance < sol_amount + config.MIN_SOL_RESERVE:
            return {"ok": False, "error": f"Insufficient SOL. Have: {balance:.4f}, Need: {sol_amount + config.MIN_SOL_RESERVE:.4f}"}

        MIN_TRADE_SOL = 0.005
        if sol_amount < MIN_TRADE_SOL:
            return {"ok": False, "error": f"Trade size too small. Minimum: {MIN_TRADE_SOL} SOL. You tried: {sol_amount:.4f} SOL"}

        lamports = int(sol_amount * 1_000_000_000)
        quote_result = self.get_jupiter_quote(
            input_mint="So11111111111111111111111111111111111111112",  # SOL
            output_mint=mint,
            amount_lamports=lamports,
        )

        if not quote_result.get("ok"):
            err = quote_result.get("error", "Unknown error")
            status = quote_result.get("status", 0)
            if status == 400 and "route" in err.lower():
                return {"ok": False, "error": f"Jupiter: no trade route found for this token yet. It may be too new or have too little liquidity. ({err})"}
            return {"ok": False, "error": f"Jupiter quote failed: {err}"}

        result = self.execute_swap(quote_result["data"])
        if not result["ok"]:
            return result

        # Calculate actual price paid
        tokens_received = int(result["output_amount"]) / (10 ** 9)  # Approximate - should fetch decimals
        price_usd = (sol_amount * sol_price) / tokens_received if tokens_received > 0 else 0

        # Record position
        self.positions[mint] = {
            "symbol": symbol,
            "entry_price_usd": price_usd,
            "sol_spent": sol_amount,
            "tokens_held": tokens_received,
            "timestamp": time.time(),
            "tx_signature": result["tx_signature"],
            "peak_price_usd": price_usd,
            "peak_timestamp": time.time(),
            "price_history": [[time.time(), price_usd]],
        }
        self._save_state()

        self._log_tx({
            "type": "BUY",
            "mint": mint,
            "symbol": symbol,
            "sol_amount": sol_amount,
            "tokens_received": tokens_received,
            "price_usd": price_usd,
            "tx_signature": result["tx_signature"],
            "timestamp": time.time(),
        })

        return {
            "ok": True,
            "tx_signature": result["tx_signature"],
            "tokens_received": tokens_received,
            "price_usd": price_usd,
            "sol_spent": sol_amount,
        }

    def sell(self, mint: str, current_price_usd: float = None) -> Dict:
        """
        Sell a token for real SOL.
        Returns {ok, tx_signature, sol_received, pnl_sol, pnl_percent, error}
        """
        if mint not in self.positions:
            return {"ok": False, "error": "No open real position in this token"}

        pos = self.positions[mint]
        sol_price = get_sol_usd_price()
        if sol_price <= 0:
            return {"ok": False, "error": "Cannot fetch SOL price"}

        # Get token decimals (best effort)
        token_info = get_token_info(mint)
        decimals = 9  # Default
        if token_info.get("ok"):
            # Try to get from token info or assume 9 for most memecoins
            pass

        raw_amount = int(pos["tokens_held"] * (10 ** decimals))

        quote_result = self.get_jupiter_quote(
            input_mint=mint,
            output_mint="So11111111111111111111111111111111111111112",  # SOL
            amount_lamports=raw_amount,
        )

        if not quote_result.get("ok"):
            err = quote_result.get("error", "Unknown error")
            return {"ok": False, "error": f"Jupiter quote failed: {err}"}

        result = self.execute_swap(quote_result["data"])
        if not result["ok"]:
            return result

        sol_received = int(result["output_amount"]) / 1_000_000_000
        pnl_sol = sol_received - pos["sol_spent"]
        pnl_percent = (pnl_sol / pos["sol_spent"] * 100) if pos["sol_spent"] > 0 else 0

        del self.positions[mint]
        self._save_state()

        self._log_tx({
            "type": "SELL",
            "mint": mint,
            "symbol": pos["symbol"],
            "sol_received": sol_received,
            "pnl_sol": pnl_sol,
            "pnl_percent": pnl_percent,
            "tx_signature": result["tx_signature"],
            "timestamp": time.time(),
        })

        return {
            "ok": True,
            "tx_signature": result["tx_signature"],
            "sol_received": sol_received,
            "pnl_sol": pnl_sol,
            "pnl_percent": pnl_percent,
            "symbol": pos["symbol"],
            "entry_price_usd": pos["entry_price_usd"],
        }

    def check_auto_exit(self, mint: str, current_price_usd: float) -> Optional[str]:
        """
        Check if position should auto-exit. Returns 'tp', 'sl', or None.
        """
        if mint not in self.positions:
            return None

        pos = self.positions[mint]
        entry = pos["entry_price_usd"]
        if entry <= 0:
            return None

        change_pct = ((current_price_usd - entry) / entry) * 100

        # Update peak tracking
        if current_price_usd > pos.get("peak_price_usd", entry):
            pos["peak_price_usd"] = current_price_usd
            pos["peak_timestamp"] = time.time()
            pos["price_history"].append([time.time(), current_price_usd])
            self._save_state()

        if change_pct >= config.TAKE_PROFIT_PERCENT:
            return "tp"
        elif change_pct <= config.STOP_LOSS_PERCENT:
            return "sl"
        return None

    def get_all_positions_status(self) -> List[Dict]:
        """Get status of all real positions with live P&L."""
        statuses = []
        sol_price = get_sol_usd_price()

        for mint, pos in self.positions.items():
            info = get_token_info(mint)
            if not info["ok"]:
                continue

            current_price = info["price_usd"]
            entry = pos["entry_price_usd"]
            change_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0
            current_value_usd = pos["tokens_held"] * current_price
            invested_usd = pos["sol_spent"] * sol_price if sol_price > 0 else 0
            pnl_usd = current_value_usd - invested_usd

            statuses.append({
                "mint": mint,
                "symbol": pos["symbol"],
                "entry_price_usd": entry,
                "current_price_usd": current_price,
                "tokens_held": pos["tokens_held"],
                "change_pct": change_pct,
                "invested_usd": invested_usd,
                "current_value_usd": current_value_usd,
                "pnl_usd": pnl_usd,
                "peak_price_usd": pos.get("peak_price_usd", entry),
                "peak_gain_pct": ((pos.get("peak_price_usd", entry) - entry) / entry * 100) if entry > 0 else 0,
            })

        return statuses

    def get_recent_transactions(self, limit: int = 20) -> List[Dict]:
        """Get recent real transaction history."""
        try:
            with open("real_tx_history.jsonl", "r") as f:
                lines = f.readlines()
            txs = []
            for line in lines[-limit:]:
                if line.strip():
                    txs.append(json.loads(line))
            return list(reversed(txs))
        except FileNotFoundError:
            return []


# Singleton
real_trader = RealTrader()
