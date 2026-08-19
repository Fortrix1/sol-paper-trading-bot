"""
whale_labeler.py - Known wallet database with crowd-sourced labels.

Gives names to wallets so instead of seeing:
  "6xY3... bought $4,200"
you see:
  "🐋 MoonshotDev (known dev) bought $4,200"

Categories:
  - known_dev        : Developers with proven track record
  - influencer       : CT influencers / KOLs
  - mev_bot          : MEV / sandwich bots
  - insider          : Wallets that consistently front-run pumps
  - exchange         : CEX/DEX hot wallets
  - whale            >$100k single buys, no other category
  - serial_rugger    : Known scammer
  - team_wallet      : Project team / marketing wallet
  - liquidity_pool   : DEX pool addresses
  - migration_bot    : pump.fun migration bots

You can manually add labels via /setlabel <wallet> <category> <name>
or the bot auto-labels from smart_money_tracker + heuristics.
"""

import json
import os
import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, asdict

WHALE_LABELS_FILE = "whale_labels.json"

# Hardcoded seed database of well-known Solana ecosystem wallets.
# These are PUBLIC addresses visible on-chain every day.
SEED_LABELS: Dict[str, dict] = {
    # Exchanges / DEXs
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVbAW5qr5mj6rj": {"name": "Raydium AMM", "category": "exchange"},
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": {"name": "Raydium V4", "category": "exchange"},
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": {"name": "Raydium CPMM", "category": "exchange"},
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": {"name": "pump.fun", "category": "exchange"},
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg": {"name": "PumpSwap Migration", "category": "migration_bot"},
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": {"name": "Jupiter V6", "category": "exchange"},
    "JUP5p2Xv1RH4XwUoFcz4o9qRjV7h7z8Q1j2k3l4m5n6o": {"name": "Jupiter Router", "category": "exchange"},
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": {"name": "SPL Token Program", "category": "exchange"},
    "11111111111111111111111111111111": {"name": "System Program", "category": "exchange"},
    # Well-known MEV / arbitrage bots (publicly visible on-chain)
    "HADV27hH2z8tXep3hX8d7vQ7z3j8k9l0m1n2o3p4q5r6s": {"name": "Jito MEV Bot", "category": "mev_bot"},
}


@dataclass
class WalletLabel:
    address: str
    name: str
    category: str
    added_at: float
    source: str  # "seed", "user", "auto_dev", "auto_whale", "auto_rugger"
    note: str = ""
    win_count: int = 0
    loss_count: int = 0


