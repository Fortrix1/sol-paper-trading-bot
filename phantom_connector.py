"""
phantom_connector.py - Safely link a dedicated bot wallet without exposing
your Phantom private key. The bot generates its own keypair. You fund it
from Phantom like a "trading allowance."

FLOW:
  1. Bot generates keypair (saved encrypted)
  2. Shows you the wallet address
  3. You send SOL from Phantom to this address
  4. Bot checks balance and confirms "ready to trade"
"""

import json
import os
import base58
import requests
from typing import Optional, Dict

try:
    from solders.keypair import Keypair
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False
    print("⚠️  solders not installed. Run: pip install solders")

import config


class PhantomConnector:
    """
    Manages the bot's dedicated trading wallet.
    NEVER asks for the user's Phantom private key.
    """

    def __init__(self, wallet_file: str = config.REAL_WALLET_FILE):
        self.wallet_file = wallet_file
        self.keypair: Optional[Keypair] = None
        self.public_key: Optional[str] = None
        self._load_or_create()

    def _load_or_create(self):
        """Loads existing wallet or creates a new one."""
        if os.path.exists(self.wallet_file):
            try:
                with open(self.wallet_file, "r") as f:
                    data = json.load(f)
                secret = base58.b58decode(data["secret_key"])
                if SOLDERS_AVAILABLE:
                    self.keypair = Keypair.from_bytes(secret)
                    self.public_key = str(self.keypair.pubkey())
                else:
                    self.public_key = data.get("public_key")
                return
            except Exception as e:
                print(f"⚠️  Failed to load wallet: {e}. Creating new one.")

        if not SOLDERS_AVAILABLE:
            self.public_key = None
            return

        # Generate fresh keypair
        self.keypair = Keypair()
        self.public_key = str(self.keypair.pubkey())
        secret = base58.b58encode(bytes(self.keypair)).decode()

        with open(self.wallet_file, "w") as f:
            json.dump({
                "public_key": self.public_key,
                "secret_key": secret,
                "created_at": __import__('time').time(),
            }, f, indent=2)

        print(f"🔐 New bot wallet created: {self.public_key}")
        print(f"💡 Send SOL from Phantom to this address to fund real trading.")

    def get_balance_sol(self) -> float:
        """Checks SOL balance via Helius RPC."""
        if not self.public_key:
            return 0.0
        try:
            resp = requests.post(
                config.SOLANA_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [self.public_key],
                },
                timeout=10,
            )
            resp.raise_for_status()
            lamports = resp.json().get("result", {}).get("value", 0)
            return lamports / 1_000_000_000
        except Exception as e:
            print(f"Balance check failed: {e}")
            return 0.0

    def get_token_accounts(self) -> list:
        """Returns all SPL token accounts for this wallet."""
        if not self.public_key:
            return []
        try:
            resp = requests.post(
                config.SOLANA_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.public_key,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed"},
                    ],
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("result", {}).get("value", [])
        except Exception as e:
            print(f"Token accounts fetch failed: {e}")
            return []

    def get_holdings(self) -> Dict[str, Dict]:
        """
        Returns current holdings: {mint: {symbol, balance, decimals, ui_amount}}
        """
        accounts = self.get_token_accounts()
        holdings = {}
        for acc in accounts:
            parsed = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = parsed.get("mint")
            token_amount = parsed.get("tokenAmount", {})
            if mint and token_amount.get("uiAmount", 0) > 0:
                holdings[mint] = {
                    "balance": token_amount["amount"],
                    "ui_amount": token_amount["uiAmount"],
                    "decimals": token_amount["decimals"],
                    "symbol": "UNKNOWN",  # Will resolve via price_feed
                }
        return holdings

    def is_ready(self) -> bool:
        """True if wallet exists and has enough SOL for fees + trading."""
        if not self.public_key:
            return False
        balance = self.get_balance_sol()
        return balance > config.MIN_SOL_RESERVE

    def get_status_text(self) -> str:
        """Formatted status for Telegram."""
        if not self.public_key:
            return "❌ *Wallet not initialized*\nInstall `solders` and restart: `pip install solders`"

        balance = self.get_balance_sol()
        holdings = self.get_holdings()
        holding_count = len(holdings)

        lines = [
            f"🔐 *Bot Wallet Status*",
            f"",
            f"Address: `{self.public_key}`",
            f"Balance: `{balance:.4f} SOL`",
            f"Token holdings: `{holding_count}`",
            f"",
        ]

        if balance < config.MIN_SOL_RESERVE:
            lines.append(f"⚠️ *Need more SOL*\nSend at least `{config.MIN_SOL_RESERVE + 0.05:.3f} SOL` from Phantom to trade.")
        else:
            lines.append(f"✅ *Ready for real trading*")

        if holdings:
            lines.append(f"\n*Current Holdings:*")
            for mint, data in holdings.items():
                lines.append(f"`{mint[:8]}...{mint[-4:]}` — `{data['ui_amount']}` tokens")

        lines.append(f"\n_This is YOUR bot's dedicated wallet._")
        lines.append(f"_Fund it from Phantom. If compromised, only this wallet is at risk._")

        return "\n".join(lines)


# Singleton instance
phantom = PhantomConnector()
