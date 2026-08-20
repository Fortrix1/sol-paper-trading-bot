"""
phantom_connector.py - Safely link a dedicated bot wallet OR import your Phantom wallet.
"""

import json
import os
import base58
import requests
import time
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
    Manages the bot's trading wallet.
    Option A: Bot generates its own wallet (default)
    Option B: Import your Phantom wallet private key
    NEVER asks for your main Phantom key — only a dedicated trading wallet.
    """

    def __init__(self, wallet_file: str = config.REAL_WALLET_FILE):
        self.wallet_file = wallet_file
        self.keypair: Optional[Keypair] = None
        self.public_key: Optional[str] = None
        self.is_imported = False
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
                self.is_imported = data.get("is_imported", False)
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
                "created_at": time.time(),
                "is_imported": False,
            }, f, indent=2)

        print(f"🔐 New bot wallet created: {self.public_key}")
        print(f"💡 Send SOL from Phantom to this address to fund real trading.")

    def import_from_phantom(self, private_key_base58: str) -> dict:
        """
        Import an existing Phantom wallet from its base58 private key.
        This replaces the bot-generated wallet entirely.
        """
        if not SOLDERS_AVAILABLE:
            return {"ok": False, "error": "solders not installed. Run: pip install solders"}

        try:
            secret = base58.b58decode(private_key_base58.strip())
            # Phantom exports 64-byte keypairs (or 32-byte secret + 32-byte pubkey)
            if len(secret) == 64:
                self.keypair = Keypair.from_bytes(secret)
            elif len(secret) == 32:
                # Some Phantom exports are just the 32-byte seed
                self.keypair = Keypair.from_seed(secret)
            else:
                return {"ok": False, "error": f"Invalid key length: {len(secret)} bytes (expected 32 or 64)"}

            self.public_key = str(self.keypair.pubkey())
            self.is_imported = True

            # Save to file
            with open(self.wallet_file, "w") as f:
                json.dump({
                    "public_key": self.public_key,
                    "secret_key": private_key_base58.strip(),
                    "created_at": time.time(),
                    "is_imported": True,
                }, f, indent=2)

            # Verify by checking balance
            balance = self.get_balance_sol()

            return {
                "ok": True,
                "address": self.public_key,
                "balance_sol": balance,
                "message": f"✅ Phantom wallet connected!\nAddress: `{self.public_key}`\nBalance: `{balance:.4f} SOL`"
            }
        except Exception as e:
            return {"ok": False, "error": f"Failed to import key: {str(e)}"}

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
                    "symbol": "UNKNOWN",
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

        wallet_type = "🔑 *Phantom Wallet (Imported)*" if self.is_imported else "🔐 *Bot-Generated Wallet*"

        lines = [
            f"{wallet_type}",
            f"",
            f"Address: `{self.public_key}`",
            f"Balance: `{balance:.4f} SOL`",
            f"Token holdings: `{holding_count}`",
            f"",
        ]

        if balance < config.MIN_SOL_RESERVE:
            lines.append(f"⚠️ *Need more SOL*\nSend at least `{config.MIN_SOL_RESERVE + 0.05:.3f} SOL` to trade.")
        else:
            lines.append(f"✅ *Ready for real trading*")

        if holdings:
            lines.append(f"\n*Current Holdings:*")
            for mint, data in holdings.items():
                lines.append(f"`{mint[:8]}...{mint[-4:]}` — `{data['ui_amount']}` tokens")

        lines.append(f"\n_This is YOUR bot's dedicated wallet._")
        if self.is_imported:
            lines.append(f"_Imported from Phantom. All trades use this wallet._")
        else:
            lines.append(f"_Fund it from Phantom. If compromised, only this wallet is at risk._")

        return "\n".join(lines)


# Singleton
phantom = PhantomConnector()