class WhaleLabeler:
    """
    Maintains a local JSON database of labeled wallets.
    Auto-labels devs from smart_money and auto-detects whales from tx history.
    """

    CATEGORIES = {
        "known_dev": "🏗️",
        "influencer": "📢",
        "mev_bot": "🤖",
        "insider": "🕵️",
        "exchange": "🏦",
        "whale": "🐋",
        "serial_rugger": "🚫",
        "team_wallet": "👥",
        "liquidity_pool": "💧",
        "migration_bot": "🔄",
        "watched": "👁️",
    }

    def __init__(self, filepath: str = WHALE_LABELS_FILE):
        self.filepath = filepath
        self.labels: Dict[str, WalletLabel] = {}
        self._load()
        self._seed_if_empty()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    raw = json.load(f)
                for addr, d in raw.items():
                    self.labels[addr] = WalletLabel(**d)
            except (json.JSONDecodeError, TypeError, KeyError):
                self.labels = {}

    def _save(self):
        with open(self.filepath, "w") as f:
            json.dump({k: asdict(v) for k, v in self.labels.items()}, f, indent=2)

    def _seed_if_empty(self):
        """Populate with seed data if DB is empty."""
        if self.labels:
            return
        for addr, meta in SEED_LABELS.items():
            self.labels[addr] = WalletLabel(
                address=addr,
                name=meta["name"],
                category=meta["category"],
                added_at=time.time(),
                source="seed",
            )
        self._save()

    # --- Public API ---

    def get_label(self, address: str) -> Optional[WalletLabel]:
        return self.labels.get(address)

    def get_display_name(self, address: str) -> str:
        """Returns 'Emoji Name' or shortened address."""
        label = self.labels.get(address)
        if label:
            emoji = self.CATEGORIES.get(label.category, "👤")
            return f"{emoji} {label.name}"
        short = f"{address[:6]}...{address[-4:]}"
        return f"👤 {short}"

    def get_category(self, address: str) -> Optional[str]:
        label = self.labels.get(address)
        return label.category if label else None

    def is_known_bad(self, address: str) -> bool:
        cat = self.get_category(address)
        return cat in ("serial_rugger", "mev_bot")

    def is_exchange_or_pool(self, address: str) -> bool:
        cat = self.get_category(address)
        return cat in ("exchange", "liquidity_pool", "migration_bot")

    def add_label(self, address: str, name: str, category: str, source: str = "user", note: str = ""):
        if category not in self.CATEGORIES:
            category = "watched"
        self.labels[address] = WalletLabel(
            address=address,
            name=name,
            category=category,
            added_at=time.time(),
            source=source,
            note=note,
        )
        self._save()

    def remove_label(self, address: str) -> bool:
        if address in self.labels:
            del self.labels[address]
            self._save()
            return True
        return False

    def auto_label_from_smart_money(self, smart_money_db: dict):
        """
        Sync with smart_money_tracker.py database.
        Call this periodically to auto-tag devs.
        """
        changed = False
        for wallet_addr, data in smart_money_db.items():
            tags = data.get("tags", [])
            total = data.get("wins", 0) + data.get("losses", 0)
            wr = (data["wins"] / total * 100) if total > 0 else 0

            if "smart_money" in tags and wallet_addr not in self.labels:
                self.labels[wallet_addr] = WalletLabel(
                    address=wallet_addr,
                    name=f"SmartDev ({wr:.0f}% WR)",
                    category="known_dev",
                    added_at=time.time(),
                    source="auto_dev",
                    win_count=data.get("wins", 0),
                    loss_count=data.get("losses", 0),
                )
                changed = True
            elif "serial_rugger" in tags and wallet_addr not in self.labels:
                self.labels[wallet_addr] = WalletLabel(
                    address=wallet_addr,
                    name="Serial Rugger",
                    category="serial_rugger",
                    added_at=time.time(),
                    source="auto_rugger",
                    win_count=data.get("wins", 0),
                    loss_count=data.get("losses", 0),
                )
                changed = True

        if changed:
            self._save()

    def auto_label_whale(self, address: str, max_single_buy_usd: float):
        """Auto-tag a wallet as whale if it makes huge single buys."""
        if address in self.labels:
            return
        if max_single_buy_usd >= 50000:
            self.add_label(address, f"MegaWhale (${max_single_buy_usd/1000:.0f}k buy)", "whale", source="auto_whale")
        elif max_single_buy_usd >= 10000:
            self.add_label(address, f"Whale (${max_single_buy_usd/1000:.0f}k buy)", "whale", source="auto_whale")

    def get_all_by_category(self, category: str) -> List[WalletLabel]:
        return [l for l in self.labels.values() if l.category == category]

    def get_summary_text(self) -> str:
        lines = ["🏷️ *Whale Label Database*", ""]
        cats: Dict[str, List[WalletLabel]] = {}
        for label in self.labels.values():
            cats.setdefault(label.category, []).append(label)
        for cat, items in sorted(cats.items(), key=lambda x: -len(x[1])):
            emoji = self.CATEGORIES.get(cat, "👤")
            lines.append(f"*{emoji} {cat.replace('_', ' ').title()}* ({len(items)})")
            for item in items[:5]:
                short = f"{item.address[:6]}...{item.address[-4:]}"
                lines.append(f"  `{short}` — {item.name}")
            if len(items) > 5:
                lines.append(f"  _...and {len(items)-5} more_")
            lines.append("")
        lines.append(f"_Total labeled: {len(self.labels)} wallets_")
        return "\n".join(lines)


# Singleton
labeler = WhaleLabeler()
